"""발행 기록 저장소 (중복 발행 방지)."""

from __future__ import annotations

import sqlite3

from v2r.store.db import now_iso

# 이미 발행된 것으로 간주하는 상태
BLOCKING_STATUSES = ("uncertain", "done")

_FIELDS = (
    "source_id",
    "url",
    "account",
    "cafe",
    "menu_id",
    "scheduled_at",
)


class PublicationStore:
    """publications 테이블 조작."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def exists(self, source_key: str, row_number: int, content_hash: str) -> bool:
        """uncertain/done 이면 이미 발행된 것으로 본다."""
        row = self.conn.execute(
            "SELECT 1 FROM publications WHERE source_key = ? AND row_number = ?"
            " AND content_hash = ? AND status IN (?, ?)",
            (source_key, row_number, content_hash, *BLOCKING_STATUSES),
        ).fetchone()
        return row is not None

    def mark(
        self,
        source_key: str,
        row_number: int,
        content_hash: str,
        status: str,
        stage: str | None = None,
        **fields: object,
    ) -> None:
        """상태 upsert. 지정하지 않은 열은 유지."""
        unknown = set(fields) - set(_FIELDS)
        if unknown:
            raise ValueError(f"알 수 없는 열: {sorted(unknown)}")
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO publications (source_key, row_number, content_hash, status, stage,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source_key, row_number, content_hash) DO UPDATE SET"
            " status = excluded.status,"
            " stage = COALESCE(excluded.stage, publications.stage),"
            " updated_at = excluded.updated_at",
            (source_key, row_number, content_hash, status, stage, ts, ts),
        )
        for name, value in fields.items():
            if value is None:
                continue
            self.conn.execute(
                f"UPDATE publications SET {name} = ?, updated_at = ?"
                " WHERE source_key = ? AND row_number = ? AND content_hash = ?",
                (value, ts, source_key, row_number, content_hash),
            )

    def list_uncertain(self) -> list[dict]:
        """미확정 건 목록."""
        rows = self.conn.execute(
            "SELECT * FROM publications WHERE status = 'uncertain' ORDER BY updated_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def count_done(self, source_key: str) -> int:
        """원본별 완료 건수."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM publications WHERE source_key = ? AND status = 'done'",
            (source_key,),
        ).fetchone()
        return int(row["n"])

    def by_source_id(self, source_id: str) -> dict | None:
        """V2R source_id로 조회."""
        row = self.conn.execute(
            "SELECT * FROM publications WHERE source_id = ?", (source_id,)
        ).fetchone()
        return dict(row) if row else None
