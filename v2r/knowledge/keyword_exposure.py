"""브랜드별 키워드 노출 현황 — 네이버 검색 카페탭 조회.

설계: `docs/reports/keyword-exposure-plan-2026-09-22.md` 2절.

대상 키워드 = 브랜드 시트 `노출 현황` 탭 **전 행**(v2r.sources.keyword_list 재사용,
`밀려남`뿐 아니라 전 상태) ∪ 우리가 발행한 브랜드 글의 키워드(같은 카페의
`article_index` 글 제목 맨 앞 키워드, 시트에 없는 것만 추가). 하루 브랜드당 최대
`DEFAULT_DAILY_CAP`(60)개, 넘치면 '밀려남'·우리 글이 확인된 키워드를 우선한다.

"우리 글 URL"은 (a) 시트에 있으면 그대로, (b) 없으면 `article_index`에서 같은
카페+제목에 키워드가 들어간 글을 찾아 `cafe_id`+`article_id`로
`https://cafe.naver.com/ca-fe/cafes/<카페번호>/articles/<글번호>`를 만들고,
(c) 그래도 없으면 검색 결과 카페 이름+제목 일치로 다시 시도하며, 아무 원천도 없으면
`unpublished`.

각 키워드를 네이버 검색 카페탭에서 조회해 우리 글이 상위 N(기본 10)위 안에 있으면
`exposed`, 있지만 순위 밖/찾지 못하면 `pushed`, 애초에 어떤 원천도 없으면
`unpublished`. 조회 실패(차단·네트워크 오류 등)는 `unknown`.

보안: `노출 현황` 탭의 비밀번호 열은 `keyword_list._drop_password_columns`가 이미
버린다. 이 모듈은 그 결과만 받으므로 비밀번호를 보거나 저장하지 않는다.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from v2r.sources.keyword_list import (
    EXPOSURE_SHEET,
    _brand_spreadsheet_id,
    _drop_password_columns,
    _norm,
    _pick,
    rows_from_xlsx,
)
from v2r.sources.sheets import SourceError, fetch_csv, gviz_csv_url
from v2r.store.db import now_iso

log = logging.getLogger(__name__)

#: 노출로 인정하는 순위 상한(카페탭 상위 N위)
DEFAULT_TOP_N = 10
#: 키워드 사이 대기(초) — 네이버 차단 방지
MIN_DELAY_SEC = 3.0
MAX_DELAY_SEC = 6.0
#: 네이버 검색 카페탭 URL
NAVER_CAFE_SEARCH_URL = "https://search.naver.com/search.naver?where=article&query={query}"

_ARTICLE_URL_HEADERS = ("게시글url", "게시글주소", "url", "게시글")
_CAFE_HEADERS = ("카페", "카페명")
_STATUS_HEADERS = ("노출상태", "상태")
_KEYWORD_HEADERS = ("키워드",)
#: V2R API가 발행 5분 뒤 1회 검사한 결과(있으면 함께 저장)
_T0_HEADERS = ("search_exposure_last_status", "T0", "검색노출")

#: 카페 글 링크 정규식 (모바일/PC 둘 다). rank는 등장 순서로 센다.
_RE_CAFE_LINK = re.compile(
    r"https?://(?:m\.)?cafe\.naver\.com/[^\s\"'<>]+", re.I
)


def _cookie_dict(path: str | Path) -> dict[str, str]:
    """`data/naver_cookies.json`(Playwright storage_state 모양) → httpx용 쿠키 dict."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    cookies = data.get("cookies") if isinstance(data, dict) else data
    out: dict[str, str] = {}
    for c in cookies or []:
        name = c.get("name")
        value = c.get("value")
        if name and value is not None:
            out[str(name)] = str(value)
    return out


def _norm_url(url: str) -> str:
    """비교용으로 URL을 단순화(끝 슬래시·쿼리 제거, 소문자화)."""
    u = str(url or "").strip()
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = re.sub(r"^m\.", "", u, flags=re.I)
    u = u.split("?", 1)[0].rstrip("/")
    return u.casefold()


#: 브랜드별 하루 검사 상한(초과분은 '밀려남'·최근 발행 우선으로 다음 회차에)
DEFAULT_DAILY_CAP = 60

#: 제목 맨 앞 "키워드" 토큰(공백/괄호/구두점 앞까지)
_RE_TITLE_LEAD = re.compile(r"^[\s\[\(【]*([^\s\[\]\(\)【】,.!?~]+)")


def _naver_article_url(cafe_id: Any, article_id: Any) -> str:
    """카페 번호+글 번호로 만드는 네이버 카페 글 URL(카페 별칭이 없어도 동작).

    docs/reports/limit-fail-fix-2026-09-22.md 참고 — V2R 글 상세/동기화로 얻는
    `cafe_id`(clubid)·`article_id`(articleid)만 있으면 만들 수 있는 표준 형태다.
    """
    cid = str(cafe_id or "").strip()
    aid = str(article_id or "").strip()
    if not cid or not aid:
        return ""
    return f"https://cafe.naver.com/ca-fe/cafes/{cid}/articles/{aid}"


