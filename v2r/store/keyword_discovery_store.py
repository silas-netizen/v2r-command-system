"""브랜드 키워드 발굴 저장소 — `data/keywords/<브랜드>.sqlite`.

`v2r/knowledge/naver_keyword_tool.py`의 BFS가 쓴다. 메인 DB(`rt.conn`)와는
별개 파일이다(브랜드당 최대 1만 행까지 늘어날 수 있어 메인 장부와 분리).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from v2r.store.db import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
    keyword TEXT PRIMARY KEY,
    pc INTEGER NOT NULL DEFAULT 0,
    mobile INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    source_seed TEXT NOT NULL DEFAULT '',
    depth INTEGER NOT NULL DEFAULT 0,
    relevance INTEGER NOT NULL DEFAULT 0,
    collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_keywords_total ON keywords(total DESC);
CREATE INDEX IF NOT EXISTS idx_keywords_relevance ON keywords(relevance);
"""


def open_db(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def save_many(conn: sqlite3.Connection, rows: list[dict[str, Any]], now: str | None = None) -> int:
    """새 키워드만 넣는다(이미 있는 키워드는 건드리지 않음 — 최초 발견 값 유지)."""
    ts = now or now_iso()
    n = 0
    for r in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO keywords "
            "(keyword, pc, mobile, total, source_seed, depth, relevance, collected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(r["keyword"]),
                int(r.get("pc", 0)),
                int(r.get("mobile", 0)),
                int(r.get("total", r.get("pc", 0) + r.get("mobile", 0))),
                str(r.get("source_seed", "")),
                int(r.get("depth", 0)),
                int(r.get("relevance", 0)),
                ts,
            ),
        )
        n += cur.rowcount
    conn.commit()
    return n


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0])


def all_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """전체 행(키워드·깊이 등 포함) — 연관도 재계산 등에서 쓴다."""
    rows = conn.execute(
        "SELECT keyword, pc, mobile, total, source_seed, depth, relevance, collected_at FROM keywords"
    ).fetchall()
    return [dict(r) for r in rows]


def update_relevance(conn: sqlite3.Connection, updates: list[tuple[str, int]]) -> int:
    """`[(keyword, relevance), ...]` 로 연관도만 갱신한다(가이드 낱말이 나중에
    보강됐을 때 재계산용). 바뀐 행 수를 돌려준다."""
    n = 0
    for keyword, relevance in updates:
        cur = conn.execute(
            "UPDATE keywords SET relevance = ? WHERE keyword = ? AND relevance != ?",
            (int(relevance), str(keyword), int(relevance)),
        )
        n += cur.rowcount
    conn.commit()
    return n


def all_keywords(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT keyword FROM keywords")]


def top(conn: sqlite3.Connection, n: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT keyword, pc, mobile, total, source_seed, depth, relevance, collected_at "
        "FROM keywords ORDER BY total DESC LIMIT ?",
        (n,),
    ).fetchall()
    return [dict(r) for r in rows]


def relevance_distribution(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        "SELECT relevance, COUNT(*) FROM keywords GROUP BY relevance"
    ).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    total = count(conn)
    total_search = conn.execute("SELECT COALESCE(SUM(total), 0) FROM keywords").fetchone()[0]
    return {
        "count": total,
        "total_search_volume": int(total_search),
        "relevance": relevance_distribution(conn),
        "top": top(conn, 20),
    }


__all__ = [
    "open_db",
    "save_many",
    "count",
    "all_keywords",
    "top",
    "relevance_distribution",
    "summary",
]
