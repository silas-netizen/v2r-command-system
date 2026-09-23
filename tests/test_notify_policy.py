"""알림 정책 통합 테스트 (사용자 지시 2026-09-23): 허용 목록·에스컬레이션 제거·
접수 답장 축소·예약 작업 종료 채널 금지·발행 실패는 점검 뒤에만 🔴."""

from __future__ import annotations

from v2r.channels import notify_all
from v2r.engine import sidecar, worker
from tests.test_engine import make_runtime, make_spec
from tests.test_schedule import RecordingChannel


# --- 허용 목록 -------------------------------------------------------
def test_실제_카테고리들만_critical로_나간다():
    from v2r import channels as channels_mod

    channels_mod._CRITICAL_STATE.clear()
    ch = RecordingChannel()
    real_allowed = [
        "sidecar_heartbeat_stale",
        "sidecar_restart_exhausted",
        "schedule_mismatch:테스트",
        "relogin_needed",
        "gpt_login_pending",
        "gpt_image_quota",
        "register_limit_spike",
        "publish_failed_confirmed",
    ]
    for i, cat in enumerate(real_allowed):
        assert notify_all([ch], f"사건 {i}", level="critical", category=cat) == 1
    channels_mod._CRITICAL_STATE.clear()


def test_목록에_없는_실제_카테고리는_강등된다():
    from v2r import channels as channels_mod

    channels_mod._CRITICAL_STATE.clear()
    ch = RecordingChannel()
    for cat in ("naver_session_warning", "photo_missing", "monitor_alert:아무거나"):
        assert notify_all([ch], "메시지", level="critical", category=cat) == 0
    assert ch.sent == []
    channels_mod._CRITICAL_STATE.clear()


# --- 에스컬레이션 제거 -------------------------------------------------
def test_사이드카_경고는_복구_전까지_되풀이하지_않고_계속됨_문구도_없다(monkeypatch):
    from v2r import channels as channels_mod

    channels_mod._CRITICAL_STATE.clear()
    monkeypatch.setattr(sidecar, "heartbeat_age_seconds", lambda rt, now_kst=None: 99999)
    rt = make_runtime(_tmp())
    rt._channels = [RecordingChannel()]
    sidecar._last_alert = 0.0
    sidecar.alert_if_stale(rt, 1)
    sidecar.alert_if_stale(rt, 1)  # 되풀이 억제
    sent = rt.channels[0].sent
    assert len(sent) == 1
    assert not any("계속됨" in s for s in sent)
    channels_mod._CRITICAL_STATE.clear()


def _tmp():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp())


# --- 복구는 1회, ✅ 아이콘 -------------------------------------------
def test_사이드카_복구되면_체크표시_한번만(monkeypatch):
    from v2r import channels as channels_mod

    channels_mod._CRITICAL_STATE.clear()
    monkeypatch.setattr(sidecar, "heartbeat_age_seconds", lambda rt, now_kst=None: 99999)
    rt = make_runtime(_tmp())
    rt._channels = [RecordingChannel()]
    sidecar._last_alert = 0.0
    sidecar.alert_if_stale(rt, 1)
    rt.channels[0].sent.clear()
    assert sidecar.alert_recovered(rt) is True
    assert sidecar.alert_recovered(rt) is False  # 이미 복구 처리됨 — 두 번 안 보냄
    assert len([s for s in rt.channels[0].sent if s.startswith("✅")]) == 1
    channels_mod._CRITICAL_STATE.clear()


# --- 접수 답장: 짧은 작업엔 없음, 긴 작업만 1줄 -------------------------
def test_poll_channels_no_accept_reply_for_short_command(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)

    class FakeChannel(RecordingChannel):
        name = "slack"

        def __init__(self, texts):
            super().__init__()
            self._texts = texts

        def poll(self):
            out = self._texts
            self._texts = []
            return out

    class Cmd:
        def __init__(self, text, chat_id="c1"):
            self.text = text
            self.chat_id = chat_id

    ch = FakeChannel([Cmd("현황 알려줘")])
    rt._channels = [ch]
    n = worker.poll_channels(rt)
    assert n == 1
    # "작업 N 진행" 접수 답장이 없다(정책: 짧은 작업은 접수 답장 없음)
    assert not any("진행:" in s for s in ch.sent)


# --- 예약/자동 작업 종료는 채널 금지, 직접 명령은 결과 1건 -------------------
def test_finish_run_publish_failed_confirmed_only_after_reconcile(tmp_path):
    from v2r.command.spec import TaskSpec

    rt = make_runtime(tmp_path)
    ch = RecordingChannel()
    rt._channels = [ch]
    spec = TaskSpec(task="reconcile")
    job_id = rt.jobs.enqueue(spec, "k1")
    worker._finish_run(rt, job_id, spec, "끊긴 작업 점검", {"ok": True, "failed": 2})
    assert any(s.startswith("🔴") and "실패 확정" in s for s in ch.sent)


def test_finish_run_reconcile_no_failures_no_critical(tmp_path):
    from v2r.command.spec import TaskSpec

    rt = make_runtime(tmp_path)
    ch = RecordingChannel()
    rt._channels = [ch]
    spec = TaskSpec(task="reconcile")
    job_id = rt.jobs.enqueue(spec, "k2")
    worker._finish_run(rt, job_id, spec, "끊긴 작업 점검", {"ok": True, "failed": 0})
    assert not any(s.startswith("🔴") for s in ch.sent)
