"""브랜드 대량 원고 생성 대기열 (`brand_queue` 표).

입력 = 브랜드 시트 `노출 현황` G열 `밀려남` ∪ DB `keyword_exposure`의 최신 `pushed`
(둘 다 `v2r.sources.keyword_list.load_pushed_keywords`가 이미 합쳐 준다) ∪
`data/keywords/<브랜드>.sqlite`의 발굴분(노출 확인된 것만 — 확인은 `keyword_exposure`
표에 들어간 것으로 판단한다. 미확인 발굴 키워드는 여기서 걸러진다).

우선순위(2026-09-24 개정, 사용자 지시): 1순위로 연관도 등급 그룹 — 0에서 2(직접/
근접/확장)를 먼저, 3(당위성)은 그 뒤(4/무관은 `keyword_relevance.is_manuscript_target`
으로 아예 걸러 대기열에 넣지 않는다). 같은 그룹 안에서는 검색량(`total`) 내림차순.
아직 LLM 연관도 재산정 전(`relevance_llm`이 없음)인 항목은 옛 낱말겹침 `relevance`
열로 대체 판정하고, 그마저 없으면 맨 뒤 그룹으로 보낸다. 시트에서만 온 키워드처럼
검색량을 모르면 그 그룹 안에서 맨 뒤로 보낸다.

팥순이는 질문형·후기형을 번갈아 채운다 (사용자 지시: 팥순이만 두 유형 다 씀).
나머지 브랜드는 질문형만 채운다.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from v2r.config import load_yaml
from v2r.knowledge import keyword_relevance as kr_mod
from v2r.store.db import now_iso

#: 팥순이만 질문형·후기형을 번갈아 채운다
ALTERNATING_BRANDS = {"팥순이"}
DEFAULT_MTYPE = "질문형"
ALT_MTYPES = ("질문형", "후기형")

TERMINAL_STATUSES = ("ready", "published")


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


#: 대기열 우선순위 그룹: 0-2(직접/근접/확장) 먼저, 3(당위성) 다음, 미산정/무관은 맨 뒤.
_GROUP_SCORED = 0
_GROUP_BRIDGE = 1
_GROUP_UNKNOWN = 2


def _priority_group(relevance_llm: float | None) -> int:
    if relevance_llm is None:
        return _GROUP_UNKNOWN
    if relevance_llm <= kr_mod.MANUSCRIPT_MAX_RELEVANCE - 1:  # 0,1,2
        return _GROUP_SCORED
    if relevance_llm == kr_mod.RELEVANCE_BRIDGE:  # 3
        return _GROUP_BRIDGE
    return _GROUP_UNKNOWN


def _volume_map(brand: str, data_dir: Path) -> dict[str, tuple[float, int, float]]:
    """`data/keywords/<브랜드>.sqlite`에서 키워드 → (검색량, 우선순위그룹, 연관도).

    연관도는 `relevance_llm`(새 척도, 0에서 4)을 우선 쓰고, 아직 재산정 전이면
    옛 낱말겹침 `relevance` 열로 대체한다. `is_manuscript_target`이 False인
    항목(4=무관 또는 needs_review)은 아예 목록에서 뺀다(호출측에서 걸러진다 —
    `refill`에서 이 맵에 없는 키워드는 원래 관대하게 통과시켰으므로, 여기서는
    "존재하되 무관으로 확인된 것"만 제외한다).
    """
    path = Path(data_dir) / "keywords" / f"{brand}.sqlite"
    out: dict[str, tuple[float, int, float]] = {}
    if not path.exists():
        return out
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(keywords)")}
            has_llm_cols = {"relevance_llm", "relevance_codex", "needs_review"}.issubset(cols)
            if has_llm_cols:
                rows = conn.execute(
                    "SELECT keyword, total, relevance, relevance_llm, relevance_codex, needs_review"
                    " FROM keywords"
                ).fetchall()
            else:
                rows = conn.execute("SELECT keyword, total, relevance FROM keywords").fetchall()
        finally:
            conn.close()
    except Exception:
        return out
    for row in rows:
        kw = row["keyword"]
        total = float(row["total"] or 0)
        if has_llm_cols and row["relevance_llm"] is not None:
            if not kr_mod.is_manuscript_target(row):
                continue  # 4(무관) 또는 needs_review — 대기열 대상 아님
            rel = float(row["relevance_llm"])
            group = _priority_group(rel)
        else:
            # 아직 LLM 재산정 전 — 옛 낱말겹침 `relevance`(0-4 스케일과 다른
            # 범위)로는 새 0-2/3 그룹을 매길 수 없으므로 그룹을 나누지 않고
            # (전부 미산정 그룹) 검색량으로만 정렬한다(기존 동작 유지).
            legacy = row["relevance"]
            rel = float(legacy) if legacy is not None else 99.0
            group = _GROUP_UNKNOWN
        out[_norm(kw)] = (total, group, rel)
    return out


def bridge_info(brand: str, keyword: str, data_dir: Path) -> tuple[int | None, str]:
    """`data/keywords/<브랜드>.sqlite`에서 키워드의 `relevance_llm`·`bridge_rationale`.

    원고 생성 배선(사용자 지시 2026-09-24, `docs/reports/relevance-split-2026-09-24.md`
    8절 3항)이 `build_body_prompt`의 【당위성 논리】 블록까지 값을 넘기는 데 쓴다.
    표/열이 없거나 그 키워드가 없으면 `(None, "")` — 호출측은 기존 동작 그대로 간다.
    """
    path = Path(data_dir) / "keywords" / f"{brand}.sqlite"
    if not path.exists():
        return None, ""
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(keywords)")}
            if "relevance_llm" not in cols:
                return None, ""
            has_rationale = "bridge_rationale" in cols
            sql = "SELECT relevance_llm" + (", bridge_rationale" if has_rationale else "")
            sql += " FROM keywords WHERE keyword = ?"
            row = conn.execute(sql, (keyword,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None, ""
    if row is None or row["relevance_llm"] is None:
        return None, ""
    rel = int(row["relevance_llm"])
    rationale = str(row["bridge_rationale"] or "") if has_rationale else ""
    return rel, rationale


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

    scored: list[tuple[int, float, float, dict]] = []
    for item in pool:
        key = _norm(item["keyword"])
        entry = vol.get(key)
        if entry is None:
            total, group, relevance = -1.0, _GROUP_UNKNOWN, 999.0
        else:
            total, group, relevance = entry
        scored.append((group, total, relevance, item))
    # 1순위: 연관도 그룹(0-2 먼저, 3 다음, 미산정/무관 맨 뒤). 그룹 안에서는 검색량 내림차순.
    scored.sort(key=lambda t: (t[0], -t[1]))

    candidates = []
    for group, total, relevance, item in scored:
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
    "bridge_info",
    "ALTERNATING_BRANDS",
]
