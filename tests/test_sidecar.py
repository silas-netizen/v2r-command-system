"""사이드카 스레드 테스트 (장애 2026-09-20 밤 → 09-21 아침).

긴 `publish_daily` 하나가 본 루프를 밤새 붙잡고 있는 동안
예약·감시·채널 수신·가벼운 작업이 **따로** 돌아야 한다.
진짜 API·진짜 시계·진짜 텔레그램은 쓰지 않는다.
"""

from __future__ import annotations

import threading
import time

from v2r.channels.base import IncomingCommand
from v2r.command.spec import TaskSpec
from v2r.engine import publish as publish_mod
from v2r.engine import sidecar as sidecar_mod
from v2r.engine import worker
from v2r.engine.context import Runtime

from tests.test_engine import make_runtime, make_spec


def _side_runtime(settings, channels=None) -> Runtime:
    """사이드카가 쓰는 **자기만의** Runtime(자기만의 SQLite 연결)."""
    rt = Runtime.open(settings)
    rt._client = object()
    rt._llm = None
    rt._llm_ready = True
    rt._channels = list(channels or [])
    return rt


class _FakeChannel:
    """한 번만 명령을 뱉고 답장을 모아 두는 가짜 채널."""

    name = "fake"
    enabled = True

    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.sent: list[tuple[str, str]] = []

    def poll(self) -> list[IncomingCommand]:
        out = [
            IncomingCommand(channel="fake", sender_id="u", chat_id="room", text=t)
            for t in self.texts
        ]
        self.texts = []
        return out

    def send(self, chat_id: str, text: str) -> bool:
        self.sent.append((chat_id, text))
        return True

    def broadcast(self, text: str) -> int:
        self.sent.append(("*", text))
        return 1


# --------------------------------------------------------------------
# 1. 줄 나누기: 가벼운 작업은 사이드카, 무거운 작업은 본 실행기
# --------------------------------------------------------------------
def test_가벼운_작업_목록은_한곳에서만_정한다():
    assert "pending_report" in sidecar_mod.LIGHT_TASKS
    for task in ("status", "dashboard", "monitor_status", "schedule_list", "schedule_run",
                 "gpt_keepalive", "slack_check", "telegram_check"):
        assert sidecar_mod.is_light(task) and sidecar_mod.scope_for(task) == "light"
    for task in ("publish_daily", "publish_brand", "wash_photos", "reconcile"):
        assert not sidecar_mod.is_light(task) and sidecar_mod.scope_for(task) == "main"


def test_등록할_때_줄_이름이_붙는다(tmp_path):
    rt = make_runtime(tmp_path)
    light = rt.jobs.enqueue(TaskSpec(task="pending_report"), "k-light")
    heavy = rt.jobs.enqueue(TaskSpec(task="publish_daily"), "k-heavy")
    assert rt.jobs.get(light)["lease_scope"] == "light"
    assert rt.jobs.get(heavy)["lease_scope"] == "main"
    rt.close()


def test_본_실행기는_가벼운_작업을_집지_않고_사이드카는_무거운_작업을_집지_않는다(tmp_path):
    rt = make_runtime(tmp_path)
    light = rt.jobs.enqueue(TaskSpec(task="status"), "k1")
    heavy = rt.jobs.enqueue(TaskSpec(task="publish_daily"), "k2")

    # 사이드카: 가벼운 작업만
    got = rt.jobs.acquire_light("side")
    assert int(got["id"]) == light
    assert rt.jobs.acquire_light("side") is None  # 무거운 작업은 절대 안 집는다

    # 본 실행기: 가벼운 작업은 건너뛰고 무거운 작업을 집는다
    main = rt.jobs.acquire("main:1", scope="main")
    assert int(main["id"]) == heavy
    rt.close()


def test_가벼운_작업은_긴_작업이_리스를_쥐고_있어도_바로_잡힌다(tmp_path):
    """실행기 리스를 본 실행기가 쥐고 있어도 사이드카는 기다리지 않는다."""
    rt = make_runtime(tmp_path)
    rt.jobs.enqueue(TaskSpec(task="publish_daily"), "heavy")
    assert rt.jobs.acquire("main:1", scope="main") is not None  # 리스 점유 + 긴 작업 실행 중
    job_id = rt.jobs.enqueue(TaskSpec(task="pending_report"), "light")
    got = rt.jobs.acquire_light("side")
    assert got is not None and int(got["id"]) == job_id
    rt.close()


