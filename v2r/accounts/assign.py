"""계정 배정. 부족하면 채우지 않고 에러. legacy §3, DESIGN §6(D2)."""

from __future__ import annotations

from collections.abc import Iterable

from v2r.accounts.loader import Account


class AssignError(RuntimeError):
    """배정 실패(계정 부족·미존재)."""


def _ids(pool: Iterable[Account | str]) -> list[str]:
    return [a if isinstance(a, str) else a.login_id for a in pool]


def assign(
    pool: Iterable[Account | str],
    *,
    mode: str = "auto",
    count: int = 0,
    explicit: Iterable[str] | None = None,
    last_used: dict[str, str | None] | None = None,
) -> list[str]:
    """계정 배정. manual=지정 계정 검증, auto=LRU 선택."""
    ids = _ids(pool)
    last_used = last_used or {}

    if mode == "manual":
        wanted = list(explicit or [])
        if not wanted:
            raise AssignError("지정 계정이 없습니다")
        available = {i.casefold(): i for i in ids}
        missing = [w for w in wanted if w.casefold() not in available]
        if missing:
            raise AssignError("풀에 없는 지정 계정: " + ", ".join(missing))
        return [available[w.casefold()] for w in wanted]

    if mode != "auto":
        raise AssignError(f"알 수 없는 배정 모드: {mode}")

    if count <= 0:
        raise AssignError("배정 개수는 1 이상이어야 합니다")
    if len(ids) < count:
        raise AssignError(f"계정 부족: 필요 {count}, 가능 {len(ids)} (채우지 않음)")

    def sort_key(idx_login: tuple[int, str]) -> tuple[int, str, int]:
        idx, login = idx_login
        used = last_used.get(login)
        return (0, "", idx) if used in (None, "") else (1, str(used), idx)

    ordered = sorted(enumerate(ids), key=sort_key)
    return [login for _, login in ordered[:count]]


def rotate(selected: list[str], index: int) -> str:
    """라운드로빈 회전 선택."""
    if not selected:
        raise AssignError("선택된 계정이 없습니다")
    return selected[index % len(selected)]
