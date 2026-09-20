"""실행기 내장 예약(스케줄러) + 감시견 테스트. 가짜 시계·가짜 접수 함수만 쓴다."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from v2r.command.parser import parse_korean_command
from v2r.engine import schedule as sched
from v2r.engine import worker
from v2r.engine.scheduler import KST
from v2r.store.db import now_iso
from tests.test_engine import make_runtime

SCHEDULE_YAML = """
catch_up_minutes: null
entries:
  - name: 아침 일상 글
    time: "09:00"
    days: daily
    enabled: true
    command: 자사 카페 일상 글 카페별 100건 실제 발행 댓글 랜덤
  - name: 평일만
    time: "10:00"
    days: weekdays
    enabled: true
    command: 끊긴 작업 점검
  - name: 꺼둔 예약
    time: "11:00"
    days: daily
    enabled: false
    command: 상태
"""


def make_rt(tmp_path):
    """임시 config/ 를 가진 Runtime."""
    rt = make_runtime(tmp_path)
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "schedule.yaml").write_text(SCHEDULE_YAML, encoding="utf-8")
    rt.settings.config_dir = cfg
    return rt


class FakeHandler:
    """worker.handle_text 대역."""

    def __init__(self, job_id: int | None = 1, ok: bool = True) -> None:
        self.calls: list[str] = []
        self.job_id = job_id
        self.ok = ok

    def __call__(self, rt, text):
        self.calls.append(text)
        return {"ok": self.ok, "job_id": self.job_id, "description": text}


# --------------------------------------------------------------------
# 예약표 읽기
# --------------------------------------------------------------------
def test_load_schedule_reads_entries(tmp_path):
    rt = make_rt(tmp_path)
    entries = sched.load_schedule(rt)
    assert [e.name for e in entries] == ["아침 일상 글", "평일만", "꺼둔 예약"]
    assert entries[0].weekdays == set(range(7))
    assert entries[1].weekdays == {0, 1, 2, 3, 4}
    assert entries[2].enabled is False
    rt.close()


def test_real_schedule_yaml_has_0900_entry():
    """저장소의 실제 예약표에 09:00 일상 글 예약이 들어 있어야 한다(장애 재발 방지)."""
    from v2r.config import get_settings

    entries = sched.load_schedule(path=get_settings().config_dir / "schedule.yaml")
    names = {e.name for e in entries}
    nine = [e for e in entries if e.time == "09:00" and "일상" in e.command]
    assert nine and nine[0].enabled
    assert "끊긴 작업 점검" in names and "GPT 세션 점검" in names


# --------------------------------------------------------------------
# due / 따라잡기 / 두 번 쏘지 않기
# --------------------------------------------------------------------
def test_due_only_after_time(tmp_path):
    rt = make_rt(tmp_path)
    entries = sched.load_schedule(rt)
    before = datetime(2026, 9, 21, 8, 59, tzinfo=KST)  # 월요일
    after = datetime(2026, 9, 21, 9, 0, tzinfo=KST)
    assert sched.due_entries(entries, before, {}) == []
    assert [e.name for e in sched.due_entries(entries, after, {})] == ["아침 일상 글"]
    rt.close()


def test_due_catch_up_same_day_and_not_next_day(tmp_path):
    """실행기가 꺼져 있었어도 그날 안이면 늦게라도 쏜다. 다음 날 새벽엔 안 쏜다."""
    rt = make_rt(tmp_path)
    entries = sched.load_schedule(rt)
    late = datetime(2026, 9, 21, 23, 30, tzinfo=KST)
    assert "아침 일상 글" in [e.name for e in sched.due_entries(entries, late, {})]
    next_dawn = datetime(2026, 9, 22, 1, 0, tzinfo=KST)
    assert [e.name for e in sched.due_entries(entries, next_dawn, {})] == []
    rt.close()


def test_catch_up_minutes_limits_window(tmp_path):
    entry = sched.ScheduleEntry(
        name="짧은 창", time="09:00", command="상태", catch_up_minutes=30
    )
    entry.weekdays = set(range(7))
    assert sched.due_entries([entry], datetime(2026, 9, 21, 9, 20, tzinfo=KST), {})
    assert not sched.due_entries([entry], datetime(2026, 9, 21, 9, 40, tzinfo=KST), {})


def test_weekdays_entry_skips_weekend(tmp_path):
    rt = make_rt(tmp_path)
    entries = sched.load_schedule(rt)
    sunday = datetime(2026, 9, 20, 10, 30, tzinfo=KST)
    monday = datetime(2026, 9, 21, 10, 30, tzinfo=KST)
    assert "평일만" not in [e.name for e in sched.due_entries(entries, sunday, {})]
    assert "평일만" in [e.name for e in sched.due_entries(entries, monday, {})]
    rt.close()


def test_tick_does_not_double_fire_across_restart(tmp_path):
    """상태 파일이 남으므로 실행기를 껐다 켜도 같은 날 두 번 쏘지 않는다."""
    from v2r.command.spec import TaskSpec

    rt = make_rt(tmp_path)
    # 감시견이 "안 돌아갔다"고 오해하지 않게, 접수된 작업을 끝난 상태로 둔다
    done_id = rt.jobs.enqueue(TaskSpec(task="status"), "done-key")
    rt.jobs.finish(done_id, "done")
    handler = FakeHandler(job_id=done_id)
    now = datetime(2026, 9, 21, 9, 1, tzinfo=KST)
    out1 = sched.tick(rt, now, handle_text=handler)
    assert [f["name"] for f in out1["fired"]] == ["아침 일상 글"]
    out2 = sched.tick(rt, now + timedelta(minutes=1), handle_text=handler)
    assert out2["fired"] == []
    # "재시작" — 상태를 파일에서 다시 읽는다
    assert sched.load_state(rt)["last_fired"]["아침 일상 글"] == "2026-09-21"
    out3 = sched.tick(rt, now + timedelta(hours=3), handle_text=handler)
    assert "아침 일상 글" not in [f["name"] for f in out3["fired"]]
    assert handler.calls.count("자사 카페 일상 글 카페별 100건 실제 발행 댓글 랜덤") == 1
    rt.close()


def test_tick_fires_missed_slot_when_executor_comes_back(tmp_path):
    rt = make_rt(tmp_path)
    handler = FakeHandler()
    out = sched.tick(rt, datetime(2026, 9, 21, 14, 5, tzinfo=KST), handle_text=handler)
    assert [f["name"] for f in out["fired"]] == ["아침 일상 글", "평일만"]
    rt.close()


def test_fire_clears_stop_flag(tmp_path):
    """옛 `중지`가 남아 있어도 예약은 나가야 한다."""
    rt = make_rt(tmp_path)
    worker.request_stop(rt)
    assert worker.stop_requested(rt)
    handler = FakeHandler()
    sched.tick(rt, datetime(2026, 9, 21, 9, 1, tzinfo=KST), handle_text=handler)
    assert not worker.stop_requested(rt)
    assert not worker.stop_flag_path(rt).exists()
    rt.close()


# --------------------------------------------------------------------
# 감시견
# --------------------------------------------------------------------
class RecordingChannel:
    name = "test"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, chat_id, text):
        self.sent.append(text)
        return True

    def broadcast(self, text):
        self.sent.append(text)
        return 1

    def broadcast_document(self, path, caption=""):
        self.sent.append(f"[문서] {path} :: {caption}")
        return 1

    def poll(self):
        return []


def test_watchdog_reenqueues_when_job_stays_queued(tmp_path):
    """5분 뒤에도 queued 그대로면 알리고 한 번 다시 쏜다."""
    rt = make_rt(tmp_path)
    channel = RecordingChannel()
    rt._channels = [channel]
    handler = FakeHandler(job_id=None)  # 큐에 없는 작업 = 움직이지 않음
    start = datetime(2026, 9, 21, 9, 1, tzinfo=KST)
    sched.tick(rt, start, handle_text=handler)
    assert len(handler.calls) == 1

    # 3분 뒤 — 아직 기다린다
    sched.tick(rt, start + timedelta(minutes=3), handle_text=handler)
    assert len(handler.calls) == 1

    # 6분 뒤 — 자동 재시도
    out = sched.tick(rt, start + timedelta(minutes=6), handle_text=handler)
    assert [a["action"] for a in out["watchdog"]] == ["retry"]
    assert len(handler.calls) == 2
    assert any("예약 실패" in t and "자동 재시도" in t for t in channel.sent)

    # 다시 6분 뒤 — 이번엔 사람에게 알리고 포기
    out2 = sched.tick(rt, start + timedelta(minutes=12), handle_text=handler)
    assert [a["action"] for a in out2["watchdog"]] == ["failed"]
    assert len(handler.calls) == 2
    assert any("자동 복구 실패" in t for t in channel.sent)
    rt.close()


def test_watchdog_quiet_when_job_runs(tmp_path):
    rt = make_rt(tmp_path)
    channel = RecordingChannel()
    rt._channels = [channel]
    from v2r.command.spec import TaskSpec

    job_id = rt.jobs.enqueue(TaskSpec(task="status"), "k1")
    rt.jobs.acquire("me")  # → running

    handler = FakeHandler(job_id=job_id)
    start = datetime(2026, 9, 21, 9, 1, tzinfo=KST)
    sched.tick(rt, start, handle_text=handler)
    out = sched.tick(rt, start + timedelta(minutes=10), handle_text=handler)
    assert [a["action"] for a in out["watchdog"]] == ["ok"]
    assert not any("예약 실패" in t for t in channel.sent)
    rt.close()


# --------------------------------------------------------------------
# 심장박동 / 보고 / 명령
# --------------------------------------------------------------------
def test_heartbeat_written_on_tick(tmp_path):
    rt = make_rt(tmp_path)
    now = datetime(2026, 9, 21, 9, 1, tzinfo=KST)
    sched.tick(rt, now, handle_text=FakeHandler())
    path = sched.heartbeat_path(rt)
    assert path.exists()
    assert sched.heartbeat_age_seconds(rt, now) == pytest.approx(0, abs=2)
    assert sched.heartbeat_age_seconds(rt, now + timedelta(minutes=10)) > 300
    rt.close()


def test_heartbeat_missing_is_stale(tmp_path):
    rt = make_rt(tmp_path)
    assert sched.heartbeat_age_seconds(rt) is None
    assert sched.heartbeat_is_stale(rt) is True
    rt.close()


def test_schedule_report_shows_next_and_last(tmp_path):
    rt = make_rt(tmp_path)
    now = datetime(2026, 9, 21, 9, 1, tzinfo=KST)
    sched.tick(rt, now, handle_text=FakeHandler())
    text = sched.schedule_report(rt, now)
    assert "아침 일상 글" in text
    assert "마지막 실행: 2026-09-21" in text
    assert "다음 실행" in text
    rt.close()


def test_run_now_fires_named_entry(tmp_path):
    rt = make_rt(tmp_path)
    handler = FakeHandler(job_id=7)
    out = sched.run_now(rt, "아침 일상 글", handle_text=handler)
    assert out["ok"] and out["job_id"] == 7
    assert handler.calls == ["자사 카페 일상 글 카페별 100건 실제 발행 댓글 랜덤"]
    bad = sched.run_now(rt, "없는 예약", handle_text=handler)
    assert bad["ok"] is False and "그런 예약이 없습니다" in bad["message"]
    rt.close()


@pytest.mark.parametrize(
    "text,task,name",
    [
        ("예약 목록", "schedule_list", ""),
        ("예약 현황 알려줘", "schedule_list", ""),
        ("예약 지금 실행 아침 일상 글", "schedule_run", "아침 일상 글"),
        ("예약 실행 GPT 세션 점검", "schedule_run", "GPT 세션 점검"),
        ("감시 상태", "monitor_status", ""),
    ],
)
def test_parser_routes_schedule_commands(text, task, name):
    spec = parse_korean_command(text)
    assert spec is not None
    assert spec.task == task
    assert spec.schedule_name == name


def test_dispatch_schedule_tasks(tmp_path):
    """텔레그램 명령이 dispatch까지 이어진다."""
    from v2r.command.spec import TaskSpec

    rt = make_rt(tmp_path)
    job_id = rt.jobs.enqueue(TaskSpec(task="schedule_list"), "s1")
    out = worker.dispatch(rt, rt.jobs.get(job_id))
    assert out["ok"] and "아침 일상 글" in out["report"]

    job_id2 = rt.jobs.enqueue(
        TaskSpec(task="monitor_status"), "s2"
    )
    out2 = worker.dispatch(rt, rt.jobs.get(job_id2))
    assert out2["ok"] and "감시 상태" in out2["report"]
    rt.close()


def test_health_report_mentions_heartbeat_and_schedule(tmp_path):
    rt = make_rt(tmp_path)

    class FakeClient:
        def session_report(self):
            return {
                "session_file": "x",
                "has_token": False,
                "has_refresh_cookie": False,
                "logins_24h": 0,
                "login_budget": 15,
            }

    rt._client = FakeClient()
    now = datetime(2026, 9, 21, 9, 1, tzinfo=KST)
    sched.tick(rt, now, handle_text=FakeHandler())
    text = sched.health_report(rt, now)
    assert "실행기 심장박동" in text
    assert "예약 목록" in text
    assert "액세스 토큰" in text
    rt.close()


def test_now_iso_is_parseable():
    """상태 파일 비교에 쓰는 시각 형식이 계속 읽히는지 확인."""
    assert datetime.fromisoformat(now_iso()) is not None
