"""작업 큐 저장소."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from v2r.command.spec import TaskSpec
from v2r.store.db import KST, now_iso

OPEN_STATUSES = ("queued", "running")
STATUSES = ("queued", "running", "done", "failed", "uncertain", "cancelled")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _owner_is_dead(owner: str | None) -> bool:
    """`호스트:pid` 소유자가 이미 죽은 프로세스인가(같은 PC일 때만 판단).

    소유자에 프로세스 번호를 붙이면서(장애 2026-09-20 B) 실행기를 껐다 켜면
    이름이 달라진다. 앞 프로세스가 죽었다면 리스를 이어받아야 멈추지 않는다.
    """
    import socket

    text = str(owner or "")
    host, sep, pid_text = text.rpartition(":")
    if not sep or not pid_text.isdigit():
        return False
    try:
        if host != socket.gethostname():
            return False
    except Exception:  # noqa: BLE001
        return False
    from v2r.engine.lock import pid_alive

    return not pid_alive(int(pid_text))


class JobStore:
    """jobs + executor_lease 조작."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def enqueue(self, spec: TaskSpec, idem_key: str) -> int:
        """작업 등록. 같은 키가 있으면 기존 id 반환."""
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE idem_key = ?", (idem_key,)
        ).fetchone()
        if row:
            return int(row["id"])
        ts = now_iso()
        cur = self.conn.execute(
            "INSERT INTO jobs (idem_key, task, spec_json, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'queued', ?, ?)",
            (idem_key, spec.task, spec.to_json(), ts, ts),
        )
        return int(cur.lastrowid)

    def find_by_idem(self, idem_key: str) -> dict | None:
        """멱등 키로 작업 1건 조회(가장 최근)."""
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE idem_key = ? ORDER BY id DESC LIMIT 1",
            (idem_key,),
        ).fetchone()
        return dict(row) if row else None

    def has_open_job(self, idem_key: str) -> bool:
        """같은 키로 아직 끝나지 않은(queued/running) 작업이 있는가."""
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE idem_key = ? AND status IN ('queued', 'running')",
            (idem_key,),
        ).fetchone()
        return row is not None

    def reap_stale_running(self, error: str = "실행기 중단") -> int:
        """리스가 만료된 running 작업을 uncertain으로 정리한다."""
        rows = self.conn.execute(
            "SELECT id, lease_until FROM jobs WHERE status = 'running'"
        ).fetchall()
        stale = []
        for row in rows:
            until = _parse(row["lease_until"])
            if until is None or until <= datetime.now(KST):
                stale.append(int(row["id"]))
        if not stale:
            return 0
        ts = now_iso()
        for job_id in stale:
            self.conn.execute(
                "UPDATE jobs SET status = 'uncertain', error = ?, lease_owner = NULL,"
                " lease_until = NULL, updated_at = ? WHERE id = ?",
                (error, ts, job_id),
            )
        return len(stale)

    # --- 리스 ---
    def _take_lease(self, owner: str, lease_seconds: int) -> bool:
        """비었거나 만료됐거나 내 것이면 리스 획득."""
        now = datetime.now(KST)
        row = self.conn.execute(
            "SELECT owner, until FROM executor_lease WHERE id = 1"
        ).fetchone()
        if row is not None:
            until = _parse(row["until"])
            if row["owner"] and row["owner"] != owner and until and until > now:
                # 살아 있는 **다른** 실행기가 잡고 있으면 절대 가져가지 않는다.
                # 죽은 프로세스가 남긴 이름이면 이어받는다.
                if not _owner_is_dead(row["owner"]):
                    return False
        until_iso = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO executor_lease (id, owner, until, created_at, updated_at)"
            " VALUES (1, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET owner = excluded.owner,"
            " until = excluded.until, updated_at = excluded.updated_at",
            (owner, until_iso, ts, ts),
        )
        return True

    def _release_lease(self, owner: str) -> None:
        self.conn.execute(
            "UPDATE executor_lease SET owner = NULL, until = NULL, updated_at = ?"
            " WHERE id = 1 AND (owner = ? OR owner IS NULL)",
            (now_iso(), owner),
        )

    def acquire(self, owner: str, lease_seconds: int = 900) -> sqlite3.Row | None:
        """리스를 잡고 가장 오래된 queued 1건을 running으로."""
        if not self._take_lease(owner, lease_seconds):
            return None
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            self._release_lease(owner)
            return None
        until = (datetime.now(KST) + timedelta(seconds=lease_seconds)).isoformat(
            timespec="seconds"
        )
        self.conn.execute(
            "UPDATE jobs SET status = 'running', lease_owner = ?, lease_until = ?,"
            " updated_at = ? WHERE id = ?",
            (owner, until, now_iso(), row["id"]),
        )
        return self.conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (row["id"],)
        ).fetchone()

    def heartbeat(self, job_id: int, owner: str, lease_seconds: int = 900) -> bool:
        """리스 연장."""
        until = (datetime.now(KST) + timedelta(seconds=lease_seconds)).isoformat(
            timespec="seconds"
        )
        ts = now_iso()
        cur = self.conn.execute(
            "UPDATE jobs SET lease_until = ?, updated_at = ?"
            " WHERE id = ? AND lease_owner = ?",
            (until, ts, job_id, owner),
        )
        self.conn.execute(
            "UPDATE executor_lease SET until = ?, updated_at = ? WHERE id = 1 AND owner = ?",
            (until, ts, owner),
        )
        return cur.rowcount > 0

    def finish(
        self,
        job_id: int,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        """작업 종료 처리 + 리스 해제."""
        if status not in STATUSES:
            raise ValueError(f"알 수 없는 상태: {status}")
        row = self.get(job_id)
        owner = (row or {}).get("lease_owner")
        self.conn.execute(
            "UPDATE jobs SET status = ?, result_json = ?, error = ?, lease_owner = NULL,"
            " lease_until = NULL, updated_at = ? WHERE id = ?",
            (
                status,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                error,
                now_iso(),
                job_id,
            ),
        )
        if owner:
            self._release_lease(str(owner))

    def cancel_open(self, owner: str) -> int:
        """대기·실행 중 작업 전부 취소."""
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'cancelled', lease_owner = NULL, lease_until = NULL,"
            " updated_at = ? WHERE status IN ('queued', 'running')",
            (now_iso(),),
        )
        self._release_lease(owner)
        return cur.rowcount

    def open_jobs(self) -> list[dict]:
        """아직 끝나지 않은(queued/running) 작업 전부. 오래된 것부터."""
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def running_jobs(self, exclude_id: int | None = None, task_prefix: str = "") -> list[dict]:
        """지금 running 인 작업들(같은 작업 두 줄 동시 실행을 막는 데 쓴다)."""
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status = 'running' ORDER BY id"
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            if exclude_id is not None and int(item["id"]) == int(exclude_id):
                continue
            if task_prefix and not str(item.get("task") or "").startswith(task_prefix):
                continue
            out.append(item)
        return out

    def live_lease_holder(self, job: dict, owner: str) -> str | None:
        """그 작업의 리스를 **다른** 실행기가 아직 살아서 잡고 있으면 그 이름."""
        holder = str(job.get("lease_owner") or "")
        if not holder or holder == owner:
            return None
        until = _parse(job.get("lease_until"))
        if until is None or until <= datetime.now(KST):
            return None
        if _owner_is_dead(holder):
            return None
        return holder

    def lease_info(self) -> dict | None:
        """실행기 리스 현황(owner, until). 없으면 None."""
        row = self.conn.execute(
            "SELECT owner, until FROM executor_lease WHERE id = 1"
        ).fetchone()
        return dict(row) if row else None

    def release_stale_lease(self) -> bool:
        """만료된 실행기 리스를 푼다. 풀었으면 True."""
        row = self.conn.execute(
            "SELECT owner, until FROM executor_lease WHERE id = 1"
        ).fetchone()
        if row is None or not row["owner"]:
            return False
        until = _parse(row["until"])
        if until is not None and until > datetime.now(KST):
            return False
        self.conn.execute(
            "UPDATE executor_lease SET owner = NULL, until = NULL, updated_at = ?"
            " WHERE id = 1",
            (now_iso(),),
        )
        return True

    def get(self, job_id: int) -> dict | None:
        """작업 1건."""
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 10) -> list[dict]:
        """최근 작업 목록."""
        rows = self.conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
