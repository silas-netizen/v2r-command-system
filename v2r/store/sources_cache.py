"""원본(시트) 캐시 저장소."""

from __future__ import annotations

import json
import sqlite3

from v2r.store.db import now_iso


class SourceCache:
    """source_cache 테이블 조작."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def put(self, key: str, payload: dict, status: str = "ok") -> None:
        """캐시 저장(덮어쓰기)."""
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO source_cache (key, payload_json, synced_at, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET payload_json = excluded.payload_json,"
            " synced_at = excluded.synced_at, status = excluded.status,"
            " updated_at = excluded.updated_at",
            (key, json.dumps(payload, ensure_ascii=False), ts, status, ts, ts),
        )

    def get(self, key: str) -> dict | None:
        """캐시 본문."""
        row = self.conn.execute(
            "SELECT payload_json FROM source_cache WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def meta(self, key: str) -> dict | None:
        """동기화 시각·상태."""
        row = self.conn.execute(
            "SELECT key, synced_at, status, updated_at FROM source_cache WHERE key = ?",
            (key,),
        ).fetchone()
        return dict(row) if row else None
