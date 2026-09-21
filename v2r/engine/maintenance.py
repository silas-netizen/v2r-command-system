"""정기 정비 — 쌓여서 느려지는 것들을 주기적으로 덜어낸다 (2026-09-22).

일요일 03:00 `정기 정비` 예약(`config/schedule.yaml`)이 부르는 가벼운 작업이다.

1. **30일 지난 이벤트 정리** — `events` 표는 끝없이 커진다. 30일이면 충분하다.
2. **주 1회 VACUUM** — 지운 자리를 실제로 돌려받아 DB 파일을 줄인다.
3. **브라우저 프로필 캐시 비우기** — `Cache` / `Code Cache` / `GPUCache` 폴더만
   지운다. **쿠키·로그인 데이터는 절대 건드리지 않는다** (철칙: 로그인 1회 후 유지).
4. **요금제 임시 파일(`data/plan-work`) 잔여물 청소** — 실행기가 시작할 때도 한다.

모든 단계는 실패해도 다음 단계를 막지 않는다.
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "BROWSER_CACHE_DIRNAMES",
    "EVENT_KEEP_DAYS",
    "PROTECTED_NAMES",
    "clean_plan_work",
    "purge_old_events",
    "run_maintenance",
    "sweep_browser_caches",
    "vacuum_db",
]

#: 이벤트를 며칠치까지 남기는가
EVENT_KEEP_DAYS = 30

#: 비워도 되는 브라우저 캐시 폴더 이름 (로그인과 무관한 임시 파일)
BROWSER_CACHE_DIRNAMES: tuple[str, ...] = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "GrShaderCache",
    "DawnCache",
    "DawnGraphiteCache",
)

#: **절대** 지우지 않는 것 (로그인 유지에 쓰이는 파일·폴더).
#: 이름이 겹치면 캐시 폴더라도 건너뛴다 — 안전이 먼저다.
PROTECTED_NAMES: tuple[str, ...] = (
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Login Data For Account",
    "Local Storage",
    "Session Storage",
    "IndexedDB",
    "Web Data",
    "Preferences",
    "Secure Preferences",
    "Network",
    "Local State",
)

#: 요금제 길 임시 파일이 이만큼 오래되면 잔여물로 본다(초)
PLAN_WORK_STALE_SECONDS = 3600


def purge_old_events(rt: Any, keep_days: int = EVENT_KEEP_DAYS) -> dict:
    """`keep_days`일보다 오래된 이벤트를 지운다. 지운 줄 수를 돌려준다."""
    cutoff = (datetime.now().astimezone() - timedelta(days=int(keep_days))).isoformat()
    try:
        cur = rt.conn.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
        deleted = int(cur.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("이벤트 정리 실패: %s", exc)
        return {"ok": False, "error": str(exc), "deleted": 0}
    log.info("이벤트 정리 — %s일 지난 %d줄 삭제", keep_days, deleted)
    return {"ok": True, "deleted": deleted, "cutoff": cutoff}


def vacuum_db(rt: Any) -> dict:
    """DB를 VACUUM 한다 (지운 자리를 실제로 돌려받는다)."""
    path = Path(getattr(rt.settings, "db_path", "") or "")
    before = path.stat().st_size if path.exists() else 0
    try:
        rt.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        rt.conn.execute("VACUUM")
    except Exception as exc:  # noqa: BLE001
        log.warning("VACUUM 실패: %s", exc)
        return {"ok": False, "error": str(exc)}
    after = path.stat().st_size if path.exists() else 0
    log.info("VACUUM — %d바이트 → %d바이트", before, after)
    return {"ok": True, "before_bytes": before, "after_bytes": after}


def clean_plan_work(data_dir: str | Path, stale_seconds: int = PLAN_WORK_STALE_SECONDS) -> dict:
    """`data/plan-work` 에 남은 임시 파일을 치운다 (실행기 시작 때도 부른다).

    지금 돌고 있는 호출의 파일을 지우지 않도록 **오래된 것만** 지운다.
    """
    folder = Path(data_dir) / "plan-work"
    if not folder.exists():
        return {"ok": True, "removed": 0}
    now = time.time()
    removed = 0
    for path in folder.iterdir():
        try:
            if now - path.stat().st_mtime < stale_seconds:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink()
            removed += 1
        except OSError as exc:
            log.debug("요금제 임시 파일 삭제 실패(%s): %s", path.name, exc)
    if removed:
        log.info("요금제 임시 파일 %d개 청소", removed)
    return {"ok": True, "removed": removed, "dir": str(folder)}


def _cache_dirs(profile: Path) -> list[Path]:
    """프로필 폴더 안의 **캐시 폴더만** 찾는다 (쿠키·로그인 폴더는 제외)."""
    found: list[Path] = []
    for path in profile.rglob("*"):
        if not path.is_dir():
            continue
        if path.name not in BROWSER_CACHE_DIRNAMES:
            continue
        # 경로 어딘가에 보호 대상 이름이 끼어 있으면 건드리지 않는다
        if any(part in PROTECTED_NAMES for part in path.parts):
            continue
        found.append(path)
    return found


def sweep_browser_caches(data_dir: str | Path) -> dict:
    """브라우저 프로필의 **캐시 폴더만** 비운다.

    쿠키·로그인 데이터는 절대 지우지 않는다 (사용자 철칙: 로그인 1회 후 유지).
    폴더 자체는 남기고 안쪽 내용만 지운다 — 크롬이 다시 만들 때 권한 문제가 없다.
    """
    root = Path(data_dir)
    freed = 0
    cleaned: list[str] = []
    for profile in sorted(root.glob("browser-profile*")):
        if not profile.is_dir():
            continue
        for folder in _cache_dirs(profile):
            for child in list(folder.iterdir()):
                try:
                    size = child.stat().st_size if child.is_file() else 0
                    if child.is_dir():
                        size = sum(
                            f.stat().st_size for f in child.rglob("*") if f.is_file()
                        )
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink()
                    freed += size
                except OSError as exc:
                    log.debug("캐시 삭제 실패(%s): %s", child, exc)
            cleaned.append(str(folder.relative_to(root)))
    if cleaned:
        log.info("브라우저 캐시 %d곳 비움 (%.1fMB)", len(cleaned), freed / 1048576)
    return {"ok": True, "folders": cleaned, "freed_bytes": freed}


def run_maintenance(rt: Any, spec: Any = None) -> dict:
    """정기 정비 한 번 (이벤트 정리 → VACUUM → 임시 파일 → 브라우저 캐시)."""
    del spec
    data_dir = Path(rt.settings.data_dir)
    events = purge_old_events(rt)
    vacuum = vacuum_db(rt)
    plan_work = clean_plan_work(data_dir)
    caches = sweep_browser_caches(data_dir)
    message = (
        f"정기 정비 완료 — 이벤트 {events.get('deleted', 0)}줄 정리,"
        f" DB {vacuum.get('before_bytes', 0) // 1048576}MB →"
        f" {vacuum.get('after_bytes', 0) // 1048576}MB,"
        f" 임시 파일 {plan_work.get('removed', 0)}개,"
        f" 브라우저 캐시 {caches.get('freed_bytes', 0) // 1048576}MB 비움"
    )
    return {
        "ok": bool(events.get("ok") and vacuum.get("ok")),
        "events": events,
        "vacuum": vacuum,
        "plan_work": plan_work,
        "browser_cache": caches,
        "message": message,
    }
