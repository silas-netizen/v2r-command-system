"""사진 수집기. 로컬 폴더나 Drive 내보내기 zip에서 원본 이미지를 창고로 모은다.

Drive API 직접 연동은 범위 밖이다. 사용자는 둘 중 하나로 준비한다.
1) Google Drive 웹에서 브랜드 폴더를 우클릭 → "다운로드" → 받은 zip 경로를
   `collect_from_drive_export(zip_path, brand)`에 넘긴다.
2) Drive 데스크톱(동기화) 폴더를 로컬 경로로 두고
   `collect_from_folder(local_dir, brand, folder)`를 쓴다.
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from v2r.warehouse.store import IMAGE_SUFFIXES, Warehouse


def _new_stats() -> dict:
    return {"scanned": 0, "added": 0, "duplicated": 0, "skipped": 0, "errors": []}


def collect_from_folder(
    local_dir: str | Path,
    brand: str,
    folder: str,
    warehouse: Warehouse | None = None,
) -> dict:
    """로컬 폴더를 재귀 탐색해 jpg/jpeg/png/webp를 창고 원본으로 복사한다."""
    base = Path(local_dir)
    wh = warehouse or Warehouse()
    wh.ensure_dirs()
    stats = _new_stats()
    if not base.is_dir():
        stats["errors"].append(f"폴더 없음: {base}")
        return stats

    before = {str(p) for p in wh.list_originals(brand, folder)}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        stats["scanned"] += 1
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            stats["skipped"] += 1
            continue
        try:
            dest = wh.add_original(path, brand, folder)
        except Exception as exc:
            stats["errors"].append(f"{path.name}: {exc}")
            continue
        if str(dest) in before:
            stats["duplicated"] += 1
        else:
            before.add(str(dest))
            stats["added"] += 1
    return stats


def collect_from_drive_export(
    zip_path: str | Path,
    brand: str,
    warehouse: Warehouse | None = None,
) -> dict:
    """Drive "폴더 다운로드" zip을 풀어 하위 폴더별로 창고에 모은다.

    zip 안의 최상위 디렉터리 이름을 창고의 `folder`로 쓴다.
    """
    src = Path(zip_path)
    wh = warehouse or Warehouse()
    wh.ensure_dirs()
    stats = _new_stats()
    if not src.is_file():
        stats["errors"].append(f"zip 없음: {src}")
        return stats

    with tempfile.TemporaryDirectory(prefix="v2r_drive_") as tmp:
        tmp_dir = Path(tmp)
        try:
            with zipfile.ZipFile(src) as zf:
                zf.extractall(tmp_dir)
        except zipfile.BadZipFile as exc:
            stats["errors"].append(f"zip 해제 실패: {exc}")
            return stats

        roots = sorted(p for p in tmp_dir.iterdir() if p.is_dir())
        loose = sorted(p for p in tmp_dir.iterdir() if p.is_file())

        for root in roots:
            _merge(stats, collect_from_folder(root, brand, root.name, wh))

        # zip 루트에 바로 놓인 파일은 zip 이름을 폴더로 삼는다.
        if loose:
            folder = src.stem
            for path in loose:
                stats["scanned"] += 1
                if path.suffix.lower() not in IMAGE_SUFFIXES:
                    stats["skipped"] += 1
                    continue
                existing = {str(p) for p in wh.list_originals(brand, folder)}
                try:
                    dest = wh.add_original(path, brand, folder)
                except Exception as exc:
                    stats["errors"].append(f"{path.name}: {exc}")
                    continue
                if str(dest) in existing:
                    stats["duplicated"] += 1
                else:
                    stats["added"] += 1
    return stats


def _merge(target: dict, part: dict) -> None:
    for key in ("scanned", "added", "duplicated", "skipped"):
        target[key] += part[key]
    target["errors"].extend(part["errors"])


__all__ = ["collect_from_drive_export", "collect_from_folder"]
