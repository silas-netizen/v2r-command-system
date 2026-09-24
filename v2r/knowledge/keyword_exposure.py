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
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from v2r.knowledge import serp
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
    #: 실제로 검색창에 넣은 최종 검색어(자동완성 1번 또는 띄어쓰기 정규화 결과).
    #: 2026-09-23 사용자 지시 — I열(통합검색 URL)이 이 검색어 기준이어야 한다.
    search_query: str = ""
    #: 예전 방식 순위(광고·내비 포함 전체 링크 순번) — 참고용(2026-09-23 정정,
    #: `rank`는 이제 일반 결과만 센다). `judge_keyword_exposure`를 거치지 않은
    #: 예전 경로(`check_keyword_unified`)에서는 늘 `None`이다.
    rank_overall: int | None = None

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
            "search_query": self.search_query,
            "rank_overall": self.rank_overall,
        }


def _sheet_rows(brand: str, cfg: dict | None, xlsx_path: str | Path | None) -> list[dict]:
    """브랜드 시트 두 번째 탭 행. **항상 실시간 시트를 먼저** 읽는다(2026-09-24 사고:
    09-19에 받아 둔 xlsx 스냅샷을 우선 써서 G열 노출완이 옛 값으로 잡혔다). export CSV
    (필터 무시)를 먼저, 안 되면 gviz, 둘 다 안 되면 그제야 xlsx 스냅샷."""
    sid = _brand_spreadsheet_id(brand, cfg)
    if sid:
        try:
            from v2r.sources.sheets_writer import _read_export_csv

            # gid는 설정(config/sources.yaml brand_sheets.<브랜드>.exposure_gid)에서 — 탭 목록을
            # Playwright로 여는 _second_tab_gid는 러너 스레드 안에서 충돌(asyncio loop)하므로 안 쓴다.
            entry = ((cfg or {}).get("brand_sheets") or {}).get(brand) or {}
            gid = entry.get("exposure_gid")
            if gid is None:
                raise SourceError("exposure_gid 미설정")
            table = _read_export_csv(sid, int(gid))
            if table and len(table) > 1:
                hdr = [str(h).strip() for h in table[0]]
                rows = [dict(zip(hdr, r + [""] * (len(hdr) - len(r)))) for r in table[1:]]
                return _drop_password_columns(rows)
        except Exception as exc:  # noqa: BLE001
            log.warning("시트 실시간 읽기 실패(%s), 대체 경로 사용: %s", brand, exc)
        try:
            url = gviz_csv_url(sid, sheet=EXPOSURE_SHEET, headers=0)
            return _drop_password_columns(fetch_csv(url))
        except Exception as exc:  # noqa: BLE001
            log.warning("시트 gviz 읽기 실패(%s): %s", brand, exc)
    if xlsx_path:
        return rows_from_xlsx(xlsx_path)
    raise SourceError(f"브랜드 시트를 찾을 수 없습니다: {brand}")


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
                # 시트 G열(노출 상태) — 우선순위 1등급 "기존 노출완" 판단용(2026-09-24)
                "sheet_status": _pick(row, _STATUS_HEADERS),
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
    """네이버 통합검색 결과 HTML을 가져온다(빠른 1차 조회, requests). 실패 시 예외."""
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


# =======================================================================
# 2026-09-23 사용자 지시: 검색어 자동완성 정규화 + 첫 페이지 끝까지 스크롤(Playwright)
# =======================================================================

#: 네이버 자동완성 API
AUTOCOMPLETE_URL = (
    "https://ac.search.naver.com/nx/ac?q={query}&st=100&frm=nx&r_format=json"
    "&r_enc=UTF-8&q_enc=UTF-8"
)
#: 최종 DOM 스크롤 시도 최대 횟수(더 내려도 높이가 안 느는지 확인하는 횟수 포함)
MAX_SCROLL_ROUNDS = 20
#: 스크롤 한 번 뒤 대기(초) — 동적 로딩(더보기 포함)이 붙을 시간
SCROLL_WAIT_SECONDS = 0.4


def naver_autocomplete_first(keyword: str, timeout: float = 5.0) -> str:
    """네이버 자동완성 첫 항목(띄어쓰기 포함). 실패/빈 결과면 빈 문자열.

    응답 모양: `{"items": [[["단어1", ...], ["단어2", ...], ...]]}` — 첫 그룹의
    첫 항목 0번째 문자열이 자동완성 1순위다.
    """
    try:
        url = AUTOCOMPLETE_URL.format(query=quote(keyword))
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        groups = (data or {}).get("items") or []
        first_group = groups[0] if groups else []
        first_item = first_group[0] if first_group else []
        suggestion = str(first_item[0]) if first_item else ""
        return suggestion.strip()
    except Exception as exc:
        log.warning("자동완성 조회 실패(%s): %s", keyword, exc)
        return ""


def parse_autocomplete_items(data: Any) -> list[str]:
    """자동완성 응답(`{"items": [[["단어", ...], ...], ...]}`) → 후보 전체 목록(순서 유지·중복 제거)."""
    out: list[str] = []
    seen: set[str] = set()
    groups = (data or {}).get("items") if isinstance(data, dict) else None
    for group in groups or []:
        if not isinstance(group, list):
            continue
        for item in group:
            text = ""
            if isinstance(item, list) and item:
                text = str(item[0])
            elif isinstance(item, str):
                text = item
            text = text.strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return out


def naver_autocomplete_all(keyword: str, timeout: float = 5.0) -> list[str]:
    """네이버 자동완성 후보 전체(시드 다양화용, 2026-09-24). 실패 시 빈 목록."""
    try:
        url = AUTOCOMPLETE_URL.format(query=quote(keyword))
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        return parse_autocomplete_items(resp.json())
    except Exception as exc:
        log.warning("자동완성 전체 조회 실패(%s): %s", keyword, exc)
        return []


#: 통검 하단 "함께 많이 찾는"(예전 이름 "연관 검색어") 항목 링크 — 실측(2026-09-24):
#: 항목은 `?where=nexearch&sm=tab_clk.ndT&query=<URL인코딩 검색어>` 링크로 렌더된다
#: (텍스트는 말줄임 처리되므로 링크의 query 값을 쓴다). 렌더는 JS라 httpx HTML에는
#: 없고 `fetch_integrated_search_dom`(헤드리스)의 최종 DOM에만 있다.
_RE_RELATED_LINK = re.compile(r'sm=tab_clk\.ndT[^"\']*?query=([^&"\'#]+)')


def parse_related_searches(html: str) -> list[str]:
    """통검 최종 DOM에서 하단 연관 검색어("함께 많이 찾는") 목록(순서 유지·중복 제거)."""
    from urllib.parse import unquote

    out: list[str] = []
    seen: set[str] = set()
    for raw in _RE_RELATED_LINK.findall(html or ""):
        text = unquote(raw.replace("+", " ")).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def naver_related_searches(keyword: str, headless: bool = True, max_rounds: int = 3) -> list[str]:
    """통검 하단 연관 검색어(기존 `fetch_integrated_search_dom` 재사용, 헤드리스만). 실패 시 빈 목록.

    호출 스레드가 이미 Playwright sync 루프를 돌리고 있을 수 있어(키워드 도구 페이지를
    연 워커) 별도 스레드에서 연다 — 실측(2026-09-24): 같은 스레드에서는
    "Sync API inside the asyncio loop" 오류로 전부 실패했다.
    """
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["html"] = fetch_integrated_search_dom(keyword, headless=headless, max_rounds=max_rounds)
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=_run, name="related-searches", daemon=True)
    t.start()
    t.join(timeout=120)
    if t.is_alive() or "err" in box or "html" not in box:
        log.warning("연관 검색어 조회 실패(%s): %s", keyword, box.get("err") or "시간 초과")
        return []
    return parse_related_searches(box["html"])


def _spacing_fallback(keyword: str) -> str:
    """자동완성이 없을 때 쓰는 띄어쓰기 정규화. `pykospacing`이 설치돼 있으면
    그걸로 교정하고, 없으면(대부분의 환경) 원문 그대로 돌려준다 — 사용자 지시의
    "실패 시 원문"에 해당한다.
    """
    try:
        from pykospacing import Spacing  # type: ignore

        spacing = Spacing()
        out = spacing(keyword)
        return out.strip() if out else keyword
    except Exception:
        return keyword


