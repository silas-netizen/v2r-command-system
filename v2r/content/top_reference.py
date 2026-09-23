"""검색량 상위 키워드에서 네이버 통합검색 1등 글의 "형식"만 참고한다 (2026-09-23 지시).

절대 규칙: 문장·표현·사례를 복사하지 않는다. 제목 유형·길이·단락 수·도입
방식·전개 순서·마무리 방식·사진 수 같은 "형식"만 뽑아 `format_brief`로 짧게
요약해 원고 프롬프트에 참고 자료로 얹는다.

흐름: `resolve_search_query`(자동완성 정규화) → `fetch_integrated_search_dom`
(헤드리스 통검 첫 페이지 끝까지 스크롤) → 광고/파워링크/쇼핑/뉴스를 뺀 첫
일반 글(카페·블로그·포스트·지식iN 순) 추출 → 그 글을 헤드리스로 열어 본문
텍스트를 가져와 형식 지표를 계산한다.

캐시: `data/top_reference/<키워드>.json`, TTL `config/bulk.yaml`의
`top_reference.ttl_days`(기본 7일). 실패는 `{"error": ...}`로 1일만 캐시해
반복 실패를 막는다.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from v2r.config import load_yaml
from v2r.store.db import now_iso

log = logging.getLogger(__name__)

#: 2026-09-23(교차 검증 후속) — SERP 추출기는 `v2r/knowledge/serp.py`로 옮겼다
#: (`keyword_exposure.py`도 같이 쓴다). 여기서는 이름만 그대로 다시 노출한다.
from v2r.knowledge import serp as _serp

_EXCLUDE_HOST_HINTS = _serp.EXCLUDE_HOST_HINTS
_EXCLUDE_TEXT_HINTS = _serp.EXCLUDE_TEXT_HINTS
_RESULT_LINK_PATTERNS = _serp.RESULT_LINK_PATTERNS
_EXCLUDE_URL_HINTS = _serp.EXCLUDE_URL_HINTS

# ---------------------------------------------------------------------------
# 2026-09-23 추가 지시 — 우리가 고른 글이 통검에서 실제 몇 번째인지 기록 +
# 다른 방식(requests, 비로그인)으로 한 번 더 교차 검증.
#
# 한계(보고서에도 적음): 순수 정규식으로 DOM을 훑는 방식이라 "VIEW"·"인플루언서"
# 같은 네이버 통검의 실제 섹션 제목까지는 못 읽는다 — 섹션은 링크의 도메인으로만
# 어림한다(카페/블로그/포스트/지식iN/쇼핑/뉴스/광고). 실제 화면 순서(등장 위치)는
# 정확하다.
# ---------------------------------------------------------------------------

def _norm_serp_url(url: str) -> str:
    u = re.sub(r"^https?://", "", url, flags=re.I)
    u = re.sub(r"^m\.", "", u, flags=re.I)
    return u.split("?", 1)[0].rstrip("/").casefold()


#: 카페/블로그/포스트/지식iN 섹션 이름 -> (strict 패턴, source 키). 각 결과 블록에는
#: "카페 이름 배지" 링크(`cafe.naver.com/<별칭>`, 글 번호 없음)가 실제 글 링크
#: 바로 앞에 따로 박혀 있다 — 2026-09-23 재정정 실측에서 확인한 오탐 원인(이
#: 배지 링크를 "카페 결과"로 잘못 세는 바람에 `rank_in_source`가 항상 실제보다
#: 1 컸다). 느슨한 도메인 매칭 대신 글 번호가 있는 `_RESULT_LINK_PATTERNS`(진짜
#: 글만 잡는 패턴)를 그대로 재사용해 이 오탐을 없앤다.
_SERP_STRICT_SECTIONS = _serp.STRICT_SECTIONS
_SERP_STRICT_PATTERN_BY_SOURCE = _serp.STRICT_PATTERN_BY_SOURCE
_SERP_LOOSE_SECTIONS = _serp.LOOSE_SECTIONS

#: 하위 호환 — 기존 호출부(`top_reference.py` 안, 시험)는 그대로 `extract_serp`를 쓴다.
extract_serp = _serp.extract_serp


def _requests_crosscheck(keyword_query: str, cookies: dict | None = None, sources: list[str] | None = None) -> dict:
    """다른 방식(requests, 비로그인 헤더 쿠키만)으로 한 번 더 1등 글(기본: 카페)을 확인한다.

    Playwright(로그인 프로필·끝까지 스크롤) 결과와 URL이 같은지만 본다 — 실패해도
    예외를 올리지 않고 `{"url": "", "error": ...}`를 돌려준다.
    """
    from v2r.knowledge.keyword_exposure import fetch_integrated_search_html

    try:
        html = fetch_integrated_search_html(keyword_query, cookies=cookies)
    except Exception as exc:
        return {"url": "", "error": str(exc)}
    picked = _pick_top_link(html, sources=sources)
    return {"url": picked["url"] if picked else "", "source": picked["source"] if picked else ""}

_RE_TAG = re.compile(r"<[^>]+>")
_RE_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)

#: 캐시 실패 결과 TTL(일) — 성공 TTL과 별도(사용자 지시: "실패는 1일만")
_FAIL_TTL_DAYS = 1


def _cache_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / "data" / "top_reference"


def _cache_path(repo_root: str | Path, keyword: str) -> Path:
    safe = re.sub(r"[\\/:*?\"<>|]", "_", str(keyword or "")).strip() or "_empty"
    return _cache_dir(repo_root) / f"{safe}.json"


def _cfg() -> dict:
    return (load_yaml("bulk") or {}).get("top_reference") or {}


#: 캐시 스키마 버전 — 2026-09-23 정정(카페 전용 + serp/rank_in_source 추가)으로
#: 1 -> 2, 같은 날 `extract_serp`의 "카페 이름 배지" 오탐(rank_in_source가 항상
#: 실제보다 1 컸던 버그)을 고치면서 2 -> 3. 옛 캐시(버전 없음/구버전)는 무효로
#: 보고 다시 수집한다.
CACHE_VERSION = 3


def _load_cache(repo_root: str | Path, keyword: str) -> dict | None:
    p = _cache_path(repo_root, keyword)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if int(data.get("version") or 0) != CACHE_VERSION:
        return None
    fetched_at = str(data.get("fetched_at") or "")
    if not fetched_at:
        return None
    from datetime import datetime, timezone

    try:
        ts = datetime.fromisoformat(fetched_at)
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(ts.tzinfo) - ts).total_seconds() / 86400.0
    ttl = _FAIL_TTL_DAYS if data.get("error") else int(_cfg().get("ttl_days", 7) or 7)
    if age_days > ttl:
        return None
    return data


def _save_cache(repo_root: str | Path, keyword: str, data: dict) -> None:
    data = {**data, "version": CACHE_VERSION}
    p = _cache_path(repo_root, keyword)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    import os

    os.replace(tmp, p)


def _strip_tags(html: str) -> str:
    html = _RE_SCRIPT.sub(" ", html or "")
    text = _RE_TAG.sub(" ", html)
    text = re.sub(r"&nbsp;|&amp;|&lt;|&gt;|&quot;|&#39;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


#: 2026-09-23 정정 — 참고할 "1위 글"은 카페 글만. `config/bulk.yaml`
#: `top_reference.sources`(기본 `[cafe]`)로 확장 가능하게 둔다.
DEFAULT_SOURCES = ["cafe"]


def _allowed_sources() -> list[str]:
    sources = _cfg().get("sources")
    if not sources:
        return list(DEFAULT_SOURCES)
    return [str(s).strip() for s in sources if str(s).strip()]


def _pick_top_link(dom_html: str, sources: list[str] | None = None) -> dict | None:
    """통검 DOM에서 광고/쇼핑/뉴스를 뺀, `sources`(기본 카페만)에 속하는 첫 글.

    `sources`를 안 주면 `config/bulk.yaml`의 `top_reference.sources`(기본
    `["cafe"]`)를 따른다. 여러 소스를 허용하면 각 소스별 DOM 첫 등장 위치를
    비교해 화면에 먼저 나오는 쪽을 우선한다.
    """
    if not dom_html:
        return None
    allowed = set(sources) if sources is not None else set(_allowed_sources())
    best: tuple[int, str, str] | None = None  # (offset, source, url)
    for source, pattern in _RESULT_LINK_PATTERNS:
        if source not in allowed:
            continue
        m = pattern.search(dom_html)
        if not m:
            continue
        url = m.group(0)
        if any(h in url for h in _EXCLUDE_HOST_HINTS) or any(h in url for h in _EXCLUDE_URL_HINTS):
            continue
        offset = m.start()
        if best is None or offset < best[0]:
            best = (offset, source, url)
    if best is None:
        return None
    _, source, url = best
    # 제목: 링크 주변 200자 안에서 태그를 뗀 텍스트로 어림(정확한 파싱기 없이도 충분)
    start = max(0, best[0] - 50)
    window = dom_html[start:start + 500]
    title = _strip_tags(window)[:80].strip()
    return {"url": url, "source": source, "title": title}


def _to_mobile_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    host = parsed.netloc
    if host.startswith("m."):
        return url
    if "cafe.naver.com" in host or "blog.naver.com" in host:
        return url.replace(f"//{host}", f"//m.{host}", 1)
    return url


def _title_type(title: str) -> str:
    t = title or ""
    if "?" in t or any(w in t for w in ("일까", "될까", "인가요", "될까요")):
        return "질문형"
    if any(w in t for w in ("후기", "써봤", "써본", "리얼", "직접")):
        return "후기형"
    if any(w in t for w in ("방법", "추천", "정리", "비교", "총정리", "가이드")):
        return "정보형"
    return "기타"


def _opening_type(first_sentence: str) -> str:
    s = (first_sentence or "").strip()
    if not s:
        return "알수없음"
    if s.endswith("?") or "?" in s[:30]:
        return "질문"
    if any(w in s[:20] for w in ("결론", "정답", "답은")):
        return "결론 먼저"
    return "상황 서술"


def _closing_type(last_paragraph: str) -> str:
    s = (last_paragraph or "").strip()
    if not s:
        return "알수없음"
    if s.endswith("?") or "?" in s[-30:]:
        return "질문"
    if any(w in s[-40:] for w in ("해보세요", "해보시길", "확인해", "찾아보")):
        return "요청"
    return "정리"


def _analyze_article(text: str, source: str, url: str, title: str) -> dict:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()] if text.strip() else []
    length = len(text)
    first_sentence = ""
    if paragraphs:
        m = re.split(r"(?<=[.!?…])\s+", paragraphs[0])
        first_sentence = m[0] if m else paragraphs[0][:60]
    flow = []
    for p in paragraphs[:5]:
        m = re.split(r"(?<=[.!?…])\s+", p)
        flow.append((m[0] if m else p[:40])[:40])
    has_list = bool(re.search(r"^\s*[-*・①②③1234567890][.)]?\s", text, re.M))
    has_subhead = bool(re.search(r"^#{1,3}\s|^【.+】$|^\[.+\]$", text, re.M))
    images = len(re.findall(r"\.(?:jpg|jpeg|png|gif|webp)(?:\?|$|[\s\"'])", text, re.I))
    return {
        "url": url,
        "source": source,
        "title": title,
        "length": length,
        "paragraphs": len(paragraphs),
        "images": images,
        "title_type": _title_type(title),
        "opening": _opening_type(first_sentence),
        "flow": flow,
        "closing_type": _closing_type(paragraphs[-1] if paragraphs else ""),
        "has_list": has_list,
        "has_subhead": has_subhead,
        "fetched_at": now_iso(),
    }


def _fetch_article_text(url: str, cookies_path: str | Path | None, headless: bool = True) -> str:
    """1등 글 링크를 헤드리스로 열어 본문 텍스트만 뽑는다. 카페는 iframe(본문 프레임)을 본다."""
    from playwright.sync_api import sync_playwright

    from v2r.knowledge.keyword_exposure import launch_chromium

    mobile_url = _to_mobile_url(url)
    storage_state = str(cookies_path) if cookies_path and Path(cookies_path).exists() else None

    with sync_playwright() as pw:
        browser = launch_chromium(pw, headless=headless)
        try:
            context = browser.new_context(
                storage_state=storage_state,
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 10; SM-G970N) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
                ),
                viewport={"width": 420, "height": 900},
            )
            page = context.new_page()
            page.goto(mobile_url, timeout=15000, wait_until="domcontentloaded")
            page.wait_for_timeout(800)
            html = ""
            if "cafe.naver.com" in mobile_url:
                # 카페 글 본문은 iframe(#cafe_main) 안에 있다
                try:
                    frame = page.frame(name="cafe_main") or next(
                        (f for f in page.frames if "ArticleRead" in f.url or "articles" in f.url), None
                    )
                    if frame:
                        html = frame.content()
                except Exception:
                    html = ""
            if not html:
                html = page.content()
            return html
        finally:
            browser.close()


def fetch_top_article(
    keyword: str,
    *,
    cookies_path: str | Path | None = None,
    headless: bool = True,
    capture_serp: bool = True,
    crosscheck: bool = True,
    screenshot_path: str | Path | None = None,
) -> dict | None:
    """키워드의 네이버 통검 1등 일반 글 형식 지표. 실패하면 `{"error": ...}`.

    `capture_serp=True`(기본)면 화면 순서대로 상위 결과를 `serp`에, 우리가 고른
    글의 `rank_overall`(광고 포함)·`rank_organic`(광고·쇼핑 제외) 순번도 함께
    담는다. `crosscheck=True`(기본)면 requests(비로그인) 방식으로 한 번 더 1등
    글을 확인해 `crosscheck_match`·`crosscheck_url`을 남긴다(2026-09-23 지시).
    """
    from v2r.knowledge.keyword_exposure import fetch_integrated_search_dom, resolve_search_query

    try:
        query = resolve_search_query(keyword) or keyword
        dom_html = fetch_integrated_search_dom(query, cookies_path=cookies_path, headless=headless)
    except Exception as exc:
        log.warning("참고 형식: 통검 조회 실패(%s): %s", keyword, exc)
        return {"error": f"통검 조회 실패: {exc}", "fetched_at": now_iso()}

    sources = _allowed_sources()
    picked = _pick_top_link(dom_html, sources=sources)
    if not picked:
        label = "카페 글" if sources == ["cafe"] else "일반 글"
        return {"error": f"{label} 없음", "fetched_at": now_iso()}

    serp_info: dict = {}
    if capture_serp:
        # 순위 계산은 넉넉히(광고가 많은 상업 키워드는 15위 밖에 첫 일반 글이 있을
        # 수 있다) 훑고, 저장은 사용자 지시대로 상위 15개만 남긴다.
        serp_full = extract_serp(dom_html, max_n=200)
        picked_norm = _norm_serp_url(picked["url"])
        rank_overall = None
        rank_organic = None
        rank_in_source = None
        organic_i = 0
        source_i = 0
        for item in serp_full:
            if not item["is_ad"] and item["section"] != "쇼핑":
                organic_i += 1
            if item["source"] == picked["source"] and not item["is_ad"]:
                source_i += 1
            if _norm_serp_url(item["url"]) == picked_norm:
                rank_overall = item["rank"]
                rank_organic = organic_i if not item["is_ad"] else None
                rank_in_source = source_i if not item["is_ad"] else None
        serp_info = {
            "serp": serp_full[:15],
            "rank_overall": rank_overall,
            "rank_organic": rank_organic,
            "rank_in_source": rank_in_source,
        }

    crosscheck_info: dict = {}
    if crosscheck:
        cc = _requests_crosscheck(query, cookies=_cookie_dict_for(cookies_path), sources=sources)
        cc_norm = _norm_serp_url(cc.get("url") or "") if cc.get("url") else ""
        picked_norm = _norm_serp_url(picked["url"])
        match = bool(cc.get("url")) and cc_norm == picked_norm
        crosscheck_info = {
            "crosscheck_match": match,
            "crosscheck_url": cc.get("url", ""),
            "crosscheck_error": cc.get("error", ""),
        }

    if screenshot_path:
        try:
            _capture_screenshot(query, screenshot_path, cookies_path, headless=headless)
        except Exception as exc:  # pragma: no cover - 덤 기능
            log.warning("참고 형식: 스크린샷 저장 실패(%s): %s", keyword, exc)

    try:
        article_html = _fetch_article_text(picked["url"], cookies_path, headless=headless)
        text = _strip_tags(article_html)
    except Exception as exc:
        log.warning("참고 형식: 본문 조회 실패(%s, %s): %s", keyword, picked["url"], exc)
        return {
            "error": f"본문 조회 실패: {exc}",
            "fetched_at": now_iso(),
            **serp_info,
            **crosscheck_info,
        }

    if len(text) < 30:
        return {
            "error": "본문이 너무 짧음(추출 실패로 추정)",
            "fetched_at": now_iso(),
            **serp_info,
            **crosscheck_info,
        }

    title_m = re.search(r"<title[^>]*>(.*?)</title>", article_html or "", re.I | re.S)
    page_title = _strip_tags(title_m.group(1)) if title_m else ""
    title = page_title or picked.get("title") or ""

    ref = _analyze_article(text, picked["source"], picked["url"], title)
    ref.update(serp_info)
    ref.update(crosscheck_info)
    return ref


def _cookie_dict_for(cookies_path: str | Path | None) -> dict[str, str]:
    if not cookies_path:
        return {}
    from v2r.knowledge.keyword_exposure import _cookie_dict

    return _cookie_dict(cookies_path)


def _capture_screenshot(
    query: str, out_path: str | Path, cookies_path: str | Path | None, headless: bool = True
) -> None:
    """통검 결과 화면을 헤드리스로 캡처(창 없음, 사용자 PC에 아무것도 안 뜸)."""
    from urllib.parse import quote

    from playwright.sync_api import sync_playwright

    from v2r.knowledge.keyword_exposure import INTEGRATED_SEARCH_URL
    from v2r.knowledge.keyword_exposure import launch_chromium

    url = INTEGRATED_SEARCH_URL.format(query=quote(query))
    storage_state = str(cookies_path) if cookies_path and Path(cookies_path).exists() else None
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = launch_chromium(pw, headless=headless)
        try:
            context = browser.new_context(storage_state=storage_state, viewport={"width": 1440, "height": 1400})
            page = context.new_page()
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            page.wait_for_timeout(600)
            page.screenshot(path=str(out))
        finally:
            browser.close()


def format_brief(ref: dict) -> str:
    """1등 글 형식을 한국어 300~400자 이내로 요약. 문장 인용은 절대 넣지 않는다."""
    if not ref or ref.get("error"):
        return ""
    parts = [
        f"1위 글 출처: {ref.get('source', '?')}, 제목 유형: {ref.get('title_type', '?')}",
        f"본문 길이 약 {ref.get('length', 0)}자, 단락 {ref.get('paragraphs', 0)}개, 사진 {ref.get('images', 0)}장",
        f"도입 방식: {ref.get('opening', '?')}, 마무리 방식: {ref.get('closing_type', '?')}",
        f"목록/소제목 사용: {'있음' if (ref.get('has_list') or ref.get('has_subhead')) else '없음'}",
    ]
    brief = " / ".join(parts)
    brief = brief[:340]
    brief += "\n※ 형식만 참고. 문장·표현·사례는 절대 베끼지 말고 우리 브랜드 가이드 논리로 새로 쓴다."
    return brief


#: 2026-09-24 사용자 지시 — 자동 주입 대신 "차이 점수" 사전 알림으로 바꾼다.
#: 5개 사례(더 완전 가까운 키워드) 조사 결과 형식이 "크게 다른" 사례가 없어
#: 우리 형식은 유지하고, 대신 매 키워드 생성 전에 1위 글과 얼마나 다른지
#: 계산해 임계(8항목 중 4개 이상 불일치)를 넘으면 생성 전에 사용자 확인
#: 대기(hold)로 돌린다. 보고서: docs/reports/top-reference-cases-2026-09-24.md
OUR_FORMAT = {
    # 우리 브랜드 원고(질문형/후기형 두 유형만 씀 — brand_writer MTYPE_ROLE)
    "title_type": {"질문형", "후기형"},
    "opening": {"질문", "상황 서술"},
    "closing_type": {"정리", "요청"},
    # 단락 3~6개, 단락당 대략 150~500자를 정상 범위로 본다(대대댓글2 구조 기준 어림)
    "paragraphs_min": 3,
    "paragraphs_max": 6,
    "para_len_min": 100,
    "para_len_max": 600,
    "has_list": False,
    "has_subhead": False,
    "images": 0,
}

#: 8개 비교 항목 이름(보고서·알림에 그대로 쓴다)
FORMAT_GAP_ITEMS = (
    "제목 유형",
    "도입 방식",
    "단락 수",
    "단락 길이",
    "마무리 방식",
    "목록 사용",
    "소제목 사용",
    "사진 수",
)

#: 8항목 중 이 이상 불일치면 "크게 다름"으로 보고 사전 알림 + hold (사용자 지시)
GAP_ALERT_THRESHOLD = 4


def format_gap(ref: dict) -> list[str]:
    """1위 글(`ref`)이 `OUR_FORMAT`과 다른 항목 이름 목록(형식만, 소재는 안 본다)."""
    if not ref or ref.get("error"):
        return []
    gaps: list[str] = []
    if ref.get("title_type") not in OUR_FORMAT["title_type"]:
        gaps.append("제목 유형")
    if ref.get("opening") not in OUR_FORMAT["opening"]:
        gaps.append("도입 방식")
    paragraphs = int(ref.get("paragraphs") or 0)
    if not (OUR_FORMAT["paragraphs_min"] <= paragraphs <= OUR_FORMAT["paragraphs_max"]):
        gaps.append("단락 수")
    para_len = (int(ref.get("length") or 0) / paragraphs) if paragraphs else 0
    if not (OUR_FORMAT["para_len_min"] <= para_len <= OUR_FORMAT["para_len_max"]):
        gaps.append("단락 길이")
    if ref.get("closing_type") not in OUR_FORMAT["closing_type"]:
        gaps.append("마무리 방식")
    if bool(ref.get("has_list")) != OUR_FORMAT["has_list"]:
        gaps.append("목록 사용")
    if bool(ref.get("has_subhead")) != OUR_FORMAT["has_subhead"]:
        gaps.append("소제목 사용")
    if int(ref.get("images") or 0) != OUR_FORMAT["images"]:
        gaps.append("사진 수")
    return gaps


def _alerts_report_path(repo_root: str | Path) -> Path:
    from datetime import date

    return Path(repo_root) / "docs" / "reports" / f"top-reference-alerts-{date.today().isoformat()}.md"


def _append_alert(repo_root: str | Path, brand: str, keyword: str, ref: dict, gaps: list[str]) -> None:
    p = _alerts_report_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    is_new = not p.exists()
    with p.open("a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# 통검 카페 1위 형식 차이 알림 ({p.stem.rsplit('-', 1)[-1]})\n\n")
            f.write("사용자 확인 대기(hold)로 넘어간 키워드 목록. 형식만 비교(소재는 안 봄).\n\n")
            f.write("| 브랜드 | 키워드 | 불일치 항목 | 1위 글 URL |\n|---|---|---|---|\n")
        f.write(f"| {brand} | {keyword} | {', '.join(gaps)} | {ref.get('url', '')} |\n")


def evaluate_alert(rt: Any, brand: str, keyword: str) -> dict:
    """원고 생성 직전 호출. 차이 점수를 계산해 임계 이상이면 알림을 남기고 hold=True.

    `top_reference.mode`가 `"alert"`일 때만 쓴다(기본은 이전처럼 `reference_for`로
    brief를 프롬프트에 얹는 흐름). 실패해도 예외를 올리지 않고 hold=False로
    돌아가 원고 생성을 막지 않는다.
    """
    result = {"hold": False, "gaps": [], "url": ""}
    try:
        if not _volume_gate(rt, brand, keyword):
            return result
    except Exception:
        return result

    repo_root = rt.settings.repo_root
    cached = _load_cache(repo_root, keyword)
    if cached is not None:
        ref = cached
    else:
        cookies_path = Path(repo_root) / "data" / "naver_cookies.json"
        try:
            ref = fetch_top_article(keyword, cookies_path=cookies_path, headless=True) or {
                "error": "빈 결과", "fetched_at": now_iso()
            }
        except Exception as exc:
            log.warning("형식 알림: 수집 실패(%s/%s): %s", brand, keyword, exc)
            ref = {"error": str(exc), "fetched_at": now_iso()}
        try:
            _save_cache(repo_root, keyword, ref)
        except Exception as exc:
            log.warning("형식 알림: 캐시 저장 실패(%s): %s", keyword, exc)

    if ref.get("error"):
        return result
    result["url"] = ref.get("url", "")
    gaps = format_gap(ref)
    result["gaps"] = gaps
    if len(gaps) >= GAP_ALERT_THRESHOLD:
        result["hold"] = True
        try:
            _append_alert(repo_root, brand, keyword, ref, gaps)
        except Exception as exc:
            log.warning("형식 알림: 보고서 기록 실패(%s/%s): %s", brand, keyword, exc)
    return result


def _volume_gate(rt: Any, brand: str, keyword: str) -> bool:
    """검색량이 브랜드 pool 상위 top_pct% 이상이거나 min_volume 이상인지."""
    from v2r.content.brand_queue import _norm, _volume_map

    cfg = _cfg()
    top_pct = float(cfg.get("top_pct", 50) or 50)
    min_volume = float(cfg.get("min_volume", 100) or 100)

    vol_map = _volume_map(brand, Path(rt.settings.data_dir))
    entry = vol_map.get(_norm(keyword), (0.0, 0, 99.0))
    total = entry[0]
    if total >= min_volume:
        return True
    if not vol_map:
        return False
    totals = sorted((v[0] for v in vol_map.values()), reverse=True)
    if not totals:
        return False
    cut_idx = max(0, min(len(totals) - 1, int(len(totals) * (top_pct / 100.0)) - 1))
    threshold = totals[cut_idx]
    return total >= threshold and total > 0


def reference_for(rt: Any, brand: str, keyword: str, meta: dict | None = None) -> str:
    """검색량 조건을 만족하면 참고 형식 brief를, 아니면 빈 문자열을 돌려준다.

    캐시를 쓰고, 실패해도 예외를 올리지 않는다(호출 쪽에서 "" 취급). `meta`를
    주면 그 dict에 `reference_url`(1등 글 URL, 실패/미충족이면 "")을 채운다
    — 호출 쪽이 stats에 남기는 용도(2026-09-23 지시).
    """
    if meta is not None:
        meta.setdefault("reference_url", "")
    if not bool(_cfg().get("enabled", True)):
        return ""
    try:
        if not _volume_gate(rt, brand, keyword):
            return ""
    except Exception as exc:
        log.warning("참고 형식: 검색량 조건 판단 실패(%s/%s): %s", brand, keyword, exc)
        return ""

    repo_root = rt.settings.repo_root
    cached = _load_cache(repo_root, keyword)
    if cached is not None:
        ref = cached
    else:
        cookies_path = Path(repo_root) / "data" / "naver_cookies.json"
        try:
            ref = fetch_top_article(keyword, cookies_path=cookies_path, headless=True) or {
                "error": "빈 결과", "fetched_at": now_iso()
            }
        except Exception as exc:
            log.warning("참고 형식: 수집 실패(%s/%s): %s", brand, keyword, exc)
            ref = {"error": str(exc), "fetched_at": now_iso()}
        try:
            _save_cache(repo_root, keyword, ref)
        except Exception as exc:
            log.warning("참고 형식: 캐시 저장 실패(%s): %s", keyword, exc)

    if meta is not None and not ref.get("error"):
        meta["reference_url"] = ref.get("url", "")
    return format_brief(ref)


class _FakeSettings:
    def __init__(self, repo_root: str) -> None:
        self.repo_root = repo_root
        self.data_dir = str(Path(repo_root) / "data")


class _FakeRuntime:
    """CLI 점검용 — DB 없이 `data/` 아래 파일만 읽는 함수들에게 필요한 최소 rt."""

    def __init__(self, repo_root: str) -> None:
        self.settings = _FakeSettings(repo_root)


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="통검 1등 글 참고 형식 점검용 CLI")
    parser.add_argument("keyword")
    parser.add_argument("--brand", default="")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()

    if args.brand:
        rt = _FakeRuntime(args.repo_root)
        brief = reference_for(rt, args.brand, args.keyword)
        print(brief or "(검색량 조건 미충족이거나 참고 형식을 만들지 못했습니다)")
    else:
        cookies_path = Path(args.repo_root) / "data" / "naver_cookies.json"
        ref = fetch_top_article(args.keyword, cookies_path=cookies_path, headless=True)
        print(json.dumps(ref, ensure_ascii=False, indent=2))
        print()
        print(format_brief(ref))


if __name__ == "__main__":
    _main()
