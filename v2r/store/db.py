"""SQLite 연결과 스키마 (DESIGN §3)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_iso() -> str:
    """현재 시각 ISO 문자열(KST)."""
    return datetime.now(KST).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """WAL + Row factory + 외래키 켠 연결."""
    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key    TEXT NOT NULL UNIQUE,
    task        TEXT NOT NULL,
    spec_json   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    lease_scope TEXT NOT NULL DEFAULT 'main',
    lease_owner TEXT,
    lease_until TEXT,
    result_json TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    source_key   TEXT NOT NULL,
    row_number   INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    status       TEXT NOT NULL,
    stage        TEXT,
    source_id    TEXT,
    url          TEXT,
    account      TEXT,
    cafe         TEXT,
    menu_id      TEXT,
    board        TEXT,
    scheduled_at TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (source_key, row_number, content_hash)
);

CREATE TABLE IF NOT EXISTS account_state (
    login_id         TEXT PRIMARY KEY,
    last_used_at     TEXT,
    restricted_until TEXT,
    restrict_code    TEXT,
    fail_count       INTEGER NOT NULL DEFAULT 0,
    note             TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_cache (
    key          TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    synced_at    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ok',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photo_usage (
    sha256         TEXT NOT NULL,
    variant        TEXT NOT NULL,
    used_in_source_id TEXT,
    used_at        TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (sha256, variant)
);

CREATE TABLE IF NOT EXISTS executor_lease (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    owner      TEXT,
    until      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER,
    level      TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS article_index (
    cafe_id    TEXT NOT NULL,
    cafe       TEXT NOT NULL DEFAULT '',
    source_id  TEXT NOT NULL,
    article_id TEXT,
    login_id   TEXT,
    title      TEXT NOT NULL DEFAULT '',
    title_norm TEXT NOT NULL DEFAULT '',
    body_hash  TEXT,
    created_at TEXT NOT NULL,
    synced_at  TEXT NOT NULL,
    PRIMARY KEY (cafe_id, source_id)
);

-- 게시글 등록 제한 등으로 **올라가지 않은** 글의 재발행 대기 줄 (2026-09-22)
-- 한 행 = 각색 시트의 같은 행을 **다른 계정으로 다시 올려야 한다**는 표시.
CREATE TABLE IF NOT EXISTS republish_queue (
    source_key   TEXT NOT NULL,
    row_number   INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    cafe         TEXT,
    board        TEXT,
    account      TEXT,
    source_id    TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (source_key, row_number, content_hash)
);

-- 브랜드별 키워드 노출 현황 이력 (docs/reports/keyword-exposure-plan-2026-09-22.md §2)
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
    search_query TEXT
);

-- 브랜드 대량 원고 생성 대기열 (밀려남 키워드 → 원고, 2026-09-23)
CREATE TABLE IF NOT EXISTS brand_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand           TEXT NOT NULL,
    keyword         TEXT NOT NULL,
    mtype           TEXT NOT NULL DEFAULT '',
    priority        REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    manuscript_path TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (brand, keyword, mtype)
);

CREATE INDEX IF NOT EXISTS idx_brand_queue_status ON brand_queue(brand, status, priority DESC);

CREATE INDEX IF NOT EXISTS idx_republish_status ON republish_queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_keyword_exposure_brand ON keyword_exposure(brand, keyword, checked_at);
CREATE INDEX IF NOT EXISTS idx_keyword_exposure_checked ON keyword_exposure(checked_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_jobs_scope ON jobs(status, lease_scope, id);
CREATE INDEX IF NOT EXISTS idx_article_index_title ON article_index(title_norm);
CREATE INDEX IF NOT EXISTS idx_article_index_hash ON article_index(body_hash);
CREATE INDEX IF NOT EXISTS idx_pub_status ON publications(status);
CREATE INDEX IF NOT EXISTS idx_pub_source_id ON publications(source_id);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
-- 발행 조회는 늘 "어느 카페 / 언제"로 찾는다 (누적 속도 조치 2026-09-22)
CREATE INDEX IF NOT EXISTS idx_pub_cafe_created ON publications(cafe, created_at);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
"""


#: 예전 DB에 뒤늦게 붙인 칸들 — (테이블, 칸 이름, 칸 정의)
MIGRATIONS = (
    ("jobs", "lease_scope", "TEXT NOT NULL DEFAULT 'main'"),
    # 게시판 연속 방지(사용자 결정 2026-09-22 A안)가 "직전 글의 게시판"을 알아야 한다
    ("publications", "board", "TEXT"),
    # 통검 자동완성 정규화 검색어(2026-09-23) — 실제로 검색창에 넣은 최종 검색어
    ("keyword_exposure", "search_query", "TEXT"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def migrate(conn: sqlite3.Connection) -> list[str]:
    """예전에 만들어진 DB에 빠진 칸을 채운다 (멱등). 추가한 칸 이름 목록 반환.

    스키마 생성(`executescript`)보다 **먼저** 돌아야 한다. 새로 붙인 칸을 쓰는
    인덱스가 SCHEMA 안에 있어서, 칸이 없는 옛 DB에서는 그 인덱스가 터진다.
    """
    added: list[str] = []
    for table, column, decl in MIGRATIONS:
        if not _table_exists(conn, table):
            continue
        names = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})")}
        if column in names:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        added.append(f"{table}.{column}")
    return added


def init_schema(conn: sqlite3.Connection) -> None:
    """모든 테이블 생성 (멱등)."""
    migrate(conn)
    conn.executescript(SCHEMA)
    ts = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO executor_lease (id, owner, until, created_at, updated_at)"
        " VALUES (1, NULL, NULL, ?, ?)",
        (ts, ts),
    )
