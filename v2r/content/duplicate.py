"""원고 중복 검사. legacy §2 기준 — 행 단위 skip, 배치 중단 없음."""

from __future__ import annotations

import re
import unicodedata

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