def test_예전_DB에도_줄_칸이_생긴다(tmp_path):
    """`lease_scope` 칸이 없던 DB를 열어도 마이그레이션으로 채운다."""
    import sqlite3

    from v2r.store.db import connect, init_schema, migrate

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, idem_key TEXT NOT NULL"
        " UNIQUE, task TEXT NOT NULL, spec_json TEXT NOT NULL, status TEXT NOT NULL"
        " DEFAULT 'queued', lease_owner TEXT, lease_until TEXT, result_json TEXT,"
        " error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO jobs (idem_key, task, spec_json, created_at, updated_at)"
        " VALUES ('a', 'publish_daily', '{}', '2026-09-20', '2026-09-20')"
    )
    conn.close()

    conn2 = connect(path)
    assert migrate(conn2) == ["jobs.lease_scope"]  # 빠진 칸을 붙인다
    assert migrate(conn2) == []  # 두 번째부터는 할 일이 없다(멱등)
    conn2.close()
    conn3 = connect(path)
    init_schema(conn3)  # 두 번 열어도 터지지 않는다(멱등)
    row = conn3.execute("SELECT lease_scope FROM jobs WHERE idem_key = 'a'").fetchone()
    assert row["lease_scope"] == "main"  # 옛 작업은 본 실행기 몫
    conn3.close()


# --------------------------------------------------------------------
# 2. 틱: 예약·감시·수신·가벼운 작업
# --------------------------------------------------------------------
def test_틱이_예약과_감시와_수신과_가벼운_작업을_모두_돌린다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    # `pending_report`(미처리 알림)가 실제로 "성공"하려면 미처리 목록 파일이
    # 있어야 한다(2026-09-23, `repo_root`가 시험용 가짜 폴더로 격리되면서
    # 파일이 없어 "실패"로 잘못 나오던 것을 고정 — 이 시험의 목적은 파일
    # 내용이 아니라 가벼운 작업이 같은 틱에서 바로 실행되는지 확인하는 것).
    reports_dir = rt.settings.repo_root / "docs" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "pending.md").write_text("# 미처리 목록\n\n(없음)\n", encoding="utf-8")
    channel = _FakeChannel(["미처리 알림"])
    rt._channels = [channel]
    seen: list[str] = []
    monkeypatch.setattr(
        "v2r.engine.schedule.tick", lambda rt_, *a, **k: seen.append("schedule") or {}
    )
    monkeypatch.setattr(
        "v2r.engine.monitor.tick", lambda rt_, *a, **k: seen.append("monitor") or {}
    )
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg, **kw: 0)

    out = sidecar_mod.tick_once(rt, "side")
    assert seen == ["schedule", "monitor"]
    assert out["received"] == 1
    # 접수된 `미처리 알림`(가벼운 작업)이 같은 틱에서 바로 실행된다
    assert [o["status"] for o in out["done"]] == ["done"]
    assert rt.jobs.recent(1)[0]["task"] == "pending_report"
    # 명령을 보낸 그 방에 접수·결과 답장이 모두 갔다
    assert len(channel.sent) >= 2
    assert sidecar_mod.heartbeat_age_seconds(rt) is not None
    rt.close()


def test_틱은_한_칸이_터져도_나머지를_계속한다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt._channels = []

    def _boom(*a, **k):
        raise RuntimeError("예약 고장")

    monkeypatch.setattr("v2r.engine.schedule.tick", _boom)
    out = sidecar_mod.tick_once(rt, "side")
    assert "error" in out["schedule"]
    assert "monitor" in out and out["done"] == []
    assert sidecar_mod.heartbeat_age_seconds(rt) is not None  # 심장박동은 그래도 찍힌다
    rt.close()


