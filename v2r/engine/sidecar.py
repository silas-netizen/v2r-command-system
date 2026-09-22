"""사이드카 스레드: 긴 작업 뒤에 줄 서면 안 되는 일들을 따로 돌린다.

왜 필요한가 (장애 2026-09-20 밤 ~ 09-21 아침)
--------------------------------------------
`serve()` 는 큐에서 꺼낸 작업을 **그 자리에서** 실행한다. 그래서 10:50 에
시작한 `publish_daily` 가 다음 날 아침까지 도는 동안 본 루프가 통째로 막혔다.

* 11:00·14:00·17:00·21:00 의 `미처리 알림` 예약이 **한 번도 발사되지 않았다**
  (`schedule_state` 에 pending 1건만 남아 있었다).
* 감시 틱이 안 돌아 멈춘 작업을 아무도 보지 못했다.
* 텔레그램 수신도 본 루프 안에 있어서 `현황`·`중지` 가 **읽히지도 않았다**.
* 다음 날 07:30·09:00 예약은 긴 작업 뒤에 줄만 섰을 것이다.

그래서 이 스레드가 5초마다 따로 돌며 다음을 맡는다.

1. 예약 틱(`schedule.tick`) — 정해진 시각에 발사
2. 감시 틱(`monitor.tick`)
3. 채널 수신(`worker.poll_channels`) — 긴 작업 중에도 명령이 즉시 읽힌다
4. **가벼운 작업**(`LIGHT_TASKS`)의 실행 — `light` 줄에 들어온 작업만 집는다
5. 사이드카 심장박동(`data/sidecar_heartbeat.json`) 기록

DB 연결은 스레드마다 따로 쓴다(SQLite 연결은 스레드 간 공유가 안 된다).
WAL 이라 읽기·쓰기가 같이 돌 수 있고, 여기서 하는 쓰기는 모두 짧다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from v2r.engine.context import Runtime
from v2r.store.db import KST

log = logging.getLogger(__name__)

#: 사이드카가 직접 실행하는 가벼운 작업들.
#: "긴 발행 작업 뒤에 줄 서면 쓸모가 없어지는 일" 이 기준이다.
LIGHT_TASKS = frozenset(
    {
        "pending_report",
        "daily_report",
        "progress_report",
        "monitor_status",
        "schedule_list",
        "schedule_run",
        "status",
        "dashboard",
        "gpt_keepalive",
        "naver_keepalive",
        "web_keepalive",
        "plan_keepalive",
        "maintenance",
    }
)

#: 사이드카 심장박동 파일 이름 (data/ 아래). 본 실행기 것과 **따로** 둔다.
HEARTBEAT_FILE = "sidecar_heartbeat.json"
#: 이보다 오래되면 사이드카가 멈춘 것으로 본다(초)
HEARTBEAT_STALE_SECONDS = 180
#: 사이드카 루프 간격(초)
TICK_SECONDS = 5
#: 한 틱에서 처리할 가벼운 작업 수 상한(한 틱이 너무 길어지지 않게)
LIGHT_LIMIT = 10
#: 같은 경고를 되풀이하지 않는 간격(초)
ALERT_REPEAT_SECONDS = 600


def is_light(task: Any) -> bool:
    """사이드카가 맡는 가벼운 작업인가."""
    return str(task) in LIGHT_TASKS


def scope_for(task: Any) -> str:
    """그 작업이 들어갈 줄 이름(`light` 또는 `main`)."""
    return "light" if is_light(task) else "main"


def sidecar_owner() -> str:
    """사이드카 리스 소유자 이름 — 본 실행기와 반드시 달라야 한다."""
    from v2r.engine.worker import default_owner

    return f"{default_owner()}:sidecar"


# --------------------------------------------------------------------
# 심장박동
# --------------------------------------------------------------------
def heartbeat_path(rt: Any) -> Path:
    """사이드카 심장박동 파일 경로."""
    return Path(rt.settings.data_dir) / HEARTBEAT_FILE


def write_heartbeat(rt: Any, now_kst: datetime | None = None) -> None:
    """사이드카가 살아 있다는 표시. 실패해도 루프를 세우지 않는다."""
    now_kst = now_kst or datetime.now(KST)
    path = heartbeat_path(rt)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"at": now_kst.isoformat(timespec="seconds"), "pid": os.getpid()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        log.warning("사이드카 심장박동 기록 실패: %s", exc)


def heartbeat_age_seconds(rt: Any, now_kst: datetime | None = None) -> float | None:
    """사이드카 심장박동이 몇 초 전 것인가. 파일이 없으면 None."""
    now_kst = now_kst or datetime.now(KST)
    try:
        data = json.loads(heartbeat_path(rt).read_text(encoding="utf-8"))
        at = datetime.fromisoformat(str(data.get("at")))
        if at.tzinfo is None:
            at = at.replace(tzinfo=KST)
        return (now_kst - at).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def heartbeat_is_stale(rt: Any, limit_seconds: int = HEARTBEAT_STALE_SECONDS) -> bool:
    """사이드카 심장박동이 오래됐는가(없으면 오래된 것으로 본다)."""
    age = heartbeat_age_seconds(rt)
    return age is None or age > limit_seconds


def heartbeat_line(rt: Any, now_kst: datetime | None = None) -> str:
    """`health` 보고에 넣을 한 줄."""
    age = heartbeat_age_seconds(rt, now_kst)
    if age is None:
        return "사이드카 심장박동: 없음 (예약·감시 스레드가 꺼져 있습니다)"
    if age > HEARTBEAT_STALE_SECONDS:
        return (
            f"사이드카 심장박동: {int(age)}초 전 — 멈춘 것 같습니다"
            f" (기준 {HEARTBEAT_STALE_SECONDS}초)"
        )
    return f"사이드카 심장박동: {int(age)}초 전 — 정상"


#: 마지막으로 "사이드카가 멈췄다" 고 알린 시각(단조 시계)
_last_alert = 0.0


def alert_if_stale(rt: Any, limit_seconds: int = HEARTBEAT_STALE_SECONDS) -> bool:
    """사이드카 심장박동이 3분 넘게 낡았으면 알린다(본 루프가 부른다).

    한 번도 켜진 적이 없으면(파일 없음) 알리지 않는다.
    """
    global _last_alert
    age = heartbeat_age_seconds(rt)
    if age is None or age <= limit_seconds:
        return False
    now = time.monotonic()
    if now - _last_alert < ALERT_REPEAT_SECONDS:
        return False
    _last_alert = now
    text = f"사이드카(예약·감시) 심장박동이 {int(age)}초째 낡았습니다 — 예약이 안 돌 수 있습니다"
    log.error("%s", text)
    try:
        rt.events.log(None, "error", text)
    except Exception as exc:  # noqa: BLE001
        log.warning("사이드카 경고 기록 실패: %s", exc)
    try:
        from v2r.channels import notify_all

        notify_all(rt.channels, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("사이드카 경고 알림 실패: %s", exc)
    return True


# --------------------------------------------------------------------
# 틱 1회분
# --------------------------------------------------------------------
def tick_once(rt: Any, owner: str | None = None) -> dict:
    """사이드카 1회분: 예약 → 감시 → 채널 수신 → 가벼운 작업 → 심장박동.

    어떤 예외로도 스레드를 죽이지 않는다(칸마다 가둔다).
    """
    from v2r.engine import monitor as monitor_mod
    from v2r.engine import schedule as schedule_mod
    from v2r.engine import worker as worker_mod

    owner = owner or sidecar_owner()
    out: dict = {}
    try:
        out["schedule"] = schedule_mod.tick(rt)
    except Exception as exc:  # noqa: BLE001
        log.exception("사이드카 예약 틱 실패(계속 진행): %s", exc)
        out["schedule"] = {"error": str(exc)}
    try:
        out["monitor"] = monitor_mod.tick(rt)
    except Exception as exc:  # noqa: BLE001
        log.exception("사이드카 감시 틱 실패(계속 진행): %s", exc)
        out["monitor"] = {"error": str(exc)}
    try:
        out["received"] = worker_mod.poll_channels(rt)
    except Exception as exc:  # noqa: BLE001
        log.exception("사이드카 채널 수신 실패(계속 진행): %s", exc)
        out["received"] = 0
    try:
        # 중지 중이어도 가벼운 작업(현황·미처리 알림)은 돌아야 한다.
        out["done"] = worker_mod.drain(rt, owner, limit=LIGHT_LIMIT, scope="light")
    except Exception as exc:  # noqa: BLE001
        log.exception("사이드카 가벼운 작업 실행 실패(계속 진행): %s", exc)
        out["done"] = []
    write_heartbeat(rt)
    return out


# --------------------------------------------------------------------
# 스레드
# --------------------------------------------------------------------
class SidecarThread:
    """예약·감시·채널 수신·가벼운 작업을 맡는 데몬 스레드.

    자기만의 `Runtime`(=자기만의 SQLite 연결)을 연다.
    """

    def __init__(
        self,
        settings: Any,
        *,
        interval: float = TICK_SECONDS,
        runtime_factory: Any = None,
    ) -> None:
        self.settings = settings
        self.interval = float(interval)
        self._runtime_factory = runtime_factory or (lambda: Runtime.open(settings))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ticks = 0
        self.last_error: str | None = None

    # --- 수명 ---
    def start(self) -> "SidecarThread":
        """스레드를 띄운다(이미 돌고 있으면 그대로)."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="v2r-sidecar", daemon=True
        )
        self._thread.start()
        return self

    def is_alive(self) -> bool:
        """스레드가 살아 있는가."""
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float = 10.0) -> None:
        """멈추라고 알리고 기다린다."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    # --- 본체 ---
    def _run(self) -> None:  # pragma: no cover - 스레드 본체(내용은 tick_once 로 시험)
        rt = None
        owner = sidecar_owner()
        try:
            rt = self._runtime_factory()
            log.info("사이드카 시작(%s)", owner)
            while not self._stop.is_set():
                try:
                    tick_once(rt, owner)
                    self.ticks += 1
                except Exception as exc:  # noqa: BLE001 - 스레드는 절대 죽지 않는다
                    self.last_error = str(exc)
                    log.exception("사이드카 틱 실패(계속 진행): %s", exc)
                self._stop.wait(self.interval)
        except BaseException as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.exception("사이드카 중단: %s", exc)
        finally:
            if rt is not None:
                try:
                    rt.close()
                except Exception:  # noqa: BLE001
                    pass
            log.info("사이드카 종료")


__all__ = [
    "HEARTBEAT_FILE",
    "HEARTBEAT_STALE_SECONDS",
    "LIGHT_TASKS",
    "SidecarThread",
    "alert_if_stale",
    "heartbeat_age_seconds",
    "heartbeat_is_stale",
    "heartbeat_line",
    "heartbeat_path",
    "is_light",
    "scope_for",
    "sidecar_owner",
    "tick_once",
    "write_heartbeat",
]
