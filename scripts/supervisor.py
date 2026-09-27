"""V2R 통합 감시자 (설계: docs/reports/design-no-stop-2026-09-27.md 2층).

표준 라이브러리만 사용한다. 실행기·노출 작업자 6개·채점/코덱스 워커 18개·
채우기 순환(1만 미달 브랜드)을 한 곳에서 살피고, 심장박동이 기준 시간
넘게 멈췄고 해당 프로세스도 없거나(있으면 먼저 멈추고) 재기동한다.

규칙:
- data/STOP 파일이 있으면 아무 것도 하지 않는다.
- 같은 대상 재기동은 15분에 1회로 제한한다(COOLDOWN_SECONDS).
- 매 실행마다 logs/supervisor.log 에 한 줄, data/supervisor_last.json 에
  시각과 조치를 기록한다.
- 창을 띄우지 않는다(모든 하위 프로세스는 CREATE_NO_WINDOW).
"""

from __future__ import annotations

import json
import subprocess
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOG_PATH = ROOT / "logs" / "supervisor.log"
LAST_PATH = ROOT / "data" / "supervisor_last.json"
STOP_PATH = ROOT / "data" / "STOP"

# scripts/rescore.cmd 와 동일한 브랜드 목록(채점/코덱스 워커 대상).
RESCORE_BRANDS = ["우아덤", "코숨핏", "뉴더미스", "장으뜸", "팥순이", "갱년기"]
# scripts/keyword-fill-hidden.cmd 와 동일(원고 발행 브랜드만, 갱년기 제외).
FILL_BRANDS = ["우아덤", "코숨핏", "뉴더미스", "장으뜸", "팥순이"]
FILL_TARGET = 10000

HEARTBEAT_STALE_SECONDS = 420.0  # 7분
FILL_STALE_SECONDS = 3600.0  # 60분
COOLDOWN_SECONDS = 900.0  # 15분

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# =======================================================================
# 순수 함수(단위 테스트 대상) — 심장박동·프로세스 상태로부터 조치를 결정한다.
# =======================================================================

def decide_action(
    heartbeat_age: Optional[float],
    stale_seconds: float,
    process_alive: bool,
) -> str:
    """반환: "ok"(정상) | "restart"(재기동 필요).

    - 심장박동이 기준보다 새로우면 프로세스 여부와 무관하게 정상.
    - 심장박동이 없거나(파일 없음/최초) 기준보다 오래됐으면, 프로세스가
      없을 때만(또는 멈춰서 정리한 뒤) 재기동한다. 프로세스가 버젓이 돌고
      있는데 심장박동만 늦는 경우는 곧 갱신될 수 있으니 이번 회차는 정상
      취급하고, 다음 회차에도 여전히 멈춰 있으면 그때 프로세스를 멈추고
      재기동한다(호출자가 stalled_process 플래그로 구분).
    """
    if heartbeat_age is not None and heartbeat_age < stale_seconds:
        return "ok"
    if not process_alive:
        return "restart"
    return "stalled"  # 프로세스는 있는데 심장박동이 오래 멈춤 -> 멈추고 재기동


def can_restart(name: str, last_map: dict, cooldown_seconds: float, now_epoch: float) -> bool:
    """15분 상한: 최근 재기동 시각이 없거나 충분히 지났으면 True."""
    info = last_map.get(name) or {}
    last = info.get("last_restart_epoch")
    if not last:
        return True
    return (now_epoch - float(last)) >= cooldown_seconds


# =======================================================================
# 파일/프로세스 헬퍼
# =======================================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def to_epoch(value) -> Optional[float]:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def append_log(line: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{now_iso()}] {line}\n")
    except Exception:
        pass


