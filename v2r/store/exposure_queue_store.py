"""노출 확인 공유 큐(`exposure_queue` 표) — 2026-09-24 5차.

배경: `docs/reports/exposure-speed-2026-09-24.md` 6-7·6-8절 — 작업자
프로세스마다 따로 universe를 정렬해 두던 방식(1~4차)은, TTL 안에서도
5개 작업자가 **각자 독립적으로 계산한** 정렬 목록의 앞쪽을 동시에
다퉈(누가 먼저 DB에 저장하느냐의 경쟁) 중복 재검사가 19.4%까지 늘었다.

이 모듈은 그 정렬·선점을 **표 하나**(`exposure_queue`, sqlite)로 옮긴다.
정렬은 브랜드당 한 프로세스가 TTL마다 한 번만 계산해 표에 갱신
(`upsert_candidates`)하고, 작업자는 `claim_batch`의 원자적
`UPDATE ... WHERE rowid IN (SELECT ...)`(한 트랜잭션) 한 번으로 배치를
선점한다 — 같은 rowid를 두 프로세스가 동시에 고를 수 없으므로(sqlite
쓰기 트랜잭션은 직렬화됨) 근본적으로 중복이 안 난다.

우선순위 규칙 자체(`exposure_priority.priority_tier`)는 건드리지 않는다 —
이 모듈은 그 결과(등급·정렬 보조키)를 어디에 어떻게 보관·선점하느냐만
바꾼다.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

#: 이 시간(초)이 지난 선점은 죽은 작업자의 것으로 보고 다시 선점 가능하게 한다
#: (검사 1건 보통 8~25초, 넉넉히 잡음 — 기존 `exposure_runner.INFLIGHT_TTL_SECONDS`
#: 와 같은 값을 유지해 동작을 바꾸지 않는다).
CLAIM_TTL_SECONDS = 180.0


def _item_json(item: dict) -> str:
    return json.dumps(item, ensure_ascii=False)


#: 2026-09-24 6차 — 시트가 브랜드당 1만 행대(우아덤 12,549행)까지 커진 뒤,
#: `upsert_candidates`를 한 트랜잭션으로 몰아 하면 그 하나의 sqlite 쓰기
#: 트랜잭션이 (WAL이라도 쓰기는 직렬화되므로) 다른 작업자 프로세스의
#: `claim_batch`/`mark_done`/`release_claim`을 오래 막는다. 후보를 이 개수
#: 단위로 나눠 여러 개의 짧은 트랜잭션으로 커밋해, 그 사이사이 다른
#: 프로세스가 끼어들 수 있게 한다.
UPSERT_CHUNK_SIZE = 500


def upsert_candidates(
    conn: sqlite3.Connection,
    brand: str,
    candidates: list[tuple[int, float, str, str, dict]],
    now_epoch: float | None = None,
    claim_ttl_sec: float = CLAIM_TTL_SECONDS,
    chunk_size: int = UPSERT_CHUNK_SIZE,
) -> None:
    """브랜드의 정렬된 후보 목록을 표에 통째로 갱신한다.

    `candidates`: `(tier, sort_key, keyword_norm, keyword, item)` 튜플 목록
    (이미 정렬돼 있을 필요는 없다 — `claim_batch`가 `ORDER BY tier, sort_key`로
    뽑는다). `sort_key`는 작을수록 먼저(오래된 순 음수 나이, 3등급은
    -(검색량*1e7 + 나이) — `exposure_priority._sort_key_for_tier` 참고).

    진행 중인 선점(`claimed_by` 있고 `done_at` 없고 TTL 안)은 **건드리지
    않는다** — 갱신 중에 다른 작업자가 검사하던 걸 방해하지 않기 위해.
    그 외(완료됐거나, 선점이 만료됐거나, 아예 새 항목)는 `tier`·`sort_key`를
    새로 쓰면서 선점 상태를 비워(다시 뽑힐 수 있게) 갱신한다. 이번에
    후보가 아닌(등급이 다시 밀려난) 기존 행은, 진행 중인 선점이 아니면
    지운다 — 2026-09-24 6차부터는 "이번 후보 목록에 없는 키워드"를 일일이
    나열(`NOT IN (수천 개)`)하지 않고, `enqueued_at`이 이번 갱신 시작
    시각(`now_epoch`)보다 오래된 행(=이번에 갱신되지 않은 행)을 지우는
    워터마크 방식을 쓴다 — SQL 크기가 후보 수와 무관하게 일정해 1만 개
    대에서도 빠르다.
    """
    now_epoch = now_epoch if now_epoch is not None else time.time()
    cutoff = now_epoch - claim_ttl_sec

    rows = [
        (brand, keyword_norm, keyword, _item_json(item), tier, sort_key, now_epoch)
        for tier, sort_key, keyword_norm, keyword, item in candidates
    ]
    for start in range(0, len(rows), max(1, chunk_size)) if rows else [0]:
        chunk = rows[start : start + max(1, chunk_size)] if rows else []
        conn.execute("BEGIN IMMEDIATE")
        try:
            if chunk:
                conn.executemany(
                    """
                    INSERT INTO exposure_queue
                        (brand, keyword_norm, keyword, item_json, tier, sort_key, enqueued_at,
                         claimed_by, claimed_at, done_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                    ON CONFLICT(brand, keyword_norm) DO UPDATE SET
                        keyword = excluded.keyword,
                        item_json = excluded.item_json,
                        tier = excluded.tier,
                        sort_key = excluded.sort_key,
                        enqueued_at = excluded.enqueued_at,
                        claimed_by = CASE
                            WHEN done_at IS NULL AND claimed_by IS NOT NULL AND claimed_at >= ?
                            THEN claimed_by ELSE NULL END,
                        claimed_at = CASE
                            WHEN done_at IS NULL AND claimed_by IS NOT NULL AND claimed_at >= ?
                            THEN claimed_at ELSE NULL END,
                        done_at = CASE
                            WHEN done_at IS NULL AND claimed_by IS NOT NULL AND claimed_at >= ?
                            THEN done_at ELSE NULL END
                    """,
                    [(*r, cutoff, cutoff, cutoff) for r in chunk],
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # 워터마크 삭제 — 이번 갱신에서 손 안 댄(그래서 enqueued_at이 예전 그대로인)
    # 행 중, 진행 중인 선점이 아닌 것만 지운다. 후보가 하나도 없었으면(빈
    # universe) 이 브랜드의 대기 중인 모든 행을 지운다(진행 중인 선점 제외).
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            DELETE FROM exposure_queue
            WHERE brand = ? AND enqueued_at < ?
              AND NOT (done_at IS NULL AND claimed_by IS NOT NULL AND claimed_at >= ?)
            """,
            (brand, now_epoch, cutoff),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def last_refreshed_at(conn: sqlite3.Connection, brand: str) -> float:
    """이 브랜드 큐를 마지막으로 갱신한 시각(epoch) — 없으면 0.0."""
    row = conn.execute(
        "SELECT MAX(enqueued_at) AS m FROM exposure_queue WHERE brand = ?", (brand,)
    ).fetchone()
    val = row["m"] if row else None
    return float(val) if val is not None else 0.0


def claim_batch(
    conn: sqlite3.Connection,
    brand: str,
    worker_id: str,
    n: int,
    now_epoch: float | None = None,
    claim_ttl_sec: float = CLAIM_TTL_SECONDS,
) -> list[dict]:
    """`n`개를 등급·정렬 순으로 원자적으로 선점해 item 딕셔너리 목록을 돌려준다.

    한 트랜잭션(`BEGIN IMMEDIATE`) 안에서 대상 rowid를 고르고 그 자리에서
    바로 `claimed_by`를 써 버리므로, 다른 프로세스가 같은 rowid를 동시에
    고를 수 없다(sqlite 쓰기 트랜잭션은 직렬화됨) — 이게 5차의 핵심: 여러
    작업자가 "누가 먼저 저장하느냐"로 경쟁하던 걸 "누가 먼저 이 원자적
    UPDATE를 커밋하느냐"로 바꿔 애초에 같은 키워드를 두 곳이 못 뽑게 한다.
    """
    now_epoch = now_epoch if now_epoch is not None else time.time()
    cutoff = now_epoch - claim_ttl_sec
    n = max(0, int(n))
    if n == 0:
        return []

    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            """
            SELECT rowid AS rid, keyword_norm, keyword, item_json, tier, sort_key
            FROM exposure_queue
            WHERE brand = ? AND done_at IS NULL AND (claimed_by IS NULL OR claimed_at < ?)
            ORDER BY tier ASC, sort_key ASC
            LIMIT ?
            """,
            (brand, cutoff, n),
        ).fetchall()
        if not rows:
            conn.execute("COMMIT")
            return []
        rowids = [r["rid"] for r in rows]
        placeholders = ", ".join("?" for _ in rowids)
        conn.execute(
            f"UPDATE exposure_queue SET claimed_by = ?, claimed_at = ? WHERE rowid IN ({placeholders})",
            (worker_id, now_epoch, *rowids),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    out: list[dict] = []
    for r in rows:
        try:
            item = json.loads(r["item_json"])
        except Exception:
            item = {"keyword": r["keyword"]}
        item.setdefault("keyword", r["keyword"])
        out.append(item)
    return out


def claim_specific(
    conn: sqlite3.Connection,
    brand: str,
    keyword_norm: str,
    keyword: str,
    item: dict,
    worker_id: str,
    now_epoch: float | None = None,
    claim_ttl_sec: float = CLAIM_TTL_SECONDS,
) -> bool:
    """특정 키워드 하나를 선점한다(없으면 최우선 등급으로 새로 넣고 선점) —
    2단계 확인 대기(`due_pending`) 재확인처럼 "지금 이 키워드를 반드시
    검사해야" 할 때 쓴다. 다른 작업자가 이미(만료 전) 선점 중이면 `False`."""
    now_epoch = now_epoch if now_epoch is not None else time.time()
    cutoff = now_epoch - claim_ttl_sec

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO exposure_queue
                (brand, keyword_norm, keyword, item_json, tier, sort_key, enqueued_at,
                 claimed_by, claimed_at, done_at)
            VALUES (?, ?, ?, ?, 0, -1e18, ?, NULL, NULL, NULL)
            ON CONFLICT(brand, keyword_norm) DO NOTHING
            """,
            (brand, keyword_norm, keyword, _item_json(item), now_epoch),
        )
        cur = conn.execute(
            """
            UPDATE exposure_queue SET claimed_by = ?, claimed_at = ?
            WHERE brand = ? AND keyword_norm = ? AND done_at IS NULL
              AND (claimed_by IS NULL OR claimed_at < ?)
            """,
            (worker_id, now_epoch, brand, keyword_norm, cutoff),
        )
        ok = cur.rowcount > 0
        conn.execute("COMMIT")
        return ok
    except Exception:
        conn.execute("ROLLBACK")
        raise


def mark_done(conn: sqlite3.Connection, brand: str, keyword_norm: str, now_epoch: float | None = None) -> None:
    """검사를 정상적으로 마쳤을 때 부른다 — 다음 갱신 때까지(또는 다시 등급에
    들 때까지) 이 키워드가 배치에서 빠진다."""
    now_epoch = now_epoch if now_epoch is not None else time.time()
    conn.execute(
        "UPDATE exposure_queue SET done_at = ? WHERE brand = ? AND keyword_norm = ?",
        (now_epoch, brand, keyword_norm),
    )


def release_claim(conn: sqlite3.Connection, brand: str, keyword_norm: str) -> None:
    """검사가 실패(예외)했을 때 선점을 풀어 다른 작업자가 바로 다시 집을 수
    있게 한다."""
    conn.execute(
        "UPDATE exposure_queue SET claimed_by = NULL, claimed_at = NULL "
        "WHERE brand = ? AND keyword_norm = ? AND done_at IS NULL",
        (brand, keyword_norm),
    )


def queue_counts(conn: sqlite3.Connection, brand: str, now_epoch: float | None = None) -> dict[str, int]:
    """보고서용 — 현재 이 브랜드 큐 상태(대기/선점 중/완료) 개수."""
    now_epoch = now_epoch if now_epoch is not None else time.time()
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN done_at IS NULL AND claimed_by IS NULL THEN 1 ELSE 0 END) AS waiting,
          SUM(CASE WHEN done_at IS NULL AND claimed_by IS NOT NULL THEN 1 ELSE 0 END) AS claimed,
          SUM(CASE WHEN done_at IS NOT NULL THEN 1 ELSE 0 END) AS done,
          COUNT(*) AS total
        FROM exposure_queue WHERE brand = ?
        """,
        (brand,),
    ).fetchone()
    return {
        "waiting": int(row["waiting"] or 0),
        "claimed": int(row["claimed"] or 0),
        "done": int(row["done"] or 0),
        "total": int(row["total"] or 0),
    }


__all__ = [
    "CLAIM_TTL_SECONDS",
    "upsert_candidates",
    "last_refreshed_at",
    "claim_batch",
    "claim_specific",
    "mark_done",
    "release_claim",
    "queue_counts",
]
