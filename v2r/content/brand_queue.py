"""브랜드 대량 원고 생성 대기열 (`brand_queue` 표).

입력 = 브랜드 시트 `노출 현황` G열 `밀려남` ∪ DB `keyword_exposure`의 최신 `pushed`
(둘 다 `v2r.sources.keyword_list.load_pushed_keywords`가 이미 합쳐 준다) ∪
`data/keywords/<브랜드>.sqlite`의 발굴분(노출 확인된 것만 — 확인은 `keyword_exposure`
표에 들어간 것으로 판단한다. 미확인 발굴 키워드는 여기서 걸러진다).

우선순위 = 검색량(`total`) 내림차순, 같은 검색량이면 연관도(`relevance`, 작을수록
가까움) 오름차순. 시트에서만 온 키워드처럼 검색량을 모르면 맨 뒤로 보낸다.

팥순이는 질문형·후기형을 번갈아 채운다 (사용자 지시: 팥순이만 두 유형 다 씀).
나머지 브랜드는 질문형만 채운다.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from v2r.config import load_yaml
from v2r.store.db import now_iso

#: 팥순이만 질문형·후기형을 번갈아 채운다
ALTERNATING_BRANDS = {"팥순이"}
DEFAULT_MTYPE = "질문형"
ALT_MTYPES = ("질문형", "후기형")

TERMINAL_STATUSES = ("ready", "published")


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _volume_map(brand: str, data_dir: Path) -> dict[str, tuple[float, float]]:
    """`data/keywords/<브랜드>.sqlite`에서 키워드 → (검색량, 연관도)."""
    path = Path(data_dir) / "keywords" / f"{brand}.sqlite"
    out: dict[str, tuple[float, float]] = {}
    if not path.exists():
        return out
    try:
        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute("SELECT keyword, total, relevance FROM keywords").fetchall()
        finally:
            conn.close()
    except Exception:
        return out
    for kw, total, relevance in rows:
        out[_norm(kw)] = (float(total or 0), float(relevance if relevance is not None else 99))
    return out


def _mtypes_for(brand: str, n: int) -> list[str]:
    if brand in ALTERNATING_BRANDS:
        return [ALT_MTYPES[i % 2] for i in range(n)]
    return [DEFAULT_MTYPE] * n


def _bulk_cfg() -> dict:
    return (load_yaml("bulk") or {}).get("bulk") or {}


def _today_v2r_count(rt: Any) -> int:
    """오늘 이미 `target='v2r'`로 채운 건수(전 브랜드 합계)."""
    from datetime import datetime

    from v2r.store.db import KST

    today = datetime.now(KST).strftime("%Y-%m-%d")
    row = rt.conn.execute(
        "SELECT COUNT(*) AS n FROM brand_queue WHERE target = 'v2r' AND created_at >= ?",
        (today,),
    ).fetchone()
    return int(row["n"]) if row else 0


def refill(rt: Any, brand: str, n: int, target: str = "") -> int:
    """대기열에 최대 `n`건을 새로 채운다. 실제로 넣은 건수를 돌려준다.

    이미 `ready`/`published`인 브랜드·키워드는 건너뛴다(중복 방지).
    `pending`/`generating`/`failed`(3회 미만)로 이미 있는 것도 다시 넣지 않는다
    (표의 UNIQUE(brand, keyword, mtype)로도 한 번 더 막힌다).

    `target`: `v2r`/`vpc`를 주면 새로 채우는 항목을 전부 그 값으로 표시한다.
    비우면(기본) 검색량 상위부터 오늘 이미 채운 `v2r` 건수가
    `config/bulk.yaml`의 `v2r_daily_cap`(기본 50)에 닿을 때까지는 `v2r`,
    그 이후는 `vpc`로 자동 배정한다(사용자 지시 2026-09-23: 가상 PC는
    하루 수백 건을 따로 처리한다).
    """
    from v2r.sources.keyword_list import load_pushed_keywords

    n = max(0, int(n))
    if n == 0 or not brand:
        return 0

    target = (target or "").strip().lower()
    auto_target = target not in ("v2r", "vpc")
    v2r_cap = int(_bulk_cfg().get("v2r_daily_cap", 50) or 50)
    v2r_so_far = _today_v2r_count(rt) if auto_target else 0

    xlsx = Path(rt.settings.repo_root) / "data" / f"brand_sheet_{brand}.xlsx"
    try:
        pool = load_pushed_keywords(
            brand,
            rt.sources_cfg,
            xlsx_path=str(xlsx) if xlsx.exists() else None,
            conn=rt.conn,
        )
    except Exception:
        pool = []

    existing = {
        (_norm(r["keyword"]), r["mtype"])
        for r in rt.conn.execute(
            "SELECT keyword, mtype FROM brand_queue WHERE brand = ? AND status != 'failed'",
            (brand,),
        )
    }
    vol = _volume_map(brand, rt.settings.data_dir)

    scored: list[tuple[float, float, dict]] = []
    for item in pool:
        key = _norm(item["keyword"])
        total, relevance = vol.get(key, (-1.0, 999.0))
        scored.append((total, relevance, item))
    # 검색량 내림차순, 같으면 연관도(작을수록 가까움) 오름차순
    scored.sort(key=lambda t: (-t[0], t[1]))

    candidates = []
    for total, relevance, item in scored:
        key = _norm(item["keyword"])
        candidates.append((key, item, total, relevance))

    mtypes_cycle = ALT_MTYPES if brand in ALTERNATING_BRANDS else (DEFAULT_MTYPE,)
    added = 0
    ts = now_iso()
    mtype_idx = 0
    for key, item, total, relevance in candidates:
        if added >= n:
            break
        mtype = mtypes_cycle[mtype_idx % len(mtypes_cycle)]
        if (key, mtype) in existing:
            continue
        priority = total if total >= 0 else -1.0
        if auto_target:
            row_target = "v2r" if v2r_so_far < v2r_cap else "vpc"
        else:
            row_target = target
        try:
            cur = rt.conn.execute(
                """
                INSERT OR IGNORE INTO brand_queue
                    (brand, keyword, mtype, priority, status, attempts, target, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (brand, item["keyword"], mtype, priority, row_target, ts, ts),
            )
        except sqlite3.Error:
            continue
        if cur.rowcount:
            added += 1
            if auto_target and row_target == "v2r":
                v2r_so_far += 1
        existing.add((key, mtype))
        mtype_idx += 1
    return added