def _title_lead_keyword(title: str) -> str:
    """제목 맨 앞 키워드 토큰."""
    m = _RE_TITLE_LEAD.match(str(title or "").strip())
    return m.group(1) if m else ""


def _resolve_our_article(article_index: Any, keyword: str, cafe: str) -> dict | None:
    """article_index에서 이 키워드(+카페)로 우리가 올린 글 후보 1건.

    `find_by_keyword`가 없는 낡은/가짜 article_index면 조용히 포기한다.
    """
    if article_index is None:
        return None
    try:
        rows = article_index.find_by_keyword(keyword, cafe=cafe)
    except AttributeError:
        return None
    except Exception as exc:  # pragma: no cover - 방어용
        log.warning("article_index 키워드 조회 실패(%s): %s", keyword, exc)
        return None
    return rows[0] if rows else None


def _article_id(url: str) -> str:
    """cafe.naver.com/<카페>/<글번호> 또는 <ca-fe/cafes/카페번호/articles/글번호> 꼴에서
    글번호만.

    검색 결과 링크는 카페 별칭(alias) 경로("cafe.naver.com/parisienlook/123")를
    쓰지만, 우리가 별칭을 모를 때 `_naver_article_url`로 만드는 링크는 번호만 쓰는
    "ca-fe/cafes/<카페번호>/articles/<글번호>" 꼴이다. 글 번호만 같으면 같은 글로
    본다(카페 번호까지 같은지는 호출 쪽에서 카페 이름/`cafe` 값으로 이미 좁혀 둔다).
    """
    m = re.search(
        r"cafe\.naver\.com/(?:ca-fe/cafes/\d+/articles|[^/]+)/(\d+)", str(url or ""), re.I
    )
    return m.group(1) if m else ""


@dataclass
class ExposureRow:
    brand: str
    keyword: str
    cafe: str
    article_url: str
    rank: int | None
    status: str  # exposed | pushed | unpublished | unknown
    checked_at: str
    t0_status: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "brand": self.brand,
            "keyword": self.keyword,
            "cafe": self.cafe,
            "article_url": self.article_url,
            "rank": self.rank,
            "status": self.status,
            "checked_at": self.checked_at,
            "t0_status": self.t0_status,
        }


def _sheet_rows(brand: str, cfg: dict | None, xlsx_path: str | Path | None) -> list[dict]:
    if xlsx_path:
        return rows_from_xlsx(xlsx_path)
    sid = _brand_spreadsheet_id(brand, cfg)
    if not sid:
        raise SourceError(f"브랜드 시트를 찾을 수 없습니다: {brand}")
    url = gviz_csv_url(sid, sheet=EXPOSURE_SHEET, headers=0)
    return _drop_password_columns(fetch_csv(url))


