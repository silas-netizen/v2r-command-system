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
    # 기본은 "실행기가 진작에 켜져 있었다" — 과거 기록 무시 규칙에 걸리지 않게.
    # 갓 켜진 상황을 보려면 set_started(rt, NOW) 로 바꾼다.
    set_started(rt, NOW - timedelta(days=1))
    return rt


def set_started(rt, when):
    """이번 실행기가 켜진 시각을 정한다(과거 기록 무시 규칙의 기준)."""
    rt.scratch["_monitor_started_at"] = when


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
    """재개 불가 작업(publish_* 가 아님)은 예전처럼: 정체 경고 → 30분 뒤 실패
    처리 + 재등록. (publish_daily 처럼 재개 가능한 작업의 즉시 복구는 별도 테스트.)"""
    rt = make_rt(tmp_path)
    job_id = add_job(rt, task="repair_comments", status="running", ago_min=16)
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


def test_publish_daily_정체시_즉시_큐로_되돌려_이어서_실행한다(tmp_path):
    """사고 2026-09-22: publish_daily가 리스 끊긴 채 15분 정체하면 감시가
    경고만 남기고 30분(DEAD_S)까지 기다리지 않는다 — 재개 가능한 작업은
    바로 queued로 되돌려 이어서 실행해야 한다."""
    rt = make_rt(tmp_path)
    job_id = add_job(rt, task="publish_daily", status="running", ago_min=16)
    # 리스가 비어 있다(add_job은 lease_until을 안 채운다) = 실행기가 끊긴 상태
    out = monitor.tick(rt, NOW)
    assert [a["action"] for a in out["actions"]] == ["stall_recovered"]
    row = rt.jobs.get(job_id)
    assert row["status"] == "queued"
    assert any("이어서 실행" in t or "되돌렸습니다" in t for t in rt.channels[0].sent)
    rt.close()


def test_publish_daily_정체여도_리스가_살아있으면_그대로_둔다(tmp_path):
    rt = make_rt(tmp_path)
    job_id = add_job(rt, task="publish_daily", status="running", ago_min=16)
    future = (NOW + timedelta(minutes=10)).isoformat(timespec="seconds")
    rt.conn.execute("UPDATE jobs SET lease_until = ? WHERE id = ?", (future, job_id))
    out = monitor.tick(rt, NOW)
    assert [a["action"] for a in out["actions"]] == ["stalled"]
    assert rt.jobs.get(job_id)["status"] == "running"
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


# --------------------------------------------------------------------
# 3-b) 장애 2026-09-20 A — 과거 기록·중지·부분 성공은 건드리지 않는다
# --------------------------------------------------------------------
def set_result(rt, job_id, result):
    """작업 결과 JSON 을 심는다."""
    rt.conn.execute(
        "UPDATE jobs SET result_json = ? WHERE id = ?",
        (json.dumps(result, ensure_ascii=False), job_id),
    )


def test_history_jobs_are_ignored_on_first_tick(tmp_path):
    """갓 켜진 실행기는 어제 실패한 작업을 알리지도 다시 등록하지도 않는다."""
    rt = make_rt(tmp_path)
    rt.scratch.pop("_monitor_started_at", None)  # 방금 켜진 상태
    old = add_job(rt, status="failed", ago_min=20 * 60, error="connection timeout")
    out = monitor.tick(rt, NOW)
    assert out["actions"] == []
    assert out["skipped_history"] >= 1
    assert rt.channels[0].sent == []
    assert len(rt.jobs.recent(limit=10)) == 1  # 재등록 없음
    assert monitor.load_state(rt)["started_at"].startswith("2026-09-21T12:00")
    assert rt.jobs.get(old)["status"] == "failed"
    rt.close()


def test_history_cutoff_keeps_recent_jobs(tmp_path):
    """켜지기 10분 안쪽에 움직인 작업은 과거로 보지 않는다."""
    rt = make_rt(tmp_path)
    rt.scratch.pop("_monitor_started_at", None)
    add_job(rt, status="failed", ago_min=5, error="connection timeout")
    out = monitor.tick(rt, NOW)
    assert [a["action"] for a in out["actions"]] == ["failed"]
    rt.close()