def resolve_search_query(keyword: str) -> str:
    """B1: 실제로 검색창에 넣을 최종 검색어 — 자동완성 1순위, 없으면 띄어쓰기
    정규화(안 되면 원문)."""
    keyword = str(keyword or "").strip()
    if not keyword:
        return keyword
    suggestion = naver_autocomplete_first(keyword)
    return suggestion or _spacing_fallback(keyword)


def fetch_integrated_search_dom(
    query: str,
    cookies_path: str | Path | None = None,
    max_rounds: int = MAX_SCROLL_ROUNDS,
    headless: bool = True,
) -> str:
    """통검 첫 페이지를 열어 **끝까지 스크롤**(더보기 포함)한 뒤 최종 DOM을
    돌려준다(Playwright, headless). 네이버 로그인 프로필 쿠키(`storage_state`
    JSON)가 있으면 그대로 쓴다. 실패 시 예외.
    """
    from playwright.sync_api import sync_playwright

    url = INTEGRATED_SEARCH_URL.format(query=quote(query))
    storage_state = str(cookies_path) if cookies_path and Path(cookies_path).exists() else None

    with sync_playwright() as pw:
        browser = launch_chromium(pw, headless=headless)
        try:
            context = browser.new_context(
                storage_state=storage_state,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 950},
            )
            page = context.new_page()
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            last_height = -1
            for _ in range(max_rounds):
                # "더보기"류 버튼이 있으면 눌러서 더 붙인다(있으면만, 없어도 무시)
                for sel in ("a.api_more", "a.more", "button.api_more"):
                    try:
                        loc = page.locator(sel)
                        if loc.count() and loc.first.is_visible():
                            loc.first.click(timeout=1000)
                    except Exception:
                        pass
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(int(SCROLL_WAIT_SECONDS * 1000))
                height = page.evaluate("document.body.scrollHeight")
                if height == last_height:
                    break
                last_height = height
            return page.content()
        finally:
            browser.close()


def check_keyword_unified(
    brand: str,
    keyword: str,
    cafe: str,
    article_url: str,
    t0_status: str = "",
    cookies: dict[str, str] | None = None,
    cookies_path: str | Path | None = None,
    top_n: int = DEFAULT_TOP_N_INTEGRATED,
    now: str | None = None,
    candidate_title_norm: str = "",
    html: str | None = None,
    search_query: str | None = None,
    use_dom_fallback: bool = True,
) -> ExposureRow:
    """B1: 통합검색 기준 판정 — 있으면 `exposed`(노출완), 없으면 `pushed`(밀려남).

    검색어는 `resolve_search_query`(자동완성 1순위 → 띄어쓰기 정규화 → 원문)로
    정한 뒤 그 검색어로 조회한다. `html`을 직접 넘기면 네트워크/자동완성을 모두
    타지 않는다(테스트/재사용용) — 이때 `search_query`도 같이 넘겨야 결과 행에
    실린다.

    requests로 받은 첫 HTML(`fetch_integrated_search_html`)에 우리 글이 안 보이면
    — 동적으로 붙는 영역 때문일 수 있어 — Playwright로 첫 페이지를 끝까지 스크롤한
    최종 DOM(`fetch_integrated_search_dom`)으로 한 번 더 확인한다
    (`use_dom_fallback=False`면 건너뛴다 — 테스트/빠른 경로용).

    기존 카페탭 함수(`parse_cafe_search_rank`/`parse_cafe_search_title_rank`)를
    그대로 재사용한다 — 둘 다 HTML 안의 cafe.naver.com 링크만 보므로 통검 결과에도
    그대로 적용된다.
    """
    checked_at = now or now_iso()
    if not article_url and not candidate_title_norm:
        return ExposureRow(brand, keyword, cafe, "", None, "unpublished", checked_at, t0_status, "")

    query = search_query if search_query is not None else (keyword if html is not None else resolve_search_query(keyword))

    def _rank(page_html: str) -> int | None:
        if article_url:
            return parse_cafe_search_rank(page_html, article_url, top_n=top_n)
        return parse_cafe_search_title_rank(page_html, candidate_title_norm, top_n=top_n)

    try:
        if html is not None:
            rank = _rank(html)
        else:
            page = fetch_integrated_search_html(query, cookies=cookies)
            rank = _rank(page)
            if rank is None and use_dom_fallback:
                try:
                    dom_html = fetch_integrated_search_dom(query, cookies_path=cookies_path)
                    rank = _rank(dom_html)
                except Exception as exc:  # pragma: no cover - 환경 의존(Playwright 미설치 등)
                    log.warning("통검 DOM(끝까지 스크롤) 확인 실패(%s): %s", keyword, exc)
    except Exception as exc:
        log.warning("통검 확인 실패(%s): %s", keyword, exc)
        return ExposureRow(brand, keyword, cafe, article_url, None, "unknown", checked_at, t0_status, query)
    status = "exposed" if rank is not None else "pushed"
    return ExposureRow(brand, keyword, cafe, article_url, rank, status, checked_at, t0_status, query)


def integrated_search_url(query: str) -> str:
    """O열/보고서용 통합검색 URL(최종 검색어 기준, 사람이 눌러볼 수 있게 인코딩 그대로)."""
    return INTEGRATED_SEARCH_URL.format(query=quote(query))


def _discovered_keywords_path(rt: Any, brand: str) -> Path:
    return Path(rt.settings.repo_root) / "data" / "keywords" / f"{brand}.csv"


def _relevance_keywords_db_path(rt: Any, brand: str) -> Path:
    return Path(rt.settings.repo_root) / "data" / "keywords" / f"{brand}.sqlite"


def _db_volume_map(rt: Any, brand: str) -> dict[str, int]:
    """`data/keywords/<브랜드>.sqlite`의 키워드 → 검색량(total, 모바일+PC 합).

    원고 대상 여부와 무관하게 전부 읽는다 — 시트에 이미 있는 키워드의 K열은
    연관도와 상관없이 키워드 도구 검색량이어야 한다(2026-09-24: 원고 대상이 아닌
    시트 키워드가 volume 0으로 잡혀 K열이 0으로 덮인 사고).
    """
    import sqlite3

    db_path = _relevance_keywords_db_path(rt, brand)
    if not db_path.exists():
        return {}
    try:
        con = sqlite3.connect(str(db_path))
        try:
            cols = {row[1] for row in con.execute("PRAGMA table_info(keywords)")}
            if "total" not in cols:
                return {}
            out: dict[str, int] = {}
            for kw, total in con.execute("SELECT keyword, total FROM keywords"):
                try:
                    v = int(total or 0)
                except (TypeError, ValueError):
                    v = 0
                if kw and v > 0:
                    out[_norm(kw)] = max(out.get(_norm(kw), 0), v)
            return out
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("검색량 DB 읽기 실패(%s): %s", brand, exc)
        return {}


#: `naver_keyword_tool.fetch_related_keywords` 한 번 호출 최대 씨앗 수(기존 값)
_VOLUME_SEED_BATCH_SIZE = 5
#: 프로필 잠금 "대기 시간 초과"일 때 재시도 전 대기(초, 사용자 지시 2026-09-24)
_VOLUME_LOCK_RETRY_WAIT_SEC = 3.0