def file_mtime_epoch(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def process_count(pattern: str) -> int:
    """커맨드라인에 pattern 부분 문자열을 포함한 python.exe 프로세스 수."""
    ps_cmd = (
        "(Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{pattern}*' }}).Count"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        text = (out.stdout or "").strip()
        return int(text) if text.isdigit() else 0
    except Exception:
        return 0


def stop_processes(pattern: str) -> None:
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{pattern}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def run_hidden(args: list[str]) -> None:
    try:
        subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        pass


# =======================================================================
# 대상별 점검
# =======================================================================

def check_serve(now_epoch: float) -> dict:
    hb = read_json(ROOT / "data" / "sidecar_heartbeat.json")
    age = None
    at = to_epoch(hb.get("at"))
    if at:
        age = now_epoch - at
    alive = process_count("v2r serve") > 0
    action = decide_action(age, HEARTBEAT_STALE_SECONDS, alive)
    return {
        "name": "serve",
        "heartbeat_age": age,
        "process_alive": alive,
        "action": action,
        "stop_pattern": "v2r serve",
        "restart": lambda: run_hidden(["schtasks", "/run", "/tn", "V2R-Serve"]),
    }


def check_exposure_workers(now_epoch: float) -> dict:
    sys.path.insert(0, str(ROOT))
    try:
        from v2r.knowledge.exposure_runner import is_alive as exposure_is_alive  # noqa: WPS433
    except Exception:
        exposure_is_alive = None

    state = read_json(ROOT / "data" / "exposure_runner_state.json")
    age = None
    updated = to_epoch(state.get("updated_at"))
    if updated:
        age = now_epoch - updated
    count = process_count("exposure_runner --worker-id")
    alive = count >= 1
    if exposure_is_alive is not None:
        try:
            alive_flag = exposure_is_alive(str(ROOT), stale_seconds=HEARTBEAT_STALE_SECONDS)
            if alive_flag and age is None:
                age = 0.0
        except Exception:
            pass
    action = decide_action(age, HEARTBEAT_STALE_SECONDS, alive and count >= 6)
    return {
        "name": "exposure_workers",
        "heartbeat_age": age,
        "process_alive": alive,
        "process_count": count,
        "action": action,
        "stop_pattern": "exposure_runner --worker-id",
        "restart": lambda: run_hidden(
            ["cmd", "/c", str(ROOT / "scripts" / "exposure-runner-hidden.cmd"), "6"]
        ),
    }


def check_score_workers(now_epoch: float) -> list[dict]:
    results = []
    any_stale = False
    for brand in RESCORE_BRANDS:
        lock_paths = [
            ROOT / "data" / "locks" / f"score-{brand}.lock",
            ROOT / "data" / "locks" / f"codex-{brand}-1.lock",
            ROOT / "data" / "locks" / f"codex-{brand}-2.lock",
        ]
        ages = []
        for lp in lock_paths:
            mt = file_mtime_epoch(lp)
            if mt is not None:
                ages.append(now_epoch - mt)
        age = min(ages) if ages else None
        score_count = process_count(f"keyword_relevance --score-worker {brand}")
        codex_count = process_count(f"keyword_relevance --codex-worker {brand}")
        alive = (score_count + codex_count) >= 3
        action = decide_action(age, HEARTBEAT_STALE_SECONDS, alive)
        if action != "ok":
            any_stale = True
        results.append(
            {
                "name": f"score_{brand}",
                "heartbeat_age": age,
                "process_alive": alive,
                "process_count": score_count + codex_count,
                "action": action,
                "stop_pattern": None,
            }
        )
    # 재기동은 rescore-hidden.vbs 한 번으로 처리(브랜드별 잠금이 중복 실행을 막는다).
    for r in results:
        r["restart"] = lambda: run_hidden(
            ["wscript", "//nologo", str(ROOT / "scripts" / "rescore-hidden.vbs")]
        )
    results.append({"__group_stale__": any_stale})
    return results


def _fill_eligible_from_db(brand: str) -> Optional[int]:
    db_path = ROOT / "data" / "keywords" / f"{brand}.sqlite"
    if not db_path.exists():
        return None
    try:
        sys.path.insert(0, str(ROOT))
        from v2r.knowledge.keyword_fill_loop import eligible_count  # noqa: WPS433

        conn = sqlite3.connect(str(db_path))
        try:
            return eligible_count(conn)
        finally:
            conn.close()
    except Exception:
        return None


def check_fill_workers(now_epoch: float) -> list[dict]:
    progress = read_json(ROOT / "data" / "keywords" / "fill_progress.json")
    results = []
    for brand in FILL_BRANDS:
        eligible = _fill_eligible_from_db(brand)
        entry = progress.get(brand) or {}
        status = entry.get("status")
        achieved = status == "achieved" or (eligible is not None and eligible >= FILL_TARGET)
        if achieved:
            results.append(
                {
                    "name": f"fill_{brand}",
                    "heartbeat_age": None,
                    "process_alive": True,
                    "action": "achieved",
                    "stop_pattern": None,
                    "restart": lambda: None,
                }
            )
            continue
        age = None
        updated = to_epoch(entry.get("updated_at"))
        if updated:
            age = now_epoch - updated
        count = process_count(f"keyword_fill_loop --worker {brand}")
        alive = count > 0
        action = decide_action(age, FILL_STALE_SECONDS, alive)
        results.append(
            {
                "name": f"fill_{brand}",
                "heartbeat_age": age,
                "process_alive": alive,
                "process_count": count,
                "action": action,
                "stop_pattern": f"keyword_fill_loop --worker {brand}",
                "restart": (
                    lambda b=brand: run_hidden(
                        [
                            "wscript",
                            "//nologo",
                            str(ROOT / "scripts" / "keyword-fill-worker.vbs"),
                            b,
                            "10000",
                            "b",
                        ]
                    )
                ),
            }
        )
    return results


# =======================================================================
# 실행
# =======================================================================

def main() -> int:
    if STOP_PATH.exists():
        append_log("정지 파일(data/STOP) 있음 - 조치 없음")
        return 0

    now_epoch = datetime.now(timezone.utc).timestamp()
    last_map = read_json(LAST_PATH).get("targets", {})

    summary_parts = []
    actions_taken = []

    def handle(target: dict) -> None:
        name = target["name"]
        action = target["action"]
        if action == "ok":
            summary_parts.append(f"{name}=정상")
            last_map.setdefault(name, {})["last_status"] = "ok"
            return
        if action == "achieved":
            summary_parts.append(f"{name}=달성")
            last_map.setdefault(name, {})["last_status"] = "achieved"
            return
        # stalled 또는 restart -> 재기동 대상. 쿨다운 확인.
        if not can_restart(name, last_map, COOLDOWN_SECONDS, now_epoch):
            summary_parts.append(f"{name}=재기동보류(15분상한)")
            last_map.setdefault(name, {})["last_status"] = "cooldown"
            return
        if action == "stalled" and target.get("stop_pattern"):
            stop_processes(target["stop_pattern"])
        restart_fn = target.get("restart")
        if restart_fn:
            restart_fn()
        summary_parts.append(f"{name}=재기동({action})")
        actions_taken.append(name)
        last_map[name] = {
            "last_status": "restarted",
            "last_restart_epoch": now_epoch,
            "last_restart_at": now_iso(),
        }

    handle(check_serve(now_epoch))
    handle(check_exposure_workers(now_epoch))

    score_results = check_score_workers(now_epoch)
    group_stale = False
    for r in score_results:
        if "__group_stale__" in r:
            group_stale = r["__group_stale__"]
            continue
        summary_parts.append(f"{r['name']}={'정상' if r['action'] == 'ok' else r['action']}")
    if group_stale:
        if can_restart("score_workers_group", last_map, COOLDOWN_SECONDS, now_epoch):
            run_hidden(["wscript", "//nologo", str(ROOT / "scripts" / "rescore-hidden.vbs")])
            actions_taken.append("score_workers_group")
            last_map["score_workers_group"] = {
                "last_status": "restarted",
                "last_restart_epoch": now_epoch,
                "last_restart_at": now_iso(),
            }
            summary_parts.append("score_workers_group=재기동")
        else:
            summary_parts.append("score_workers_group=재기동보류(15분상한)")

    for target in check_fill_workers(now_epoch):
        handle(target)

    write_json(
        LAST_PATH,
        {"at": now_iso(), "targets": last_map, "actions_taken": actions_taken},
    )
    append_log(" | ".join(summary_parts) if summary_parts else "점검 대상 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
