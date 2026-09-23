"""네이버 통합검색(통검) 결과 링크 추출 — 공용 모듈.

`v2r/content/top_reference.py`가 먼저 만든 `extract_serp`(광고·내비·카페 이름
배지·쇼핑·뉴스를 구분하고, 카페·블로그·포스트·지식iN은 "글 번호가 있는
진짜 글"만 세는 엄격한 패턴)을 여기로 옮겨 `keyword_exposure.py`도 같이
쓴다(2026-09-23 지시 — 노출 판정 `rank`가 광고·내비 링크까지 다 세는
오류를 고치려고 top_reference의 이미 검증된 추출기를 재사용).

`top_reference.py`는 하위 호환을 위해 이 모듈에서 다시 import한다.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

#: 광고/파워링크/쇼핑/뉴스 등 "일반 글"이 아닌 것으로 보는 URL/텍스트 표시
EXCLUDE_HOST_HINTS = ("shopping.naver.com", "ad.naver.com", "adcr.naver.com", "news.naver.com")
EXCLUDE_TEXT_HINTS = ("파워링크", "광고", "네이버쇼핑", "브랜드검색")

#: 일반 글로 인정하는 링크 패턴 — 카페 > 블로그 > 포스트 > 지식iN 순으로 우선한다.
#: 실제 글(숫자 게시글 ID)만 잡는다 — "내 블로그" 같은 내비게이션/메뉴 링크는
#: 게시글 ID가 없어 여기서 자연히 걸러진다(2026-09-23 실측에서 확인한 오탐).
RESULT_LINK_PATTERNS = [
    ("cafe", re.compile(r"https?://(?:m\.)?cafe\.naver\.com/(?:ca-fe/cafes/\d+/articles/\d+|[^/\s\"'<>]+/\d+)", re.I)),
    ("blog", re.compile(r"https?://(?:m\.)?blog\.naver\.com/(?:PostView\.naver\?[^\s\"'<>]*|[^/\s\"'<>]+/\d{6,})", re.I)),
    ("post", re.compile(r"https?://(?:m\.)?post\.naver\.com/viewer/postView\.naver\?[^\s\"'<>]+", re.I)),
    ("jisik", re.compile(r"https?://(?:m\.)?kin\.naver\.com/(?:qna/detail\.naver\?[^\s\"'<>]+|[^/\s\"'<>]+/\d+)", re.I)),
]
#: 내비게이션/메뉴류로 걸러낼 URL 조각
EXCLUDE_URL_HINTS = ("MyBlog.naver", "BlogHome.naver", "gnb_", "/PostList.naver", "/ns/home")

#: 카페/블로그/포스트/지식iN 섹션 이름 -> (strict 패턴, source 키). 각 결과 블록에는
#: "카페 이름 배지" 링크(`cafe.naver.com/<별칭>`, 글 번호 없음)가 실제 글 링크
#: 바로 앞에 따로 박혀 있다 — 느슨한 도메인 매칭 대신 글 번호가 있는
#: `RESULT_LINK_PATTERNS`(진짜 글만 잡는 패턴)를 그대로 써 이 오탐을 없앤다.
STRICT_SECTIONS = [
    ("카페", "cafe"),
    ("블로그", "blog"),
    ("포스트", "post"),
    ("지식iN", "jisik"),
]
STRICT_PATTERN_BY_SOURCE = dict(RESULT_LINK_PATTERNS)

#: 광고/쇼핑/뉴스는 "진짜 글 번호" 개념이 없어 도메인 기반으로 그대로 훑는다.
LOOSE_SECTIONS = [
    (re.compile(r"https?://(?:ader|adcr)\.naver\.com/[^\s\"'<>]+", re.I), "파워링크/광고", "ad", True),
    (re.compile(r"https?://shopping\.naver\.com/[^\s\"'<>]+", re.I), "쇼핑", "shopping", False),
    (re.compile(r"https?://(?:m\.)?news\.naver\.com/[^\s\"'<>]+", re.I), "뉴스", "news", False),
]

_RE_TAG = re.compile(r"<[^>]+>")
_RE_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)


def norm_serp_url(url: str) -> str:
    u = re.sub(r"^https?://", "", url, flags=re.I)
    u = re.sub(r"^m\.", "", u, flags=re.I)
    return u.split("?", 1)[0].rstrip("/").casefold()


def strip_tags(html: str) -> str:
    html = _RE_SCRIPT.sub(" ", html or "")
    text = _RE_TAG.sub(" ", html)
    text = re.sub(r"&nbsp;|&amp;|&lt;|&gt;|&quot;|&#39;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def extract_serp(dom_html: str, max_n: int = 15) -> list[dict]:
    """통검 최종 DOM에서 화면 순서대로 결과 링크를 뽑는다(상위 `max_n`개, 중복 제거).

    반환: `[{rank, section, source, title, url, is_ad}]`. 카페·블로그·포스트·
    지식iN은 **진짜 글(글 번호가 있는 URL)만** 센다 — 카페 이름 배지·내비게이션·
    도움말·로그인·`ader.naver.com` 광고 캐러셀 같은 비-글 링크는 여기서 아예
    후보에 들지 않는다(광고는 `is_ad=True`로 따로 센다).
    """
    if not dom_html:
        return []
    matches: list[tuple[int, str, str, str, bool]] = []  # (start, url, section, source, is_ad)
    for section, source in STRICT_SECTIONS:
        pattern = STRICT_PATTERN_BY_SOURCE[source]
        for m in pattern.finditer(dom_html):
            url = m.group(0)
            if any(h in url for h in EXCLUDE_HOST_HINTS) or any(h in url for h in EXCLUDE_URL_HINTS):
                continue
            matches.append((m.start(), url, section, source, False))
    for pattern, section, source, is_ad in LOOSE_SECTIONS:
        for m in pattern.finditer(dom_html):
            url = m.group(0)
            if any(h in url for h in EXCLUDE_URL_HINTS):
                continue
            path = urlparse(url).path
            if not is_ad and section != "쇼핑" and path in ("", "/"):
                continue  # 상단 메뉴 바로가기(실제 결과가 아니다)
            matches.append((m.start(), url, section, source, is_ad))
    matches.sort(key=lambda t: t[0])

    seen: set[str] = set()
    out: list[dict] = []
    for start, url, section, source, is_ad in matches:
        norm = norm_serp_url(url)
        if is_ad:
            # 파워링크 광고 한 칸에 썸네일/제목/설명 등 여러 클릭 영역이 있어 URL이
            # 조금씩 다르다 — 리다이렉트 토큰 앞 16자로 "같은 광고 칸"인지 본다.
            token = re.search(r"/v1/([^?&\s\"']{16})", url)
            norm = f"ad:{token.group(1)}" if token else norm
        if norm in seen:
            continue
        seen.add(norm)
        window_start = max(0, start - 40)
        window = dom_html[window_start:window_start + 400]
        title = strip_tags(window)[:60].strip()
        out.append(
            {
                "rank": len(out) + 1,
                "section": section,
                "source": source,
                "title": title,
                "url": url,
                "is_ad": is_ad,
            }
        )
        if len(out) >= max_n:
            break
    return out


# ---------------------------------------------------------------------------
# 2026-09-23 사용자 정정 — 카페 카드 안 "대표 글" vs "서브 링크"
#
# 실측(헤드리스로 통검 DOM을 열어 `베르베린` 카드를 직접 뜯어봄,
# `data/exposure_audit/팥순이/_beoberin_raw.html`): 카페 결과 한 "카드"는
# 1) 카페 이름 배지 — `<a ... data-heatmap-target="articleSourceJSX_title" ...>`
#    (글 번호 없는 카페 홈 링크, 카드 시작을 알아보는 마커로 쓴다)
# 2) 대표 글(카드 헤드라인) — 카페 이름 배지 바로 다음에 나오는 **같은 글 URL**이
#    썸네일·제목(`fds-ugc-ellipsis3`)·날짜 링크로 3~4번 반복된다.
# 3) 댓글 미리보기 캐러셀 — `class="... fds-reply-box"`, `data-heatmap-target=
#    "reviewbox_reply"`. 전부 **대표 글과 같은 URL**이다(댓글이 그 글에 달린
#    것뿐이라 대표 글과 같은 article id).
# 4) 관련 글(시리즈) — 댓글 캐러셀 다음, `<span class="... VertDivider">` 구분선
#    뒤에 `data-heatmap-target=".series"`인 링크. **다른 article id**를 가리킨다
#    (같은 카페의 다른 글). 사용자 지시: 우리 글이 여기(4번)로만 보이면 밀려남이다.
#
# 즉 "대표 글"과 "서브 글"을 가르는 실측 기준은 article id가 같냐 다르냐다 —
# 카드 안에서 **처음 나오는 article id**가 대표, 그 뒤에 나오는 **다른** article
# id는 전부 서브(댓글 미리보기든 관련 글이든, 어느 쪽이든 대표가 아니다).
# ---------------------------------------------------------------------------

#: 카페 카드 시작 마커(카페 이름 배지)
_RE_CARD_START = re.compile(r'data-heatmap-target="articleSourceJSX_title"')
#: 카드 안에서 카페 "글"(글 번호 있는) href만 순서대로
_RE_CARD_CAFE_HREF = re.compile(
    r'href="(https?://(?:m\.)?cafe\.naver\.com/(?:ca-fe/cafes/\d+/articles/\d+|[^/\s"\'<>]+/\d+)[^"\']*)"',
    re.I,
)


def extract_cafe_cards(dom_html: str) -> list[dict]:
    """통검 최종 DOM에서 카페 "카드" 단위로 대표 글/서브 링크를 나눈다.

    반환: `[{representative_url, representative_title, sub_urls: [...]}, ...]`
    (화면에 나온 카드 순서대로). 카드 마커(`articleSourceJSX_title`)가 없는
    화면(옛 마크업·모바일 등)이면 빈 목록 — 호출 쪽은 그러면 카드 구분 없이
    기존 방식(모든 글 링크가 대표)으로 넘어간다.
    """
    if not dom_html:
        return []
    starts = [m.start() for m in _RE_CARD_START.finditer(dom_html)]
    if not starts:
        return []
    out: list[dict] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else min(len(dom_html), start + 20000)
        window = dom_html[start:end]
        seen_ids: list[str] = []
        rep_url = ""
        rep_title = ""
        sub_urls: list[str] = []
        for m in _RE_CARD_CAFE_HREF.finditer(window):
            url = m.group(1)
            aid_m = re.search(r"cafe\.naver\.com/(?:ca-fe/cafes/\d+/articles|[^/]+)/(\d+)", url, re.I)
            aid = aid_m.group(1) if aid_m else norm_serp_url(url)
            if aid in seen_ids:
                continue
            seen_ids.append(aid)
            if not rep_url:
                rep_url = url
                title_window = window[m.end():m.end() + 400]
                rep_title = strip_tags(title_window)[:80].strip()
            else:
                sub_urls.append(url)
        if rep_url:
            out.append({"representative_url": rep_url, "representative_title": rep_title, "sub_urls": sub_urls})
    return out


def general_result_rank(dom_html: str, article_url: str, max_n: int = 200) -> int | None:
    """`article_url`이 **광고·쇼핑·뉴스·내비 제외** 일반 결과(카페/블로그/포스트/
    지식iN) 중 몇 번째인지(1부터). 없으면 `None`. `judge_keyword_exposure`의
    `rank`(광고 포함 전체 링크 순번, `rank_overall`)와 구분해 쓴다.
    """
    if not article_url:
        return None
    target = norm_serp_url(article_url)
    organic_rank = 0
    for item in extract_serp(dom_html, max_n=max_n):
        if item["is_ad"]:
            continue
        organic_rank += 1
        if norm_serp_url(item["url"]) == target:
            return organic_rank
    return None


__all__ = [
    "EXCLUDE_HOST_HINTS",
    "EXCLUDE_TEXT_HINTS",
    "RESULT_LINK_PATTERNS",
    "EXCLUDE_URL_HINTS",
    "STRICT_SECTIONS",
    "STRICT_PATTERN_BY_SOURCE",
    "LOOSE_SECTIONS",
    "norm_serp_url",
    "strip_tags",
    "extract_serp",
    "general_result_rank",
]