def pending(rt: Any, brand: str, limit: int = 0, target: str = "") -> list[sqlite3.Row]:
    """대기 중(pending)인 항목을 우선순위 순으로. `target`을 주면 그것만."""
    target = (target or "").strip().lower()
    sql = "SELECT * FROM brand_queue WHERE brand = ? AND status = 'pending'"
    params: list = [brand]
    if target:
        sql += " AND target = ?"
        params.append(target)
    sql += " ORDER BY priority DESC, id ASC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(rt.conn.execute(sql, params))


def ready_count(rt: Any, target: str = "") -> int:
    """전체 재고(`ready` 상태) 건수. `target`을 주면 그것만."""
    target = (target or "").strip().lower()
    sql = "SELECT COUNT(*) AS n FROM brand_queue WHERE status = 'ready'"
    params: list = []
    if target:
        sql += " AND target = ?"
        params.append(target)
    row = rt.conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def mark(rt: Any, row_id: int, status: str, manuscript_path: str = "", attempts: int | None = None) -> None:
    ts = now_iso()
    if attempts is None:
        rt.conn.execute(
            "UPDATE brand_queue SET status = ?, manuscript_path = ?, updated_at = ? WHERE id = ?",
            (status, manuscript_path or None, ts, row_id),
        )
    else:
        rt.conn.execute(
            "UPDATE brand_queue SET status = ?, manuscript_path = ?, attempts = ?, updated_at = ?"
            " WHERE id = ?",
            (status, manuscript_path or None, attempts, ts, row_id),
        )


def counts(rt: Any, brand: str = "") -> dict[str, int]:
    """상태별 건수 (브랜드 비우면 전체)."""
    where = "WHERE brand = ?" if brand else ""
    params = (brand,) if brand else ()
    rows = rt.conn.execute(
        f"SELECT status, COUNT(*) AS n FROM brand_queue {where} GROUP BY status", params
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def counts_by_target(rt: Any) -> dict[str, dict[str, int]]:
    """target별(v2r/vpc) 상태별 건수."""
    rows = rt.conn.execute(
        "SELECT target, status, COUNT(*) AS n FROM brand_queue GROUP BY target, status"
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["target"], {})[r["status"]] = r["n"]
    return out


def ready_rows_for_export(rt: Any, target: str = "vpc") -> list[sqlite3.Row]:
    """내보낼 원고 목록 — `ready` 상태이고 아직 `manuscript_path`가 있는 것."""
    return list(
        rt.conn.execute(
            "SELECT * FROM brand_queue WHERE status = 'ready' AND target = ?"
            " AND manuscript_path IS NOT NULL ORDER BY brand, priority DESC, id ASC",
            (target,),
        )
    )


__all__ = [
    "refill",
    "pending",
    "mark",
    "counts",
    "counts_by_target",
    "ready_count",
    "ready_rows_for_export",
    "ALTERNATING_BRANDS",
]
