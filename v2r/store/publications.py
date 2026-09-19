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


def _same_cafe(query: str, name: str) -> bool:
    """카페 이름 두 개가 같은 카페인가 (`publish.cafe_matches`와 같은 규칙)."""
    from v2r.api.catalog import korean_only, normalize_name

    q, n = normalize_name(query), normalize_name(name)
    if not q or not n:
        return False
    if q == n:
        return True
    kq, kn = korean_only(query), korean_only(name)
    if kq and kq == kn:
        return True
    return q in n or n in q


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

    def exists_hash(self, content_hash: str) -> bool:
        """본문 해시 전역 검사 — 다른 파일·다른 행이라도 같은 본문이면 이미 발행된 것.

        각색 엑셀끼리 같은 글이 겹쳐 두 번 올라가는 것을 막는다
        (docs/reference/self-cafe-daily-rules.md §3).
        """
        if not content_hash:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM publications WHERE content_hash = ? AND status IN (?, ?)",
            (content_hash, *BLOCKING_STATUSES),
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
        """상태 upsert. 지정하지 않은 열은 유지.

        `done` → `uncertain` 역행은 막는다(확정된 성공을 미확정으로 되돌리지 않음).
        """
        unknown = set(fields) - set(_FIELDS)
        if unknown:
            raise ValueError(f"알 수 없는 열: {sorted(unknown)}")
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO publications (source_key, row_number, content_hash, status, stage,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source_key, row_number, content_hash) DO UPDATE SET"
            " status = CASE WHEN publications.status = 'done' AND excluded.status = 'uncertain'"
            "   THEN publications.status ELSE excluded.status END,"
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

    def count_today(
        self,
        cafe: str,
        kst_date: str,
        *,
        exclude_sources: "set[str] | None" = None,
    ) -> int:
        """오늘(KST `kst_date`) 그 카페에 이미 올라간 글 수 (uncertain/done).

        `exclude_sources`에 든 `source_key`(브랜드 원고 시트 등)는 세지 않는다.
        일상 글만 세기 위한 장치다 (self-cafe-daily-rules §7).
        `created_at`은 `now_iso()`가 남긴 KST ISO 문자열이라 앞 10글자가 날짜다.
        """
        if not cafe or not kst_date:
            return 0
        rows = self.conn.execute(
            "SELECT cafe, source_key FROM publications"
            " WHERE status IN (?, ?) AND substr(created_at, 1, 10) = ?",
            (*BLOCKING_STATUSES, kst_date),
        ).fetchall()
        skip = {str(s) for s in (exclude_sources or set())}
        n = 0
        for r in rows:
            if str(r["source_key"] or "") in skip:
                continue
            if _same_cafe(cafe, str(r["cafe"] or "")):
                n += 1
        return n

    def by_source_id(self, source_id: str) -> dict | None:
        """V2R source_id로 조회."""
        row = self.conn.execute(
            "SELECT * FROM publications WHERE source_id = ?", (source_id,)
        ).fetchone()
        return dict(row) if row else None
