"""사진 재고 규칙.

브랜드/폴더 단위로 원본·세탁본·사용·미사용 수를 세고, 미사용 세탁본이
기준치(`threshold`)보다 적은 폴더를 자동으로 보충한다.

배치 규칙은 `store.py`와 같다.
    originals/<브랜드>/<폴더>/<원본>
    washed/<원본 sha256>/<변형번호>.jpg
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from v2r.warehouse.store import IMAGE_SUFFIXES, Warehouse, sha256

log = logging.getLogger(__name__)

#: 폴더별 미사용 세탁본 기준치 (이보다 적으면 보충 대상)
DEFAULT_THRESHOLD = 30
#: 보충 시 원본 1장당 추가로 만들 세탁본 수
DEFAULT_ADD_PER_ORIGINAL = 2
#: 브랜드 폴더 바로 아래 원본을 가리키는 이름
ROOT_FOLDER_LABEL = "(루트)"


def used_variants(conn: Any) -> set[str]:
    """`photo_usage`에 기록된 사용 변형 집합 (경로 문자열 + 파일 이름)."""
    out: set[str] = set()
    try:
        rows = conn.execute("SELECT variant FROM photo_usage").fetchall()
    except Exception as exc:  # pragma: no cover - DB가 없거나 스키마 이전
        log.info("photo_usage 조회 실패: %s", exc)
        return out
    for row in rows:
        value = str(row["variant"] if hasattr(row, "keys") else row[0])
        out.add(value)
        out.add(Path(value).stem)
    return out


def iter_folders(wh: Warehouse) -> list[tuple[str, str, list[Path]]]:
    """`(브랜드, 폴더, 원본 목록)` 목록. 브랜드 루트 파일은 `(루트)` 폴더로 본다."""
    base = wh.originals_dir
    if not base.is_dir():
        return []
    out: list[tuple[str, str, list[Path]]] = []
    for brand_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        loose = sorted(
            p
            for p in brand_dir.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        )
        if loose:
            out.append((brand_dir.name, ROOT_FOLDER_LABEL, loose))
        for folder in sorted(p for p in brand_dir.iterdir() if p.is_dir()):
            files = sorted(
                p
                for p in folder.rglob("*")
                if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
            )
            if files:
                out.append((brand_dir.name, folder.name, files))
    return out


def inventory(wh: Warehouse, used: Iterable[str] | None = None) -> list[dict]:
    """브랜드/폴더별 재고: 원본·세탁본·사용·미사용 수.

    `used`는 `photo_usage`의 변형 식별자 모음(경로 또는 파일 이름). 없으면 0으로 본다.
    """
    used_set = set(used or ())
    rows: list[dict] = []
    for brand, folder, originals in iter_folders(wh):
        variants = 0
        used_count = 0
        for original in originals:
            try:
                sha = sha256(original)
            except OSError as exc:
                log.warning("원본 해시 실패 %s: %s", original, exc)
                continue
            for variant in wh.washed_variants(sha):
                variants += 1
                if str(variant) in used_set or variant.stem in used_set:
                    used_count += 1
        rows.append(
            {
                "brand": brand,
                "folder": folder,
                "originals": len(originals),
                "variants": variants,
                "used": used_count,
                "unused": variants - used_count,
            }
        )
    return rows


def low_stock(inv: list[dict], threshold: int = DEFAULT_THRESHOLD) -> list[dict]:
    """미사용 세탁본이 `threshold` 미만인 폴더만 (부족한 순)."""
    rows = [r for r in inv if int(r.get("unused") or 0) < threshold]
    return sorted(rows, key=lambda r: int(r.get("unused") or 0))


def top_up(
    wh: Warehouse,
    threshold: int = DEFAULT_THRESHOLD,
    add_per_original: int = DEFAULT_ADD_PER_ORIGINAL,
    used: Iterable[str] | None = None,
    max_new: int = 3000,
) -> dict:
    """부족한 폴더를 원본 1장당 `add_per_original`장씩 추가 세탁한다."""
    from v2r.warehouse.photo_washer import make_variants

    inv = inventory(wh, used)
    targets = low_stock(inv, threshold)
    by_key = {(r["brand"], r["folder"]): r for r in targets}
    made = 0
    errors: list[str] = []
    folders: list[dict] = []

    for brand, folder, originals in iter_folders(wh):
        if (brand, folder) not in by_key:
            continue
        before = made
        for original in originals:
            if made >= max_new:
                break
            try:
                sha = sha256(original)
                paths = make_variants(
                    original, min(add_per_original, max_new - made), wh.washed_folder(sha)
                )
            except Exception as exc:
                errors.append(f"{brand}/{folder}/{original.name}: {exc}")
                continue
            made += len(paths)
        folders.append(
            {
                "brand": brand,
                "folder": folder,
                "originals": len(originals),
                "created": made - before,
            }
        )
        if made >= max_new:
            break

    return {
        "ok": not errors,
        "threshold": threshold,
        "low": len(targets),
        "created": made,
        "folders": folders,
        "errors": errors,
    }


def brand_summary(inv: list[dict]) -> list[dict]:
    """브랜드 단위 합계 (상태 보고용)."""
    acc: dict[str, dict] = {}
    for row in inv:
        entry = acc.setdefault(
            row["brand"],
            {"brand": row["brand"], "folders": 0, "originals": 0, "variants": 0, "unused": 0},
        )
        entry["folders"] += 1
        entry["originals"] += int(row.get("originals") or 0)
        entry["variants"] += int(row.get("variants") or 0)
        entry["unused"] += int(row.get("unused") or 0)
    return [acc[k] for k in sorted(acc)]


__all__ = [
    "DEFAULT_ADD_PER_ORIGINAL",
    "DEFAULT_THRESHOLD",
    "ROOT_FOLDER_LABEL",
    "brand_summary",
    "inventory",
    "iter_folders",
    "low_stock",
    "top_up",
    "used_variants",
]
