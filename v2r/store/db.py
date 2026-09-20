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

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_article_index_title ON article_index(title_norm);
CREATE INDEX IF NOT EXISTS idx_article_index_hash ON article_index(body_hash);
CREATE INDEX IF NOT EXISTS idx_pub_status ON publications(status);
CREATE INDEX IF NOT EXISTS idx_pub_source_id ON publications(source_id);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """모든 테이블 생성 (멱등)."""
    conn.executescript(SCHEMA)
    ts = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO executor_lease (id, owner, until, created_at, updated_at)"
        " VALUES (1, NULL, NULL, ?, ?)",
        (ts, ts),
    )
