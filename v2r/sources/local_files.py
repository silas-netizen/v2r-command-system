"""로컬 엑셀/CSV 행 적재."""

from __future__ import annotations

import csv
from pathlib import Path


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
