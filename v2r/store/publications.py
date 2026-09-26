"""발행 기록 저장소 (중복 발행 방지)."""

from __future__ import annotations

import sqlite3
import time

from v2r.store.db import now_iso

#: 등록 제한(네이버 "ID/IP당 게시글 등록 제한")으로 못 올라간 건의 `stage` 표시
LIMIT_STAGE = "제한"

#: `database is locked`/`busy` 오류에서 다시 시도할 최대 횟수
#: (사고 2026-09-26: 노출 러너 6개·키워드 워커 15개가 같은 v2r.sqlite를 물고 있는
#: 동안 publish_daily가 한 번의 잠금으로 190건에서 통째로 failed 됐다.)
MAX_LOCK_RETRIES = 5
#: 재시도 사이 대기(초, 지수 백오프): 0.2 → 0.4 → 0.8 → 1.6 → 3.2
LOCK_RETRY_BASE_S = 0.2


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    """`database is locked` / `database is busy` 오류인가."""
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def retry_on_lock(fn, *, max_retries: int = MAX_LOCK_RETRIES, base_delay: float = LOCK_RETRY_BASE_S):
    """`fn()`을 부르되, DB 잠금 오류면 지수 백오프로 다시 시도한다.

    잠금이 아닌 `OperationalError`(스키마 오류 등)는 그대로 올린다.
    재시도를 다 써도 안 되면 마지막 오류를 그대로 올린다(부르는 쪽이 그 건만
    건너뛰고 다음으로 넘어갈 수 있게).
    """
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _is_lock_error(exc):
                raise
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2**attempt))
    assert last_exc is not None
    raise last_exc

# 이미 발행된 것으로 간주하는 상태
BLOCKING_STATUSES = ("uncertain", "done")

_FIELDS = (
    "source_id",
    "url",
    "account",
    "cafe",
    "menu_id",
    "board",
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

        DB가 다른 작업(노출 러너·키워드 워커 등)과 경합해 `database is locked`가
        나면 지수 백오프로 최대 `MAX_LOCK_RETRIES`번 다시 쓴다(사고 2026-09-26).
        그래도 안 되면 오류를 그대로 올린다 — 부르는 쪽(발행 루프)이 **그 글 1건만**
        건너뛰고 다음 글로 넘어갈 수 있게, 작업 전체를 죽이지 않는다.
        """
        unknown = set(fields) - set(_FIELDS)
        if unknown:
            raise ValueError(f"알 수 없는 열: {sorted(unknown)}")
        ts = now_iso()

        def _write() -> None:
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

        retry_on_lock(_write)

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

    def last_board_today(
        self,
        cafe: str,
        kst_date: str,
        *,
        exclude_sources: "set[str] | None" = None,
    ) -> str:
        """오늘 그 카페에 **마지막으로** 올린 일상 글의 게시판 표시.

        게시판 연속 방지(사용자 결정 2026-09-22 A안)가 실행 시작 시점의 "직전 글"을
        알아야 해서 쓴다. `menu_id`가 있으면 `menu_id`, 없으면 게시판 이름을 돌려준다.
        `publish.board_key`와 같은 모양(`menu:<id>` / 정규화된 이름)으로 맞춘다.
        """
        if not cafe or not kst_date:
            return ""
        rows = self.conn.execute(
            "SELECT cafe, source_key, menu_id, board FROM publications"
            " WHERE status IN (?, ?) AND substr(created_at, 1, 10) = ?"
            " ORDER BY updated_at, rowid",
            (*BLOCKING_STATUSES, kst_date),
        ).fetchall()
        skip = {str(s) for s in (exclude_sources or set())}
        last = ""
        for r in rows:
            if str(r["source_key"] or "") in skip:
                continue
            if not _same_cafe(cafe, str(r["cafe"] or "")):
                continue
            from v2r.engine.publish import board_key

            last = board_key(str(r["menu_id"] or ""), str(r["board"] or ""))
        return last

    def count_today_for_account(self, login_id: str, kst_date: str) -> int:
        """오늘(KST `kst_date`) 그 계정으로 올린(또는 올리는 중인) 글 수.

        계정별 하루 상한(규칙 2026-09-22)을 지키려고 쓴다. 제한에 걸려 `failed`가 된
        건은 실제로 올라가지 않았으므로 세지 않는다.
        """
        if not login_id or not kst_date:
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM publications"
            " WHERE status IN (?, ?) AND substr(created_at, 1, 10) = ?"
            " AND LOWER(COALESCE(account, '')) = LOWER(?)",
            (*BLOCKING_STATUSES, kst_date, login_id),
        ).fetchone()
        return int(row["n"])

    def counts_today_by_account(self, kst_date: str) -> dict[str, int]:
        """오늘 계정별 발행 건수 `{계정(소문자): 건수}` — 한 번에 읽는다."""
        if not kst_date:
            return {}
        rows = self.conn.execute(
            "SELECT LOWER(COALESCE(account, '')) AS login, COUNT(*) AS n FROM publications"
            " WHERE status IN (?, ?) AND substr(created_at, 1, 10) = ?"
            " GROUP BY login",
            (*BLOCKING_STATUSES, kst_date),
        ).fetchall()
        return {str(r["login"]): int(r["n"]) for r in rows if r["login"]}

    def count_limited(self, kst_date: str = "") -> int:
        """등록 제한에 걸려 못 올라간 글 수(오늘 또는 전체)."""
        sql = (
            "SELECT COUNT(*) AS n FROM publications"
            " WHERE status = 'failed' AND COALESCE(stage, '') LIKE '%제한%'"
        )
        params: list[object] = []
        if kst_date:
            sql += " AND substr(created_at, 1, 10) = ?"
            params.append(kst_date)
        return int(self.conn.execute(sql, params).fetchone()["n"])

    def list_limited(self, kst_date: str = "") -> list[dict]:
        """등록 제한으로 실패한 행 목록."""
        sql = (
            "SELECT * FROM publications"
            " WHERE status = 'failed' AND COALESCE(stage, '') LIKE '%제한%'"
        )
        params: list[object] = []
        if kst_date:
            sql += " AND substr(created_at, 1, 10) = ?"
            params.append(kst_date)
        sql += " ORDER BY updated_at DESC, rowid DESC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def by_source_id(self, source_id: str) -> dict | None:
        """V2R source_id로 조회."""
        row = self.conn.execute(
            "SELECT * FROM publications WHERE source_id = ?", (source_id,)
        ).fetchone()
        return dict(row) if row else None
