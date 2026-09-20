"""모든 작업 감시견(monitor) 테스트. 가짜 시계·가짜 알림·가짜 모델만 쓴다."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from v2r.command.parser import parse_korean_command
from v2r.command.spec import TaskSpec
from v2r.engine import monitor
from v2r.engine import worker
from v2r.engine.recovery_rules import match_rule, rule_names
from v2r.engine.scheduler import KST
from v2r.store.db import now_iso
from tests.test_engine import make_runtime
from tests.test_schedule import RecordingChannel

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=KST)


def make_rt(tmp_path):
    """가짜 채널을 단 Runtime + 임시 config/."""
    rt = make_runtime(tmp_path)
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "monitor.yaml").write_text(
        "llm_diagnosis:\n  enabled: true\n  daily_cap: 20\n"
        'daily_summary_at: "23:59"\ncache_seconds: 0\n',
        encoding="utf-8",
    )
    rt.settings.config_dir = cfg
    rt._channels = [RecordingChannel()]
    return rt


def add_job(rt, *, task="publish_daily", status="queued", ago_min=0, error=None, notes="일상 글 3개 올려줘"):
    """원하는 시각·상태의 작업 1건을 넣는다."""
    spec = TaskSpec(task=task, notes=notes)
    job_id = rt.jobs.enqueue(spec, f"key-{task}-{ago_min}-{status}-{job_seq()}")
    ts = (NOW - timedelta(minutes=ago_min)).isoformat(timespec="seconds")
    rt.conn.execute(
        "UPDATE jobs SET status = ?, created_at = ?, updated_at = ?, error = ?"
        " WHERE id = ?",
        (status, ts, ts, error, job_id),
    )
    return job_id


_seq = [0]


def job_seq() -> int:
    _seq[0] += 1
    return _seq[0]


# --------------------------------------------------------------------
# 1) 대기만 하는 작업
# --------------------------------------------------------------------
def test_queued_job_alerts_and_self_heals(tmp_path):
    rt = make_rt(tmp_path)
    worker.request_stop(rt)  # 낡은 중지 플래그가 큐를 막고 있는 상황
    job_id = add_job(rt, ago_min=6)
    out = monitor.tick(rt, NOW)
    assert [a["action"] for a in out["actions"]] == ["start_delay"]
    assert "중지 플래그 해제" in out["actions"][0]["healed"]
    assert not worker.stop_requested(rt)
    sent = rt.channels[0].sent
    assert any(f"작업 {job_id} 시작 지연" in t for t in sent)

    # 같은 알림을 되풀이하지 않는다(상태 파일에 남는다)
    rt.channels[0].sent.clear()
    monitor.tick(rt, NOW + timedelta(minutes=1))
    assert rt.channels[0].sent == []
    rt.close()


def test_queued_job_second_alert_names_reason(tmp_path):
    rt = make_rt(tmp_path)
    add_job(rt, ago_min=6)
    monitor.tick(rt, NOW)
    rt.channels[0].sent.clear()
    out = monitor.tick(rt, NOW + timedelta(minutes=6))
    assert [a["action"] for a in out["actions"]] == ["start_blocked"]
    assert any("아직 시작 못 함" in t for t in rt.channels[0].sent)
    rt.close()


def test_queued_job_quiet_before_5_minutes(tmp_path):
    rt = make_rt(tmp_path)
    add_job(rt, ago_min=2)
    out = monitor.tick(rt, NOW)
    assert out["actions"] == []
    assert rt.channels[0].sent == []
    rt.close()


def test_release_stale_lease(tmp_path):
    rt = make_rt(tmp_path)
    rt.jobs._take_lease("죽은실행기", lease_seconds=-10)
    assert rt.jobs.lease_info()["owner"] == "죽은실행기"
    assert rt.jobs.release_stale_lease() is True
    assert rt.jobs.lease_info()["owner"] is None
    # 살아 있는 리스는 건드리지 않는다
    rt.jobs._take_lease("산실행기", lease_seconds=900)
    assert rt.jobs.release_stale_lease() is False
    rt.close()


# --------------------------------------------------------------------
# 2) 멈춘 실행 중 작업
# --------------------------------------------------------------------
def test_running_job_stall_alert_then_reap_and_requeue(tmp_path):
    rt = make_rt(tmp_path)
    job_id = add_job(rt, status="running", ago_min=16)
    out = monitor.tick(rt, NOW)
    assert [a["action"] for a in out["actions"]] == ["stalled"]
    assert any("정체" in t for t in rt.channels[0].sent)

    # 30분을 넘기고 리스도 끊겼으면 실패 처리 + 같은 명령 재등록
    rt.conn.execute(
        "UPDATE jobs SET created_at = ?, updated_at = ? WHERE id = ?",
        ((NOW - timedelta(minutes=40)).isoformat(timespec="seconds"),) * 2 + (job_id,),
    )
    rt.channels[0].sent.clear()
    out2 = monitor.tick(rt, NOW)
    reaped = [a for a in out2["actions"] if a["action"] == "reaped"]
    assert reaped and reaped[0]["new_job_id"]
    assert rt.jobs.get(job_id)["status"] == "failed"
    assert rt.jobs.get(reaped[0]["new_job_id"])["status"] == "queued"
    rt.close()


def test_running_job_with_recent_event_is_not_stalled(tmp_path):
    """레이트 제한 대기 중에도 진행 이벤트가 있으면 정체로 보지 않는다."""
    rt = make_rt(tmp_path)
    job_id = add_job(rt, status="running", ago_min=40)
    rt.events.log(job_id, "info", "요청 제한 대기 중 (10분째)")
    rt.conn.execute(
        "UPDATE events SET created_at = ? WHERE job_id = ?",
        ((NOW - timedelta(minutes=2)).isoformat(timespec="seconds"), job_id),
    )
    out = monitor.tick(rt, NOW)
    assert out["actions"] == []
    rt.close()


def test_sleep_with_beat_writes_progress_event(tmp_path):
    """긴 대기는 10분마다 진행 이벤트를 남긴다(감시견이 오해하지 않게)."""
    rt = make_rt(tmp_path)
    job_id = add_job(rt, status="running")
    slept: list[float] = []
    worker._sleep_with_beat(
        1500.0, lambda: None, sleep=slept.append, rt=rt, job_id=job_id
    )
    rows = rt.events.recent(job_id, limit=10)
    assert sum(1 for r in rows if "대기 중" in r["message"]) == 2
    assert sum(slept) == pytest.approx(1500.0)
    rt.close()


def test_wait_for_rate_limit_writes_progress_event(tmp_path):
    rt = make_rt(tmp_path)
    job_id = add_job(rt, status="running")
    worker._wait_for_rate_limit(
        rt, 1300.0, lambda: None, sleep=lambda _s: None, job_id=job_id
    )
    rows = rt.events.recent(job_id, limit=10)
    assert any("요청 제한 대기 중" in r["message"] for r in rows)
    rt.close()


# --------------------------------------------------------------------
# 3) 끝난 작업 + 규칙표(Tier 0)
# --------------------------------------------------------------------
def test_failed_job_rate_limited_rule_is_tier0_and_retries(tmp_path):
    rt = make_rt(tmp_path)
    job_id = add_job(rt, status="failed", error="HTTP 429 요청 제한")
    out = monitor.tick(rt, NOW)
    act = [a for a in out["actions"] if a["action"] == "failed"][0]
    assert act["rule"] == "rate_limited"
    text = "\n".join(rt.channels[0].sent)
    assert f"작업 {job_id} 실패" in text and "Tier 0" in text
    rt.close()


def test_failed_job_network_error_requeued_once(tmp_path):
    rt = make_rt(tmp_path)
    add_job(rt, status="failed", error="connection timeout")
    out = monitor.tick(rt, NOW)
    act = [a for a in out["actions"] if a["action"] == "failed"][0]
    assert act["rule"] == "network" and act["new_job_id"]

    # 두 번째 틱에서는 다시 알리지도, 다시 등록하지도 않는다
    rt.channels[0].sent.clear()
    out2 = monitor.tick(rt, NOW + timedelta(minutes=1))
    assert [a for a in out2["actions"] if a["action"] == "failed"] == []
    assert not [t for t in rt.channels[0].sent if "실패:" in t]
    rt.close()


def test_failed_job_logic_error_is_not_retried_and_shows_resume(tmp_path):
    rt = make_rt(tmp_path)
    add_job(rt, status="failed", error="카페를 찾을 수 없습니다: 없는카페")
    out = monitor.tick(rt, NOW)
    act = [a for a in out["actions"] if a["action"] == "failed"][0]
    assert act["rule"] == "catalog_mismatch"
    assert act["new_job_id"] is None
    assert any("다시 하려면 이렇게 보내세요" in t for t in rt.channels[0].sent)
    rt.close()


def test_recovery_rules_cover_expected_kinds():
    names = rule_names()
    for expected in (
        "rate_limited",
        "login_budget",
        "token_expired",
        "network",
        "catalog_mismatch",
        "photo_shortage",
        "emoji",
        "duplicate",
    ):
        assert expected in names
    assert match_rule("하루 20회 로그인 한도").name == "login_budget"
    assert match_rule("") is None


# --------------------------------------------------------------------
# 4) Tier 1 — 모델은 딱 한 번, 상한 안에서만
# --------------------------------------------------------------------
class FakeRouter:
    """LLMRouter 대역."""

    enabled = True

    def __init__(self, payload=None) -> None:
        self.calls = 0
        self.usage = {"input_tokens": 0}
        self.payload = payload or {
            "rule": "network",
            "action": "retry",
            "explain": "일시적 네트워크 문제입니다",
        }

    def complete_json(self, purpose, system, user, max_tokens=1200):
        self.calls += 1
        self.usage["input_tokens"] += 100
        return self.payload


def test_tier0_never_calls_the_model(tmp_path):
    rt = make_rt(tmp_path)
    router = FakeRouter()
    rt._llm, rt._llm_ready = router, True
    state = monitor.load_state(rt)
    out = monitor.diagnose(rt, state, "HTTP 429 rate limit", NOW)
    assert out["tier"] == 0 and out["rule"] == "rate_limited"
    assert router.calls == 0
    rt.close()


def test_tier1_calls_model_once_per_signature_per_day(tmp_path):
    rt = make_rt(tmp_path)
    router = FakeRouter()
    rt._llm, rt._llm_ready = router, True
    state = monitor.load_state(rt)
    weird = "알 수 없는 장애 코드 XZ-31 발생"
    first = monitor.diagnose(rt, state, weird, NOW)
    assert first["tier"] == 1 and first["action"] == "retry"
    assert router.calls == 1

    again = monitor.diagnose(rt, state, "알 수 없는 장애 코드 XZ-99 발생", NOW)
    assert again.get("cached") is True  # 숫자만 다르면 같은 지문
    assert router.calls == 1

    box = state["daily"][NOW.date().isoformat()]
    assert box["llm_calls"] == 1 and box["llm_tokens"] == 100
    rt.close()


def test_tier1_respects_daily_cap(tmp_path):
    rt = make_rt(tmp_path)
    router = FakeRouter()
    rt._llm, rt._llm_ready = router, True
    state = monitor.load_state(rt)
    cfg = {**monitor.load_config(rt), "llm_daily_cap": 1}
    monitor.diagnose(rt, state, "이상한 오류 하나", NOW, cfg)
    out = monitor.diagnose(rt, state, "또 다른 이상한 오류", NOW, cfg)
    assert router.calls == 1
    assert out["tier"] == 2 and out["action"] == "human"
    rt.close()


def test_tier2_when_model_disabled(tmp_path):
    rt = make_rt(tmp_path)
    rt._llm, rt._llm_ready = None, True
    state = monitor.load_state(rt)
    out = monitor.diagnose(rt, state, "처음 보는 문구", NOW)
    assert out["tier"] == 2 and out["action"] == "human"
    rt.close()


# --------------------------------------------------------------------
# 5) 하루 요약 / 보고 / 명령
# --------------------------------------------------------------------
def test_daily_summary_sent_once(tmp_path):
    rt = make_rt(tmp_path)
    add_job(rt, status="done", ago_min=60)
    cfg = {**monitor.load_config(rt), "daily_summary_at": "08:35"}
    state = monitor.load_state(rt)
    morning = datetime(2026, 9, 21, 8, 36, tzinfo=KST)
    text = monitor.daily_summary(rt, state, morning, cfg)
    assert text and "감시 요약" in text and "모델 진단 0회" in text
    assert monitor.daily_summary(rt, state, morning, cfg) is None
    rt.close()


def test_daily_summary_not_before_time(tmp_path):
    rt = make_rt(tmp_path)
    state = monitor.load_state(rt)
    early = datetime(2026, 9, 21, 7, 0, tzinfo=KST)
    cfg = {**monitor.load_config(rt), "daily_summary_at": "08:35"}
    assert monitor.daily_summary(rt, state, early, cfg) is None
    rt.close()


def test_monitor_report_shows_tier1_usage(tmp_path):
    rt = make_rt(tmp_path)
    add_job(rt, status="running", ago_min=1)
    router = FakeRouter()
    rt._llm, rt._llm_ready = router, True
    state = monitor.load_state(rt)
    monitor.diagnose(rt, state, "처음 보는 오류 문구", NOW)
    monitor.save_state(rt, state)
    text = monitor.monitor_report(rt, NOW)
    assert "감시 상태" in text
    assert "오늘 모델 진단(Tier 1): 1회" in text
    assert "지켜보는 작업 1건" in text
    rt.close()


def test_monitor_state_survives_restart(tmp_path):
    rt = make_rt(tmp_path)
    add_job(rt, ago_min=6)
    monitor.tick(rt, NOW)
    assert monitor.state_path(rt).exists()
    saved = json.loads(monitor.state_path(rt).read_text(encoding="utf-8"))
    assert any(v.get("start_alert") for v in saved["jobs"].values())
    rt.close()


def test_watch_tick_runs_both_schedulers(tmp_path):
    """serve 루프가 부르는 진입점이 예약·감시·심장박동을 모두 돌린다."""
    from v2r.engine import schedule as sched
    from tests.test_schedule import SCHEDULE_YAML

    rt = make_rt(tmp_path)
    (rt.settings.config_dir / "schedule.yaml").write_text(SCHEDULE_YAML, encoding="utf-8")
    out = worker.watch_tick(rt)
    assert "schedule" in out and "monitor" in out
    assert sched.heartbeat_path(rt).exists()
    rt.close()


# --------------------------------------------------------------------
# 6) 미처리 목록 파일 보내기
# --------------------------------------------------------------------
def test_parser_routes_pending_report():
    for text in ("미처리 알림", "미처리 목록"):
        spec = parse_korean_command(text)
        assert spec is not None and spec.task == "pending_report"


def test_pending_report_sends_file(tmp_path):
    rt = make_rt(tmp_path)
    rt.settings.repo_root = tmp_path
    target = tmp_path / "docs" / "reports" / "pending.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("- 미처리 1건\n", encoding="utf-8")

    job_id = rt.jobs.enqueue(TaskSpec(task="pending_report"), "p1")
    out = worker.dispatch(rt, rt.jobs.get(job_id))
    assert out["ok"] and out["sent"] == 1
    assert any("[문서]" in t and "미처리 목록" in t for t in rt.channels[0].sent)
    # 현황판도 두 번째 파일로 함께 나간다
    assert out["dashboard_sent"] == 1
    assert out["dashboard_path"].endswith("dashboard.html")
    assert any("운영 현황판" in t for t in rt.channels[0].sent)
    rt.close()


def test_pending_report_missing_file(tmp_path):
    rt = make_rt(tmp_path)
    rt.settings.repo_root = tmp_path
    job_id = rt.jobs.enqueue(TaskSpec(task="pending_report"), "p2")
    out = worker.dispatch(rt, rt.jobs.get(job_id))
    assert out["ok"] is False and "없습니다" in out["message"]
    rt.close()


def test_telegram_send_document_multipart(tmp_path):
    """sendDocument를 multipart로 부르는지 가짜 httpx로 확인한다."""
    from v2r.channels.telegram import TelegramChannel

    doc = tmp_path / "pending.md"
    doc.write_text("내용", encoding="utf-8")

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResp()

    client = FakeClient()
    channel = TelegramChannel(
        token="123:abc", allowed_chat_ids={"7"}, data_dir=tmp_path, client=client
    )
    assert channel.send_document("7", doc, "미처리 목록 2026-09-21 08:40") is True
    url, kwargs = client.calls[0]
    assert url.endswith("/sendDocument")
    assert "document" in kwargs["files"]
    assert kwargs["data"]["caption"].startswith("미처리 목록")
    # 허용되지 않은 방에는 보내지 않는다
    assert channel.send_document("999", doc) is False
    assert channel.broadcast_document(doc, "설명") == 1


def test_notify_document_all_skips_channels_without_support(tmp_path):
    from v2r.channels import notify_document_all

    class NoDoc:
        name = "nodoc"

    doc = tmp_path / "a.md"
    doc.write_text("x", encoding="utf-8")
    channel = RecordingChannel()
    assert notify_document_all([channel, NoDoc()], doc, "설명") == 1


def test_real_schedule_has_five_pending_entries():
    from v2r.config import get_settings
    from v2r.engine import schedule as sched

    entries = sched.load_schedule(path=get_settings().config_dir / "schedule.yaml")
    pending = [e for e in entries if e.command == "미처리 알림"]
    assert len(pending) == 5
    assert sorted(e.time for e in pending) == ["08:40", "11:00", "14:00", "17:00", "21:00"]
    assert all(e.enabled for e in pending)


def test_now_iso_format_is_parseable():
    assert datetime.fromisoformat(now_iso()) is not None
