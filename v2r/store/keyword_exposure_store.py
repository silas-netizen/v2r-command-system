"""`keyword_exposure` 표 읽기/쓰기 (DESIGN: keyword-exposure-plan-2026-09-22 §2).

2026-09-27 DB 분리(docs/reports/db-split-plan-2026-09-27.md) — `exposure_db(rt)`가
노출 관련 표(`keyword_exposure`, `exposure_queue`)에 접근할 커넥션을 정하는
**유일한 통로**다. `config/exposure.yaml`의 `db_path`가 비어 있으면(기본)
지금까지처럼 `rt.conn`(메인 `data/v2r.sqlite`)을 그대로 돌려주고, 값이 있으면
그 경로의 별도 sqlite 파일에 연결(WAL·busy_timeout 동일 적용)해 캐시해 둔
커넥션을 돌려준다. 노출 표를 만지는 모든 코드(`keyword_exposure.py`,
`exposure_priority.py`, `exposure_runner.py`, 시트/보고서 반영 경로 포함)는
`rt.conn`을 직접 쓰지 말고 이 함수를 거쳐야 한다."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

log = logging.getLogger(__name__)

_T = TypeVar("_T")


def with_sqlite_retry(fn: Callable[..., _T], *args: Any, retries: int = 5, base_delay: float = 0.2, **kwargs: Any) -> _T:
    """2026-09-27 — sqlite `OperationalError`("database is locked"/"database
    is busy")면 지수 백오프(0.2, 0.4, 0.8, 1.6, 3.2초)로 최대 `retries`번
    재시도한다. `busy_timeout` 프라그마(현재 30초)를 이미 다 기다린 뒤에도
    나는 잠금 오류를 한 번 더 흡수하기 위한 안전망 — 락과 무관한
    `OperationalError`(스키마 오류 등)는 그대로 올린다."""
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(max(1, retries)):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last_exc = exc
            if attempt == retries - 1:
                break
            delay = base_delay * (2**attempt)
            log.warning(
                "sqlite 잠금(%s/%s), %.2fs 후 재시도: %s", attempt + 1, retries, delay, exc
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


#: 분리 대상 표 — 마이그레이션 스크립트·조사 보고서와 이름을 맞춘다.
EXPOSURE_TABLES = ("keyword_exposure", "exposure_queue")

#: 별도 파일로 연결했을 때 그 표를 만들 스키마(v2r/store/db.py SCHEMA의 해당
#: 부분과 반드시 같은 정의를 유지한다 — 스키마가 갈리면 마이그레이션 스크립트의
#: "행 수만 비교"로는 못 잡는 차이가 생긴다).
_EXPOSURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS keyword_exposure (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    brand        TEXT NOT NULL,
    keyword      TEXT NOT NULL,
    cafe         TEXT,
    article_url  TEXT,
    rank         INTEGER,
    status       TEXT NOT NULL,
    t0_status    TEXT,
    checked_at   TEXT NOT NULL,
    search_query TEXT,
    rank_overall INTEGER
);

CREATE TABLE IF NOT EXISTS exposure_queue (
    brand        TEXT NOT NULL,
    keyword_norm TEXT NOT NULL,
    keyword      TEXT NOT NULL,
    item_json    TEXT NOT NULL,
    tier         INTEGER NOT NULL,
    sort_key     REAL NOT NULL,
    enqueued_at  REAL NOT NULL,
    claimed_by   TEXT,
    claimed_at   REAL,
    done_at      REAL,
    PRIMARY KEY (brand, keyword_norm)
);

CREATE INDEX IF NOT EXISTS idx_exposure_queue_pick
    ON exposure_queue(brand, done_at, claimed_by, tier, sort_key);
CREATE INDEX IF NOT EXISTS idx_keyword_exposure_brand ON keyword_exposure(brand, keyword, checked_at);
CREATE INDEX IF NOT EXISTS idx_keyword_exposure_checked ON keyword_exposure(checked_at);
"""

#: rt 객체 하나당 별도 DB 커넥션 한 개만 열어 재사용한다(rt는 프로세스마다
#: 하나이므로 sqlite3.connect 반복 호출을 막는 캐시 키로 `id(rt)`를 쓴다).
_EXPOSURE_CONN_CACHE: dict[int, sqlite3.Connection] = {}


def _connect_exposure_db(db_path: str) -> sqlite3.Connection:
    from v2r.store.db import connect as _connect

    conn = _connect(db_path)
    conn.executescript(_EXPOSURE_SCHEMA)
    return conn


def exposure_db(rt: Any) -> sqlite3.Connection:
    """노출 표(`keyword_exposure`/`exposure_queue`) 접근용 커넥션.

    `config/exposure.yaml`의 `db_path`가 비어 있으면 `rt.conn`(기존 동작 그대로).
    값이 있으면 그 파일에 연결한 커넥션을 `rt`당 하나만 열어 캐시해 돌려준다.
    """
    try:
        from v2r.config import load_yaml

        db_path = str((load_yaml("exposure") or {}).get("db_path") or "").strip()
    except Exception:
        db_path = ""
    if not db_path:
        return rt.conn

    key = id(rt)
    conn = _EXPOSURE_CONN_CACHE.get(key)
    if conn is not None:
        return conn

    repo_root = getattr(getattr(rt, "settings", None), "repo_root", None)
    path = Path(db_path)
    if not path.is_absolute() and repo_root:
        path = Path(repo_root) / path
    conn = _connect_exposure_db(str(path))
    _EXPOSURE_CONN_CACHE[key] = conn
    return conn


def _save_impl(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO keyword_exposure
            (brand, keyword, cafe, article_url, rank, status, t0_status, checked_at, search_query, rank_overall)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.get("brand", ""),
            row.get("keyword", ""),
            row.get("cafe", ""),
            row.get("article_url", ""),
            row.get("rank"),
            row.get("status", "unknown"),
            row.get("t0_status", ""),
            row.get("checked_at", ""),
            row.get("search_query", ""),
            row.get("rank_overall"),
        ),
    )


