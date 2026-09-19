"""계정 자격 규칙. legacy §3, DESIGN §6 기준."""

from __future__ import annotations

import re
from collections.abc import Iterable

from v2r.accounts.loader import (
    AFFILIATE_WORK_TYPE,
    LINKED_V2R,
    SELF_WORK_TYPE,
    Account,
)

_MANAGER = {"m", "매니저", "manager"}
_NAME_STRIP = re.compile(r"[^0-9a-z가-힣]")


class CafeMatchError(RuntimeError):
    """카페 이름이 여러 건 일치하거나 없음."""


def normalize_name(name: str) -> str:
    """이름 정규화: 한글/영숫자만 + casefold."""
    return _NAME_STRIP.sub("", (name or "").casefold())


def affiliate_names(cafes_cfg: dict) -> list[dict]:
    """제휴 카페 설정 목록."""
    return list((cafes_cfg or {}).get("affiliate") or [])


def find_affiliate(cafe: str, cafes_cfg: dict) -> dict | None:
    """카페 이름/별칭으로 제휴 카페 설정 찾기. 여러 건이면 에러."""
    key = normalize_name(cafe)
    if not key:
        return None
    hits = []
    for entry in affiliate_names(cafes_cfg):
        names = [entry.get("name", "")] + list(entry.get("aliases") or [])
        if any(normalize_name(n) == key for n in names):
            hits.append(entry)
    if len(hits) > 1:
        raise CafeMatchError(f"카페 이름이 여러 건 일치: {cafe}")
    return hits[0] if hits else None


def is_excluded(a: Account) -> bool:
    """회색 음영·제외 표시 계정인가."""
    return bool(a.excluded) or a.shade.strip() == "회색"


def is_manager(a: Account) -> bool:
    """매니저 등급인가."""
    return (a.grade or "").strip().casefold() in _MANAGER


_STAFF_WORDS = ("스탭", "스텝", "스태프", "staff", "매니저", "운영진")


def is_staff_level(level_name: str | None) -> bool:
    """V2R 카페 회원 조회의 등급 이름이 스텝(운영진)인지. 자사 카페 계정은 스텝 등급만 쓴다(사용자 결정 2026-09-19)."""
    text = (level_name or "").strip().casefold()
    return any(w.casefold() in text for w in _STAFF_WORDS)


def work_type_for(task: str, cafe: str, cafes_cfg: dict) -> str:
    """작업·카페 → 필요한 work_type."""
    if find_affiliate(cafe, cafes_cfg) is not None:
        return AFFILIATE_WORK_TYPE
    if task == "publish_brand":
        return AFFILIATE_WORK_TYPE
    if task == "publish_info":
        return SELF_WORK_TYPE
    return SELF_WORK_TYPE


def eligible(
    accounts: Iterable[Account],
    work_type: str,
    comment_accounts: set[str] | None = None,
    restricted: set[str] | None = None,
) -> list[Account]:
    """자격 있는 계정만 추린다."""
    comment_accounts = {c.casefold() for c in (comment_accounts or set())}
    restricted = {c.casefold() for c in (restricted or set())}
    out: list[Account] = []
    for a in accounts:
        if is_excluded(a) or is_manager(a):
            continue
        if (a.linked or "").strip().upper() != LINKED_V2R:
            continue
        if (a.work_type or "").strip() != work_type:
            continue
        lid = a.login_id.casefold()
        if lid in comment_accounts or lid in restricted:
            continue
        out.append(a)
    return out