def target_keywords(
    brand: str,
    cfg: dict | None = None,
    xlsx_path: str | Path | None = None,
    article_index: Any = None,
) -> list[dict]:
    """브랜드의 조회 대상 키워드 목록.

    반환: `[{"keyword", "cafe", "article_url", "t0_status"}]` — 시트 `노출 현황`
    전 행(밀려남 제한 없음) ∪ 우리가 발행한 브랜드 글의 키워드(`article_index`에서
    카페+제목으로 찾은 것). 키워드 기준 중복 제거, 시트 순서 우선.

    "우리 글 URL"은 세 갈래로 채운다:
      (a) 시트에 이미 있으면 그대로.
      (b) 없으면 같은 카페에서 이 키워드가 제목에 들어간 `article_index` 행을 찾아
          `cafe_id`+`article_id`로 `https://cafe.naver.com/ca-fe/cafes/<카페>/articles/<글번호>`
          를 만든다.
      (c) 그래도 없으면 빈 채로 둔다 — `check_keyword`가 검색 결과에서 카페 이름+
          제목 일치로 다시 시도하고, 그마저 안 되면 `unpublished`.
    """
    rows = _sheet_rows(brand, cfg, xlsx_path)
    out: list[dict] = []
    seen: set[str] = set()
    sheet_cafes: set[str] = set()
    for row in rows:
        keyword = _pick(row, _KEYWORD_HEADERS)
        if not keyword:
            continue
        key = _norm(keyword)
        if key in seen:
            continue
        seen.add(key)
        cafe = _pick(row, _CAFE_HEADERS)
        if cafe:
            sheet_cafes.add(cafe)
        article_url = _pick(row, _ARTICLE_URL_HEADERS)
        if article_url and not re.match(r"^https?://", article_url.strip(), re.I):
            # 실측(2026-09-22): '노출 현황' 탭 `url` 칸엔 실제로 '노출완'/'밀려남' 같은
            # 상태 문구가 들어있는 브랜드 시트가 있다. URL처럼 보이지 않으면 버린다.
            article_url = ""
        candidate_title_norm = ""
        if not article_url:
            found = _resolve_our_article(article_index, keyword, cafe)
            if found:
                built = _naver_article_url(found.get("cafe_id"), found.get("article_id"))
                if built:
                    article_url = built
                else:
                    candidate_title_norm = str(found.get("title_norm") or "")
        out.append(
            {
                "keyword": keyword,
                "cafe": cafe,
                "article_url": article_url,
                "t0_status": _pick(row, _T0_HEADERS),
                "candidate_title_norm": candidate_title_norm,
            }
        )

    # 우리가 발행한 브랜드 글: 시트에 등장하는 카페들에서, article_index에 있는
    # 글 제목 맨 앞 키워드를 뽑아 시트에 없는 키워드만 더한다.
    if article_index is not None and sheet_cafes:
        for cafe in sorted(sheet_cafes):
            try:
                rows_idx = article_index.rows_for_cafe(cafe)
            except AttributeError:
                rows_idx = []
            except Exception as exc:  # pragma: no cover - 방어용
                log.warning("article_index 카페 조회 실패(%s): %s", cafe, exc)
                rows_idx = []
            for item in rows_idx or []:
                keyword = _title_lead_keyword(item.get("title") or "")
                if not keyword:
                    continue
                key = _norm(keyword)
                if key in seen:
                    continue
                seen.add(key)
                article_url = _naver_article_url(item.get("cafe_id"), item.get("article_id"))
                out.append(
                    {
                        "keyword": keyword,
                        "cafe": cafe,
                        "article_url": article_url,
                        "t0_status": "",
                        "candidate_title_norm": "" if article_url else str(item.get("title_norm") or ""),
                    }
                )
    return out


