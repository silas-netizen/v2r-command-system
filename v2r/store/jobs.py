"""작업 큐 저장소."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from v2r.command.spec import TaskSpec
from v2r.store.db import KST, now_iso

OPEN_STATUSES = ("queued", "running")
STATUSES = ("queued", "running", "done", "failed", "uncertain", "cancelled")

#: 실행기가 죽어도 **이어서** 돌릴 수 있는 작업(남은 건수를 다시 계산하는
#: idempotent 작업). 이 목록에 없는 작업은 재시작 시 uncertain 으로만 정리한다
#: (사고 2026-09-22: publish_daily 가 uncertain 으로만 정리되고 아무도 이어받지
#: 않아 1시간 발행이 멈췄다).
RESUMABLE_TASKS = frozenset({"publish_daily", "publish_batch"})


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
    try:
        me = socket.gethostname()
    except Exception:  # noqa: BLE001
        return False
    if not sep or not pid_text.isdigit():
        # 옛 형식(호스트명만): 같은 PC의 이전 실행기다. 지금 실행기는 반드시 host:pid를 쓰므로
        # 이 소유자는 죽은 것으로 보고 이어받는다(2026-09-20 실측: 큐가 영영 안 잡히던 원인).
        return text == me
    if host != me:
        return False
    from v2r.engine.lock import pid_alive

    return not pid_alive(int(pid_text))


#: `long` 줄 작업의 기본 리스 시간(초) — 발행과 무관하게 돌되, 잠깐의
#: 재시작에 죽은 작업으로 잘못 정리되지 않도록 넉넉히 잡는다(사고 2026-09-24:
#: keyword_relevance_rescore_legacy 가 main 줄에 서서 아침 발행을 52분 막았다).
LONG_LEASE_SECONDS = 12 * 3600


def _scope_for(task: str) -> str:
    """그 작업을 어느 줄(main/light/long)에서 잡아야 하는가.

    가벼운 조회 작업은 긴 발행 작업 뒤에 줄 서면 안 된다(장애 2026-09-20 C).
    사이드카 스레드가 `light` 줄만 집어간다. 오래 걸리는(몇 시간짜리) 배치
    작업도 마찬가지 이유로 `long` 줄로 뺀다(장애 2026-09-24) — 별도 사이드카
    스레드가 발행(main)과 무관하게 처리한다.
    """
    try:
        from v2r.engine.sidecar import LIGHT_TASKS, LONG_TASKS

        text = str(task)
        if text in LONG_TASKS:
            return "long"
        if text in LIGHT_TASKS:
            return "light"
        return "main"
    except Exception:  # noqa: BLE001 - 순환 참조·초기화 실패로 큐가 막히면 안 된다
        return "main"


class JobStore:
    """jobs + executor_lease 조작."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def enqueue(self, spec: TaskSpec, idem_key: str, lease_scope: str | None = None) -> int:
        """작업 등록. 같은 키가 있으면 기존 id 반환."""
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE idem_key = ?", (idem_key,)
        ).fetchone()
        if row:
            return int(row["id"])
        ts = now_iso()
        scope = lease_scope or _scope_for(spec.task)
        cur = self.conn.execute(
            "INSERT INTO jobs (idem_key, task, spec_json, status, lease_scope,"
            " created_at, updated_at) VALUES (?, ?, ?, 'queued', ?, ?, ?)",
            (idem_key, spec.task, spec.to_json(), scope, ts, ts),
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

    def reap_stale_running(
        self, error: str = "실행기 중단", resumable_tasks: frozenset[str] | None = None
    ) -> list[dict]:
        """리스가 만료된 running 작업을 정리한다.

        재개 가능한 작업(``resumable_tasks``, 기본 `RESUMABLE_TASKS`)은
        **queued로 되돌려 이어서 실행**한다. 그 외는 지금까지처럼 uncertain으로
        정리한다(사람 확인이 필요할 수 있는 작업).

        돌려주는 값은 ``[{"id", "task", "action": "queued"|"uncertain"}, ...]``.
        총 처리 건수는 ``len(...)``으로 잰다(기존 int 반환과 같은 뜻).
        """
        resumable = RESUMABLE_TASKS if resumable_tasks is None else resumable_tasks
        rows = self.conn.execute(
            "SELECT id, task, lease_until FROM jobs WHERE status = 'running'"
        ).fetchall()
        stale = []
        for row in rows:
            until = _parse(row["lease_until"])
            if until is None or until <= datetime.now(KST):
                stale.append((int(row["id"]), str(row["task"] or "")))
        if not stale:
            return []
        ts = now_iso()
        out: list[dict] = []
        for job_id, task in stale:
            if task in resumable:
                self.conn.execute(
                    "UPDATE jobs SET status = 'queued', lease_owner = NULL,"
                    " lease_until = NULL, updated_at = ? WHERE id = ?",
                    (ts, job_id),
                )
                out.append({"id": job_id, "task": task, "action": "queued"})
            else:
                self.conn.execute(
                    "UPDATE jobs SET status = 'uncertain', error = ?, lease_owner = NULL,"
                    " lease_until = NULL, updated_at = ? WHERE id = ?",
                    (error, ts, job_id),
                )
                out.append({"id": job_id, "task": task, "action": "uncertain"})
        return out

    def revive_to_queued(self, job_id: int) -> bool:
        """uncertain(또는 멈춘) 작업을 queued로 되살린다(같은 작업을 이어서 실행)."""
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'queued', error = NULL, lease_owner = NULL,"
            " lease_until = NULL, updated_at = ? WHERE id = ?",
            (now_iso(), job_id),
        )
        return cur.rowcount > 0

    def requeue_running(self, job_id: int) -> bool:
        """리스가 끊긴 running 작업 1건을 (원자적으로) queued로 되돌린다.

        `status='running'`이고 리스가 비었거나 만료된 경우에만 바뀐다 —
        이미 다른 곳에서 처리됐으면 아무 일도 하지 않는다(중복 실행 방지).
        """
        now_s = datetime.now(KST).isoformat(timespec="seconds")
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'queued', lease_owner = NULL, lease_until = NULL,"
            " updated_at = ? WHERE id = ? AND status = 'running'"
            " AND (lease_until IS NULL OR lease_until <= ?)",
            (now_iso(), job_id, now_s),
        )
        return cur.rowcount > 0

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

    def acquire_scope(
        self, owner: str, scope: str, lease_seconds: int = 300
    ) -> sqlite3.Row | None:
        """`scope`(light 또는 long) 줄의 가장 오래된 queued 1건을 running으로.

        실행기 리스(executor_lease)를 **쓰지 않는다**. 그래야 긴 발행 작업이
        리스를 쥐고 있는 동안에도 사이드카가 가벼운·장시간 작업을 바로 돌릴
        수 있다(장애 2026-09-20 C, 2026-09-24).
        """
        until = (datetime.now(KST) + timedelta(seconds=lease_seconds)).isoformat(
            timespec="seconds"
        )
        ts = now_iso()
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE status = 'queued' AND lease_scope = ?"
            " ORDER BY id LIMIT 1",
            (scope,),
        ).fetchone()
        if row is None:
            return None
        job_id = int(row["id"])
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'running', lease_owner = ?, lease_until = ?,"
            " updated_at = ? WHERE id = ? AND status = 'queued'",
            (owner, until, ts, job_id),
        )
        if cur.rowcount <= 0:
            return None  # 그 사이 누가 집어갔다
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def acquire_light(self, owner: str, lease_seconds: int = 300) -> sqlite3.Row | None:
        """`light` 줄의 가장 오래된 queued 1건을 running으로 (하위 호환 이름)."""
        return self.acquire_scope(owner, "light", lease_seconds)

    def acquire(
        self, owner: str, lease_seconds: int = 900, scope: str | None = None
    ) -> sqlite3.Row | None:
        """리스를 잡고 가장 오래된 queued 1건을 running으로.

        `scope="main"` 이면 사이드카 몫(`light`/`long`)은 건너뛴다. 기본값(None)은
        예전처럼 줄을 가리지 않는다(사이드카 없이 도는 한 번짜리 실행용).
        """
        if scope in ("light", "long"):
            return self.acquire_scope(owner, scope, lease_seconds)
        if not self._take_lease(owner, lease_seconds):
            return None
        if scope is None:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' AND lease_scope = ?"
                " ORDER BY id LIMIT 1",
                (scope,),
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
        """만료된, 또는 소유자 프로세스가 이미 죽은 실행기 리스를 푼다.

        시간만 보면 리스 시간(기본 900초)이 남아 있는 동안은 재시작 후에도
        최대 15분을 공백으로 날린다(장애 2026-09-24) — 실행기가 재시작될 때
        이전 소유자(``호스트:pid``)가 **같은 호스트**에서 이미 죽었으면
        시간과 무관하게 즉시 회수한다.
        """
        row = self.conn.execute(
            "SELECT owner, until FROM executor_lease WHERE id = 1"
        ).fetchone()
        if row is None or not row["owner"]:
            return False
        if _owner_is_dead(row["owner"]):
            self.conn.execute(
                "UPDATE executor_lease SET owner = NULL, until = NULL, updated_at = ?"
                " WHERE id = 1",
                (now_iso(),),
            )
            return True
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
