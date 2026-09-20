"""원고 중복 검사. legacy §2 기준 — 행 단위 skip, 배치 중단 없음.

두 겹이다.

1. **로컬 발행 기록**(`publications`) — 이 시스템이 올린 글.
2. **V2R 글 목록 색인**(`article_index`) — 서버에 이미 있는 글 전부
   (이 시스템이 생기기 전 메이크/수동 글 포함). `is_duplicate_against_index()`가 본다.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

from v2r.content.manuscript import Manuscript

_KEEP = re.compile(r"[^0-9a-z가-힣]")


def normalize_text(text: str) -> str:
    """NFKC + casefold + 한글/영숫자만 남기기."""
    norm = unicodedata.normalize("NFKC", text or "").casefold()
    return _KEEP.sub("", norm)


def _bigrams(text: str) -> set[str]:
    """2-gram 집합."""
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def dice_score(a: str, b: str) -> float:
    """bigram Dice 계수(0.0~1.0)."""
    na, nb = normalize_text(a), normalize_text(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ga, gb = _bigrams(na), _bigrams(nb)
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def compare(a: Manuscript, b: Manuscript) -> tuple[bool, float]:
    """두 원고 비교 → (완전일치 여부, 유사도)."""
    text_a = f"{a.title}\n{a.body}"
    text_b = f"{b.title}\n{b.body}"
    exact = normalize_text(text_a) == normalize_text(text_b)
    return exact, 1.0 if exact else dice_score(text_a, text_b)


def check_against_history(
    m: Manuscript, history: list[Manuscript], threshold: float = 0.92
) -> str | None:
    """이력과 비교해 "exact" / "similar" / None 반환."""
    best: str | None = None
    for old in history:
        exact, score = compare(m, old)
        if exact:
            return "exact"
        if score >= threshold:
            best = "similar"
    return best


# --------------------------------------------------------------------
# V2R 글 목록 색인 대조 (사용자 절대 규칙)
# --------------------------------------------------------------------
#: 제목이 "사실상 같다"고 볼 유사도 (difflib 비율)
TITLE_NEAR_THRESHOLD = 0.92


def _index_cafe_names(rt: Any, cafe: str) -> list[str]:
    """색인에서 이 카페를 가리키는 이름들 (원고 표기와 설정 표기가 다를 수 있다)."""
    names: list[str] = []
    for name in [cafe] + list(_self_cafe_names(rt)):
        if not name:
            continue
        from v2r.engine.publish import cafe_matches

        if name == cafe or cafe_matches(cafe, name):
            if name not in names:
                names.append(name)
    return names


def _self_cafe_names(rt: Any) -> list[str]:
    """자사 카페 이름 전부(제외 카페도 — V2R에는 그 글도 존재한다)."""
    try:
        from v2r.engine.publish import self_cafe_names

        return self_cafe_names(rt, include_excluded=True)
    except Exception:
        return []


def is_duplicate_against_index(
    rt: Any, manuscript: Manuscript, cafe: str = ""
) -> tuple[bool, str]:
    """V2R 글 목록 색인과 대조 → `(중복인가, 까닭)`.

    세 가지를 본다 (self-cafe-daily-rules §3).

    1. **같은 카페에 같은 제목** (`title_norm` 완전일치)
    2. **같은 본문 해시**가 자사 카페 어디에든 있음 (카페를 옮겨도 같은 글은 같은 글)
    3. **같은 카페에 거의 같은 제목** (difflib 비율 ≥ 0.92)

    색인이 비어 있으면(아직 동기화 전) 아무것도 막지 않는다.
    """
    index = getattr(rt, "article_index", None)
    if index is None or manuscript is None:
        return False, ""
    title = str(getattr(manuscript, "title", "") or "")
    target = str(cafe or getattr(manuscript, "cafe", "") or "")

    from v2r.store.article_index import normalize_title

    norm = normalize_title(title)
    cafe_names = _index_cafe_names(rt, target) if target else []

    for name in cafe_names:
        hit = index.title_match(name, norm)
        if hit:
            return True, f"제목 동일({name}): {hit.get('title') or title}"

    body_hash = str(getattr(manuscript, "content_hash", "") or "")
    if body_hash:
        allowed = set(_self_cafe_names(rt)) | ({target} if target else set())
        hit = index.hash_match(body_hash, allowed or None)
        if hit:
            return True, f"본문 동일({hit.get('cafe') or '자사 카페'}): {hit.get('title') or title}"

    if norm:
        for name in cafe_names:
            for old_title, old_norm in index.titles_for_cafe(name):
                if not old_norm:
                    continue
                ratio = difflib.SequenceMatcher(None, norm, old_norm).ratio()
                if ratio >= TITLE_NEAR_THRESHOLD:
                    return True, f"제목 유사({name}, {ratio:.2f}): {old_title}"
    return False, ""


def index_skip_reason(reason: str) -> str:
    """선택 단계 skip 사유 문구."""
    return f"V2R 기존 글과 중복: {reason}"
