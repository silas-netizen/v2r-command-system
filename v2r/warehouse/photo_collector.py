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


def _inbox_roots(wh: Warehouse) -> list[Path]:
    """인박스 이미지 루트 후보. `inbox/image/image`(이중 경로)를 우선한다."""
    base = wh.root / "inbox" / "image"
    if not base.is_dir():
        return []
    doubled = base / "image"
    return [doubled] if doubled.is_dir() else [base]


def import_inbox(wh: Warehouse | None = None) -> dict:
    """인박스(`warehouse/inbox/image[/image]`)를 창고 원본으로 가져온다.

    - 폴더 이름을 `config/brands.yaml`의 브랜드(별칭 포함)로 맞춘다.
    - `<브랜드>/<폴더>/파일` → `originals/<브랜드>/<폴더>`,
      `<브랜드>/파일`(루트 직접) → `originals/<브랜드>` (키워드 폴더 씨앗으로만 쓰인다).
    - `ignored_inbox_folders`(포토워셔 등)는 건너뛴다.
    - 인박스 파일은 지우지 않는다(복사만).
    """
    from v2r.warehouse.store import brand_entry, brand_folder_name, load_brands_config

    wh = wh or Warehouse()
    wh.ensure_dirs()
    cfg = load_brands_config()
    ignored = {
        str(n).strip().casefold() for n in (cfg.get("ignored_inbox_folders") or [])
    }
    stats = _new_stats()
    per_brand: dict[str, dict[str, int]] = {}
    stats["brands"] = per_brand
    roots = _inbox_roots(wh)
    if not roots:
        stats["errors"].append(f"인박스 폴더 없음: {wh.root / 'inbox' / 'image'}")
        return stats

    for root in roots:
        for brand_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if brand_dir.name.casefold() in ignored:
                stats["skipped"] += sum(1 for _ in brand_dir.rglob("*"))
                continue
            if not brand_entry(brand_dir.name, cfg):
                stats["skipped"] += sum(1 for _ in brand_dir.rglob("*"))
                stats["errors"].append(f"브랜드를 알 수 없는 폴더: {brand_dir.name}")
                continue
            brand = brand_folder_name(brand_dir.name, cfg) or brand_dir.name
            counts = per_brand.setdefault(brand, {})

            # 1) 브랜드 루트 직접 파일 → originals/<브랜드> 바로 아래
            loose = [
                p
                for p in sorted(brand_dir.iterdir())
                if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
            ]
            added = 0
            for path in loose:
                stats["scanned"] += 1
                dest_dir = wh.originals_dir / brand
                dest_dir.mkdir(parents=True, exist_ok=True)
                try:
                    dest = _copy_into(wh, path, dest_dir)
                except Exception as exc:
                    stats["errors"].append(f"{path.name}: {exc}")
                    continue
                if dest is None:
                    stats["duplicated"] += 1
                else:
                    stats["added"] += 1
                    added += 1
            if loose:
                counts["(루트)"] = counts.get("(루트)", 0) + added

            # 2) 하위 폴더 → originals/<브랜드>/<폴더>
            for sub in sorted(p for p in brand_dir.iterdir() if p.is_dir()):
                part = collect_from_folder(sub, brand, sub.name, wh)
                _merge(stats, part)
                counts[sub.name] = counts.get(sub.name, 0) + part["added"]
    return stats


def _copy_into(wh: Warehouse, src: Path, dest_dir: Path) -> Path | None:
    """`dest_dir`에 같은 내용이 없으면 복사한다. 이미 있으면 None."""
    import shutil

    from v2r.warehouse.store import sha256

    sha = sha256(src)
    for existing in sorted(dest_dir.iterdir()):
        if existing.is_file() and existing.suffix.lower() in IMAGE_SUFFIXES:
            if sha256(existing) == sha:
                return None
    dest = dest_dir / f"{sha[:16]}{src.suffix.lower() or '.jpg'}"
    if dest.exists():
        return None
    shutil.copy2(src, dest)
    return dest


__all__ = ["collect_from_drive_export", "collect_from_folder", "import_inbox"]