def save(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """검사 결과 한 줄을 이력으로 쌓는다(누적, 덮어쓰지 않음). 잠금 시 재시도."""
    with_sqlite_retry(_save_impl, conn, row)


def latest_for_keyword(conn: sqlite3.Connection, brand: str, keyword: str) -> sqlite3.Row | None:
    """이 브랜드·키워드 한 건의 가장 최근 검사 결과 — 단건 조회(`idx_keyword_exposure_brand`
    (brand, keyword, checked_at) 인덱스로 밀리초 단위). 2026-09-24 3차 — 작업자가
    실제로 검사하기 직전에 캐시와 무관하게 항상 이걸로 최종 확인한다(다른
    작업자가 그 사이 검사한 걸 캐시가 놓쳐 중복 재검사되던 문제 수정)."""
    return conn.execute(
        "SELECT status, checked_at FROM keyword_exposure WHERE brand = ? AND keyword = ? "
        "ORDER BY checked_at DESC, rowid DESC LIMIT 1",
        (brand, keyword),
    ).fetchone()


def latest_by_keyword(conn: sqlite3.Connection, brand: str = "") -> list[sqlite3.Row]:
    """브랜드(비우면 전체)의 키워드별 **가장 최근** 검사 결과 1행씩."""
    where = "WHERE brand = ?" if brand else ""
    params = (brand,) if brand else ()
    sql = f"""
        SELECT ke.*
        FROM keyword_exposure ke
        JOIN (
            SELECT brand, keyword, MAX(checked_at) AS max_checked
            FROM keyword_exposure
            {where}
            GROUP BY brand, keyword
        ) latest
          ON latest.brand = ke.brand
         AND latest.keyword = ke.keyword
         AND latest.max_checked = ke.checked_at
        ORDER BY ke.brand, ke.keyword
    """
    return list(conn.execute(sql, params).fetchall())


def pushed_keywords(conn: sqlite3.Connection, brand: str) -> list[dict]:
    """브랜드의 최신 검사에서 `pushed`(밀려남)로 나온 키워드 목록."""
    rows = latest_by_keyword(conn, brand)
    return [
        {"keyword": r["keyword"], "cafe": r["cafe"] or ""}
        for r in rows
        if r["status"] == "pushed"
    ]


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """브랜드별 노출/밀려남/미확인 수 + 이번에 새로 밀려난 키워드.

    "새로 밀려난"은 최신 검사가 `pushed`이고 바로 전 검사는 `pushed`가 아니었던 키워드.
    """
    rows = latest_by_keyword(conn)
    by_brand: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = r["brand"]
        entry = by_brand.setdefault(
            b, {"exposed": 0, "pushed": 0, "unpublished": 0, "unknown": 0, "newly_pushed": []}
        )
        status = r["status"] if r["status"] in entry else "unknown"
        entry[status] += 1
        if status == "pushed":
            prev = conn.execute(
                """
                SELECT status FROM keyword_exposure
                WHERE brand = ? AND keyword = ? AND checked_at < ?
                ORDER BY checked_at DESC LIMIT 1
                """,
                (b, r["keyword"], r["checked_at"]),
            ).fetchone()
            if prev is None or prev["status"] != "pushed":
                entry["newly_pushed"].append(r["keyword"])
    return by_brand


__all__ = [
    "save",
    "latest_for_keyword",
    "latest_by_keyword",
    "pushed_keywords",
    "summary",
    "exposure_db",
    "EXPOSURE_TABLES",
    "with_sqlite_retry",
]
