"""실행기 내장 예약(스케줄러) + 감시견.

배경: 2026-09-20 09:00, 윈도우 작업 스케줄러가 부른 cmd가 한글 인자를 깨뜨려
예약 명령이 큐에 들어가지 않았고 29분 동안 아무도 몰랐다.
그래서 예약은 **실행기 안에서** 돈다. cmd도, 코드페이지도 끼지 않는다.

흐름
    serve 루프 1회 → ``tick(rt)``
      1) 감시견: 조금 전에 쏜 예약이 정말 큐에서 돌고 있는지 확인(5분)
      2) 지금 쏴야 할 예약을 찾아 ``worker.handle_text`` 로 접수
      3) 실행기 심장박동(heartbeat) 파일 기록

상태는 ``data/schedule_state.json`` 에 남는다. 그래서 실행기를 껐다 켜도
같은 날 같은 예약을 두 번 쏘지 않고, 꺼져 있던 동안 놓친 예약은 켜지자마자 쏜다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

from v2r.engine.scheduler import KST

log = logging.getLogger(__name__)

#: 예약표 파일 이름 (config/ 아래)
SCHEDULE_FILE = "schedule.yaml"
#: 예약 상태 파일 이름 (data/ 아래)
STATE_FILE = "schedule_state.json"
#: 실행기 심장박동 파일 이름 (data/ 아래)
HEARTBEAT_FILE = "serve_heartbeat.json"
#: 예약을 쏜 뒤 이 시간 안에 큐가 움직여야 한다 (초)
WATCHDOG_SECONDS = 300
#: 심장박동이 이보다 오래되면 실행기가 죽은 것으로 본다 (초)
HEARTBEAT_STALE_SECONDS = 180
#: 요일 이름 → 월=0 … 일=6
WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6,
}


class ScheduleConfigError(RuntimeError):
    """예약표를 읽지 못했다."""


def _parse_hhmm(value: Any) -> time:
    """"09:00" / "0900" / 9 → time. 잘못된 값이면 오류."""
    raw = str(value).strip()
    try:
        if ":" in raw:
            h, m = raw.split(":", 1)
        elif len(raw) == 4 and raw.isdigit():
            h, m = raw[:2], raw[2:]
        else:
            h, m = raw, "0"
        return time(int(h), int(m))
    except Exception as exc:  # noqa: BLE001
        raise ScheduleConfigError(f"시각 형식이 잘못됐습니다: {value!r}") from exc


@dataclass
class ScheduleEntry:
    """예약 한 줄."""

    name: str
    time: str
    command: str
    days: Any = "daily"
    enabled: bool = True
    catch_up_minutes: int | None = None
    #: (선택) 이 예약이 정확히 어떤 작업으로 해석돼야 하는가(예: "keyword_discovery_all").
    #: 비워 두면 검증하지 않는다. 옛 실행기가 새 명령을 다른 작업으로 오해석해
    #: 발사한 사고(2026-09-23)를 막는다 — 파서가 다른 작업으로 풀면 발사하지 않는다.
    expect_task: str | None = None
    #: 요일 집합(월=0 … 일=6)
    weekdays: set[int] = field(default_factory=set)

    @property
    def at(self) -> time:
        """예약 시각(KST 시:분)."""
        return _parse_hhmm(self.time)

    def runs_on(self, day: _date) -> bool:
        """이 날짜에 도는 예약인가."""
        return self.enabled and day.weekday() in self.weekdays

    def fire_at(self, day: _date) -> datetime:
        """그 날의 발사 시각(KST)."""
        return datetime.combine(day, self.at, tzinfo=KST)

    def deadline(self, day: _date) -> datetime:
        """따라잡기 마감 시각. 안 적었으면 그날 23:59:59."""
        if self.catch_up_minutes:
            return self.fire_at(day) + timedelta(minutes=int(self.catch_up_minutes))
        return datetime.combine(day, time(23, 59, 59), tzinfo=KST)


def _weekday_set(days: Any) -> set[int]:
    """days 값 → 요일 집합."""
    if days is None or (isinstance(days, str) and days.strip().lower() in ("daily", "매일", "")):
        return set(range(7))
    if isinstance(days, str):
        key = days.strip().lower()
        if key in ("weekdays", "평일"):
            return {0, 1, 2, 3, 4}
        if key in ("weekend", "주말"):
            return {5, 6}
        days = [p for p in key.replace(",", " ").split() if p]
    out: set[int] = set()
    for item in days or []:
        idx = WEEKDAYS.get(str(item).strip().lower()[:3]) or WEEKDAYS.get(str(item).strip().lower())
        if idx is None:
            raise ScheduleConfigError(f"알 수 없는 요일: {item!r}")
        out.add(idx)
    if not out:
        raise ScheduleConfigError("요일이 비었습니다")
    return out


def schedule_path(rt: Any) -> Path:
    """예약표 파일 경로."""
    return Path(rt.settings.config_dir) / SCHEDULE_FILE


def state_path(rt: Any) -> Path:
    """예약 상태 파일 경로."""
    return Path(rt.settings.data_dir) / STATE_FILE


def heartbeat_path(rt: Any) -> Path:
    """실행기 심장박동 파일 경로."""
    return Path(rt.settings.data_dir) / HEARTBEAT_FILE


def load_schedule(rt: Any = None, path: Path | str | None = None) -> list[ScheduleEntry]:
    """``config/schedule.yaml`` → 예약 목록. 파일이 없으면 빈 목록."""
    target = Path(path) if path is not None else schedule_path(rt)
    if not target.exists():
        return []
    with target.open("r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    if not isinstance(data, dict):
        raise ScheduleConfigError("예약표 형식이 잘못됐습니다(맨 위가 목록이 아니어야 합니다)")
    default_catch_up = data.get("catch_up_minutes")
    entries: list[ScheduleEntry] = []
    for raw in data.get("entries") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        command = str(raw.get("command") or "").strip()
        if not name or not command:
            raise ScheduleConfigError(f"예약에 이름이나 명령이 없습니다: {raw!r}")
        entry = ScheduleEntry(
            name=name,
            time=str(raw.get("time") or "09:00"),
            command=command,
            days=raw.get("days", "daily"),
            enabled=bool(raw.get("enabled", True)),
            catch_up_minutes=raw.get("catch_up_minutes", default_catch_up),
            expect_task=(str(raw["expect_task"]).strip() if raw.get("expect_task") else None),
        )
        entry.weekdays = _weekday_set(entry.days)
        _parse_hhmm(entry.time)  # 형식 검증
        entries.append(entry)
    return entries


# --------------------------------------------------------------------
# 상태 저장 (두 번 쏘지 않기 / 놓친 예약 따라잡기)
# --------------------------------------------------------------------
def load_state(rt: Any) -> dict:
    """예약 상태 읽기. 깨져 있으면 빈 상태로 시작한다."""
    path = state_path(rt)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("last_fired", {})
                data.setdefault("pending", [])
                return data
    except Exception as exc:  # noqa: BLE001
        log.warning("예약 상태 파일을 읽지 못했습니다(새로 시작): %s", exc)
    return {"last_fired": {}, "pending": []}


def save_state(rt: Any, state: dict) -> None:
    """예약 상태 저장(원자적)."""
    path = state_path(rt)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        log.warning("예약 상태 저장 실패: %s", exc)


def due_entries(
    entries: list[ScheduleEntry], now_kst: datetime, last_fired: dict
) -> list[ScheduleEntry]:
    """지금 쏴야 할 예약들.

    - 오늘 도는 예약이고
    - 예약 시각이 지났고
    - 따라잡기 마감(기본: 그날 23:59) 전이고
    - 오늘 아직 안 쐈으면 → 쏜다.
    """
    if now_kst.tzinfo is None:
        now_kst = now_kst.replace(tzinfo=KST)
    day = now_kst.date()
    today = day.isoformat()
    out: list[ScheduleEntry] = []
    for entry in entries:
        if not entry.runs_on(day):
            continue
        if last_fired.get(entry.name) == today:
            continue
        if now_kst < entry.fire_at(day) or now_kst > entry.deadline(day):
            continue
        out.append(entry)
    return out


def next_fire(entry: ScheduleEntry, now_kst: datetime, last_fired: dict) -> datetime | None:
    """다음 발사 예정 시각. 도는 요일이 없으면 None."""
    if not entry.enabled or not entry.weekdays:
        return None
    if now_kst.tzinfo is None:
        now_kst = now_kst.replace(tzinfo=KST)
    for offset in range(0, 8):
        day = (now_kst + timedelta(days=offset)).date()
        if day.weekday() not in entry.weekdays:
            continue
        when = entry.fire_at(day)
        if offset == 0:
            if last_fired.get(entry.name) == day.isoformat():
                continue
            if now_kst > entry.deadline(day):
                continue
            return max(when, now_kst) if now_kst > when else when
        return when
    return None


# --------------------------------------------------------------------
# 발사 + 감시견
# --------------------------------------------------------------------
def _handler(rt: Any) -> Callable[[Any, str], dict]:
    """명령 접수 함수(테스트에서 갈아끼울 수 있게 분리)."""
    from v2r.engine import worker

    return worker.handle_text


def _resolved_task(command: str) -> str | None:
    """그 명령을 지금 코드의 파서가 어떤 작업으로 푸는지(발사 전 확인용)."""
    from v2r.command.parser import parse_korean_command

    try:
        spec = parse_korean_command(command)
    except Exception:  # noqa: BLE001
        return None
    return None if spec is None else spec.task


def fire(rt: Any, entry: ScheduleEntry, *, handle_text: Callable | None = None) -> dict:
    """예약 1건을 지금 접수한다.

    `entry.expect_task` 가 있으면 발사 **전에** 지금 파서가 이 명령을 정말
    그 작업으로 푸는지 확인한다. 어긋나면(옛 코드가 새 명령을 다른 작업으로
    오해석하는 사고 2026-09-23 재발 방지) 발사하지 않고 경고 1회만 남긴다.

    옛 `중지` 플래그가 남아 있으면 지운다 — 예약은 중지에 막히면 안 된다.
    """
    from v2r.engine import worker

    if entry.expect_task:
        resolved = _resolved_task(entry.command)
        if resolved != entry.expect_task:
            text = (
                f"예약 '{entry.name}' 발사 건너뜀 — 명령 '{entry.command}' 을(를) "
                f"'{entry.expect_task}' 가 아니라 '{resolved}' 로 해석했습니다"
                " (실행기가 옛 코드일 수 있습니다)"
            )
            log.error("%s", text)
            try:
                rt.events.log(None, "error", text)
            except Exception:  # noqa: BLE001 pragma: no cover
                pass
            try:
                from v2r.channels import notify_all

                notify_all(rt.channels, text, level="critical", category=f"schedule_mismatch:{entry.name}", tag="schedule")
            except Exception as exc:  # noqa: BLE001
                log.warning("예약 불일치 알림 실패: %s", exc)
            return {"ok": False, "error": text, "description": "", "skipped_mismatch": True}

    try:
        worker.clear_stop(rt)
    except Exception as exc:  # noqa: BLE001
        log.warning("중지 플래그 해제 실패: %s", exc)
    # 2026-09-24 사고: "시트 키워드 반영 전체"가 이미 돌고 있는데(작업 186) 예약이 같은
    # 명령을 두 번 더 쏘고(187·188) 감시견이 "예약 실패"를 슬랙에 보냈다. 같은 명령이
    # 아직 queued/running 이면 새로 만들지 않고 그 작업을 그대로 쓴다.
    dup = _open_job_for_command(rt, entry.command)
    if dup is not None:
        try:
            rt.events.log(int(dup["id"]), "info", f"예약 발사 생략: {entry.name} — 같은 명령 작업 {dup['id']} 진행 중")
        except Exception:  # noqa: BLE001 pragma: no cover
            pass
        return {"ok": True, "job_id": int(dup["id"]), "description": entry.command, "deduped": True}
    handler = handle_text or _handler(rt)
    out = handler(rt, entry.command)
    job_id = out.get("job_id")
    ok = bool(out.get("ok"))
    try:
        rt.events.log(
            job_id,
            "info" if ok else "error",
            f"예약 발사: {entry.name} — {entry.command}"
            + ("" if ok else f" (실패: {out.get('error')})"),
        )
    except Exception:  # noqa: BLE001 pragma: no cover
        pass
    return out


def _job_healthy(rt: Any, job_id: Any) -> bool:
    """그 작업이 정말 돌고 있거나 끝났는가(큐에 방치된 게 아닌가)."""
    if job_id is None:
        return False
    try:
        job = rt.jobs.get(int(job_id))
    except Exception:  # noqa: BLE001
        return False
    if job is None:
        return False
    status = str(job.get("status"))
    if status == "queued":
        # 실행기가 다른 작업을 돌리는 중이라 차례를 기다리는 것 — 방치가 아니다(2026-09-24)
        try:
            return bool(rt.jobs.running_jobs())
        except Exception:  # noqa: BLE001
            return False
    return status in ("running", "done", "uncertain", "failed")


def _open_job_for_command(rt: Any, command: str) -> dict | None:
    """같은 명령(설명 텍스트 또는 task)이 아직 queued/running 이면 그 작업."""
    try:
        from v2r.command.parser import parse_korean_command

        spec = parse_korean_command(command)
        task = getattr(spec, "task", "") or ""
        brand = (getattr(spec, "brand", "") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not task:
        return None
    try:
        for job in rt.jobs.open_jobs():
            if str(job.get("task") or "") != task:
                continue
            try:
                import json as _json

                jb = (_json.loads(job.get("spec_json") or "{}").get("brand") or "").strip()
            except Exception:  # noqa: BLE001
                jb = ""
            if jb == brand:
                return job
    except Exception:  # noqa: BLE001
        return None
    return None


def check_pending(
    rt: Any, state: dict, now_kst: datetime, *, handle_text: Callable | None = None
) -> list[dict]:
    """감시견: 쏜 예약이 5분 안에 움직였는지 본다. 아니면 한 번 다시 쏜다."""
    from v2r.channels import notify_all

    pending = state.get("pending") or []
    keep: list[dict] = []
    actions: list[dict] = []
    for item in pending:
        name = item.get("name", "")
        if _job_healthy(rt, item.get("job_id")):
            actions.append({"name": name, "action": "ok"})
            continue
        try:
            fired_at = datetime.fromisoformat(str(item.get("fired_at")))
        except Exception:  # noqa: BLE001
            fired_at = now_kst
        if fired_at.tzinfo is None:
            fired_at = fired_at.replace(tzinfo=KST)
        if (now_kst - fired_at).total_seconds() < WATCHDOG_SECONDS:
            keep.append(item)  # 아직 기다려 본다
            continue

        entry = ScheduleEntry(name=name, time="00:00", command=item.get("command", ""))
        if int(item.get("retries", 0)) == 0:
            # 예약 미복구 사건의 첫 신호라 critical(작업 미복구 카테고리) — 바로
            # 뒤에 "자동 복구 실패"가 나면 같은 사건으로 묶여 되풀이 억제된다.
            notify_all(
                rt.channels,
                f"예약 실패: {name} — 자동 재시도",
                level="critical",
                category=f"schedule_retry:{name}",
                tag="schedule",
            )
            try:
                rt.events.log(None, "warn", f"예약 감시견: {name} 큐가 움직이지 않아 재시도")
            except Exception:  # noqa: BLE001 pragma: no cover
                pass
            out = fire(rt, entry, handle_text=handle_text)
            keep.append(
                {
                    "name": name,
                    "command": entry.command,
                    "job_id": out.get("job_id"),
                    "fired_at": now_kst.isoformat(timespec="seconds"),
                    "retries": 1,
                }
            )
            actions.append({"name": name, "action": "retry", "job_id": out.get("job_id")})
            continue

        reason = item.get("error") or "재시도 뒤에도 큐가 움직이지 않았습니다"
        notify_all(
            rt.channels,
            f"예약 실패: {name} — 자동 복구 실패 ({reason})",
            level="critical",
            category=f"schedule_recovery_failed:{name}",
            tag="schedule",
        )
        try:
            rt.events.log(None, "error", f"예약 복구 실패: {name} — {reason}")
        except Exception:  # noqa: BLE001 pragma: no cover
            pass
        actions.append({"name": name, "action": "failed"})
    state["pending"] = keep
    return actions


def write_heartbeat(rt: Any, now_kst: datetime | None = None) -> None:
    """실행기가 살아 있다는 표시. 실패해도 루프를 세우지 않는다.

    이 함수는 **본 루프(긴 작업 중의 `touch_heartbeat`)와 사이드카 스레드
    (`schedule.tick`)가 동시에** 부를 수 있다(사고 2026-09-26: 두 스레드가 같은
    `.json.tmp`에 동시에 쓰다가 `WinError 32`/`Permission denied`로 계속
    실패했다). 임시 파일 이름을 프로세스+스레드+호출마다 다르게 해서 겹치지
    않게 한다. 그래도 실패하면(다른 프로세스가 잠깐 물고 있는 등) 경고만
    남기고 넘어간다 — 심장박동 기록 실패가 실행을 막아서는 안 된다.
    """
    now_kst = now_kst or datetime.now(KST)
    path = heartbeat_path(rt)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(
            f"{path.stem}.{os.getpid()}.{threading.get_ident()}.{_time.monotonic_ns()}.tmp"
        )
        tmp.write_text(
            json.dumps(
                {"at": now_kst.isoformat(timespec="seconds"), "pid": os.getpid()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        log.warning("심장박동 기록 실패: %s", exc)


def heartbeat_age_seconds(rt: Any, now_kst: datetime | None = None) -> float | None:
    """심장박동이 몇 초 전 것인가. 파일이 없으면 None."""
    now_kst = now_kst or datetime.now(KST)
    path = heartbeat_path(rt)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(str(data.get("at")))
        if at.tzinfo is None:
            at = at.replace(tzinfo=KST)
        return (now_kst - at).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def tick(
    rt: Any,
    now_kst: datetime | None = None,
    *,
    handle_text: Callable | None = None,
    entries: list[ScheduleEntry] | None = None,
) -> dict:
    """serve 루프 1회분의 예약 처리. 어떤 예외로도 루프를 죽이지 않는다."""
    now_kst = now_kst or datetime.now(KST)
    if now_kst.tzinfo is None:
        now_kst = now_kst.replace(tzinfo=KST)
    try:
        items = entries if entries is not None else load_schedule(rt)
    except Exception as exc:  # noqa: BLE001
        log.warning("예약표를 읽지 못했습니다: %s", exc)
        return {"fired": [], "watchdog": [], "error": str(exc)}

    state = load_state(rt)
    watchdog = check_pending(rt, state, now_kst, handle_text=handle_text)

    fired: list[dict] = []
    for entry in due_entries(items, now_kst, state.get("last_fired") or {}):
        out = fire(rt, entry, handle_text=handle_text)
        state.setdefault("last_fired", {})[entry.name] = now_kst.date().isoformat()
        if out.get("ok") and not out.get("stopped"):
            state.setdefault("pending", []).append(
                {
                    "name": entry.name,
                    "command": entry.command,
                    "job_id": out.get("job_id"),
                    "fired_at": now_kst.isoformat(timespec="seconds"),
                    "retries": 0,
                }
            )
        else:
            state.setdefault("pending", []).append(
                {
                    "name": entry.name,
                    "command": entry.command,
                    "job_id": None,
                    "fired_at": now_kst.isoformat(timespec="seconds"),
                    "retries": 0,
                    "error": out.get("error"),
                }
            )
        fired.append({"name": entry.name, "job_id": out.get("job_id"), "ok": bool(out.get("ok"))})

    save_state(rt, state)
    write_heartbeat(rt, now_kst)
    return {"fired": fired, "watchdog": watchdog}


def run_now(rt: Any, name: str, *, handle_text: Callable | None = None) -> dict:
    """`예약 지금 실행 <이름>` — 예약 1건을 즉시 쏜다."""
    entries = load_schedule(rt)
    wanted = (name or "").strip()
    match = next((e for e in entries if e.name == wanted), None)
    if match is None:
        match = next((e for e in entries if wanted and wanted in e.name), None)
    if match is None:
        names = ", ".join(e.name for e in entries) or "(예약 없음)"
        return {"ok": False, "message": f"그런 예약이 없습니다: {wanted}\n등록된 예약: {names}"}

    now_kst = datetime.now(KST)
    out = fire(rt, match, handle_text=handle_text)
    state = load_state(rt)
    state.setdefault("last_fired", {})[match.name] = now_kst.date().isoformat()
    state.setdefault("pending", []).append(
        {
            "name": match.name,
            "command": match.command,
            "job_id": out.get("job_id"),
            "fired_at": now_kst.isoformat(timespec="seconds"),
            "retries": 0,
        }
    )
    save_state(rt, state)
    return {
        "ok": bool(out.get("ok")),
        "message": f"예약 '{match.name}' 지금 실행: {match.command}"
        + (f" (작업 {out.get('job_id')})" if out.get("job_id") else ""),
        "job_id": out.get("job_id"),
    }


def schedule_report(rt: Any, now_kst: datetime | None = None) -> str:
    """`예약 목록` 보고문(한국어)."""
    now_kst = now_kst or datetime.now(KST)
    try:
        entries = load_schedule(rt)
    except Exception as exc:  # noqa: BLE001
        return f"예약표를 읽지 못했습니다: {exc}"
    if not entries:
        return "등록된 예약이 없습니다 (config/schedule.yaml)"
    state = load_state(rt)
    last = state.get("last_fired") or {}
    lines = ["예약 목록 (실행기 내장, 한국 시각)"]
    for entry in entries:
        nxt = next_fire(entry, now_kst, last)
        lines.append(
            f"- {entry.name} / {entry.time} / {entry.days}"
            f" / {'켜짐' if entry.enabled else '꺼짐'}"
        )
        lines.append(f"    명령: {entry.command}")
        if nxt is None:
            when = "없음"
        elif nxt <= now_kst:
            when = f"지금 (밀린 예약, {entry.time} 것)"
        else:
            when = nxt.strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"    다음 실행: {when} / 마지막 실행: {last.get(entry.name, '기록 없음')}"
        )
    waiting = state.get("pending") or []
    if waiting:
        lines.append("확인 중인 예약: " + ", ".join(str(p.get("name")) for p in waiting))
    return "\n".join(lines)


def health_report(rt: Any, now_kst: datetime | None = None) -> str:
    """`python -m v2r health` 보고문: 심장박동 + 예약 상태 + 세션."""
    now_kst = now_kst or datetime.now(KST)
    age = heartbeat_age_seconds(rt, now_kst)
    if age is None:
        beat = "실행기 심장박동: 없음 (한 번도 켜진 적이 없거나 파일이 지워졌습니다)"
    elif age > HEARTBEAT_STALE_SECONDS:
        beat = f"실행기 심장박동: {int(age)}초 전 — 멈춘 것 같습니다 (기준 {HEARTBEAT_STALE_SECONDS}초)"
    else:
        beat = f"실행기 심장박동: {int(age)}초 전 — 정상"
    try:
        from v2r.engine.sidecar import heartbeat_line

        side = heartbeat_line(rt, now_kst)
    except Exception as exc:  # noqa: BLE001
        side = f"사이드카 심장박동: 확인 실패 ({exc})"
    try:
        from v2r.engine.lock import lock_report

        lines = [beat, side, lock_report(rt), "", schedule_report(rt, now_kst)]
    except Exception as exc:  # noqa: BLE001 - 잠금 상태를 못 읽어도 보고는 나간다
        lines = [
            beat,
            side,
            f"단일 실행기 잠금: 확인 실패 ({exc})",
            "",
            schedule_report(rt, now_kst),
        ]
    try:
        from v2r.__main__ import session_report_text

        lines += ["", session_report_text(rt.client.session_report())]
    except Exception as exc:  # noqa: BLE001
        lines += ["", f"세션 상태를 읽지 못했습니다: {exc}"]
    return "\n".join(lines)


def heartbeat_is_stale(rt: Any, limit_seconds: int = HEARTBEAT_STALE_SECONDS) -> bool:
    """심장박동이 오래됐는가(없으면 오래된 것으로 본다)."""
    age = heartbeat_age_seconds(rt)
    return age is None or age > limit_seconds


__all__ = [
    "KST",
    "ScheduleEntry",
    "ScheduleConfigError",
    "check_pending",
    "due_entries",
    "fire",
    "health_report",
    "heartbeat_age_seconds",
    "heartbeat_is_stale",
    "load_schedule",
    "load_state",
    "next_fire",
    "run_now",
    "save_state",
    "schedule_report",
    "tick",
    "write_heartbeat",
]