# --------------------------------------------------------------------
# 3. 긴 작업이 본 루프를 막는 동안 사이드카는 계속 돈다
# --------------------------------------------------------------------
def test_긴_작업이_본_루프를_막아도_사이드카가_예약과_수신을_계속한다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    channel = _FakeChannel(["현황"])
    ticks: list[str] = []
    monkeypatch.setattr(
        "v2r.engine.schedule.tick", lambda rt_, *a, **k: ticks.append("s") or {}
    )
    monkeypatch.setattr(
        "v2r.engine.monitor.tick", lambda rt_, *a, **k: ticks.append("m") or {}
    )
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg, **kw: 0)

    side = sidecar_mod.SidecarThread(
        rt.settings,
        interval=0.02,
        runtime_factory=lambda: _side_runtime(rt.settings, [channel]),
    )
    side.start()
    try:
        # 본 루프는 긴 작업 안에 갇혀 있다
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if len(ticks) >= 4 and channel.sent:
                break
            time.sleep(0.02)
    finally:
        side.stop()

    assert side.ticks >= 2  # 본 루프가 막혀 있는 동안에도 여러 번 돌았다
    assert ticks.count("s") >= 2 and ticks.count("m") >= 2
    assert channel.sent  # `현황`이 즉시 읽히고 답장까지 갔다
    assert not side.is_alive()
    rt.close()


def test_긴_작업_중_중지_명령이_즉시_먹힌다(tmp_path, monkeypatch):
    """사이드카가 읽은 `중지` 가 본 루프의 긴 발행을 슬롯 사이에서 멈춘다."""
    rt = make_runtime(tmp_path)
    rt._channels = []
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg, **kw: 0)
    monkeypatch.setattr("v2r.engine.schedule.tick", lambda rt_, *a, **k: {})
    monkeypatch.setattr("v2r.engine.monitor.tick", lambda rt_, *a, **k: {})

    channel = _FakeChannel(["중지"])
    side = sidecar_mod.SidecarThread(
        rt.settings,
        interval=0.02,
        runtime_factory=lambda: _side_runtime(rt.settings, [channel]),
    )

    started = threading.Event()

    def slow_slot(rt_, spec_, slot, **kwargs):
        started.set()
        # 사이드카가 `중지`를 읽어 플래그를 세울 때까지만 기다린다
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not worker.stop_requested(rt):
            time.sleep(0.02)
        return {"status": "done", "title": slot.manuscript.title}

    monkeypatch.setattr(publish_mod, "run_slot", slow_slot)

    side.start()
    try:
        out = worker._run_publish(rt, None, make_spec(dry_run=True))
    finally:
        side.stop()

    assert started.is_set()
    assert worker.stop_requested(rt) is True
    assert out.get("stopped") is True  # 남은 슬롯은 건너뛰었다
    assert len(out["results"]) == 1
    rt.close()


# --------------------------------------------------------------------
# 4. 되살리기 + 심장박동
# --------------------------------------------------------------------
class _DeadSidecar:
    def __init__(self) -> None:
        self.stopped = False

    def is_alive(self) -> bool:
        return False

    def stop(self, timeout: float = 0.0) -> None:
        self.stopped = True


def test_사이드카가_죽으면_다시_띄운다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt._channels = []
    notices: list[str] = []
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg, **kw: notices.append(msg))
    started: list[object] = []

    class _Fresh:
        def __init__(self, settings) -> None:
            self.settings = settings

        def start(self):
            started.append(self)
            return self

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(sidecar_mod, "SidecarThread", _Fresh)

    fresh = worker.ensure_sidecar(rt, _DeadSidecar())
    assert started and fresh is started[0]
    assert any("사이드카" in n for n in notices)
    rows = [e for e in rt.events.recent(limit=20) if "사이드카" in e["message"]]
    assert rows and rows[0]["level"] == "error"

    # 살아 있으면 새로 띄우지 않는다
    same = worker.ensure_sidecar(rt, fresh)
    assert same is fresh and len(started) == 1
    rt.close()


