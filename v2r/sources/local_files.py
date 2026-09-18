"""로컬 엑셀/CSV 행 적재 (결정 3·7, 2026-09-19).

`각색_…xlsx` = 브랜드 원고(각색된 글) 엑셀. `config/sources.yaml`의
`local_xlsx_dir`(기본 `warehouse/inbox/sheets`)에 두면 자동으로 원본으로 등록된다.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

KIND = "xlsx"

#: 등록 대상 파일 패턴 (`각색`이 이름에 든 xlsx)
XLSX_GLOB = "*각색*.xlsx"
#: 기본 폴더
DEFAULT_XLSX_DIR = "warehouse/inbox/sheets"

_PAREN = re.compile(r"\(([^()]+)\)")


def discover_xlsx(directory: str | Path) -> list[dict]:
    """폴더에서 `*각색*.xlsx`를 찾아 원본 항목 목록으로 만든다.

    항목 형식: `{name: <파일 stem>, kind: "xlsx", path: <절대 경로 문자열>}`
    """
    base = Path(directory)
    if not base.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(base.glob(XLSX_GLOB)):
        if not path.is_file() or path.name.startswith("~$"):
            continue
        out.append({"name": path.stem, "kind": KIND, "path": str(path)})
    return out


def brand_from_filename(name: str) -> str:
    """파일 이름 마지막 괄호 안 문자열 = 브랜드 (legacy `brand_from_sheet_title`)."""
    found = _PAREN.findall(Path(str(name or "")).stem)
    return found[-1].strip() if found else ""


def brand_from_rows(rows: list[dict]) -> str:
    """행의 `브랜드` 열(없으면 `카페` 계열 열)에서 브랜드를 추정한다."""
    for row in rows or []:
        for key, value in row.items():
            norm = re.sub(r"\s+", "", str(key or ""))
            if norm in ("브랜드", "브랜드명") and str(value or "").strip():
                return str(value).strip()
    for row in rows or []:
        for key, value in row.items():
            norm = re.sub(r"\s+", "", str(key or ""))
            if norm in ("카페", "카페명") and str(value or "").strip():
                return brand_from_filename(str(value))
    return ""


def parse_xlsx_entry(entry: dict, cafes_cfg: dict | None = None):
    """xlsx 원본 1건 → 원고 목록. 헤더를 보고 각색/제휴 파서를 고른다."""
    from v2r.sources import sheets

    rows = load_xlsx_rows(entry.get("path") or "")
    name = str(entry.get("name") or "")
    if not rows:
        return []
    have = {sheets._norm_key(k) for k in rows[0].keys()}
    if {sheets._norm_key("각색제목"), sheets._norm_key("각색본문")} <= have:
        return sheets.parse_adapted_rows(rows, cafes_cfg, source=name)
    return sheets.parse_affiliate_rows(rows, source=name)


def load_xlsx_rows(path: str | Path, sheet: str | None = None) -> list[dict]:
    """엑셀 첫 행을 헤더로 읽어 dict 행 목록 반환."""
    from openpyxl import load_workbook

    wb = load_workbook(Path(path), data_only=True)
    ws = wb[sheet] if sheet else wb[wb.sheetnames[0]]
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = next(rows_iter)
    except StopIteration:
        wb.close()
        return []
    keys = [str(h).strip() if h is not None else f"col{i}" for i, h in enumerate(header)]
    out: list[dict] = []
    for values in rows_iter:
        if values is None or all(v is None or str(v).strip() == "" for v in values):
            continue
        out.append(
            {
                keys[i]: ("" if values[i] is None else str(values[i]).strip())
                for i in range(min(len(keys), len(values)))
            }
        )
    wb.close()
    return out


def load_csv_rows(path: str | Path) -> list[dict]:
    """CSV를 dict 행 목록으로 읽는다(UTF-8 BOM 허용)."""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fp:
        return [
            {k: ("" if v is None else str(v).strip()) for k, v in row.items()}
            for row in csv.DictReader(fp)
        ]


__all__ = [
    "DEFAULT_XLSX_DIR",
    "KIND",
    "XLSX_GLOB",
    "brand_from_filename",
    "brand_from_rows",
    "discover_xlsx",
    "load_csv_rows",
    "load_xlsx_rows",
    "parse_xlsx_entry",
]