def fetch_cafe_search_html(
    keyword: str,
    cookies: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> str:
    """네이버 검색 카페탭 결과 HTML을 가져온다. 실패 시 예외."""
    url = NAVER_CAFE_SEARCH_URL.format(query=quote(keyword))
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    resp = httpx.get(
        url, headers=headers, cookies=cookies or {}, timeout=timeout, follow_redirects=True
    )
    resp.raise_for_status()
    return resp.text


def parse_cafe_search_rank(html: str, article_url: str, top_n: int = DEFAULT_TOP_N) -> int | None:
    """카페탭 검색 결과 HTML에서 `article_url`의 순위(1부터). 없으면 None.

    캡차/차단 페이지(정상 결과가 하나도 없음)면 예외를 던진다.
    """
    if not article_url:
        return None
    links = _RE_CAFE_LINK.findall(html or "")
    if not links and _looks_blocked(html):
        raise SourceError("네이버 검색 차단/캡차로 보입니다")
    target = _norm_url(article_url)
    target_id = _article_id(article_url)
    seen: list[str] = []
    for link in links:
        norm = _norm_url(link)
        if norm in seen:
            continue
        seen.append(norm)
        if len(seen) > top_n:
            break
        if norm == target or (target_id and _article_id(link) == target_id):
            return len(seen)
    return None


#: 검색 결과 한 항목(제목 포함) — `<a ...>제목</a>` 꼴, 카페 글 링크만.
_RE_RESULT_TITLE = re.compile(
    r'<a[^>]+href="(?P<url>https?://(?:m\.)?cafe\.naver\.com/[^"\']+)"[^>]*>(?P<title>.*?)</a>',
    re.I | re.S,
)
_RE_TAG = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    return _RE_TAG.sub("", text or "").strip()


def parse_cafe_search_title_rank(
    html: str, title_norm: str, top_n: int = DEFAULT_TOP_N
) -> int | None:
    """게시글 URL을 모를 때 — 제목(정규화)이 같은 카페 글의 순위(1부터).

    `article_index.normalize_title`과 같은 정규화를 쓴다(호출 쪽에서 넘겨줌).
    URL 기반 매칭(`parse_cafe_search_rank`)보다 느슨하니, URL을 만들 수 있으면
    그쪽을 우선한다.
    """
    if not title_norm:
        return None
    from v2r.store.article_index import normalize_title

    seen: list[str] = []
    for m in _RE_RESULT_TITLE.finditer(html or ""):
        norm = _norm_url(m.group("url"))
        if norm in seen:
            continue
        seen.append(norm)
        if len(seen) > top_n:
            break
        if normalize_title(_strip_tags(m.group("title"))) == title_norm:
            return len(seen)
    return None


def _looks_blocked(html: str) -> bool:
    text = html or ""
    return any(
        s in text
        for s in ("자동입력 방지", "captcha", "비정상적인 접근", "일시적으로 제한")
    )


def check_keyword(
    brand: str,
    keyword: str,
    cafe: str,
    article_url: str,
    t0_status: str = "",
    cookies: dict[str, str] | None = None,
    top_n: int = DEFAULT_TOP_N,
    now: str | None = None,
    candidate_title_norm: str = "",
) -> ExposureRow:
    """키워드 하나를 검색해 판정한다.

    글 URL이 있으면 URL로 순위를 찾는다. URL이 없어도 `candidate_title_norm`(같은
    카페에서 이 키워드로 우리가 올렸을 법한 글의 정규화 제목)이 있으면 검색 결과의
    제목으로 대신 찾는다. 둘 다 없으면 검색 없이 `unpublished`.
    """
    checked_at = now or now_iso()
    if not article_url and not candidate_title_norm:
        return ExposureRow(brand, keyword, cafe, "", None, "unpublished", checked_at, t0_status)
    try:
        html = fetch_cafe_search_html(keyword, cookies=cookies)
        if article_url:
            rank = parse_cafe_search_rank(html, article_url, top_n=top_n)
        else:
            rank = parse_cafe_search_title_rank(html, candidate_title_norm, top_n=top_n)
    except Exception as exc:
        log.warning("키워드 검색 실패(%s): %s", keyword, exc)
        return ExposureRow(brand, keyword, cafe, article_url, None, "unknown", checked_at, t0_status)
    status = "exposed" if rank is not None else "pushed"
    return ExposureRow(brand, keyword, cafe, article_url, rank, status, checked_at, t0_status)


def _prioritize(rt: Any, brand: str, items: list[dict]) -> list[dict]:
    """상한(하루 60개)에 걸릴 때 앞에 둘 것 — '밀려남'·우리 글이 있는 키워드 우선.

    안정 정렬이라 같은 우선순위 안에서는 원래 순서(시트 순서 → 발행 글 발견 순서)
    가 유지된다.
    """
    try:
        from v2r.store import keyword_exposure_store as store

        pushed = {_norm(p["keyword"]) for p in store.pushed_keywords(rt.conn, brand)}
    except Exception:  # pragma: no cover - DB 문제여도 순서만 못 바꿀 뿐
        pushed = set()

    def score(item: dict) -> int:
        has_our_article = bool(item.get("article_url") or item.get("candidate_title_norm"))
        is_pushed = _norm(item.get("keyword", "")) in pushed
        return 0 if (has_our_article or is_pushed) else 1

    return sorted(items, key=score)


def run_check(
    rt: Any,
    brand: str,
    limit: int = 0,
    top_n: int = DEFAULT_TOP_N,
    delay_range: tuple[float, float] = (MIN_DELAY_SEC, MAX_DELAY_SEC),
    sleep_fn: Any = time.sleep,
    daily_cap: int = DEFAULT_DAILY_CAP,
) -> list[ExposureRow]:
    """브랜드 키워드를 검사해 DB에 이력을 쌓고 결과를 돌려준다.

    대상은 하루 최대 `daily_cap`개(기본 60)로 자른다. `limit`을 주면 그보다 더
    좁힐 수 있지만(`min(limit, daily_cap)`), `daily_cap`을 넘길 수는 없다. 넘치는
    분은 `_prioritize`가 '밀려남'·우리 글이 확인된 키워드를 앞으로 보낸 뒤 자른다.
    """
    import random

    from v2r.store import keyword_exposure_store as store

    cfg = getattr(rt, "sources_cfg", None)
    xlsx = Path(rt.settings.repo_root) / "data" / f"brand_sheet_{brand}.xlsx"
    targets = target_keywords(
        brand,
        cfg,
        xlsx_path=str(xlsx) if xlsx.exists() else None,
        article_index=getattr(rt, "article_index", None),
    )
    targets = _prioritize(rt, brand, targets)
    cap = max(0, int(daily_cap)) or None
    if cap and limit:
        cap = min(cap, limit)
    elif limit:
        cap = limit
    if cap:
        targets = targets[:cap]
    cookies_path = Path(rt.settings.repo_root) / "data" / "naver_cookies.json"
    cookies = _cookie_dict(cookies_path)

    results: list[ExposureRow] = []
    for i, item in enumerate(targets):
        row = check_keyword(
            brand,
            item["keyword"],
            item.get("cafe", ""),
            item.get("article_url", ""),
            t0_status=item.get("t0_status", ""),
            cookies=cookies,
            top_n=top_n,
            candidate_title_norm=item.get("candidate_title_norm", ""),
        )
        results.append(row)
        store.save(rt.conn, row.as_row())
        if row.status == "unknown" and _looks_blocked_result(row):
            log.warning("네이버 차단으로 보여 %s번째에서 멈춥니다: %s", i + 1, brand)
            break
        if i < len(targets) - 1 and (item.get("article_url") or item.get("candidate_title_norm")):
            sleep_fn(random.uniform(*delay_range))
    return results


def _looks_blocked_result(row: ExposureRow) -> bool:
    return False


def summary(rt: Any) -> dict[str, Any]:
    """현황판용 요약: 브랜드별 노출/밀려남/미확인 수 + 새로 밀려난 키워드.

    dashboard.py는 이 함수 결과만 쓰고 이 모듈을 직접 고치지 않는다(설계 §5).
    """
    from v2r.store import keyword_exposure_store as store

    return store.summary(rt.conn)


def write_report(
    rt: Any,
    rows: list[ExposureRow],
    brand: str,
    now: Any = None,
) -> tuple[Path, Path]:
    """`docs/reports/exposure-YYYY-MM-DD.md` + `data/exposure-YYYY-MM-DD.csv` 생성."""
    import csv
    from datetime import datetime

    from v2r.store.db import KST

    stamp = (now or datetime.now(KST)).strftime("%Y-%m-%d")
    repo = Path(rt.settings.repo_root)
    md_path = repo / "docs" / "reports" / f"exposure-{stamp}.md"
    csv_path = repo / "data" / f"exposure-{stamp}.csv"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    counts = {"exposed": 0, "pushed": 0, "unpublished": 0, "unknown": 0}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1

    lines = [
        f"# 키워드 노출 현황 — {brand} ({stamp})",
        "",
        f"검사 {len(rows)}개 — 노출 {counts['exposed']} · 밀려남 {counts['pushed']} · "
        f"미발행 {counts['unpublished']} · 확인 실패 {counts['unknown']}",
        "",
        "| 카페 | 키워드 | 상태 | 순위 | 게시글 URL | 검사시각 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.cafe} | {r.keyword} | {r.status} | {r.rank or '-'} | "
            f"{r.article_url or '-'} | {r.checked_at} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["카페", "게시글 URL", "노출 상태", "키워드", "순위", "검사시각"])
        for r in rows:
            writer.writerow([r.cafe, r.article_url, r.status, r.keyword, r.rank or "", r.checked_at])

    return md_path, csv_path


# =======================================================================
# B1~B4 (2026-09-22 재설계): 통합검색(통검) 판정 + 무한 순환 + CSV/현황판
# 설계: docs/reports/keyword-program-plan-2026-09-22.md B절.
# 기존 카페탭 검사(`check_keyword`/`run_check`)는 보조로 그대로 남긴다.
# =======================================================================

import csv
import os

#: 네이버 통합검색(통검) URL — 카페·블로그·VIEW·인플루언서 등 전 영역이 한 페이지에 나온다
INTEGRATED_SEARCH_URL = "https://search.naver.com/search.naver?query={query}"
#: 통검 결과에서 우리 글을 찾는 범위(이 안에 있으면 '노출완')
DEFAULT_TOP_N_INTEGRATED = 30
#: 이 순위 이내면 O열 "1~5순위 진입"
TOP5_RANK = 5
#: 연속 이 횟수만큼 unknown(차단 의심)이면 30분 휴식
BLOCK_STREAK_LIMIT = 10
#: 휴식 시간(초)
BLOCK_REST_SECONDS = 1800

#: 순환기 상태 파일 (data/ 아래)
CYCLE_STATE_FILE = "exposure_cycle_state.json"

KOREAN_STATUS = {
    "exposed": "노출완",
    "pushed": "밀려남",
    "unpublished": "미발행",
    "unknown": "미확인",
}


def fetch_integrated_search_html(
    keyword: str, cookies: dict[str, str] | None = None, timeout: float = 10.0
) -> str:
    """네이버 통합검색 결과 HTML을 가져온다. 실패 시 예외."""
    url = INTEGRATED_SEARCH_URL.format(query=quote(keyword))
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    resp = httpx.get(
        url, headers=headers, cookies=cookies or {}, timeout=timeout, follow_redirects=True
    )
    resp.raise_for_status()
    return resp.text


def check_keyword_unified(
    brand: str,
    keyword: str,
    cafe: str,
    article_url: str,
    t0_status: str = "",
    cookies: dict[str, str] | None = None,
    top_n: int = DEFAULT_TOP_N_INTEGRATED,
    now: str | None = None,
    candidate_title_norm: str = "",
    html: str | None = None,
) -> ExposureRow:
    """B1: 통합검색 기준 판정 — 있으면 `exposed`(노출완), 없으면 `pushed`(밀려남).

    `html`을 직접 넘기면 네트워크를 타지 않는다(테스트/재사용용). 기존 카페탭
    함수(`parse_cafe_search_rank`/`parse_cafe_search_title_rank`)를 그대로
    재사용한다 — 둘 다 HTML 안의 cafe.naver.com 링크만 보므로 통검 결과에도
    그대로 적용된다.
    """
    checked_at = now or now_iso()
    if not article_url and not candidate_title_norm:
        return ExposureRow(brand, keyword, cafe, "", None, "unpublished", checked_at, t0_status)
    try:
        page = html if html is not None else fetch_integrated_search_html(keyword, cookies=cookies)
        if article_url:
            rank = parse_cafe_search_rank(page, article_url, top_n=top_n)
        else:
            rank = parse_cafe_search_title_rank(page, candidate_title_norm, top_n=top_n)
    except Exception as exc:
        log.warning("통검 확인 실패(%s): %s", keyword, exc)
        return ExposureRow(brand, keyword, cafe, article_url, None, "unknown", checked_at, t0_status)
    status = "exposed" if rank is not None else "pushed"
    return ExposureRow(brand, keyword, cafe, article_url, rank, status, checked_at, t0_status)


def integrated_search_url(keyword: str) -> str:
    """O열/보고서용 통합검색 URL(사람이 눌러볼 수 있게 인코딩 그대로)."""
    return INTEGRATED_SEARCH_URL.format(query=quote(keyword))


def _discovered_keywords_path(rt: Any, brand: str) -> Path:
    return Path(rt.settings.repo_root) / "data" / "keywords" / f"{brand}.csv"


def known_brands(rt: Any) -> list[str]:
    """`data/brand_sheet_<브랜드>.xlsx` 파일명에서 뽑은 브랜드 목록."""
    repo = Path(rt.settings.repo_root)
    out = []
    for p in sorted(repo.glob("data/brand_sheet_*.xlsx")):
        name = p.stem[len("brand_sheet_"):]
        if name:
            out.append(name)
    return out


def keyword_universe(rt: Any, brand: str) -> list[dict]:
    """B2: 전체 키워드 = 시트 둘째 탭(H열, 기존 `target_keywords`) ∪
    `data/keywords/<브랜드>.csv`(다른 일꾼이 만드는 발굴 결과, 있으면 병합).

    각 항목: `{keyword, cafe, article_url, t0_status, candidate_title_norm, volume}`.
    발굴 CSV는 아직 형식이 정해지는 중이라 방어적으로 읽는다(없거나 형식이
    달라도 조용히 건너뛴다).
    """
    cfg = getattr(rt, "sources_cfg", None)
    xlsx = Path(rt.settings.repo_root) / "data" / f"brand_sheet_{brand}.xlsx"
    try:
        targets = target_keywords(
            brand,
            cfg,
            xlsx_path=str(xlsx) if xlsx.exists() else None,
            article_index=getattr(rt, "article_index", None),
        )
    except Exception as exc:
        log.warning("키워드 시트 조회 실패(%s): %s", brand, exc)
        targets = []

    out: dict[str, dict] = {}
    for t in targets:
        out[_norm(t["keyword"])] = {**t, "volume": 0}

    disc_path = _discovered_keywords_path(rt, brand)
    if disc_path.exists():
        try:
            with disc_path.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    kw = (row.get("키워드") or row.get("keyword") or "").strip()
                    if not kw:
                        continue
                    key = _norm(kw)
                    vol = 0
                    for vh in ("검색량", "월간검색량", "volume", "search_volume"):
                        raw = row.get(vh)
                        if raw:
                            try:
                                vol = int(re.sub(r"[^\d]", "", str(raw)) or 0)
                            except Exception:
                                vol = 0
                            break
                    if key in out:
                        out[key]["volume"] = max(int(out[key].get("volume") or 0), vol)
                    else:
                        out[key] = {
                            "keyword": kw,
                            "cafe": row.get("카페") or "",
                            "article_url": row.get("url") or row.get("게시글url") or "",
                            "t0_status": "",
                            "candidate_title_norm": "",
                            "volume": vol,
                        }
        except Exception as exc:
            log.warning("발굴 키워드 CSV 읽기 실패(%s, %s): %s", brand, disc_path, exc)
    return list(out.values())


def next_cycle_batch(rt: Any, brand: str, n: int = 1) -> list[dict]:
    """B2: 다음에 확인할 키워드 n개 — 검색량 큰 순 → 마지막 확인 오래된 순.

    커서를 따로 두지 않는다: 확인한 키워드는 DB의 `checked_at`이 갱신되어
    자연히 정렬 맨 뒤로 밀리므로, 매번 이 함수를 부르는 것만으로 끝없이
    순환한다.
    """
    from v2r.store import keyword_exposure_store as store

    universe = keyword_universe(rt, brand)
    if not universe:
        return []
    last_checked = {r["keyword"]: str(r["checked_at"] or "") for r in store.latest_by_keyword(rt.conn, brand)}

    def sort_key(item: dict) -> tuple:
        vol = -(int(item.get("volume") or 0))
        last = last_checked.get(item["keyword"], "")  # 빈 문자열(미확인)이 가장 먼저
        return (vol, last)

    ordered = sorted(universe, key=sort_key)
    return ordered[: max(0, n)]


def cycle_state_path(rt: Any) -> Path:
    return Path(rt.settings.data_dir) / CYCLE_STATE_FILE


def cycle_status(rt: Any) -> dict:
    """B2: 순환기 현재 상태(`노출 순환 상태` 명령이 그대로 씀)."""
    p = cycle_state_path(rt)
    if not p.exists():
        return {"enabled": False}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": False}


def _write_cycle_state(rt: Any, state: dict) -> None:
    p = cycle_state_path(rt)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def cycle_start(rt: Any, brands: list[str] | None = None) -> dict:
    """`노출 순환 시작`."""
    state = cycle_status(rt)
    state.update(
        {
            "enabled": True,
            "brands": brands or state.get("brands") or known_brands(rt),
            "started_at": now_iso(),
            "paused_until_mono": None,
        }
    )
    _write_cycle_state(rt, state)
    return state


def cycle_stop(rt: Any) -> dict:
    """`노출 순환 중지`."""
    state = cycle_status(rt)
    state["enabled"] = False
    _write_cycle_state(rt, state)
    return state


def cycle_tick(rt: Any, now_mono: float | None = None) -> dict | None:
    """B2: 사이드카 한 틱에서 부른다. 꺼져 있으면 아무 일도 안 한다.

    한 틱에 키워드 1개만 확인한다(3~6초 최소 간격은 상태에 기록한 마지막
    호출 시각으로 지킨다 — 사이드카 틱 자체가 5초 간격이라 이 정도면 충분).
    연속 `BLOCK_STREAK_LIMIT`번 unknown(차단 의심)이면 30분 휴식한다.
    """
    import random

    state = cycle_status(rt)
    if not state.get("enabled"):
        return None
    mono = time.monotonic() if now_mono is None else now_mono

    paused_until = state.get("paused_until_mono")
    if paused_until and mono < paused_until:
        return {"paused": True}

    last_mono = state.get("_last_mono")
    min_gap = random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC)
    if last_mono is not None and (mono - float(last_mono)) < min_gap:
        return {"waiting": True}

    brands = state.get("brands") or known_brands(rt)
    if not brands:
        return {"idle": True}
    idx = int(state.get("brand_idx", 0)) % len(brands)
    brand = brands[idx]
    state["brand_idx"] = (idx + 1) % len(brands)

    batch = next_cycle_batch(rt, brand, n=1)
    state["_last_mono"] = mono
    if not batch:
        _write_cycle_state(rt, state)
        return {"brand": brand, "checked": 0}

    item = batch[0]
    cookies_path = Path(rt.settings.repo_root) / "data" / "naver_cookies.json"
    cookies = _cookie_dict(cookies_path)

    from v2r.store import keyword_exposure_store as store

    row = check_keyword_unified(
        brand,
        item["keyword"],
        item.get("cafe", ""),
        item.get("article_url", ""),
        t0_status=item.get("t0_status", ""),
        cookies=cookies,
        candidate_title_norm=item.get("candidate_title_norm", ""),
    )
    store.save(rt.conn, row.as_row())
    state["last_checked"] = {
        "brand": brand,
        "keyword": item["keyword"],
        "status": row.status,
        "at": row.checked_at,
    }
    streak = int(state.get("_block_streak", 0))
    streak = streak + 1 if row.status == "unknown" else 0
    state["_block_streak"] = streak
    if streak >= BLOCK_STREAK_LIMIT:
        state["paused_until_mono"] = mono + BLOCK_REST_SECONDS
        state["_block_streak"] = 0
        log.warning("노출 순환: 차단 징후(%s연속 미확인)로 30분 휴식 — %s", BLOCK_STREAK_LIMIT, brand)
    _write_cycle_state(rt, state)

    try:
        write_exposure_csv(rt, brand)
    except Exception as exc:  # pragma: no cover - 방어용
        log.warning("노출 CSV 갱신 실패(%s): %s", brand, exc)

    return {"brand": brand, "keyword": item["keyword"], "status": row.status, "rank": row.rank}


#: B3: `data/exposure/<브랜드>.csv` 열 순서
EXPOSURE_CSV_HEADERS = [
    "카페", "url", "발행시간", "작성자 아이디", "비밀번호",
    "발행 URL", "노출 상태", "키워드", "통합검색 URL", "최종 편집 일시",
    "키워드 검색량", "노출된 검색량", "비고", "본문 분류", "1~5순위 진입",
]


def exposure_dir(rt: Any) -> Path:
    return Path(rt.settings.repo_root) / "data" / "exposure"


def write_exposure_csv(rt: Any, brand: str) -> tuple[Path, Path]:
    """B3: DB의 최신 검사 결과 + 키워드 전체(검색량 포함)로
    `data/exposure/<브랜드>.csv` + `summary.json`을 다시 쓴다.

    비밀번호 열은 항상 빈칸(이 모듈은 비밀번호를 받지도, 저장하지도 않는다).
    """
    from v2r.store import keyword_exposure_store as store

    universe = {_norm(i["keyword"]): i for i in keyword_universe(rt, brand)}
    latest = {r["keyword"]: r for r in store.latest_by_keyword(rt.conn, brand)}

    out_dir = exposure_dir(rt)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{brand}.csv"
    summary_path = out_dir / f"{brand}.summary.json"

    total_volume = 0
    exposed_volume = 0
    rows_out = []
    all_keywords = {**{k: v for k, v in universe.items()}}
    # DB에만 있고 universe엔 없을 수도 있는(발굴 목록이 바뀐) 키워드도 포함
    for kw, r in latest.items():
        all_keywords.setdefault(_norm(kw), {"keyword": kw, "cafe": r["cafe"] or "", "volume": 0})

    for key in sorted(all_keywords, key=lambda k: -(int(all_keywords[k].get("volume") or 0))):
        item = all_keywords[key]
        keyword = item["keyword"]
        r = latest.get(keyword)
        status = r["status"] if r else "unknown"
        rank = r["rank"] if r else None
        vol = int(item.get("volume") or 0)
        total_volume += vol
        exposed_vol = vol if status == "exposed" else 0
        exposed_volume += exposed_vol
        top5 = "예" if (status == "exposed" and rank is not None and rank <= TOP5_RANK) else ""
        rows_out.append(
            [
                item.get("cafe", "") or (r["cafe"] if r else ""),
                r["article_url"] if r else item.get("article_url", ""),
                "",  # 발행시간 — 이 모듈은 모른다(article_index/시트가 채움)
                "",  # 작성자 아이디
                "",  # 비밀번호 — 항상 빈칸
                r["article_url"] if r else item.get("article_url", ""),
                KOREAN_STATUS.get(status, status),
                keyword,
                integrated_search_url(keyword),
                r["checked_at"] if r else "",
                vol,
                exposed_vol,
                "",  # 비고
                "",  # 본문 분류
                top5,
            ]
        )

    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(EXPOSURE_CSV_HEADERS)
        writer.writerows(rows_out)

    counts = {"exposed": 0, "pushed": 0, "unpublished": 0, "unknown": 0}
    for key, item in all_keywords.items():
        r = latest.get(item["keyword"])
        status = r["status"] if r else "unknown"
        counts[status] = counts.get(status, 0) + 1

    summary_data = {
        "brand": brand,
        "updated_at": now_iso(),
        "total_keywords": len(all_keywords),
        "counts": counts,
        "total_volume_p1": total_volume,
        "exposed_volume_q1": exposed_volume,
        "exposed_volume_ratio": (exposed_volume / total_volume) if total_volume else 0.0,
    }
    summary_path.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, summary_path


def summary(rt: Any) -> dict[str, Any]:
    """현황판용 요약: 브랜드별 노출/밀려남/미확인 수 + 새로 밀려난 키워드
    (B4) + 검색량 비율(`data/exposure/<브랜드>.summary.json`이 있으면 얹는다).

    dashboard.py는 이 함수 결과만 쓰고 이 모듈을 직접 고치지 않는다(설계 §5).
    """
    from v2r.store import keyword_exposure_store as store

    out = store.summary(rt.conn)
    out_dir = exposure_dir(rt)
    for brand, entry in out.items():
        p = out_dir / f"{brand}.summary.json"
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        entry["exposed_volume_ratio"] = data.get("exposed_volume_ratio", 0.0)
        entry["total_volume_p1"] = data.get("total_volume_p1", 0)
        entry["exposed_volume_q1"] = data.get("exposed_volume_q1", 0)
    return out


__all__ = [
    "DEFAULT_TOP_N",
    "DEFAULT_DAILY_CAP",
    "ExposureRow",
    "target_keywords",
    "fetch_cafe_search_html",
    "parse_cafe_search_rank",
    "parse_cafe_search_title_rank",
    "check_keyword",
    "run_check",
    "summary",
    "write_report",
    # B1~B4
    "INTEGRATED_SEARCH_URL",
    "DEFAULT_TOP_N_INTEGRATED",
    "TOP5_RANK",
    "KOREAN_STATUS",
    "fetch_integrated_search_html",
    "check_keyword_unified",
    "integrated_search_url",
    "known_brands",
    "keyword_universe",
    "next_cycle_batch",
    "cycle_state_path",
    "cycle_status",
    "cycle_start",
    "cycle_stop",
    "cycle_tick",
    "EXPOSURE_CSV_HEADERS",
    "exposure_dir",
    "write_exposure_csv",
]