def test_심장박동이_3분_넘게_낡으면_경고한다(tmp_path, monkeypatch):
    from datetime import timedelta

    from v2r.store.db import KST

    rt = make_runtime(tmp_path)
    rt._channels = []
    notices: list[str] = []
    monkeypatch.setattr("v2r.channels.notify_all", lambda ch, msg, **kw: notices.append(msg))

    sidecar_mod._last_alert = 0.0
    assert sidecar_mod.alert_if_stale(rt) is False  # 파일이 없으면(한 번도 안 켜짐) 조용히

    from datetime import datetime

    sidecar_mod.write_heartbeat(rt, datetime.now(KST))
    assert sidecar_mod.heartbeat_is_stale(rt) is False
    assert sidecar_mod.alert_if_stale(rt) is False

    sidecar_mod.write_heartbeat(rt, datetime.now(KST) - timedelta(seconds=600))
    assert sidecar_mod.heartbeat_is_stale(rt) is True
    assert sidecar_mod.alert_if_stale(rt) is True
    assert notices and "사이드카" in notices[0]
    # 같은 경고를 되풀이하지 않는다
    assert sidecar_mod.alert_if_stale(rt) is False
    sidecar_mod._last_alert = 0.0
    rt.close()


def test_health_보고에_심장박동_두_개가_나온다(tmp_path, monkeypatch):
    from datetime import datetime

    from v2r.engine import schedule as schedule_mod
    from v2r.store.db import KST

    rt = make_runtime(tmp_path)
    rt._channels = []
    schedule_mod.write_heartbeat(rt, datetime.now(KST))
    sidecar_mod.write_heartbeat(rt, datetime.now(KST))
    text = schedule_mod.health_report(rt)
    assert "실행기 심장박동" in text
    assert "사이드카 심장박동" in text and "정상" in text
    rt.close()


# --------------------------------------------------------------------
# 5. 본 루프 쪽 배선
# --------------------------------------------------------------------
def test_serve_poll은_사이드카가_읽을_때_채널을_두_번_읽지_않는다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    channel = _FakeChannel(["현황"])
    rt._channels = [channel]
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg, **kw: 0)

    out = worker.serve_poll(rt, "main:1", scope="main", poll=False)
    assert out["received"] == 0
    assert channel.texts == ["현황"]  # 아직 아무도 읽지 않았다
    assert out["done"] == []
    rt.close()


def test_watch_tick은_사이드카가_맡으면_심장박동만_찍는다(tmp_path, monkeypatch):
    from v2r.engine import schedule as schedule_mod

    rt = make_runtime(tmp_path)
    rt._channels = []
    calls: list[str] = []
    monkeypatch.setattr(
        "v2r.engine.schedule.tick", lambda rt_, *a, **k: calls.append("s") or {}
    )
    monkeypatch.setattr(
        "v2r.engine.monitor.tick", lambda rt_, *a, **k: calls.append("m") or {}
    )
    out = worker.watch_tick(rt, schedule=False, monitor=False)
    assert calls == [] and out == {}
    assert schedule_mod.heartbeat_age_seconds(rt) is not None
    # 기본값은 예전 그대로(예약·감시 모두 돈다)
    worker.watch_tick(rt)
    assert calls == ["s", "m"]
    rt.close()


def test_명령을_보낸_방에_결과가_돌아온다(tmp_path, monkeypatch):
    """접수는 사이드카, 실행은 본 실행기여도 답장은 그 방으로 간다."""
    rt = make_runtime(tmp_path)
    rt._channels = []
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg, **kw: 0)
    channel = _FakeChannel([])
    job_id = rt.jobs.enqueue(TaskSpec(task="status"), "k")
    worker.remember_origin(job_id, channel, "room")  # 사이드카가 기억해 둔 방
    out = worker.run_once(rt, "main:1", scope=None)
    assert out["job_id"] == job_id
    assert channel.sent and channel.sent[0][0] == "room"
    assert worker.pop_origin(job_id) is None  # 한 번만 답한다
    rt.close()