def test_cancelled_job_is_never_requeued(tmp_path):
    """사용자가 `중지` 로 끊은 작업은 다시 등록하지 않는다."""
    rt = make_rt(tmp_path)
    job_id = add_job(rt, status="failed", error="리스 상실: 다른 실행기가 작업을 가져갔습니다")
    out = monitor.tick(rt, NOW)
    act = [a for a in out["actions"] if a["action"] == "failed"][0]
    assert act["new_job_id"] is None
    assert "다시 등록하지 않았습니다" in "\n".join(rt.channels[0].sent)
    assert len(rt.jobs.recent(limit=10)) == 1
    assert rt.jobs.get(job_id)["status"] == "failed"
    rt.close()


@pytest.mark.parametrize(
    "job_kwargs",
    [
        {"status": "cancelled", "error": None},
        {"status": "failed", "error": "사용자 중지 요청으로 건너뜀"},
        {"status": "failed", "error": "작업이 취소되었습니다"},
        {"status": "failed", "error": "lease lost"},
    ],
)
def test_no_retry_reason_covers_stop_and_cancel(tmp_path, job_kwargs):
    rt = make_rt(tmp_path)
    job_id = add_job(rt, **job_kwargs)
    assert monitor.no_retry_reason(rt.jobs.get(job_id)) is not None
    rt.close()


def test_monitor_retry_child_is_not_retried_again(tmp_path):
    """60 → 67 → 70 처럼 사슬이 길어지지 않는다(원래 작업당 재시도 1회)."""
    rt = make_rt(tmp_path)
    add_job(rt, status="failed", error="connection timeout")
    out = monitor.tick(rt, NOW)
    child = [a for a in out["actions"] if a["action"] == "failed"][0]["new_job_id"]
    assert child
    # 다시 등록된 작업마저 같은 이유로 실패했다
    rt.conn.execute(
        "UPDATE jobs SET status = 'failed', error = ?, created_at = ?, updated_at = ?"
        " WHERE id = ?",
        ("connection timeout", NOW.isoformat(timespec="seconds"),
         NOW.isoformat(timespec="seconds"), child),
    )
    rt.channels[0].sent.clear()
    out2 = monitor.tick(rt, NOW + timedelta(minutes=1))
    act2 = [a for a in out2["actions"] if a["action"] == "failed" and a["job_id"] == child][0]
    assert act2["new_job_id"] is None
    assert act2["blocked"]
    assert monitor.root_key(rt.jobs.get(child)) == monitor.root_key(rt.jobs.get(child - 1))
    rt.close()


def test_partial_success_publish_is_reported_not_retried(tmp_path):
    """일부라도 올라간 발행은 다시 돌리지 않는다(중복 발행 방지)."""
    rt = make_rt(tmp_path)
    job_id = add_job(rt, status="failed", error="connection timeout")
    set_result(
        rt,
        job_id,
        {"ok": False, "results": [{"title": "가", "status": "posted"}], "failures": ["나: 실패"]},
    )
    out = monitor.tick(rt, NOW)
    act = [a for a in out["actions"] if a["action"] == "failed"][0]
    assert act["new_job_id"] is None
    assert "일부는 성공했습니다" in "\n".join(rt.channels[0].sent)
    rt.close()


def test_partial_success_counted_from_per_cafe(tmp_path):
    rt = make_rt(tmp_path)
    job_id = add_job(rt, status="failed", error="connection timeout")
    set_result(rt, job_id, {"ok": False, "per_cafe": {"고요한 아침": {"ok": 3, "fail": 2}}})
    out = monitor.tick(rt, NOW)
    assert [a for a in out["actions"] if a["action"] == "failed"][0]["new_job_id"] is None
    # 아무것도 못 올렸으면 다시 등록한다
    rt2 = make_rt(tmp_path / "b")
    job2 = add_job(rt2, status="failed", error="connection timeout")
    set_result(rt2, job2, {"ok": False, "per_cafe": {"고요한 아침": {"ok": 0, "fail": 5}}})
    out2 = monitor.tick(rt2, NOW)
    assert [a for a in out2["actions"] if a["action"] == "failed"][0]["new_job_id"]
    rt.close()
    rt2.close()


def test_reaped_stalled_cancelled_job_is_not_requeued(tmp_path):
    """멈춘 작업 정리에도 같은 금지 규칙이 걸린다."""
    rt = make_rt(tmp_path)
    job_id = add_job(rt, status="running", ago_min=40, error="사용자 중지 요청")
    out = monitor.tick(rt, NOW)
    reaped = [a for a in out["actions"] if a["action"] == "reaped"][0]
    assert reaped["new_job_id"] is None and reaped["blocked"]
    assert rt.jobs.get(job_id)["status"] == "failed"
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
