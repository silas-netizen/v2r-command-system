"""브랜드 5개 동시 병렬 키워드 발굴.

배경: `docs/reports/naver-profile-lock-2026-09-23.md`가 잠금·헤드리스·재시도까지
검증을 끝냈지만 "5개 브랜드 동시" 실행은 범위 밖으로 남겨 뒀다. 여기서는
사용자 명시 지시(2026-09-23 00:40)대로 브랜드마다 **복제 프로필**을 만들어
진짜 OS 프로세스 5개를 동시에 띄운다 — 원본 `data/browser-profile-naver`는
절대 건드리지 않고, 복제본은 기존 쿠키로만 쓰며 재로그인은 절대 하지 않는다.

이 모듈은 두 갈래로 쓰인다:
- 오케스트레이터(`run_parallel`): 백업 → 복제 5개 생성 → 헤드리스 확인 →
  `subprocess.Popen` 5개로 각 브랜드를 별도 파이썬 프로세스에서 돌린다
  (진짜 병렬 — 브라우저가 GIL과 무관하게 각자 프로세스에서 돈다).
- 워커(`_worker_main`, `python -m v2r.knowledge.keyword_discovery_parallel
  --worker <브랜드> <프로필경로> <목표개수> <마감ISO시각>`): 자식 프로세스
  진입점 — `naver_keyword_tool.run_for_brand`를 호출하고 30초마다
  `data/keywords/progress.json`에 그 브랜드 몫을 갱신한다.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

def _brand_names() -> list[str]:
    from v2r.command.parser import BRAND_NAMES

    return list(BRAND_NAMES)


#: 브랜드 목록 — `v2r.command.parser.BRAND_NAMES`를 그대로 따른다(우아덤·코숨핏·
#: 뉴더미스·장으뜸·팥순이, 순서는 parser 쪽이 기준).
BRANDS = _brand_names()

#: 프로필을 복제할 때 절대 복사하지 않을 이름(Chromium 잠금류) — 그대로
#: 복사하면 원본이 열려 있을 때 복제 프로세스가 잠금을 가로채거나, 복제
#: 프로필이 "이미 열려 있다"고 오판될 수 있다.
_PROFILE_LOCK_NAMES = {
    "lockfile",
    "LOCK",
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
}


def clone_profile_name(brand: str) -> str:
    return f"browser-profile-naver-kw-{brand}"


def _ignore_locks(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in _PROFILE_LOCK_NAMES}


def clone_profile(brand: str, src: Path, data_dir: Path) -> Path:
    """`src`(원본 네이버 프로필)를 브랜드 전용 복제본으로 복사(잠금 파일류 제외).

    원본은 절대 건드리지 않는다(읽기만). 복제본이 이미 있으면 지우고 새로
    복사한다(항상 최신 쿠키로 시작 — 로그인 입력은 절대 하지 않는다).
    """
    dst = data_dir / clone_profile_name(brand)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=_ignore_locks)
    return dst


def clone_profile_to(src: Path, dst: Path) -> Path:
    """`src` 프로필을 `dst`로 복사(잠금 파일류 제외). 원본은 읽기만. 재로그인 없음."""
    src = Path(src)
    dst = Path(dst)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=_ignore_locks, dirs_exist_ok=True)
    return dst


def clone_all_profiles(data_dir: Path | str = "data", brands: list[str] | None = None) -> dict[str, Path]:
    """5개(또는 지정한) 브랜드 복제 프로필을 만든다. 원본은 먼저 백업만 한다."""
    from v2r.warehouse import naver_session

    data_dir = Path(data_dir)
    src = naver_session.default_profile_dir()
    if not src.is_dir():
        raise RuntimeError(f"원본 네이버 프로필이 없습니다: {src}")
    # 지시: 백업(naver_session.backup_profile) 먼저 — 원본은 절대 덮어쓰지 않는다.
    naver_session.backup_profile(src)
    out: dict[str, Path] = {}
    for brand in brands or BRANDS:
        out[brand] = clone_profile(brand, src, data_dir)
    return out


def verify_profile_logged_in(profile_dir: Path) -> bool:
    """복제 프로필을 헤드리스로 열어 로그인 쿠키가 살아있는지만 확인하고 닫는다.

    로그인 입력은 절대 하지 않는다 — 쿠키가 없으면 그냥 False.
    """
    from v2r.knowledge import naver_keyword_tool as kt_mod
    from v2r.warehouse import naver_session

    playwright = context = page = None
    try:
        playwright, context, page, logged_in = kt_mod.open_keyword_tool_page(
            profile_dir=profile_dir, headless=True
        )
        return bool(logged_in)
    except Exception as exc:  # noqa: BLE001
        log.warning("복제 프로필 확인 실패(%s): %s", profile_dir, exc)
        return False
    finally:
        if playwright is not None:
            naver_session._close(playwright, context, page)


# --- 진행 상황 파일 ----------------------------------------------------------
def progress_path(data_dir: Path | str = "data") -> Path:
    p = Path(data_dir) / "keywords" / "progress.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def log_path_for(brand: str, data_dir: Path | str = "data") -> Path:
    p = Path(data_dir) / "keywords" / "logs" / f"{brand}.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def update_progress(brand: str, patch: dict[str, Any], data_dir: Path | str = "data") -> None:
    """`progress.json`의 그 브랜드 몫만 갱신한다(다른 프로세스가 쓴 다른 브랜드
    몫은 건드리지 않는다 — 파일 잠금 없이 read-merge-write라 드물게 경쟁이
    나도 최악은 그 순간 한 번의 갱신 유실이라 다음 30초 주기에 바로 회복된다)."""
    path = progress_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:  # noqa: BLE001
        data = {}
    if not isinstance(data, dict):
        data = {}
    entry = dict(data.get(brand) or {})
    entry.update(patch)
    entry["brand"] = brand
    entry["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    data[brand] = entry
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# --- 워커(자식 프로세스) -----------------------------------------------------
def worker_run(
    brand: str,
    profile_dir: Path,
    target: int,
    deadline: _dt.datetime,
    data_dir: Path | str = "data",
    session_cap: int = 200,
    rest_sec: float = 60.0,
    delay_range: tuple[float, float] = (4.0, 7.0),
    headless: bool = True,
    offscreen: bool = False,
) -> dict[str, Any]:
    """자식 프로세스 안에서 실제로 도는 함수(오케스트레이터가 아니라
    `_worker_main`에서 호출) — 주기적으로 `progress.json`을 갱신하며
    `run_for_brand`를 부른다. 마감(`deadline`)이 지나면 다음 진행 콜백에서
    멈추도록 `naver_keyword_tool.discover`에 넘기는 `progress_cb`가
    `TimeoutError`를 던진다."""
    from v2r.engine.context import Runtime
    from v2r.knowledge import naver_keyword_tool as kt_mod

    last_write = 0.0

    def _progress_cb(info: dict[str, Any]) -> None:
        nonlocal last_write
        now = time.monotonic()
        if info.get("done") or now - last_write >= 30:
            update_progress(
                brand,
                {
                    "collected": info.get("collected", 0),
                    "queries": info.get("queries", 0),
                    "status": "done" if info.get("done") else "running",
                    "stopped_reason": info.get("stopped_reason", ""),
                },
                data_dir=data_dir,
            )
            last_write = now
        if _dt.datetime.now() >= deadline:
            raise TimeoutError("08:20 상한 도달 — 이 브랜드 조회를 멈춥니다")

    update_progress(brand, {"status": "starting", "collected": 0, "queries": 0}, data_dir=data_dir)
    rt = Runtime.open()
    try:
        out = kt_mod.run_for_brand(
            rt,
            brand,
            target=target,
            headless=headless,
            offscreen=offscreen,
            profile_dir=profile_dir,
            session_cap=session_cap,
            rest_sec=rest_sec,
            delay_range=delay_range,
            progress_cb=_progress_cb,
        )
    except TimeoutError as exc:
        out = {"ok": True, "brand": brand, "stopped_reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 이 브랜드만 중단, 나머지는 계속
        out = {"ok": False, "brand": brand, "error": f"{exc.__class__.__name__}: {exc}"}
    finally:
        rt.close()
    update_progress(
        brand,
        {
            "status": "stopped" if not out.get("ok") else "done",
            "collected": out.get("collected", 0),
            "queries": out.get("queries", 0),
            "stopped_reason": out.get("stopped_reason") or out.get("error", ""),
        },
        data_dir=data_dir,
    )
    return out


def _worker_main(argv: list[str]) -> int:
    brand, profile_dir, target, deadline_iso = argv[:4]
    data_dir = argv[4] if len(argv) > 4 else "data"
    headless = argv[5] != "0" if len(argv) > 5 else True
    offscreen = argv[6] != "0" if len(argv) > 6 else True
    log_file = log_path_for(brand, data_dir)
    logging.basicConfig(
        filename=str(log_file),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    deadline = _dt.datetime.fromisoformat(deadline_iso)
    try:
        out = worker_run(
            brand, Path(profile_dir), int(target), deadline, data_dir=data_dir,
            headless=headless, offscreen=offscreen,
        )
        log.info("종료: %s", out)
        return 0 if out.get("ok") else 1
    except Exception:  # noqa: BLE001
        log.exception("워커 예외로 중단")
        return 1


# --- 오케스트레이터 -----------------------------------------------------------
def run_parallel(
    target: int = 10_000,
    brands: list[str] | None = None,
    data_dir: Path | str = "data",
    deadline: _dt.datetime | None = None,
    session_cap: int = 200,
    rest_sec: float = 60.0,
    headless: bool = True,
    offscreen: bool = False,
) -> dict[str, Any]:
    """복제 프로필 생성 → 헤드리스 확인 → 브랜드마다 별도 프로세스로 동시 실행.

    확인에서 로그인이 안 되는 브랜드는 그 프로세스를 시작하지 않는다(재로그인
    금지 — 나머지는 계속). 반환은 브랜드별 `{pid, profile_dir, verified}` 요약.
    실제 완료는 `data/keywords/progress.json`/`data/keywords/logs/<브랜드>.log`로
    추적한다(이 함수는 프로세스를 띄우기만 하고 기다리지 않는다 — 호출 쪽이
    `wait_all`로 기다린다).
    """
    data_dir = Path(data_dir)
    brands = brands or BRANDS
    clones = clone_all_profiles(data_dir, brands)

    deadline = deadline or (_dt.datetime.now() + _dt.timedelta(hours=6))
    repo_root = Path(__file__).resolve().parents[2]

    result: dict[str, Any] = {}
    procs: dict[str, subprocess.Popen] = {}
    for brand in brands:
        profile_dir = clones[brand]
        ok = verify_profile_logged_in(profile_dir)
        if not ok:
            result[brand] = {"started": False, "reason": "복제 프로필 로그인 확인 실패(재로그인 시도 안 함)"}
            update_progress(brand, {"status": "skipped", "collected": 0}, data_dir=data_dir)
            continue
        cmd = [
            sys.executable,
            "-m",
            "v2r.knowledge.keyword_discovery_parallel",
            "--worker",
            brand,
            str(profile_dir),
            str(target),
            deadline.isoformat(timespec="seconds"),
            str(data_dir),
            "1" if headless else "0",
            "1" if offscreen else "0",
        ]
        #: 지시(2026-09-23 01:22): 터미널·크롬 창을 어떤 경우에도 띄우지 않는다 —
        #: 자식 파이썬 프로세스도 콘솔 창 없이(CREATE_NO_WINDOW), 브라우저도
        #: headless=True로만 연다(offscreen도 창이 존재해 금지).
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(cmd, cwd=str(repo_root), creationflags=creationflags)
        procs[brand] = proc
        result[brand] = {"started": True, "pid": proc.pid, "profile_dir": str(profile_dir)}

    result["_procs"] = procs
    return result


def wait_all(procs: dict[str, subprocess.Popen], poll_sec: float = 10.0) -> dict[str, int]:
    """모든 자식 프로세스가 끝날 때까지 기다리고(폴링) 각 브랜드 종료 코드를 돌려준다."""
    exit_codes: dict[str, int] = {}
    remaining = dict(procs)
    while remaining:
        for brand, proc in list(remaining.items()):
            code = proc.poll()
            if code is not None:
                exit_codes[brand] = code
                del remaining[brand]
        if remaining:
            time.sleep(poll_sec)
    return exit_codes


def kill_all(procs: dict[str, subprocess.Popen]) -> None:
    for proc in procs.values():
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        sys.exit(_worker_main(sys.argv[2:]))
    print("사용법: python -m v2r.knowledge.keyword_discovery_parallel --worker <브랜드> <프로필경로> <목표> <마감ISO> [data_dir]")
    sys.exit(2)


__all__ = [
    "BRANDS",
    "clone_profile_name",
    "clone_profile",
    "clone_all_profiles",
    "verify_profile_logged_in",
    "progress_path",
    "log_path_for",
    "update_progress",
    "worker_run",
    "run_parallel",
    "wait_all",
    "kill_all",
]