def _real_volume_fetch_fn(brand: str, repo_root: str) -> Any:
    """`ensure_volumes` 기본 조회 함수 — 브랜드 전용 복제 프로필로 키워드 도구를
    헤드리스로 열어 씨앗(최대 5개)의 pc/mobile 검색량을 받는다.

    러너(노출 순환)와 채우기 순환이 같은 프로필을 동시에 열지 않도록
    `open_keyword_tool_page`가 내부에서 쓰는 프로필 잠금을 그대로 따른다 —
    "잠금 대기 시간 초과"면 3초 쉬고 1회 재시도한다.
    """
    from v2r.knowledge import keyword_discovery_parallel as kdp
    from v2r.knowledge import naver_keyword_tool as kt
    from v2r.warehouse import naver_session

    data_dir = Path(repo_root) / "data"
    profile_dir = data_dir / kdp.clone_profile_name(brand)
    if not profile_dir.is_dir():
        kdp.clone_all_profiles(data_dir, [brand])

    def _fetch(seeds: list[str]) -> list[Any]:
        for attempt in range(2):
            try:
                playwright, context, page, logged_in = kt.open_keyword_tool_page(
                    profile_dir=profile_dir, headless=True
                )
            except naver_session.ProfileLockTimeout as exc:
                if attempt == 0:
                    log.warning("검색량 조회: 프로필 잠금 대기 시간 초과, 3초 후 재시도(%s): %s", brand, exc)
                    time.sleep(_VOLUME_LOCK_RETRY_WAIT_SEC)
                    continue
                log.warning("검색량 조회: 프로필 잠금 재시도도 실패(%s): %s", brand, exc)
                return []
            try:
                if not logged_in:
                    log.warning("검색량 조회: 네이버 로그인이 풀려 있어 건너뜁니다(%s)", brand)
                    return []
                return kt.fetch_related_keywords(page, seeds, account_id="685753", download_dir=data_dir / "keywords" / "_tmp")
            finally:
                naver_session._close(playwright, context, page)
        return []

    return _fetch


def ensure_volumes(rt: Any, brand: str, keywords: list[str], fetch_fn: Any = None) -> dict[str, int]:
    """요청한 키워드마다 검색량(total = pc+mobile)을 반드시 채워 돌려준다.

    1) `data/keywords/<브랜드>.sqlite`에 total>0 이면 그 값을 쓴다.
    2) 없거나 0이면 네이버 키워드 도구에 5개씩 넣어 조회하고, 그 결과(씨앗 자신의
       행 포함)를 DB에 반영한다(없는 키워드는 새 행 삽입, 있으면 pc/mobile/total만
       갱신 — `relevance` 열은 건드리지 않는다).
    3) 도구가 그 키워드를 돌려주지 않으면(네이버 "< 10" 표기 규칙과 같은 최소값
       관례를 따라 `naver_keyword_tool._to_count`가 이미 쓰는 5) 최소값으로 채운다.
    4) 조회 자체가 실패하면(로그인 풀림·차단 등) 예외를 올리지 않고 경고만 남기고
       그 키워드는 빈 값(0, dict에서 제외)으로 둔다 — 이때만 호출 쪽(K열)을 건드리지 않는다.
    """
    import sqlite3

    from v2r.knowledge import naver_keyword_tool as kt

    want = [str(k).strip() for k in keywords if str(k or "").strip()]
    if not want:
        return {}
    norm_to_raw: dict[str, str] = {}
    for k in want:
        norm_to_raw.setdefault(_norm(k), k)

    db_path = _relevance_keywords_db_path(rt, brand)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS keywords ("
            "keyword TEXT PRIMARY KEY, pc INTEGER NOT NULL DEFAULT 0, "
            "mobile INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0, "
            "source_seed TEXT NOT NULL DEFAULT '', depth INTEGER NOT NULL DEFAULT 0, "
            "relevance INTEGER NOT NULL DEFAULT 0, collected_at TEXT NOT NULL)"
        )
        con.commit()

        result: dict[str, int] = {}
        missing: list[str] = []
        for norm_kw, raw_kw in norm_to_raw.items():
            row = con.execute("SELECT total FROM keywords WHERE keyword = ?", (raw_kw,)).fetchone()
            total = int(row[0]) if row and row[0] else 0
            if total > 0:
                result[norm_kw] = total
            else:
                missing.append(raw_kw)
        if not missing:
            return result

        fetch = fetch_fn or _real_volume_fetch_fn(brand, rt.settings.repo_root)
        for i in range(0, len(missing), _VOLUME_SEED_BATCH_SIZE):
            batch = missing[i : i + _VOLUME_SEED_BATCH_SIZE]
            try:
                rows = fetch(batch) or []
            except Exception as exc:  # noqa: BLE001 - 조회 실패는 경고만
                log.warning("검색량 조회 실패(%s, %s): %s", brand, batch, exc)
                continue
            by_norm = {_norm(r.keyword): r for r in rows if getattr(r, "keyword", "")}
            now = now_iso()
            for raw_kw in batch:
                norm_kw = _norm(raw_kw)
                r = by_norm.get(norm_kw)
                if r is None:
                    # 도구가 씨앗 자신을 돌려주지 않음 — 네이버 "< 10" 관례를 따른 최소값
                    pc, mobile, total = 0, 5, 5
                else:
                    pc, mobile, total = int(r.pc), int(r.mobile), int(r.total)
                if total <= 0:
                    continue
                con.execute(
                    "INSERT INTO keywords (keyword, pc, mobile, total, source_seed, depth, relevance, collected_at) "
                    "VALUES (?, ?, ?, ?, '', 0, 0, ?) "
                    "ON CONFLICT(keyword) DO UPDATE SET pc=excluded.pc, mobile=excluded.mobile, total=excluded.total",
                    (raw_kw, pc, mobile, total, now),
                )
                result[norm_kw] = total
        con.commit()
        return result
    finally:
        con.close()


