"""계정 상태 저장소 (LRU·제한)."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from v2r.store.db import KST, now_iso


def _aware(dt: datetime) -> datetime:
    """naive datetime은 KST로 간주한다(프로젝트 전역 기준)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=KST)


def _iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return _aware(value).isoformat(timespec="seconds")
    return value


def _parse(value: str) -> datetime:
    """저장된 ISO 문자열 → aware datetime (naive면 KST)."""
    return _aware(datetime.fromisoformat(value))


class AccountStateStore:
    """account_state 테이블 조작."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _ensure(self, login_id: str) -> None:
        ts = now_iso()
        self.conn.execute(
            "INSERT OR IGNORE INTO account_state (login_id, fail_count, created_at, updated_at)"
            " VALUES (?, 0, ?, ?)",
            (login_id, ts, ts),
        )

    def touch_used(self, login_id: str, when: datetime | str | None = None) -> None:
        """마지막 사용 시각 기록."""
        self._ensure(login_id)
        ts = now_iso()
        self.conn.execute(
            "UPDATE account_state SET last_used_at = ?, updated_at = ? WHERE login_id = ?",
            (_iso(when) if when else ts, ts, login_id),
        )

    def restrict(
        self,
        login_id: str,
        until: datetime | str,
        code: str = "",
        note: str = "",
    ) -> None:
        """계정 제한 기록 (예: 코드 27000)."""
        self._ensure(login_id)
        ts = now_iso()
        self.conn.execute(
            "UPDATE account_state SET restricted_until = ?, restrict_code = ?, note = ?,"
            " fail_count = fail_count + 1, updated_at = ? WHERE login_id = ?",
            (_iso(until), code, note, ts, login_id),
        )

    def is_restricted(self, login_id: str, now: datetime | str | None = None) -> bool:
        """지금 제한 중인지."""
        row = self.conn.execute(
            "SELECT restricted_until FROM account_state WHERE login_id = ?", (login_id,)
        ).fetchone()
        if row is None or not row["restricted_until"]:
            return False
        ref = _iso(now) if now else now_iso()
        try:
            return _parse(row["restricted_until"]) > _parse(ref)
        except (ValueError, TypeError):
            # 해석할 수 없으면 보수적으로 "제한 중"으로 본다
            return True

    def last_used_map(self) -> dict[str, str | None]:
        """계정별 마지막 사용 시각."""
        rows = self.conn.execute(
            "SELECT login_id, last_used_at FROM account_state"
        ).fetchall()
        return {r["login_id"]: r["last_used_at"] for r in rows}