# --------------------------------------------------------------------
# 6. 자가 복구 (사고 2026-09-23): LIGHT 작업 시간 상한 + 심장박동 분리 + 재시작 한도
# --------------------------------------------------------------------
def test_LIGHT_작업이_시간_상한을_넘기면_failed로_끝내고_넘어간다(tmp_path, monkeypatch):
    """옛 코드가 새 예약 명령을 오해석해 keyword_exposure 를 15분 붙잡은 사고 재현.

    한 작업이 상한을 넘겨도 `run_once` 는 **바로 돌아와야** 한다 — 사이드카
    루프가 막히지 않는다는 뜻이다.
    """
    rt = make_runtime(tmp_path)
    rt._channels = []
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg, **kw: 0)
    job_id = rt.jobs.enqueue(TaskSpec(task="status"), "slow")

    started = threading.Event()
    release = threading.Event()

    def _slow_dispatch(rt_, job_, owner_=None):
        started.set()
        release.wait(5.0)  # 사이드카가 상한을 넘기고 돌아온 뒤에도 계속 돈다(데몬)
        return {"ok": True, "report": "늦게 끝남"}

    monkeypatch.setattr(worker, "dispatch", _slow_dispatch)

    t0 = time.monotonic()
    out = worker.run_once(rt, "side", scope="light", timeout_seconds=0.1)
    elapsed = time.monotonic() - t0

    assert started.is_set()
    assert elapsed < 2.0  # 0.1초 상한을 거의 바로 지키고 돌아왔다(실제 작업은 5초 안 기다림)
    assert out["timed_out"] is True
    assert out["status"] == "failed"
    assert rt.jobs.get(job_id)["status"] == "failed"
    assert "시간 상한" in rt.jobs.get(job_id)["error"]
    release.set()  # 백그라운드로 마저 도는 스레드를 풀어 준다(테스트 정리)
    rt.close()


def test_사이드카_재시작_한도를_넘기면_실행기_재시작을_시도한다(tmp_path, monkeypatch):
    """사이드카를 SIDECAR_RESTART_MAX 번 다시 띄워도 안 살아나면 실행기를 재시작한다."""
    rt = make_runtime(tmp_path)
    rt._channels = []
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg, **kw: 0)

    class _AlwaysDead:
        def is_alive(self) -> bool:
            return False

        def stop(self, timeout: float = 0.0) -> None:
            pass

    class _Fresh:
        def __init__(self, settings) -> None:
            pass

        def start(self):
            return self

        def is_alive(self) -> bool:
            return False  # 다시 띄워도 여전히 죽어 있다고 가정

    monkeypatch.setattr(sidecar_mod, "SidecarThread", _Fresh)
    restart_calls: list[str] = []
    monkeypatch.setattr(worker, "_restart_serve_process", lambda rt_, reason: restart_calls.append(reason))
    worker._sidecar_restart_count = 0

    side = _AlwaysDead()
    for _ in range(worker.SIDECAR_RESTART_MAX):
        side = worker.ensure_sidecar(rt, side)
    assert restart_calls  # 한도를 넘기자 실행기 재시작을 시도했다
    worker._sidecar_restart_count = 0
    rt.close()


def test_실행기_재시작은_발행_중이면_미룬다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt._channels = []
    notices: list[str] = []
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg, **kw: notices.append(msg))
    monkeypatch.setattr(rt.jobs, "running_jobs", lambda: [{"task": "publish_daily"}])
    popen_calls: list[list[str]] = []
    monkeypatch.setattr(
        "subprocess.Popen", lambda args, **kw: popen_calls.append(args)
    )
    worker._restart_serve_process(rt, "테스트")
    assert popen_calls == []  # 발행 중이라 재시작 스크립트를 부르지 않았다
    rt.close()


def test_심장박동_스레드는_틱_스레드와_따로_돈다(tmp_path):
    """`_Status` 가 "지금 뭘 하는지"를 기록하고, 그 값을 심장박동 파일에 쓸 수 있다."""
    rt = make_runtime(tmp_path)
    rt._channels = []
    status = sidecar_mod._Status()
    status.set("light:keyword_exposure")
    snap = status.snapshot()
    assert snap["step"] == "light:keyword_exposure"
    sidecar_mod.write_heartbeat(rt, busy=snap)
    import json

    data = json.loads(sidecar_mod.heartbeat_path(rt).read_text(encoding="utf-8"))
    assert data["busy"]["step"] == "light:keyword_exposure"
    rt.close()
