"""Claude Code CLI 로그인 자격증명 백업·복원 (사용자 허용 2026-09-21).

사용자 철칙: "로그인은 1회, 이후 절대 유지. 풀리면 알아서 방법을 찾는다. 자동 복원까지 허용."
- 로그인 확인이 될 때마다 `~/.claude/.credentials.json`을 `data/claude-cli-backup/`에 복사(최근 3개 보관).
- 풀렸으면 최신 백업을 되돌려 놓고 다시 확인한다. 그래도 안 되면 그때만 사람에게 알린다.
- 파일 내용은 읽지도 출력하지도 않는다(바이트 복사만). 비밀번호와 무관한 OAuth 토큰 파일이다.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

BACKUP_DIRNAME = "claude-cli-backup"
KEEP = 3


def credentials_path() -> Path:
    override = os.environ.get("V2R_CLAUDE_CREDENTIALS")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".claude" / ".credentials.json"


def backup_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / BACKUP_DIRNAME


def backup(data_dir: str | Path, src: Path | None = None) -> Path | None:
    """자격증명 파일을 백업 폴더에 복사. 내용이 최신 백업과 같으면 건너뛴다."""
    src = src or credentials_path()
    if not src.is_file() or src.stat().st_size == 0:
        return None
    dst_dir = backup_dir(data_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    latest = dst_dir / "credentials.json"
    try:
        if latest.is_file() and latest.read_bytes() == src.read_bytes():
            return latest
        stamped = dst_dir / f"credentials-{time.strftime('%Y%m%d-%H%M%S')}.json"
        shutil.copy2(src, stamped)
        shutil.copy2(src, latest)
        # 오래된 것 정리
        old = sorted(dst_dir.glob("credentials-*.json"))
        for p in old[:-KEEP]:
            try:
                p.unlink()
            except OSError:
                pass
        return latest
    except OSError as exc:
        log.warning("자격증명 백업 실패: %s", exc)
        return None


def restore(data_dir: str | Path, dst: Path | None = None) -> bool:
    """최신 백업을 자격증명 위치로 되돌린다(깨진 현재 파일은 .broken으로 보관)."""
    dst = dst or credentials_path()
    latest = backup_dir(data_dir) / "credentials.json"
    if not latest.is_file() or latest.stat().st_size == 0:
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file():
            try:
                shutil.copy2(dst, dst.with_suffix(".json.broken"))
            except OSError:
                pass
        shutil.copy2(latest, dst)
        return True
    except OSError as exc:
        log.warning("자격증명 복원 실패: %s", exc)
        return False
