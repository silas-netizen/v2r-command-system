"""실행기 단일 실행 잠금 · 리스 소유자 · 동시 발행 금지 테스트.

장애 2026-09-20 B: publish_daily 65와 67이 같은 PC에서 나란히 돌았다.
리스 소유자가 호스트 이름뿐이어서 두 번째 실행기가 같은 리스를 자기 것으로 봤다.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta

from v2r.command.spec import TaskSpec
from v2r.engine import lock as lock_mod
from v2r.engine import worker
from v2r.engine.scheduler import KST
from tests.test_engine import make_runtime

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=KST)


def _add_job(rt, *, task="publish_daily", status="running", owner=None, lease_min=15):
    job_id = rt.jobs.enqueue(TaskSpec(task=task, notes="일상 글"), f"key-{task}-{status}-{owner}")
    until = (datetime.now(KST) + timedelta(minutes=lease_min)).isoformat(timespec="seconds")
    rt.conn.execute(
        "UPDATE jobs SET status = ?, lease_owner = ?, lease_until = ? WHERE id = ?",
        (status, owner, until if owner else None, job_id),
    )
    return job_id


# --------------------------------------------------------------------
# 1) 리스 소유자 이름
# --------------------------------------------------------------------
def test_default_owner_has_host_and_pid():
    owner = worker.default_owner()
    host, _, pid = owner.rpartition(":")
    assert host == socket.gethostname()
    assert pid == str(os.getpid())


def test_acquire_refuses_lease_held_by_another_owner(tmp_path):
    """같은 PC의 두 번째 실행기는 남의 리스를 가져가지 못한다."""
    rt = make_runtime(tmp_path)
    rt.jobs.enqueue(TaskSpec(task="publish_daily", notes="일상 글"), "key-1")
    first = f"{socket.gethostname()}:{os.getpid()}"
    second = f"{socket.gethostname()}:{os.getpid() + 1}"  # 살아 있다고 볼 수 없는 번호여도
    assert rt.jobs.acquire(first) is not None
    assert rt.jobs.acquire(second) is None  # 리스는 첫 번째 것
    assert rt.jobs.lease_info()["owner"] == first
    rt.close()


def test_dead_owner_lease_can_be_taken_over(tmp_path):
    """앞 프로세스가 죽었으면 리스를 이어받는다(실행기 재시작)."""
    rt = make_runtime(tmp_path)
    rt.jobs.enqueue(TaskSpec(task="publish_daily", notes="일상 글"), "key-1")
    dead = f"{socket.gethostname()}:999999"
    rt.jobs._take_lease(dead, lease_seconds=900)
    assert rt.jobs.acquire(worker.default_owner()) is not None
    rt.close()


# --------------------------------------------------------------------
# 2) serve 단일 실행 잠금
# --------------------------------------------------------------------
def test_second_serve_is_blocked_by_lock(tmp_path):
    rt = make_runtime(tmp_path)
    held = worker.ensure_single_serve(rt)
    assert held is not None and held.held
    assert lock_mod.holder_pid(rt) == os.getpid()

    second = worker.ensure_single_serve(rt)  # 두 번째 실행기
    assert second is None
    events = rt.events.recent(limit=10)
    assert any("serve 중복 실행 차단" in e["message"] for e in events)

    held.release()
    again = worker.ensure_single_serve(rt)  # 첫 번째가 꺼지면 다시 켤 수 있다
    assert again is not None
    again.release()
    rt.close()


def test_lock_report_names_holder_pid(tmp_path):
    rt = make_runtime(tmp_path)
    assert "잡은 프로세스 없음" in lock_mod.lock_report(rt)
    with lock_mod.ServeLock(lock_mod.lock_path(rt)) as held:
        assert held.held
        assert f"프로세스 {os.getpid()} 번" in lock_mod.lock_report(rt)
    rt.close()


def test_health_report_shows_lock_line(tmp_path):
    from v2r.engine.schedule import health_report

    rt = make_runtime(tmp_path)
    with lock_mod.ServeLock(lock_mod.lock_path(rt)):
        text = health_report(rt, NOW)
    assert "단일 실행기 잠금" in text
    assert str(os.getpid()) in text
    rt.close()


def test_pid_alive_knows_self_and_unused():
    assert lock_mod.pid_alive(os.getpid()) is True
    assert lock_mod.pid_alive(0) is False


# --------------------------------------------------------------------
# 3) 동시 발행 금지 (겹겹이 막는 마지막 겹)
# --------------------------------------------------------------------
def test_wait_for_other_publish_waits_then_proceeds(tmp_path):
    """다른 실행기가 발행 중이면 끝날 때까지 기다린다."""
    rt = make_runtime(tmp_path)
    other = _add_job(rt, owner="다른PC:4242")
    mine = _add_job(rt, status="running", owner=worker.default_owner())
    naps: list[float] = []

    def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) == 2:  # 두 번 쉰 뒤 상대가 끝났다
            rt.conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (other,))

    clock = [0.0]

    def fake_now():
        clock[0] += 30.0
        return clock[0]

    out = worker.wait_for_other_publish(
        rt, mine, worker.default_owner(), sleep=fake_sleep, now_fn=fake_now
    )
    assert len(naps) == 2 and out["holder"] is None and out["timed_out"] is False
    messages = [e["message"] for e in rt.events.recent(mine, limit=10)]
    assert any("기다립니다" in m for m in messages)
    rt.close()


def test_wait_for_other_publish_returns_at_once_when_alone(tmp_path):
    rt = make_runtime(tmp_path)
    mine = _add_job(rt, status="running", owner=worker.default_owner())
    naps: list[float] = []
    out = worker.wait_for_other_publish(
        rt, mine, worker.default_owner(), sleep=naps.append
    )
    assert naps == [] and out["waited_s"] == 0.0
    rt.close()


def test_wait_for_other_publish_ignores_own_and_dead_leases(tmp_path):
    """내 작업과, 리스가 끊긴 남의 작업은 기다림의 이유가 아니다."""
    rt = make_runtime(tmp_path)
    mine = _add_job(rt, status="running", owner=worker.default_owner())
    stale = _add_job(rt, owner="다른PC:4242", lease_min=-10)  # 리스 만료
    assert rt.jobs.live_lease_holder(rt.jobs.get(stale), worker.default_owner()) is None
    naps: list[float] = []
    worker.wait_for_other_publish(rt, mine, worker.default_owner(), sleep=naps.append)
    assert naps == []
    rt.close()


def test_wait_for_other_publish_gives_up_after_limit(tmp_path):
    """상대가 영영 안 끝나도 영원히 멈춰 있지는 않는다(경고를 남기고 간다)."""
    rt = make_runtime(tmp_path)
    _add_job(rt, owner="다른PC:4242")
    mine = _add_job(rt, status="running", owner=worker.default_owner())
    clock = [0.0]

    def fake_now():
        clock[0] += 600.0
        return clock[0]

    out = worker.wait_for_other_publish(
        rt,
        mine,
        worker.default_owner(),
        max_wait_s=1200,
        sleep=lambda _s: None,
        now_fn=fake_now,
    )
    assert out["timed_out"] is True and out["holder"] == "다른PC:4242"
    assert any("끝나지 않습니다" in e["message"] for e in rt.events.recent(mine, limit=10))
    rt.close()


def test_running_jobs_filters_by_task_and_id(tmp_path):
    rt = make_runtime(tmp_path)
    mine = _add_job(rt, status="running", owner=worker.default_owner())
    other = _add_job(rt, owner="다른PC:4242")
    _add_job(rt, task="status", owner="다른PC:4242")
    rows = rt.jobs.running_jobs(exclude_id=mine, task_prefix="publish_")
    assert [int(r["id"]) for r in rows] == [other]
    rt.close()


def test_legacy_hostname_owner_is_treated_as_dead():
    import socket

    from v2r.store import jobs as jobs_mod

    assert jobs_mod._owner_is_dead(socket.gethostname()) is True
    assert jobs_mod._owner_is_dead("other-host") is False
