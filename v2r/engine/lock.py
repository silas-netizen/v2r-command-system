"""실행기 단일 실행 잠금 (`data/serve.lock`).

같은 PC에서 ``serve`` 가 두 번 켜지면 **발행이 두 줄로 동시에 돈다**
(장애 2026-09-20: 작업 65와 67이 나란히 돌았다). 리스 소유자 이름이
호스트 이름뿐이어서 두 번째 실행기가 같은 리스를 자기 것으로 봤기 때문이다.

그래서 두 겹으로 막는다.

1. 리스 소유자에 **프로세스 번호(pid)** 를 붙인다 (``worker.default_owner``).
2. ``serve`` 는 시작할 때 이 파일 잠금을 **배타적으로** 잡는다. 못 잡으면
   두 번째 실행기이므로 바로 끝낸다.

주의: venv 의 ``python.exe`` 는 자식 파이썬을 띄우는 껍데기라 프로세스 수를
세는 방식(실행기 1개당 파이썬 2개)은 믿을 수 없다. 그래서 **파일 잠금**으로 센다.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 잠금 파일 이름 (data/ 아래)
LOCK_FILE = "serve.lock"
#: 실제로 잠그는 바이트 위치. 파일 앞부분(pid 를 적는 자리)은 잠그지 않는다 —
#: 그래야 다른 프로세스가 "누가 잡고 있는지"를 읽을 수 있다.
LOCK_OFFSET = 4096

try:  # pragma: no cover - 플랫폼마다 하나만 있다
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]
try:  # pragma: no cover
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def lock_path(rt: Any) -> Path:
    """잠금 파일 경로."""
    return Path(rt.settings.data_dir) / LOCK_FILE


def pid_alive(pid: int) -> bool:
    """그 번호의 프로세스가 아직 살아 있는가(psutil 없이)."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - 윈도우 전용 가지
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _try_lock(fh: Any) -> bool:
    """잠금 자리(`LOCK_OFFSET`)를 배타적으로 잠근다. 이미 잡혀 있으면 False."""
    fh.seek(LOCK_OFFSET)
    try:
        if msvcrt is not None:  # pragma: no cover - 윈도우 전용 가지
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:  # pragma: no cover - POSIX 전용 가지
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:  # pragma: no cover - 둘 다 없으면 pid 살아있음 검사로 대신한다
            return _pid_file_free(fh)
    except OSError:
        return False
    return True


def _unlock(fh: Any) -> None:
    fh.seek(LOCK_OFFSET)
    try:
        if msvcrt is not None:  # pragma: no cover
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:  # pragma: no cover
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - 이미 풀렸으면 그만
        pass


def _pid_file_free(fh: Any) -> bool:  # pragma: no cover - 잠금 API 가 없을 때만
    """잠금 API 가 없는 환경: 적혀 있는 pid 가 죽었으면 비어 있다고 본다."""
    fh.seek(0)
    try:
        pid = int((fh.read() or "0").strip() or 0)
    except ValueError:
        return True
    return pid == os.getpid() or not pid_alive(pid)


def holder_pid(rt: Any) -> int | None:
    """잠금 파일에 적힌 프로세스 번호. 없거나 죽었으면 None."""
    path = lock_path(rt)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001 - 파일이 없거나 읽을 수 없다
        return None
    try:
        pid = int(text or 0)
    except ValueError:
        return None
    if pid <= 0 or not pid_alive(pid):
        return None
    return pid


class ServeLock:
    """``with`` 로도 쓸 수 있는 실행기 잠금 한 개."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.pid = os.getpid()
        self._fh: Any | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        """잠금을 잡는다. 이미 다른 실행기가 잡고 있으면 False."""
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = "r+" if self.path.exists() else "w+"
        try:
            fh = open(self.path, mode, encoding="utf-8")  # noqa: SIM115 - 오래 잡고 있는다
        except OSError as exc:  # pragma: no cover - 방어용
            log.warning("잠금 파일을 열지 못했습니다: %s", exc)
            return False
        if not _try_lock(fh):
            fh.close()
            return False
        try:
            fh.seek(0)
            fh.write(f"{self.pid:<16}")
            fh.flush()
        except OSError as exc:  # pragma: no cover - 방어용
            log.warning("잠금 파일에 pid 를 적지 못했습니다: %s", exc)
        self._fh = fh
        return True

    def release(self) -> None:
        """잠금을 푼다(두 번 불러도 안전)."""
        fh, self._fh = self._fh, None
        if fh is None:
            return
        _unlock(fh)
        try:
            fh.close()
        except OSError:  # pragma: no cover
            pass
        try:
            if holder_pid_of_file(self.path) == self.pid:
                self.path.unlink()
        except Exception:  # noqa: BLE001 - 지우지 못해도 그만
            pass

    def __enter__(self) -> ServeLock:
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def holder_pid_of_file(path: Path) -> int | None:
    """파일에 적힌 pid(살아 있는지는 보지 않는다)."""
    try:
        return int((Path(path).read_text(encoding="utf-8").strip() or 0))
    except Exception:  # noqa: BLE001
        return None


def acquire_serve_lock(rt: Any) -> ServeLock | None:
    """실행기 잠금을 잡아 돌려준다. 두 번째 실행기면 None."""
    lock = ServeLock(lock_path(rt))
    return lock if lock.acquire() else None


def lock_report(rt: Any) -> str:
    """`health` 에 넣는 한 줄."""
    pid = holder_pid(rt)
    if pid is None:
        return "단일 실행기 잠금: 잡은 프로세스 없음 (실행기가 꺼져 있을 수 있습니다)"
    return f"단일 실행기 잠금: 프로세스 {pid} 번이 잡고 있습니다"


__all__ = [
    "LOCK_FILE",
    "LOCK_OFFSET",
    "ServeLock",
    "acquire_serve_lock",
    "holder_pid",
    "lock_path",
    "lock_report",
    "pid_alive",
]
