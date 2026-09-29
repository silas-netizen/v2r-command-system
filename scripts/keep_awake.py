"""PC가 절전에 들어가지 않게 붙잡는 상주 프로세스(설정 변경 없음, 앱 수준 요청).

원격 제어(모바일 이어보기)는 PC 클로드 앱이 살아 있어야 하므로, Windows 전원 설정을
건드리지 않고 SetThreadExecutionState 로 "시스템 필요" 상태를 계속 요청한다.
단일 실행(잠금 파일 data/keep_awake.lock). 정지: data/STOP 파일.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "data" / "keep_awake.lock"
STOP = ROOT / "data" / "STOP"
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


def _pid_alive(pid: int) -> bool:
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    k.CloseHandle(h)
    return True


def main() -> int:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            other = int(LOCK.read_text().strip() or 0)
        except ValueError:
            other = 0
        if other and other != os.getpid() and _pid_alive(other):
            return 0  # 이미 하나 돌고 있음
    LOCK.write_text(str(os.getpid()))
    k = ctypes.windll.kernel32
    try:
        while not STOP.exists():
            k.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
            LOCK.write_text(str(os.getpid()))  # 심장박동(수정 시각)
            time.sleep(50)
    finally:
        k.SetThreadExecutionState(ES_CONTINUOUS)
        try:
            LOCK.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
