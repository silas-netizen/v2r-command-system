"""`keyword_exposure` 표 읽기/쓰기 (DESIGN: keyword-exposure-plan-2026-09-22 §2)."""

from __future__ import annotations

import sqlite3
from typing import Any


def save(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """검사 결과 한 줄을 이력으로 쌓는다(누적, 덮어쓰지 않음)."""
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


__all__ = ["save", "latest_by_keyword", "pushed_keywords", "summary"]