def _relevance_eligible_keywords(rt: Any, brand: str) -> list[dict]:
    """`data/keywords/<브랜드>.sqlite`에서 원고 대상(`is_manuscript_target`)만.

    `sheets_writer.sync_keywords_to_sheet`와 같은 조건이다(연결 3, 2026-09-23;
    2026-09-24부터 0에서 3(당위성 포함), 4(무관)만 제외 — 사용자 지시).
    DB가 없거나(발굴/재산정 전) 읽는 중 문제가 있으면 조용히 빈 목록.
    """
    import sqlite3

    from v2r.knowledge import keyword_relevance as kr_mod

    db_path = _relevance_keywords_db_path(rt, brand)
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        try:
            cols = {row[1] for row in con.execute("PRAGMA table_info(keywords)")}
            required = {"relevance_llm", "relevance_codex", "needs_review"}
            if not required.issubset(cols):
                return []
            has_bridge = "bridge_rationale" in cols
            rows = con.execute(
                "select keyword, total, relevance_llm, relevance_codex, needs_review{bridge_col}"
                " from keywords where relevance_llm is not null".format(
                    bridge_col=", bridge_rationale" if has_bridge else ", '' as bridge_rationale"
                )
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:  # pragma: no cover - 방어용(다른 일꾼이 동시에 쓰는 중일 수 있음)
        log.warning("연관도 DB 읽기 실패(%s, %s): %s", brand, db_path, exc)
        return []
    return [
        {
            "keyword": r["keyword"],
            "cafe": "",
            "article_url": "",
            "t0_status": "",
            "candidate_title_norm": "",
            "volume": int(r["total"] or 0),
        }
        for r in rows
        if kr_mod.is_manuscript_target(r)
    ]


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

    # 2026-09-23 사용자 지시(연결 3), 2026-09-24 엄격화 — DB 원고 대상
    # (`is_manuscript_target`: relevance_llm·relevance_codex 둘 다 채점 완료,
    # 0~2이거나 3은 bridge_rationale 있을 때만, needs_review 아님)도 순환
    # 대상에 합친다. `keyword_relevance.py`는 다른 일꾼이 전량 재산정 중이라
    # 이 모듈은 그 sqlite를 읽기만 한다(쓰지 않음).
    for item in _relevance_eligible_keywords(rt, brand):
        key = _norm(item["keyword"])
        if key in out:
            out[key]["volume"] = max(int(out[key].get("volume") or 0), int(item.get("volume") or 0))
        else:
            out[key] = item

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
    # 시트·발굴 CSV에 검색량이 없는 키워드는 DB 검색량으로 채운다(연관도 무관).
    vol_map = _db_volume_map(rt, brand)
    if vol_map:
        for key, item in out.items():
            if int(item.get("volume") or 0) <= 0 and vol_map.get(key):
                item["volume"] = vol_map[key]
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
    picked = ordered[: max(0, n)]
    # 2026-09-24 사용자 지시: 검색량은 몰라선 안 된다 — 배치를 뽑을 때 0/없음인
    # 키워드는 키워드 도구로 직접 조회해 채운다(실패 시에만 그대로 0 유지).
    need = [p["keyword"] for p in picked if int(p.get("volume") or 0) <= 0]
    if need:
        try:
            got = ensure_volumes(rt, brand, need)
        except Exception as exc:  # noqa: BLE001 - 방어용(조회 실패는 volume 미기재로)
            log.warning("배치 검색량 채우기 실패(%s): %s", brand, exc)
            got = {}
        for p in picked:
            v = got.get(_norm(p["keyword"]))
            if v:
                p["volume"] = v
    return picked


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
    resolved_brands = brands or state.get("brands") or known_brands(rt)
    state.update(
        {
            "enabled": True,
            "brands": resolved_brands,
            "started_at": now_iso(),
            "paused_until_mono": None,
        }
    )
    _write_cycle_state(rt, state)
    # 연결 3(2026-09-23): 시작할 때 딱 한 번, 시트에 없는 키워드(H열 미존재)를
    # sync_keywords_to_sheet가 넣게 한다. 실패해도 순환 시작은 막지 않는다(경고 1회).
    try:
        from v2r.sources import sheets_writer

        for b in resolved_brands:
            try:
                sheets_writer.sync_keywords_to_sheet(b, repo_root=rt.settings.repo_root)
            except Exception as exc:
                log.warning("노출 순환 시작: 시트 키워드 반영 실패(%s): %s", b, exc)
    except Exception as exc:  # pragma: no cover - 방어용
        log.warning("노출 순환 시작: 시트 키워드 반영 건너뜀: %s", exc)
    return state


def cycle_stop(rt: Any) -> dict:
    """`노출 순환 중지`."""
    state = cycle_status(rt)
    state["enabled"] = False
    _write_cycle_state(rt, state)
    return state


# =======================================================================
# 연결 1(2026-09-23): 순환 판정 → 시트 반영 배칭
#
# 시트 쓰기(Google Sheets API/Playwright)는 느려서 판정마다 바로 부르면 순환
# 속도가 막힌다. 판정 결과를 브랜드별로 모아뒀다가 **20건 또는 5분**(먼저
# 차는 쪽) 마다 한 번씩, 별도 스레드에서 `sheets_writer.apply_exposure`를
# 부른다. 실패해도 다음 판정(순환)은 그대로 계속되고, 경고는 1회만 남긴다.
# =======================================================================

SHEET_BATCH_SIZE = 20
SHEET_BATCH_INTERVAL_SEC = 300

_sheet_batch_lock = threading.Lock()
#: brand -> [{keyword,status,final_url,edited_at,exposed_total,rank}, ...]
_sheet_batch: dict[str, list[dict]] = {}
_sheet_batch_last_flush: dict[str, float] = {}
#: 브랜드별 시트 반영 실패를 경고 1회만 남기기 위한 표시
_sheet_batch_warned: set[str] = set()


def _sheet_row_from_result(item: dict, row: "ExposureRow") -> dict:
    """`sheets_writer.apply_exposure`에 줄 배치 행 — A·G·J·K·L만 바꾸는 최종
    규칙(2026-09-23)에 맞춘다. `cafe`는 노출완일 때만 채운다(밀려남이면 A를
    아예 안 바꾸도록 `apply_exposure`가 빈 값을 걸러낸다)."""
    top5 = row.status == "exposed" and row.rank is not None and row.rank <= TOP5_RANK
    return {
        "keyword": row.keyword,
        "status": KOREAN_STATUS.get(row.status, row.status),
        "final_url": integrated_search_url(row.search_query or row.keyword),
        "edited_at": row.checked_at,
        "cafe": row.cafe if row.status == "exposed" else "",
        # 검색량을 모르면(0 포함) K열을 건드리지 않는다 — 0으로 덮지 않음(2026-09-24)
        "volume": int(item["volume"]) if item.get("volume") and int(item.get("volume") or 0) > 0 else None,
        "rank": "예" if top5 else "",
    }


def _sheet_totals(rt: Any, brand: str) -> dict[str, Any] | None:
    """`write_exposure_csv`가 이미 쓴 `<브랜드>.summary.json`에서 P1/Q1 합계를 읽는다."""
    p = exposure_dir(rt) / f"{brand}.summary.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    # 사용자 지시 2026-09-23: P1/Q1에 숫자만 덜렁 쓰지 말고 P2부터 이름 붙은 표로.
    total = int(data.get("total_volume_p1", 0) or 0)
    exposed = int(data.get("exposed_volume_q1", 0) or 0)
    return {
        "block": {
            "cell": "P2",
            "rows": [
                ["키워드 총 검색량", f"{total:,}"],
                ["노출 키워드 총 검색량", f"{exposed:,}"],
            ],
        }
    }


def _flush_sheet_batch_async(rt: Any, brand: str, rows: list[dict]) -> None:
    def _run() -> None:
        try:
            from v2r.sources import sheets_writer

            totals = _sheet_totals(rt, brand)
            res = sheets_writer.apply_exposure(
                brand, rows, totals=totals, repo_root=rt.settings.repo_root
            )
            if res.get("error"):
                raise SourceError(res["error"])
            _sheet_batch_warned.discard(brand)
        except Exception as exc:
            if brand not in _sheet_batch_warned:
                log.warning("노출 순환: 시트 반영 실패(%s, 이후 같은 경고 생략): %s", brand, exc)
                _sheet_batch_warned.add(brand)

    threading.Thread(target=_run, daemon=True, name=f"exposure-sheet-flush-{brand}").start()


def _enqueue_sheet_row(rt: Any, brand: str, item: dict, row: "ExposureRow") -> None:
    """판정 결과를 배치에 쌓고, 20건 또는 5분이 찼으면 별도 스레드로 흘려보낸다."""
    sheet_row = _sheet_row_from_result(item, row)
    now_mono = time.monotonic()
    to_flush: list[dict] | None = None
    with _sheet_batch_lock:
        pending = _sheet_batch.setdefault(brand, [])
        pending.append(sheet_row)
        last = _sheet_batch_last_flush.setdefault(brand, now_mono)
        if len(pending) >= SHEET_BATCH_SIZE or (now_mono - last) >= SHEET_BATCH_INTERVAL_SEC:
            to_flush = pending[:]
            _sheet_batch[brand] = []
            _sheet_batch_last_flush[brand] = now_mono
    if to_flush:
        _flush_sheet_batch_async(rt, brand, to_flush)


def flush_sheet_batch_now(rt: Any, brand: str) -> None:
    """대기 중인 배치를 즉시 흘려보낸다(순환 중지/테스트용)."""
    with _sheet_batch_lock:
        pending = _sheet_batch.get(brand) or []
        if not pending:
            return
        _sheet_batch[brand] = []
        _sheet_batch_last_flush[brand] = time.monotonic()
    _flush_sheet_batch_async(rt, brand, pending)


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

    from v2r.store import keyword_exposure_store as store

    verdict = judge_keyword_exposure(
        rt,
        brand,
        item["keyword"],
        cookies_path=cookies_path,
        article_index=getattr(rt, "article_index", None),
    )
    row = ExposureRow(
        brand,
        item["keyword"],
        item.get("cafe", ""),
        verdict["matched_url"],
        verdict["rank"],
        verdict["status"],
        now_iso(),
        item.get("t0_status", ""),
        verdict["search_query"],
        verdict.get("rank_overall"),
    )
    store.save(rt.conn, row.as_row())
    _enqueue_sheet_row(rt, brand, item, row)
    state["last_checked"] = {
        "brand": brand,
        "keyword": item["keyword"],
        "status": row.status,
        "at": row.checked_at,
        "candidates": verdict["candidates"],
        "opened": verdict["opened"],
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

    return {
        "brand": brand, "keyword": item["keyword"], "status": row.status, "rank": row.rank,
        "candidates": verdict["candidates"], "opened": verdict["opened"],
    }


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
                integrated_search_url((r["search_query"] if (r and r["search_query"]) else keyword)),
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


# =======================================================================
# 2026-09-23 사용자 지시 2차: "우리 글" 판별 — 카페 후보 → 댓글 식별어로 확정
#
# 1차 관문: 통검 최종 화면(스크롤 끝) 문서 중 우리 제휴·자사 카페(config/cafes.yaml)
# 소속인 것만 후보로 삼는다(다른 카페·블로그는 아예 열지 않는다).
# 2차 확정: 후보를 열어(네이버 프로필, headless, 3~6초 간격) 댓글(+본문)에
# 브랜드 식별어(config/brands.yaml `identifiers`)가 있으면 우리 글로 확정한다.
# article_index에 이미 있는 글이면 열지 않고 바로 확정한다. 확인 결과는
# 24시간 캐시(같은 글 URL 재확인 생략).
# =======================================================================

CAFES_CONFIG_PATH = "config/cafes.yaml"
BRANDS_CONFIG_PATH = "config/brands.yaml"
#: 글 확인 결과 캐시(data/ 아래) — 같은 URL은 24시간 재확인 안 함
ARTICLE_JUDGMENT_CACHE_FILE = "exposure_article_cache.json"
ARTICLE_CACHE_TTL_SECONDS = 24 * 3600
#: 후보 중 실제로 열어볼 최대 개수(전부 열면 느려지니 상한, 순위 위에서부터)
MAX_CANDIDATES_TO_OPEN = 5

#: 통검 결과 문서 링크(카페든 블로그든 안 가리고 화면 순서대로 센다)
_RE_ANY_RESULT_LINK = re.compile(
    r'<a[^>]+href="(?P<url>https?://[^"\']+)"[^>]*>(?P<title>.*?)</a>', re.I | re.S
)
#: 세지 않는 링크(검색 자체 내비게이션·광고 등)
_RE_SKIP_HOST = re.compile(r"search\.naver\.com|naver\.com/(?:ad|adcenter)", re.I)


def load_cafe_registry(rt: Any) -> list[dict]:
    """`config/cafes.yaml`의 제휴+자사 카페 목록(이름·번호·별칭)."""
    import yaml

    path = Path(rt.settings.config_dir.parent) / CAFES_CONFIG_PATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("카페 카탈로그 읽기 실패: %s", exc)
        return []
    out: list[dict] = []
    for section in ("affiliate", "self_owned"):
        for c in data.get(section) or []:
            out.append(
                {
                    "name": c.get("name", ""),
                    "cafe_id": c.get("cafe_id"),
                    "aliases": [a for a in (list(c.get("aliases") or []) + [c.get("name", "")]) if a],
                }
            )
    return out


def is_our_cafe_url(url: str, registry: list[dict]) -> bool:
    """이 카페 글 URL이 우리 제휴·자사 카페 소속인가(카페번호 또는 별칭 경로로 판단)."""
    u = str(url or "")
    if "cafe.naver.com" not in u:
        return False
    for c in registry:
        cid = c.get("cafe_id")
        if cid and re.search(rf"cafe\.naver\.com/(?:ca-fe/cafes/{cid}\b|{re.escape(str(cid))}/)", u, re.I):
            return True
        for alias in c.get("aliases") or []:
            if alias and re.search(rf"cafe\.naver\.com/{re.escape(str(alias))}(?:/|$)", u, re.I):
                return True
    return False


#: 실제 통검 결과 링크는 카페번호가 아니라 **URL 별칭**(영문, 예:
#: `cafe.naver.com/llchyll/12345?art=<jwt>`) 형태다. `config/cafes.yaml`은
#: 카페번호와 한국어 표시 이름만 알고 있어(2026-09-23 재현율 시험에서 발견)
#: `is_our_cafe_url`만으로는 후보를 거의 못 찾는다 — 별칭을 실제로 찾아
#: 카페번호로 바꿔 봐야 한다.
_RE_CAFE_ALIAS = re.compile(r"cafe\.naver\.com/(?!ca-fe/)([A-Za-z0-9_\-]{2,})", re.I)
_RE_CLUBID = re.compile(r"clubid=(\d+)", re.I)
#: 별칭→카페번호 조회 결과 캐시(data/ 아래) — 카페-별칭 결합은 거의 안 바뀐다
CAFE_ALIAS_CACHE_FILE = "exposure_cafe_alias_cache.json"


def cafe_alias_from_url(url: str) -> str:
    """카페 글 URL에서 별칭(영문 URL 경로 첫 조각)을 뽑는다. 못 찾으면 빈 문자열."""
    m = _RE_CAFE_ALIAS.search(str(url or ""))
    alias = m.group(1) if m else ""
    if alias.lower() in ("m", "articleread.nhn", "cafeprofile.nhn"):
        return ""
    return alias


def _alias_cache_path(rt: Any) -> Path:
    return Path(rt.settings.data_dir) / CAFE_ALIAS_CACHE_FILE


def _alias_cache_load(rt: Any) -> dict:
    p = _alias_cache_path(rt)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _alias_cache_save(rt: Any, data: dict) -> None:
    p = _alias_cache_path(rt)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def resolve_cafe_alias_id(
    rt: Any, alias: str, cookies: dict[str, str] | None = None, timeout: float = 8.0
) -> int | None:
    """카페 URL 별칭 → 카페번호(clubid). `https://cafe.naver.com/<별칭>` 페이지의
    `clubid=` 값을 읽는다(회원이 아니어도 보이는 페이지엔 대개 있다). 캐시.
    실패/못 찾으면 `None`(캐시에도 `None`으로 남겨 매번 다시 조회하지 않는다).
    """
    if not alias:
        return None
    cache = _alias_cache_load(rt)
    if alias in cache:
        return cache[alias]
    cid: int | None = None
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
        resp = httpx.get(
            f"https://cafe.naver.com/{alias}",
            cookies=cookies or {}, headers=headers, timeout=timeout, follow_redirects=True,
        )
        m = _RE_CLUBID.search(resp.text)
        if m:
            cid = int(m.group(1))
    except Exception as exc:
        log.warning("카페 별칭(%s) 조회 실패: %s", alias, exc)
    cache[alias] = cid
    _alias_cache_save(rt, cache)
    return cid


#: 카페 "홈"/클러스터 카드 링크(글이 아니라 카페 전체)는 후보로 열어봐야
#: 소용없다 — 실제 글 링크(번호가 붙은 경로 또는 ca-fe/cafes/.../articles/...)만
#: 후보로 삼는다(2026-09-23 재현율 시험에서 발견: 카페 홈 링크가 섞여 있었다).
_RE_CAFE_ARTICLE_LIKE = re.compile(
    r"cafe\.naver\.com/(?:ca-fe/cafes/\d+/articles/\d+|[^/?]+/\d+)", re.I
)


def looks_like_cafe_article_url(url: str) -> bool:
    """이 URL이 카페 "글"(번호 붙은 게시물)처럼 보이는가 — 카페 홈은 제외."""
    return bool(_RE_CAFE_ARTICLE_LIKE.search(str(url or "")))


def is_our_cafe_candidate(
    rt: Any, url: str, registry: list[dict], cookies: dict[str, str] | None = None
) -> bool:
    """이 통검 결과 링크가 우리 제휴·자사 카페 소속 후보인가.

    먼저 `is_our_cafe_url`(카페번호 경로·등록된 별칭)로 빠르게 보고, 아니면
    URL의 별칭을 실제로 조회(`resolve_cafe_alias_id`, 캐시됨)해 카페번호가
    레지스트리에 있는지 확인한다 — 통검 결과 링크는 거의 다 별칭 형태라 이
    2단계가 없으면 후보를 거의 못 찾는다(2026-09-23 재현율 시험에서 발견).
    """
    if is_our_cafe_url(url, registry):
        return True
    alias = cafe_alias_from_url(url)
    if not alias:
        return False
    cid = resolve_cafe_alias_id(rt, alias, cookies=cookies)
    if cid is None:
        return False
    return any(c.get("cafe_id") == cid for c in registry)


def extract_ordered_result_links(html: str) -> list[dict]:
    """통검 최종 DOM에서 문서 링크를 화면(작성 순서) 순서대로(중복 제거)."""
    seen: set[str] = set()
    out: list[dict] = []
    for m in _RE_ANY_RESULT_LINK.finditer(html or ""):
        url = m.group("url")
        if _RE_SKIP_HOST.search(url):
            continue
        norm = _norm_url(url)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append({"url": url, "title": _strip_tags(m.group("title"))})
    return out


def brand_identifiers(rt: Any, brand: str) -> list[str]:
    """`config/brands.yaml`의 `identifiers`(없으면 브랜드명 자체 하나)."""
    import yaml

    path = Path(rt.settings.config_dir.parent) / BRANDS_CONFIG_PATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("브랜드 설정 읽기 실패: %s", exc)
        return [brand]
    entry = (data.get("brands") or {}).get(brand) or {}
    ids = [i for i in (entry.get("identifiers") or []) if i]
    return ids or [brand]


def _article_cache_path(rt: Any) -> Path:
    return Path(rt.settings.data_dir) / ARTICLE_JUDGMENT_CACHE_FILE


def _article_cache_load(rt: Any) -> dict:
    p = _article_cache_path(rt)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _article_cache_save(rt: Any, data: dict) -> None:
    p = _article_cache_path(rt)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _cache_key(brand: str, url: str) -> str:
    """캐시 키 = 브랜드 + 정규화 URL(2026-09-24 수정 — 브랜드 없는 URL 전용 키는 다른
    브랜드가 먼저 확정한 판정을 그대로 재사용하는 오염을 일으켰다. 예: 팥순이 글
    `cafe.naver.com/cantsb/3544901`이 `ours=True`로 캐시된 뒤, 같은 URL이 뉴더미스
    후보로도 잡히면 뉴더미스 식별어 확인 없이 그대로 노출완으로 잘못 확정됐다)."""
    return f"{brand}::{_norm_url(url)}"


def cached_verdict(rt: Any, brand: str, url: str, now: str | None = None) -> bool | None:
    """이 브랜드의 이 글 URL을 24시간 안에 이미 확인했으면 그 결과(참/거짓), 아니면 `None`.

    브랜드 없이 저장된 옛 캐시 항목(`_norm_url(url)`만 키인 것)은 브랜드를 구분할
    수 없으므로 무효로 보고 무시한다(2026-09-24 크로스 브랜드 오염 수정)."""
    from datetime import datetime as _dt

    data = _article_cache_load(rt)
    entry = data.get(_cache_key(brand, url))
    if not entry:
        return None
    try:
        at = _dt.fromisoformat(str(entry["at"]))
        now_dt = _dt.fromisoformat(now) if now else _dt.now(at.tzinfo or KST)
        if (now_dt - at).total_seconds() > ARTICLE_CACHE_TTL_SECONDS:
            return None
    except Exception:
        return None
    return bool(entry.get("ours"))


def set_cached_verdict(rt: Any, brand: str, url: str, ours: bool, now: str | None = None) -> None:
    data = _article_cache_load(rt)
    data[_cache_key(brand, url)] = {"ours": bool(ours), "at": now or now_iso()}
    _article_cache_save(rt, data)


def fetch_article_text(
    url: str, cookies_path: str | Path | None = None, headless: bool = True
) -> str:
    """글 상세(댓글 포함) 페이지 텍스트. Playwright(네이버 프로필, headless)로 연다.

    카페 글은 본문·댓글이 `cafe_main` iframe 안에 있는 경우가 많아 그 프레임을
    우선 읽고, 없으면 메인 프레임을 읽는다.

    2026-09-23 재현율 시험에서 발견: `cafe_main` 프레임은 바로 안 뜬다(SPA가
    비동기로 붙인다) — 800ms만 기다리면 프레임을 못 찾거나(빈 텍스트) 댓글이
    아직 안 붙은 채로 읽힌다. 프레임을 최대 5번(500ms 간격) 찾아보고,
    찾으면 그 프레임이 `networkidle`(댓글 API 포함)까지 갈 때까지 기다린다.
    실패 시 예외.
    """
    from playwright.sync_api import sync_playwright

    storage_state = str(cookies_path) if cookies_path and Path(cookies_path).exists() else None
    with sync_playwright() as pw:
        browser = launch_chromium(pw, headless=headless)
        try:
            context = browser.new_context(storage_state=storage_state)
            page = context.new_page()
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            frame = None
            for _ in range(5):
                try:
                    frame = page.frame(name="cafe_main")
                except Exception:
                    frame = None
                if frame is not None:
                    break
                page.wait_for_timeout(500)
            if frame is not None:
                try:
                    frame.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                frame.wait_for_timeout(1200)
            else:
                page.wait_for_timeout(1500)
            target = frame or page.main_frame
            return target.inner_text("body")
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# 2026-09-23 사용자 확정 — 식별어는 **댓글2 계열**(댓글2 → 대댓글2 → 대대댓글2 →
# 대대대댓글2, `config/brands.yaml` comment_profiles의 라벨)에만 심는다. 댓글
# 트리에서 "몇 번째 최상위 댓글"과 "그 밑 답글이냐"를 실측 DOM 클래스로 가른다.
#
# 실측 근거(2026-09-23, `data/exposure_audit/팥순이/_hankki_article_raw.html`,
# 헤드리스로 카페 글 상세를 열어 `cafe_main` 프레임 원본 HTML을 그대로 저장해 확인):
#   <li id="<댓글id>" class="CommentItem">              ← 최상위 댓글(댓글N)
#   <li id="<댓글id>" class="CommentItem CommentItem--reply">  ← 그 바로 위 최상위
#       댓글에 달린 답글(대댓글N/대대댓글N/대대대댓글N — Naver 카페 UI 자체는 답글을
#       한 단계로만 펼쳐 보여주지만, DOM 순서상 같은 최상위 댓글 밑에 연달아 나온다.
#       그래서 "몇 번째 최상위 댓글 다음이냐"로 댓글N 계열을 가른다)
#   본문 텍스트는 그 `<li>` 안 `class="comment_text_view"` 문단(<p>) 하나.
# 실측 결과: `한끼통살`/`비만도 계산기` 둘 다 식별어가 **두 번째 최상위 댓글**
# (top_index=2, 정확히 "댓글2")의 본문 자체에서 나왔다 — 사용자 규칙과 일치.
# ---------------------------------------------------------------------------

_RE_COMMENT_LI = re.compile(
    r'<li[^>]*\bid="(\d+)"[^>]*\bclass="(CommentItem(?: CommentItem--reply)?)"', re.I
)
_RE_COMMENT_TEXT_P = re.compile(r'class="comment_text_view"[^>]*>(.*?)</p>', re.S | re.I)
_RE_HTML_TAG = re.compile(r"<[^>]+>")


def extract_comment_tree(article_html: str) -> list[dict]:
    """글 상세 **원본 HTML**(`fetch_article_html`)에서 댓글을 화면 순서대로,
    "몇 번째 최상위 댓글 계열이냐"(`top_index`, 1부터)와 답글 여부(`is_reply`)를
    같이 뽑는다. `CommentItem`류 마커가 없는 화면(옛 마크업 등)이면 빈 목록 —
    호출 쪽은 그러면 위치 구분 없이 `extract_comments`(댓글 전체)로 넘어간다.
    """
    items = list(_RE_COMMENT_LI.finditer(article_html or ""))
    if not items:
        return []
    out: list[dict] = []
    top_index = 0
    for i, m in enumerate(items):
        comment_id, cls = m.group(1), m.group(2)
        is_reply = "reply" in cls
        if not is_reply:
            top_index += 1
        start = m.end()
        end = items[i + 1].start() if i + 1 < len(items) else min(len(article_html), start + 4000)
        block = article_html[start:end]
        tm = _RE_COMMENT_TEXT_P.search(block)
        text = _RE_HTML_TAG.sub("", tm.group(1)).strip() if tm else ""
        out.append({"comment_id": comment_id, "top_index": top_index, "is_reply": is_reply, "text": text})
    return out


def find_identifier_in_reply2_series(
    tree: list[dict], identifiers: list[str]
) -> dict | None:
    """댓글 트리에서 **댓글2 계열**(top_index == 2, 대댓글2/대대댓글2/대대대댓글2
    포함)에서만 식별어를 찾는다. 찾으면 `{comment_id, top_index, is_reply,
    ident, out_of_position: False}`, 댓글2 계열 밖에서만 나오면
    `{..., out_of_position: True}`(보고용, 확정에는 안 씀), 아예 없으면 `None`.
    """
    norm_idents = [(ident, _norm_identifier_text(ident)) for ident in identifiers if ident]
    out_of_position_hit: dict | None = None
    for item in tree:
        norm_text = _norm_identifier_text(item["text"])
        for ident, norm_ident in norm_idents:
            if norm_ident and norm_ident in norm_text:
                if item["top_index"] == 2:
                    return {
                        "comment_id": item["comment_id"], "top_index": item["top_index"],
                        "is_reply": item["is_reply"], "ident": ident, "out_of_position": False,
                    }
                if out_of_position_hit is None:
                    out_of_position_hit = {
                        "comment_id": item["comment_id"], "top_index": item["top_index"],
                        "is_reply": item["is_reply"], "ident": ident, "out_of_position": True,
                    }
    return out_of_position_hit


def fetch_article_html(
    url: str, cookies_path: str | Path | None = None, headless: bool = True
) -> str:
    """글 상세(댓글 포함) **원본 HTML**(innerText가 아니라). `extract_comment_tree`용
    — `fetch_article_text`와 프레임 대기 로직은 같고 반환만 `frame.content()`."""
    from playwright.sync_api import sync_playwright

    storage_state = str(cookies_path) if cookies_path and Path(cookies_path).exists() else None
    with sync_playwright() as pw:
        browser = launch_chromium(pw, headless=headless)
        try:
            context = browser.new_context(storage_state=storage_state)
            page = context.new_page()
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            frame = None
            for _ in range(5):
                try:
                    frame = page.frame(name="cafe_main")
                except Exception:
                    frame = None
                if frame is not None:
                    break
                page.wait_for_timeout(500)
            if frame is not None:
                try:
                    frame.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                frame.wait_for_timeout(1200)
            else:
                page.wait_for_timeout(1500)
            target = frame or page.main_frame
            return target.content()
        finally:
            browser.close()


def article_has_identifier(text: str, identifiers: list[str]) -> bool:
    body = text or ""
    return any(ident and ident in body for ident in identifiers)


#: 댓글 한 건의 끝을 표시하는 네이버 카페 댓글 UI 문구("답글쓰기" 버튼) — 이 마커
#: 앞까지가 댓글 본문+작성자+날짜다. 2026-09-23 재지시: 식별어는 **댓글에서만**
#: (본문 제외) 찾아야 하므로, 본문·댓글을 나눠 세야 한다.
_RE_COMMENT_BLOCK = re.compile(r"(.*?)\n답글쓰기", re.S)
#: 댓글 섹션 시작(제목 줄 "댓글 12" 류) — 이보다 앞은 본문으로 본다.
_RE_COMMENT_SECTION_START = re.compile(r"\n\s*댓글\s*\d+\s*\n")


def _norm_identifier_text(s: str) -> str:
    """식별어 비교용 정규화 — 공백 제거·대소문자 무시(2026-09-23 재지시)."""
    return re.sub(r"\s+", "", str(s or "")).casefold()


def extract_comments(article_text: str) -> list[str]:
    """글 상세 텍스트(`fetch_article_text` 결과)에서 **댓글만** 순서대로 뽑는다.

    네이버 카페 댓글 UI는 각 댓글이 "작성자\\n\\n내용\\n\\n날짜\\n답글쓰기" 꼴로
    끝난다(2026-09-23 실측). 본문(첫 "댓글 N" 표시줄 앞)은 제외한다. 패턴이 없는
    글(구조가 다르거나 댓글 0개)이면 빈 목록.
    """
    text = article_text or ""
    m = _RE_COMMENT_SECTION_START.search(text)
    tail = text[m.end():] if m else text
    # "댓글을 입력하세요" 아래는 작성창(다음 글 미리보기 등)이라 제외
    tail = tail.split("댓글을 입력하세요", 1)[0]
    return [blk.strip() for blk in _RE_COMMENT_BLOCK.findall(tail) if blk.strip()]


def find_identifier_in_comments(
    comments: list[str], identifiers: list[str]
) -> tuple[int, str] | None:
    """댓글 목록(1번부터)에서 식별어를 처음 찾은 (번호, 식별어) — 없으면 `None`.

    공백 제거·대소문자 무시로 비교한다(2026-09-23 재지시 — "팥순ㅇㅣ" 같은
    자모 분리 표기·띄어쓰기 차이를 놓치지 않기 위함).
    """
    norm_idents = [(ident, _norm_identifier_text(ident)) for ident in identifiers if ident]
    for i, comment in enumerate(comments, start=1):
        norm_comment = _norm_identifier_text(comment)
        for ident, norm_ident in norm_idents:
            if norm_ident and norm_ident in norm_comment:
                return i, ident
    return None


def confirm_our_article_detail(
    rt: Any,
    brand: str,
    url: str,
    identifiers: list[str],
    cookies_path: str | Path | None = None,
    article_index: Any = None,
    now: str | None = None,
) -> dict:
    """`confirm_our_article`의 상세판 — 위치(댓글2 계열인지)까지 돌려준다.

    반환: `{ours: bool, via: "article_index"|"cache"|"tree"|"flat_comments"|"error",
    hit: find_identifier_in_reply2_series 결과 또는 None}`.

    순서: (1) `article_index`에 이미 있으면 열지 않고 바로 확정(위치 확인 불필요 —
    우리가 이미 발행 기록으로 아는 글) → (2) 24시간 캐시 → (3) 직접 열어 **원본
    HTML**로 댓글 트리를 뽑아 **댓글2 계열에서만** 식별어를 찾는다(2026-09-23
    사용자 확정 규칙). `CommentItem` 마커가 없는 화면(옛 마크업)이면 위치 구분
    없이 댓글 전체(`extract_comments`)로 대신 확인한다(`find_identifier_in_comments`).
    """
    if article_index is not None:
        try:
            aid = _article_id(url)
            if aid and article_index.has_article_id(aid):
                return {"ours": True, "via": "article_index", "hit": None}
        except AttributeError:
            pass
        except Exception as exc:  # pragma: no cover - 방어용
            log.warning("article_index 확인 실패(%s): %s", url, exc)

    cached = cached_verdict(rt, brand, url, now=now)
    if cached is not None:
        return {"ours": cached, "via": "cache", "hit": None}

    try:
        html = fetch_article_html(url, cookies_path=cookies_path)
        tree = extract_comment_tree(html)
        if tree:
            hit = find_identifier_in_reply2_series(tree, identifiers)
            ours = bool(hit) and not hit.get("out_of_position")
            via = "tree"
        else:
            # 옛 마크업 등 트리 파싱이 안 되는 화면 — 위치 구분 없이(댓글 전체) 확인.
            text = fetch_article_text(url, cookies_path=cookies_path)
            comments = extract_comments(text)
            flat_hit = find_identifier_in_comments(comments, identifiers)
            hit = (
                {"comment_id": "", "top_index": flat_hit[0], "is_reply": None,
                 "ident": flat_hit[1], "out_of_position": None}
                if flat_hit else None
            )
            ours = flat_hit is not None
            via = "flat_comments"
    except Exception as exc:
        log.warning("글 열람 확인 실패(%s): %s", url, exc)
        return {"ours": False, "via": "error", "hit": None}
    set_cached_verdict(rt, brand, url, ours, now=now)
    return {"ours": ours, "via": via, "hit": hit}


def confirm_our_article(
    rt: Any,
    brand: str,
    url: str,
    identifiers: list[str],
    cookies_path: str | Path | None = None,
    article_index: Any = None,
    now: str | None = None,
) -> bool:
    """후보 글이 진짜 우리 글인지 확정한다(참/거짓만 — 위치까지 필요하면
    `confirm_our_article_detail`)."""
    return confirm_our_article_detail(
        rt, brand, url, identifiers, cookies_path=cookies_path,
        article_index=article_index, now=now,
    )["ours"]


def judge_keyword_exposure(
    rt: Any,
    brand: str,
    keyword: str,
    cookies_path: str | Path | None = None,
    dom_html: str | None = None,
    cafe_registry: list[dict] | None = None,
    identifiers: list[str] | None = None,
    article_index: Any = None,
    max_candidates: int = MAX_CANDIDATES_TO_OPEN,
    sleep_fn: Any = time.sleep,
    search_query: str | None = None,
) -> dict:
    """B1 2차 재설계 — 카페 후보(1차 관문) → 댓글 식별어(2차 확정)로 판정.

    반환: `{search_query, candidates, opened, status(exposed|pushed|unknown),
    rank, rank_overall, matched_url, matched_as, identifier_position,
    sub_link_hits}`.
    `rank`는 **일반 결과만**(광고·쇼핑·뉴스·내비·도움말·`ader.naver.com`
    캐러셀 제외, 카페·블로그·포스트·지식iN 중 진짜 글만) 셌을 때 몇 번째인지 —
    `v2r/knowledge/serp.py`의 `extract_serp` 재사용(2026-09-23 정정, `top_reference.py`
    가 먼저 검증한 추출기). `rank_overall`은 예전 방식(통검 화면의 **모든**
    `<a href>`를 순서대로 센 것, 광고·내비도 포함) — 참고용으로 남겨 둔다.

    2026-09-23 사용자 정정(카페 카드 구조) — 카페 결과 한 "카드"는 ① 대표 글
    (큰 제목 링크, 그 밑 댓글 미리보기는 대표 글에 딸린 일부일 뿐 별도 글이
    아니다) ② 서브 링크(같은 카페의 **다른** 글 제목 링크, 댓글 미리보기 없음)
    로 이뤄진다. 우리 글이 **서브 링크로만** 보이면 밀려남이다(`serp.py`의
    `extract_cafe_cards` 재사용). `matched_as`는 "representative"|"sub".

    2026-09-23 사용자 확정(댓글 위치) — 식별어는 **댓글2 계열**(댓글2·대댓글2·
    대대댓글2·대대대댓글2)에만 심는다. 대표 글이어도 식별어가 그 계열 밖에서만
    나오면 확정하지 않는다(`confirm_our_article_detail`이 판정). `identifier_position`
    에 `{top_index, is_reply, out_of_position}`을 남긴다(위치 이탈이면
    `out_of_position=True`로 보고에 표시).
    """
    import random

    query = (
        search_query
        if search_query is not None
        else (keyword if dom_html is not None else resolve_search_query(keyword))
    )
    registry = cafe_registry if cafe_registry is not None else load_cafe_registry(rt)
    idents = identifiers if identifiers is not None else brand_identifiers(rt, brand)
    cookies = _cookie_dict(cookies_path) if cookies_path else {}

    try:
        html = dom_html if dom_html is not None else fetch_integrated_search_dom(query, cookies_path=cookies_path)
    except Exception as exc:
        log.warning("통검 DOM 확인 실패(%s): %s", keyword, exc)
        return {
            "search_query": query, "candidates": 0, "opened": 0,
            "status": "unknown", "rank": None, "rank_overall": None, "matched_url": "",
            "matched_as": "", "identifier_position": None, "sub_link_hits": [],
        }

    ordered = extract_ordered_result_links(html)
    # 카드 파싱이 되는 화면이면 대표 글만 후보로 삼고, 서브 링크는 따로 기록만
    # 한다(위 docstring 참고). 카드 마커가 없으면(옛 마크업) 예전처럼 모든
    # 카페 글 링크를 대표로 본다 — 판정이 아예 안 되는 것보다는 낫다.
    cards = serp.extract_cafe_cards(html)
    sub_norm: set[str] = set()
    sub_link_hits: list[dict] = []
    if cards:
        for card in cards:
            for sub_url in card["sub_urls"]:
                if looks_like_cafe_article_url(sub_url) and is_our_cafe_candidate(rt, sub_url, registry, cookies=cookies):
                    sub_norm.add(_norm_url(sub_url))
                    sub_link_hits.append({"url": sub_url, "representative_url": card["representative_url"]})

    candidates = []
    for i, item in enumerate(ordered):
        if not looks_like_cafe_article_url(item["url"]):
            continue
        if not is_our_cafe_candidate(rt, item["url"], registry, cookies=cookies):
            continue
        if cards and _norm_url(item["url"]) in sub_norm:
            continue  # 서브 링크는 후보에서 뺀다(대표 글로만 판단)
        candidates.append((i + 1, item))

    opened = 0
    to_open = candidates[:max_candidates]
    for i, (rank_overall, item) in enumerate(to_open):
        detail = confirm_our_article_detail(
            rt, brand, item["url"], idents, cookies_path=cookies_path, article_index=article_index
        )
        opened += 1
        if detail["ours"]:
            general_rank = serp.general_result_rank(html, item["url"])
            hit = detail.get("hit")
            return {
                "search_query": query, "candidates": len(candidates), "opened": opened,
                "status": "exposed", "rank": general_rank, "rank_overall": rank_overall,
                "matched_url": item["url"], "matched_as": "representative",
                "identifier_position": hit, "sub_link_hits": sub_link_hits,
            }
        if i < len(to_open) - 1:
            sleep_fn(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC))

    return {
        "search_query": query, "candidates": len(candidates), "opened": opened,
        "status": "pushed", "rank": None, "rank_overall": None, "matched_url": "",
        "matched_as": "", "identifier_position": None, "sub_link_hits": sub_link_hits,
    }


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
    "ensure_volumes",
    "cycle_state_path",
    "cycle_status",
    "cycle_start",
    "cycle_stop",
    "cycle_tick",
    "EXPOSURE_CSV_HEADERS",
    "exposure_dir",
    "write_exposure_csv",
    # 검색어 자동완성/끝까지 스크롤(2026-09-23)
    "AUTOCOMPLETE_URL",
    "naver_autocomplete_first",
    "resolve_search_query",
    "fetch_integrated_search_dom",
    # 카페 후보 → 댓글 식별어 확정(2026-09-23 2차)
    "CAFES_CONFIG_PATH",
    "BRANDS_CONFIG_PATH",
    "ARTICLE_JUDGMENT_CACHE_FILE",
    "ARTICLE_CACHE_TTL_SECONDS",
    "MAX_CANDIDATES_TO_OPEN",
    "load_cafe_registry",
    "is_our_cafe_url",
    "cafe_alias_from_url",
    "resolve_cafe_alias_id",
    "is_our_cafe_candidate",
    "looks_like_cafe_article_url",
    "CAFE_ALIAS_CACHE_FILE",
    "extract_ordered_result_links",
    "brand_identifiers",
    "cached_verdict",
    "set_cached_verdict",
    "fetch_article_text",
    "article_has_identifier",
    "extract_comments",
    "find_identifier_in_comments",
    "confirm_our_article",
    "confirm_our_article_detail",
    "extract_comment_tree",
    "find_identifier_in_reply2_series",
    "fetch_article_html",
    "judge_keyword_exposure",
    # 연결 1: 순환→시트 배칭 (2026-09-23)
    "SHEET_BATCH_SIZE",
    "SHEET_BATCH_INTERVAL_SEC",
    "flush_sheet_batch_now",
    # 연결 3: 연관도 DB 병합
    "_relevance_eligible_keywords",
]


def launch_chromium(pw, headless: bool = True):
    """헤드리스 크로미움을 띄운다. 기본 헤드리스 셸이 "Executable doesn't exist"로 실패하면
    (2026-09-23 실행기 숨김 실행에서 재현) 전체 크로미움(chromium-*/chrome.exe)으로 대신 띄운다."""
    import os, glob
    try:
        return pw.chromium.launch(headless=headless)
    except Exception as exc:  # noqa: BLE001
        if "Executable doesn't exist" not in str(exc):
            raise
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    cands = sorted(glob.glob(os.path.join(base, "chromium-*", "chrome-win64", "chrome.exe")), reverse=True)
    if not cands:
        raise RuntimeError(f"크로미움 실행 파일을 찾지 못함: {base}")
    return pw.chromium.launch(headless=headless, executable_path=cands[0], args=["--headless=new"] if headless else None)
