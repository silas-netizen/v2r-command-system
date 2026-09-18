"""보고·감사 로그."""

from __future__ import annotations

import sqlite3

from v2r.store.db import now_iso


class EventLog:
    """events 테이블 조작."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def log(self, job_id: int | None, level: str, message: str) -> int:
        """이벤트 1건 기록."""
        cur = self.conn.execute(
            "INSERT INTO events (job_id, level, message, created_at) VALUES (?, ?, ?, ?)",
            (job_id, level, message, now_iso()),
        )
        return int(cur.lastrowid)

    def recent(self, job_id: int | None = None, limit: int = 50) -> list[dict]:
        """최근 이벤트."""
        if job_id is None:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE job_id = ? ORDER BY id DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]
