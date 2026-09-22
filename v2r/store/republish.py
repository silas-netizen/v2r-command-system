"""재발행 대기 줄 (`republish_queue`).

왜 필요한가
-----------
네이버가 "ID/IP당 게시글 등록 제한을 초과해 신규 게시글 등록이 잠시 제한됩니다"라고
막으면 **글이 아예 올라가지 않는다**. 그런데 V2R 글 목록에는 그 글이 "준비" 상태로
남아 있어서, 점검(reconcile)이 목록에서 글 번호를 찾았다는 이유로 완료로 확정해
버리는 일이 있었다 (2026-09-21 peecics 4건).

이제 그런 건은 `publications`에서 `failed`(사유 `제한`)로 남기고, **같은 시트 행**을
다른 계정으로 다시 올리도록 이 줄에 넣어 둔다. 실제 재발행은 사람이나 다음 작업이
`list_pending()`을 보고 결정한다 — 이 모듈은 **기억만** 한다.
"""

from __future__ import annotations

import sqlite3

from v2r.store.db import now_iso

#: 아직 다시 올리지 않은 상태
PENDING = "pending"
#: 다시 올렸거나 사람이 취소한 상태
DONE = "done"
CANCELED = "canceled"


class RepublishQueue:
    """`republish_queue` 표 조작."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(
        self,
        source_key: str,
        row_number: int,
        content_hash: str,
        *,
        reason: str = "",
        cafe: str = "",
        board: str = "",
        account: str = "",
        source_id: str = "",
    ) -> bool:
        """대기 줄에 넣는다(같은 행이 이미 있으면 사유만 갱신). 새로 넣었으면 True."""
        ts = now_iso()
        before = self.conn.execute(
            "SELECT 1 FROM republish_queue WHERE source_key = ? AND row_number = ?"
            " AND content_hash = ?",
            (source_key, int(row_number), content_hash),
        ).fetchone()
        self.conn.execute(
            "INSERT INTO republish_queue (source_key, row_number, content_hash, reason,"
            " cafe, board, account, source_id, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source_key, row_number, content_hash) DO UPDATE SET"
            "  reason = excluded.reason,"
            "  cafe = COALESCE(NULLIF(excluded.cafe, ''), republish_queue.cafe),"
            "  board = COALESCE(NULLIF(excluded.board, ''), republish_queue.board),"
            "  account = COALESCE(NULLIF(excluded.account, ''), republish_queue.account),"
            "  source_id = COALESCE(NULLIF(excluded.source_id, ''), republish_queue.source_id),"
            "  status = ?,"
            "  updated_at = excluded.updated_at",
            (
                source_key,
                int(row_number),
                content_hash,
                str(reason or ""),
                str(cafe or ""),
                str(board or ""),
                str(account or ""),
                str(source_id or ""),
                PENDING,
                ts,
                ts,
                PENDING,
            ),
        )
        return before is None

    def list_pending(self) -> list[dict]:
        """아직 다시 올리지 않은 행 목록(오래된 것부터)."""
        rows = self.conn.execute(
            "SELECT * FROM republish_queue WHERE status = ? ORDER BY created_at, rowid",
            (PENDING,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_pending(self) -> int:
        """대기 건수."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM republish_queue WHERE status = ?", (PENDING,)
        ).fetchone()
        return int(row["n"])

    def resolve(
        self, source_key: str, row_number: int, content_hash: str, status: str = DONE
    ) -> None:
        """다시 올렸거나(또는 취소했으므로) 대기 줄에서 내린다."""
        self.conn.execute(
            "UPDATE republish_queue SET status = ?, updated_at = ?"
            " WHERE source_key = ? AND row_number = ? AND content_hash = ?",
            (status, now_iso(), source_key, int(row_number), content_hash),
        )


__all__ = ["RepublishQueue", "PENDING", "DONE", "CANCELED"]
