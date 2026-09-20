"""모든 작업 감시견 (사용자 규칙: "모든 명령에 감시 에이전트 붙여").

예약만이 아니라 **큐에 들어온 모든 작업**을 실행기가 매 틱 지켜본다.

    대기(queued)  → 5분 안에 시작해야 한다. 안 하면 알리고 스스로 고친다
                    (중지 플래그 해제 + 만료된 실행기 리스 반납).
                    10분이 지나도 안 되면 이유를 붙여 다시 알린다.
    실행(running) → 15분 안에 뭔가 움직여야 한다(이벤트 또는 갱신 시각).
                    30분 넘게 멈췄고 리스도 끊겼으면 실패로 정리하고
                    같은 명령을 **한 번만** 다시 등록한다.
    종료(failed)  → 한 번만 알린다. 레이트 제한·네트워크처럼 다시 해볼 만한
                    실패면 한 번 자동 재등록한다. 문법·설정 오류는 재시도하지 않는다.

알림 중복을 막기 위해 상태는 ``data/monitor_state.json`` 에 남는다(재시작해도 유지).

**건드리지 않는 것** (장애 2026-09-20 A, 자세히는 `docs/reference/ops-scheduling.md` §3-1)

    과거      → 실행기가 켜지기 10분보다 더 전에 멈춘 작업(어제 기록)은 못 본 척한다.
    중지·취소 → 사용자가 끊은 것은 "다시 해볼 실패"가 아니다.
    재등록본  → 감시가 만든 작업은 또 만들지 않는다(원래 작업당 1회).
    부분 성공 → 한 줄이라도 올라간 발행은 보고만 한다(다시 돌리면 중복 발행).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from v2r.engine.recovery_rules import KNOWN_ACTIONS, match_rule, rule_by_name, rule_names
from v2r.engine.scheduler import KST

log = logging.getLogger(__name__)

#: 감시 상태 파일 이름 (data/ 아래)
STATE_FILE = "monitor_state.json"
#: queued → running 까지 봐 주는 시간(초)
START_GRACE_S = 300
#: 그 뒤로 한 번 더 기다리는 시간(초)
START_SECOND_GRACE_S = 600
#: running 이 아무 기척 없이 버틸 수 있는 시간(초)
STALL_S = 15 * 60
#: 이 이상 멈추면 실패로 정리하고 다시 등록한다(초)
DEAD_S = 30 * 60
#: 상태 파일에 남겨 두는 최근 알림 수
ALERT_KEEP = 20
#: 다시 해볼 만한 실패(레이트 제한·네트워크)
RETRYABLE_RE = re.compile(
    r"429|레이트|요청\s*제한|너무\s*많|rate.?limit|timeout|timed out|시간\s*초과"
    r"|네트워크|network|connection|연결|일시적|temporarily",
    re.I,
)
#: 감시 설정 파일 이름 (config/ 아래)
CONFIG_FILE = "monitor.yaml"
#: 같은 조회를 다시 하지 않는 기본 시간(초) — 틱을 가볍게 유지한다
DEFAULT_CACHE_S = 30
#: Tier 1(모델 분류) 하루 기본 상한
DEFAULT_LLM_CAP = 20
#: 하루 요약을 보내는 기본 시각
DEFAULT_SUMMARY_AT = "08:35"
#: Tier 1 프롬프트 — 한 번만, 짧게, 정해진 이름 중에서 고르게 한다
DIAGNOSE_SYSTEM = (
    "너는 한국어 자동 발행기의 오류 분류기다. 오류 문구 하나를 받고"
    " 아래 규칙 이름 중 가장 가까운 하나와 조치를 고른다.\n"
    "규칙 이름: {rules}\n조치: retry, wait, refresh, human\n"
    'JSON 한 개만 답한다: {{"rule": "<이름>", "action": "<조치>",'
    ' "explain": "<한국어 한 줄 설명>"}}'
)

#: 감시 대상에서 빼는 작업(조회·중지처럼 순식간에 끝나는 것)
SKIP_TASKS = frozenset(
    {"stop", "status", "dashboard", "schedule_list", "monitor_status", "pending_report"}
)
#: 실행기가 켜지기 이 시간보다 더 전에 멈춘 작업은 **과거**로 본다(초).
#: 장애 2026-09-20: 켜자마자 어제 실패한 작업 59·62를 알리고 60을 다시 등록했다.
HISTORY_GRACE_S = 600
#: 절대 다시 등록하지 않는 실패 문구(중지·취소·리스 상실은 "다시 해볼 실패"가 아니다)
NO_RETRY_RE = re.compile(r"중지|취소|cancel|리스\s*상실|lease", re.I)
#: 재등록으로 만들어진 작업임을 알리는 멱등 키 꼬리표
RETRY_MARK = "|monitor-retry"
#: 재시도 사슬 기록을 남겨 두는 최대 개수
CHAIN_KEEP = 200
#: publish 결과에서 실패로 세는 줄 상태
FAILED_ROW_STATUSES = frozenset({"failed", "error", "skipped"})


def load_config(rt: Any) -> dict:
    """`config/monitor.yaml`. 없으면 기본값."""
    import yaml

    path = Path(rt.settings.config_dir) / CONFIG_FILE
    data: dict = {}
    try:
        if path.exists():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    except Exception as exc:  # noqa: BLE001
        log.warning("감시 설정을 읽지 못했습니다(기본값 사용): %s", exc)
    llm = data.get("llm_diagnosis") or {}
    return {
        "cache_seconds": int(
            DEFAULT_CACHE_S if data.get("cache_seconds") is None else data["cache_seconds"]
        ),
        "daily_summary_at": str(data.get("daily_summary_at") or DEFAULT_SUMMARY_AT),
        "llm_enabled": bool(llm.get("enabled", True)),
        "llm_daily_cap": int(llm.get("daily_cap") or DEFAULT_LLM_CAP),
    }


def state_path(rt: Any) -> Path:
    """감시 상태 파일 경로."""
    return Path(rt.settings.data_dir) / STATE_FILE


def load_state(rt: Any) -> dict:
    """감시 상태 읽기. 깨져 있으면 빈 상태."""
    path = state_path(rt)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("jobs", {})
                data.setdefault("alerts", [])
                return data
    except Exception as exc:  # noqa: BLE001
        log.warning("감시 상태 파일을 읽지 못했습니다(새로 시작): %s", exc)
    return {"jobs": {}, "alerts": []}


def save_state(rt: Any, state: dict) -> None:
    """감시 상태 저장(원자적)."""
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
        log.warning("감시 상태 저장 실패: %s", exc)


def _ts(value: Any) -> datetime | None:
    """ISO 문자열 → KST datetime."""
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return out.replace(tzinfo=KST) if out.tzinfo is None else out


def _today(now: datetime) -> str:
    return now.date().isoformat()


def _daily(state: dict, now: datetime) -> dict:
    """그날의 집계 칸."""
    box = state.setdefault("daily", {}).setdefault(
        _today(now),
        {"alerts": 0, "recovered": 0, "llm_calls": 0, "llm_tokens": 0, "summary_sent": False},
    )
    # 일주일 넘은 집계는 버린다
    keys = sorted(state["daily"])
    for old in keys[:-7]:
        state["daily"].pop(old, None)
    return box


def _alert(rt: Any, state: dict, text: str) -> None:
    """알림 1건: 채널 + 이벤트 + 상태 파일 기록."""
    from v2r.channels import notify_all

    try:
        notify_all(rt.channels, text)
    except Exception as exc:  # noqa: BLE001 pragma: no cover
        log.warning("감시 알림 실패: %s", exc)
    now = datetime.now(KST)
    alerts = state.setdefault("alerts", [])
    alerts.append({"at": now.isoformat(timespec="seconds"), "text": text})
    del alerts[:-ALERT_KEEP]
    _daily(state, now)["alerts"] += 1


def last_progress(rt: Any, job: dict) -> datetime | None:
    """그 작업이 마지막으로 움직인 시각(갱신 시각 또는 마지막 이벤트)."""
    marks = [_ts(job.get("updated_at")), _ts(job.get("created_at"))]
    try:
        rows = rt.events.recent(int(job["id"]), limit=1)
        if rows:
            marks.append(_ts(rows[0].get("created_at")))
    except Exception:  # noqa: BLE001
        pass
    marks = [m for m in marks if m is not None]
    return max(marks) if marks else None


def _job_state(state: dict, job_id: int) -> dict:
    return state.setdefault("jobs", {}).setdefault(str(job_id), {})


def _retryable_rows(job: dict) -> int:
    """publish_* 실패 결과에서 다시 해볼 만한 줄 수."""
    try:
        result = json.loads(job.get("result_json") or "{}")
    except Exception:  # noqa: BLE001
        return 0
    return len(result.get("failures") or [])


def _success_rows(job: dict) -> int:
    """publish_* 결과에서 **성공한 줄** 수. 하나라도 있으면 부분 성공이다."""
    try:
        result = json.loads(job.get("result_json") or "{}")
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(result, dict):
        return 0
    rows = [
        r
        for r in (result.get("results") or [])
        if isinstance(r, dict) and str(r.get("status") or "") not in FAILED_ROW_STATUSES
    ]
    if rows:
        return len(rows)
    total = 0
    for counts in (result.get("per_cafe") or {}).values():
        if isinstance(counts, dict):
            try:
                total += int(counts.get("ok") or 0)
            except (TypeError, ValueError):
                continue
    return total


def root_key(job: dict) -> str:
    """재등록 사슬의 **맨 처음 작업**을 가리키는 키."""
    key = str(job.get("idem_key") or f"job-{job.get('id')}")
    return key.split(RETRY_MARK)[0]


def no_retry_reason(job: dict) -> str | None:
    """다시 등록하면 안 되는 이유(있으면 한국어 한 줄, 없으면 None).

    장애 2026-09-20: 어제 실패한 작업 60(다시 해볼 수 없는 줄 2건)을 67로,
    사용자가 `중지` 로 끊은 67을 다시 70으로 등록했다. 아래가 그 재발 방지다.
    """
    status = str(job.get("status") or "")
    if status == "cancelled":
        return "사용자가 중지한 작업입니다"
    error = str(job.get("error") or "")
    if error and NO_RETRY_RE.search(error):
        return "중지·취소·리스 상실은 다시 해볼 실패가 아닙니다"
    if RETRY_MARK in str(job.get("idem_key") or ""):
        return "이미 감시가 한 번 다시 등록한 작업입니다"
    if str(job.get("task") or "").startswith("publish_"):
        rows = _success_rows(job)
        if rows:
            return f"일부는 성공했습니다(성공 {rows}건) — 다시 올리면 중복이 됩니다"
    return None


def _chain_used(state: dict, job: dict) -> bool:
    """이 사슬(원래 작업 기준)에서 이미 재등록을 썼는가."""
    return int((state.get("retry_chain") or {}).get(root_key(job), 0)) > 0


def _chain_mark(state: dict, job: dict) -> None:
    """사슬에 재등록 1회를 기록한다(기록은 최근 것만 남긴다)."""
    chain = state.setdefault("retry_chain", {})
    chain[root_key(job)] = 1
    for old in list(chain)[:-CHAIN_KEEP]:
        chain.pop(old, None)


def may_retry(state: dict, job: dict, js: dict) -> str | None:
    """재등록해도 되는가. 안 되면 이유를 돌려준다."""
    if int(js.get("retries", 0)) > 0:
        return "이미 한 번 다시 등록했습니다"
    if _chain_used(state, job):
        return "같은 명령은 한 번만 다시 등록합니다"
    return no_retry_reason(job)


def started_at(rt: Any, state: dict, now: datetime) -> datetime:
    """이번 실행기가 켜진 시각. 프로세스마다 첫 틱에서 정해 상태 파일에 남긴다."""
    scratch = getattr(rt, "scratch", None)
    cached = scratch.get("_monitor_started_at") if isinstance(scratch, dict) else None
    if cached is None:
        cached = now
        if isinstance(scratch, dict):
            scratch["_monitor_started_at"] = cached
        state["started_at"] = cached.isoformat(timespec="seconds")
    return cached


def is_history(job: dict, since: datetime) -> bool:
    """실행기가 켜지기 한참 전에 멈춘 작업(=과거 기록)인가."""
    mark = _ts(job.get("updated_at")) or _ts(job.get("created_at"))
    return mark is not None and mark < since


def _requeue(rt: Any, job: dict, marker: str) -> int | None:
    """같은 명령을 한 번 더 등록한다(멱등 키에 꼬리표를 붙여 중복을 피한다)."""
    from v2r.command.spec import TaskSpec

    try:
        spec = TaskSpec.from_json(job["spec_json"])
        key = f"{job.get('idem_key')}|{marker}"
        return int(rt.jobs.enqueue(spec, key))
    except Exception as exc:  # noqa: BLE001
        log.warning("작업 재등록 실패: %s", exc)
        return None


def error_signature(error: str) -> str:
    """오류 문구의 지문. 숫자·아이디를 지워 같은 종류를 한 덩어리로 본다."""
    text = re.sub(r"\d+", "#", str(error or "")).strip().lower()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def diagnose(rt: Any, state: dict, error: str, now: datetime, cfg: dict | None = None) -> dict:
    """오류 1건을 해석한다.

    Tier 0 — 규칙표(`recovery_rules`)로 맞히면 **토큰 0**.
    Tier 1 — 표에 없는 문구만, 같은 지문에 대해 **하루 한 번**, 상한 안에서만
             Haiku를 한 번 부른다. 절대 반복 호출하지 않는다.
    """
    cfg = cfg or load_config(rt)
    rule = match_rule(error)
    if rule is not None:
        return {
            "tier": 0,
            "rule": rule.name,
            "action": rule.action,
            "explain": rule.hint,
            "resume": rule.resume,
        }

    sig = error_signature(error)
    seen = state.setdefault("diagnosed", {}).setdefault(_today(now), {})
    if sig in seen:
        return {**seen[sig], "tier": 1, "cached": True}

    box = _daily(state, now)
    router = getattr(rt, "llm", None)
    if (
        not cfg.get("llm_enabled")
        or router is None
        or not getattr(router, "enabled", False)
        or int(box.get("llm_calls", 0)) >= int(cfg.get("llm_daily_cap", DEFAULT_LLM_CAP))
    ):
        return {
            "tier": 2,
            "rule": "unknown",
            "action": "human",
            "explain": "처음 보는 오류입니다. 사람이 봐야 합니다.",
            "resume": "",
        }

    before = sum(int(v) for v in (getattr(router, "usage", {}) or {}).values())
    try:
        data = router.complete_json(
            "error_diagnosis",
            DIAGNOSE_SYSTEM.format(rules=", ".join(rule_names())),
            str(error)[:500],
            max_tokens=200,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Tier 1 오류 분류 실패(사람에게 넘김): %s", exc)
        return {
            "tier": 2,
            "rule": "unknown",
            "action": "human",
            "explain": "처음 보는 오류입니다. 사람이 봐야 합니다.",
            "resume": "",
        }
    after = sum(int(v) for v in (getattr(router, "usage", {}) or {}).values())
    box["llm_calls"] = int(box.get("llm_calls", 0)) + 1
    box["llm_tokens"] = int(box.get("llm_tokens", 0)) + max(0, after - before)

    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {}
    if not isinstance(data, dict):
        data = {}
    name = str(data.get("rule") or "unknown")
    action = str(data.get("action") or "human")
    known = rule_by_name(name)
    if action not in KNOWN_ACTIONS:
        action = known.action if known else "human"
    out = {
        "tier": 1,
        "rule": name,
        "action": action,
        "explain": str(data.get("explain") or (known.hint if known else "사람이 봐야 합니다.")),
        "resume": known.resume if known else "",
    }
    seen[sig] = out
    return out


def _snapshot(rt: Any, cache_seconds: int) -> tuple[list[dict], list[dict]]:
    """작업 목록 조회 결과를 잠깐 캐시한다(틱을 가볍게)."""
    cache = getattr(rt, "scratch", {}).get("_monitor_cache")
    if cache and (_time.monotonic() - cache[0]) < max(0, cache_seconds):
        return cache[1], cache[2]
    open_jobs = rt.jobs.open_jobs()
    recent = rt.jobs.recent(limit=30)
    try:
        rt.scratch["_monitor_cache"] = (_time.monotonic(), open_jobs, recent)
    except Exception:  # noqa: BLE001 pragma: no cover
        pass
    return open_jobs, recent


def daily_summary(rt: Any, state: dict, now: datetime, cfg: dict) -> str | None:
    """하루 한 번(기본 08:35) 요약 한 줄. 이미 보냈으면 None."""
    try:
        h, m = str(cfg.get("daily_summary_at") or DEFAULT_SUMMARY_AT).split(":")
        at = time(int(h), int(m))
    except Exception:  # noqa: BLE001
        at = time(8, 35)
    box = _daily(state, now)
    if box.get("summary_sent") or now.time() < at:
        return None
    today = _today(now)
    try:
        rows = rt.jobs.recent(limit=200)
    except Exception:  # noqa: BLE001
        rows = []
    mine = [r for r in rows if str(r.get("created_at", ""))[:10] == today]
    done = sum(1 for r in mine if str(r.get("status")) == "done")
    failed = sum(1 for r in mine if str(r.get("status")) == "failed")
    text = (
        f"[감시 요약 {today}] 작업 {len(mine)}건 (완료 {done} / 실패 {failed}),"
        f" 자동 복구 {int(box.get('recovered', 0))}건,"
        f" 알림 {int(box.get('alerts', 0))}건,"
        f" 모델 진단 {int(box.get('llm_calls', 0))}회"
        f" (토큰 {int(box.get('llm_tokens', 0))})"
    )
    from v2r.channels import notify_all

    try:
        notify_all(rt.channels, text)
    except Exception as exc:  # noqa: BLE001 pragma: no cover
        log.warning("하루 요약 전송 실패: %s", exc)
    box["summary_sent"] = True
    return text


# --------------------------------------------------------------------
def _watch_queued(rt: Any, state: dict, job: dict, now: datetime) -> list[dict]:
    """대기만 하고 시작하지 않는 작업을 살린다."""
    job_id = int(job["id"])
    js = _job_state(state, job_id)
    created = _ts(job.get("created_at")) or now
    waited = (now - created).total_seconds()
    acts: list[dict] = []

    if waited >= START_GRACE_S and not js.get("start_alert"):
        js["start_alert"] = True
        _alert(rt, state, f"작업 {job_id} 시작 지연 ({int(waited // 60)}분) — 자동 복구 시도")
        healed: list[str] = []
        try:
            from v2r.engine import worker

            if worker.stop_requested(rt):
                worker.clear_stop(rt)
                healed.append("중지 플래그 해제")
        except Exception as exc:  # noqa: BLE001
            log.warning("중지 플래그 해제 실패: %s", exc)
        try:
            if rt.jobs.release_stale_lease():
                healed.append("만료된 실행기 리스 반납")
        except Exception as exc:  # noqa: BLE001
            log.warning("리스 반납 실패: %s", exc)
        rt.events.log(job_id, "warn", "감시: 시작 지연 — " + (", ".join(healed) or "고칠 것 없음"))
        acts.append({"job_id": job_id, "action": "start_delay", "healed": healed})

    elif waited >= START_SECOND_GRACE_S and js.get("start_alert") and not js.get("start_alert2"):
        js["start_alert2"] = True
        lease = rt.jobs.lease_info() or {}
        owner, until = lease.get("owner"), lease.get("until")
        reason = (
            f"실행기 리스를 {owner} 가 {until} 까지 잡고 있습니다"
            if owner
            else "실행기가 큐를 꺼내지 못하고 있습니다(실행기가 꺼져 있을 수 있습니다)"
        )
        _alert(rt, state, f"작업 {job_id} 아직 시작 못 함 ({int(waited // 60)}분) — {reason}")
        rt.events.log(job_id, "error", f"감시: 시작 지연 지속 — {reason}")
        acts.append({"job_id": job_id, "action": "start_blocked", "reason": reason})
    return acts


def _watch_running(rt: Any, state: dict, job: dict, now: datetime) -> list[dict]:
    """움직이지 않는 실행 중 작업을 정리하고 한 번 다시 돌린다."""
    job_id = int(job["id"])
    js = _job_state(state, job_id)
    mark = last_progress(rt, job) or now
    idle = (now - mark).total_seconds()
    lease_until = _ts(job.get("lease_until"))
    acts: list[dict] = []

    if idle >= STALL_S and not js.get("stall_alert"):
        js["stall_alert"] = True
        _alert(rt, state, f"작업 {job_id} 정체 {int(idle // 60)}분 — 계속 지켜봅니다")
        rt.events.log(job_id, "warn", f"감시: 진행 없음 {int(idle // 60)}분")
        acts.append({"job_id": job_id, "action": "stalled"})

    if idle >= DEAD_S and (lease_until is None or lease_until <= now) and not js.get("reaped"):
        js["reaped"] = True
        block = may_retry(state, job, js)
        try:
            rt.jobs.finish(job_id, "failed", None, f"감시: {int(idle // 60)}분 멈춤 — 자동 정리")
        except Exception as exc:  # noqa: BLE001
            log.warning("정체 작업 정리 실패: %s", exc)
        new_id = None
        if block is None:
            js["retries"] = 1
            _chain_mark(state, job)
            new_id = _requeue(rt, job, "monitor-retry1")
        tail = (
            f" 같은 명령을 작업 {new_id} 로 다시 등록했습니다."
            if new_id
            else (f" 다시 등록하지 않았습니다: {block}" if block else " 재등록 실패")
        )
        _alert(
            rt,
            state,
            f"작업 {job_id} 이(가) {int(idle // 60)}분 멈춰 실패로 정리했습니다." + tail,
        )
        rt.events.log(job_id, "error", f"감시: 멈춤 정리 + 재등록 → {new_id}")
        acts.append(
            {"job_id": job_id, "action": "reaped", "new_job_id": new_id, "blocked": block}
        )
    return acts


def _watch_finished(
    rt: Any, state: dict, job: dict, now: datetime, cfg: dict | None = None
) -> list[dict]:
    """끝난 작업: 실패는 한 번만 알리고, 다시 해볼 만하면 한 번 재등록.

    알림에는 **규칙 이름 / 시도한 조치 / 그대로 보내면 되는 재개 명령**을 넣는다
    (Tier 2 = 사람).
    """
    job_id = int(job["id"])
    js = _job_state(state, job_id)
    if js.get("failed_notified"):
        return []
    js["failed_notified"] = True
    error = str(job.get("error") or "사유 없음")
    task = str(job.get("task") or "")
    verdict = diagnose(rt, state, error, now, cfg)

    lines = [f"작업 {job_id} 실패: {error}"]
    if task.startswith("publish"):
        rows = _retryable_rows(job)
        if rows:
            lines[0] += f" (다시 해볼 줄 {rows}건)"
    lines.append(f"판정: {verdict['rule']} / 조치 {verdict['action']} (Tier {verdict['tier']})")
    lines.append(f"설명: {verdict['explain']}")

    acts: list[dict] = []
    new_id = None
    retryable = bool(verdict["action"] == "retry" or RETRYABLE_RE.search(error))
    block = may_retry(state, job, js) if retryable else None
    if retryable and block is None:
        js["retries"] = 1
        _chain_mark(state, job)
        new_id = _requeue(rt, job, "monitor-retry1")
        if new_id:
            lines.append(f"시도함: 같은 명령을 작업 {new_id} 로 한 번 더 등록했습니다")
            _daily(state, now)["recovered"] += 1
    elif block:
        lines.append(f"다시 등록하지 않았습니다: {block}")
    if verdict["action"] == "refresh":
        try:
            rt.client.maintain_auth()
            lines.append("시도함: 로그인 토큰을 새로 받았습니다")
            _daily(state, now)["recovered"] += 1
        except Exception as exc:  # noqa: BLE001
            lines.append(f"시도함: 토큰 갱신 실패 ({exc})")
    if new_id is None and verdict["action"] != "wait":
        resume = verdict.get("resume") or _command_text(job)
        if resume:
            lines.append(f"다시 하려면 이렇게 보내세요: {resume}")
    _alert(rt, state, "\n".join(lines))
    acts.append(
        {
            "job_id": job_id,
            "action": "failed",
            "new_job_id": new_id,
            "rule": verdict["rule"],
            "blocked": block,
        }
    )
    return acts


def _command_text(job: dict) -> str:
    """그 작업을 만든 원래 한국어 문장."""
    try:
        return str(json.loads(job.get("spec_json") or "{}").get("notes") or "")
    except Exception:  # noqa: BLE001
        return ""


def tick(rt: Any, now: datetime | None = None) -> dict:
    """serve 루프 1회분의 작업 감시. 어떤 예외로도 루프를 죽이지 않는다."""
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    state = load_state(rt)
    cfg = load_config(rt)
    actions: list[dict] = []
    watched = 0
    try:
        open_jobs, recent = _snapshot(rt, int(cfg["cache_seconds"]))
    except Exception as exc:  # noqa: BLE001
        log.warning("감시 대상 조회 실패: %s", exc)
        return {"watched": 0, "actions": [], "error": str(exc)}

    # 실행기가 켜지기 한참 전에 멈춘 작업(어제 기록)은 건드리지 않는다.
    # 알리지도, 다시 등록하지도 않는다 (장애 2026-09-20 B안).
    since = started_at(rt, state, now) - timedelta(seconds=HISTORY_GRACE_S)
    skipped_history = 0

    open_ids = set()
    for job in open_jobs:
        if str(job.get("task")) in SKIP_TASKS:
            continue
        if is_history(job, since):
            skipped_history += 1
            continue
        open_ids.add(int(job["id"]))
        watched += 1
        try:
            if str(job.get("status")) == "queued":
                actions += _watch_queued(rt, state, job, now)
            else:
                actions += _watch_running(rt, state, job, now)
        except Exception as exc:  # noqa: BLE001
            log.warning("작업 %s 감시 실패: %s", job.get("id"), exc)

    for job in recent:
        if str(job.get("status")) != "failed" or str(job.get("task")) in SKIP_TASKS:
            continue
        if is_history(job, since):
            skipped_history += 1
            continue
        try:
            actions += _watch_finished(rt, state, job, now, cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("실패 작업 %s 감시 실패: %s", job.get("id"), exc)

    # 끝난 지 오래된 작업의 기록은 지운다(상태 파일이 무한정 커지지 않게)
    alive = open_ids | {int(j["id"]) for j in recent}
    state["jobs"] = {k: v for k, v in (state.get("jobs") or {}).items() if int(k) in alive}
    state["watched"] = sorted(open_ids)
    summary = None
    try:
        summary = daily_summary(rt, state, now, cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("하루 요약 실패: %s", exc)
    save_state(rt, state)
    return {
        "watched": watched,
        "actions": actions,
        "summary": summary,
        "skipped_history": skipped_history,
        "started_at": state.get("started_at"),
    }


def monitor_report(rt: Any, now: datetime | None = None) -> str:
    """`감시 상태` 보고문(한국어)."""
    from v2r.engine import schedule as schedule_mod

    now = now or datetime.now(KST)
    age = schedule_mod.heartbeat_age_seconds(rt, now)
    if age is None:
        head = "실행기 심장박동: 없음 — 실행기가 꺼져 있을 수 있습니다"
    elif age > schedule_mod.HEARTBEAT_STALE_SECONDS:
        head = f"실행기 심장박동: {int(age)}초 전 — 멈춘 것 같습니다"
    else:
        head = f"실행기 심장박동: {int(age)}초 전 — 정상"

    state = load_state(rt)
    lines = ["감시 상태", head, ""]
    try:
        open_jobs = [j for j in rt.jobs.open_jobs() if str(j.get("task")) not in SKIP_TASKS]
    except Exception as exc:  # noqa: BLE001
        open_jobs = []
        lines.append(f"작업 목록을 읽지 못했습니다: {exc}")
    if open_jobs:
        lines.append(f"지켜보는 작업 {len(open_jobs)}건")
        for job in open_jobs:
            mark = last_progress(rt, job)
            idle = int((now - mark).total_seconds() // 60) if mark else None
            lines.append(
                f"- 작업 {job['id']} / {job.get('task')} / {job.get('status')}"
                + (f" / 마지막 움직임 {idle}분 전" if idle is not None else "")
            )
    else:
        lines.append("지켜보는 작업 없음 (큐가 비었습니다)")

    box = (state.get("daily") or {}).get(_today(now)) or {}
    lines.append("")
    lines.append(
        f"오늘 모델 진단(Tier 1): {int(box.get('llm_calls', 0))}회"
        f" / 토큰 {int(box.get('llm_tokens', 0))}"
        f" (상한 {load_config(rt)['llm_daily_cap']}회, 기본 점검은 토큰 0)"
    )
    lines.append(f"오늘 자동 복구: {int(box.get('recovered', 0))}건")

    alerts = state.get("alerts") or []
    lines.append("")
    if alerts:
        lines.append("최근 알림")
        for item in alerts[-5:]:
            lines.append(f"- {item.get('at', '')} {item.get('text', '')}")
    else:
        lines.append("최근 알림 없음")
    return "\n".join(lines)


__all__ = [
    "DEAD_S",
    "HISTORY_GRACE_S",
    "NO_RETRY_RE",
    "RETRY_MARK",
    "is_history",
    "may_retry",
    "no_retry_reason",
    "root_key",
    "started_at",
    "daily_summary",
    "diagnose",
    "error_signature",
    "load_config",
    "STALL_S",
    "START_GRACE_S",
    "last_progress",
    "load_state",
    "monitor_report",
    "save_state",
    "state_path",
    "tick",
]
