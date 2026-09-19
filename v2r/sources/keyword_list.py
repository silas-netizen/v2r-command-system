"""브랜드 시트 `노출 현황` 탭에서 다시 쓸 키워드를 읽는다.

노출 상태가 `밀려남`인 행 = 검색 결과에서 밀려나 **새 원고가 필요한 키워드**다.

보안: 이 탭에는 `작성자 비밀번호` 열이 있다. **절대 읽지도 기록하지도 않는다.**
`PASSWORD_HEADERS`에 걸리는 열은 값을 꺼내기 전에 통째로 버린다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from v2r.sources.sheets import SourceError, fetch_csv, gviz_csv_url

#: 노출 현황 탭 이름
EXPOSURE_SHEET = "노출 현황"

#: 새 원고가 필요한 노출 상태
PUSHED_STATUS = "밀려남"

#: 절대 읽지 않는 열 (비밀번호)
PASSWORD_HEADERS = ("작성자비밀번호", "비밀번호", "password", "pw")

_STATUS_HEADERS = ("노출상태", "상태")
_KEYWORD_HEADERS = ("키워드",)
_CAFE_HEADERS = ("카페", "카페명")


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _is_password_header(name: Any) -> bool:
    """비밀번호 열인가."""
    key = _norm(name)
    return any(key == _norm(h) or _norm(h) in key for h in PASSWORD_HEADERS)


def _pick(row: dict, aliases: tuple[str, ...]) -> str:
    for key, value in row.items():
        if _norm(key) in {_norm(a) for a in aliases}:
            return str(value or "").strip()
    return ""


def _drop_password_columns(rows: list[dict]) -> list[dict]:
    """비밀번호 열을 통째로 제거한 행 목록."""
    return [
        {k: v for k, v in row.items() if not _is_password_header(k)} for row in rows
    ]


def rows_from_xlsx(path: str | Path, sheet: str = EXPOSURE_SHEET) -> list[dict]:
    """로컬 xlsx의 탭 한 개 → dict 행 목록 (비밀번호 열 제외)."""
    import openpyxl

    book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        if sheet not in book.sheetnames:
            raise SourceError(f"탭을 찾을 수 없습니다: {sheet}")
        ws = book[sheet]
        table = [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        book.close()
    if not table:
        return []
    header = [str(c or "").strip() for c in table[0]]
    # 비밀번호 열은 자리 자체를 버린다 (값이 메모리에 남지 않게)
    keep = [i for i, name in enumerate(header) if not _is_password_header(name)]
    out: list[dict] = []
    for raw in table[1:]:
        row = {}
        for i in keep:
            name = header[i] or f"_{i}"
            row[name] = "" if i >= len(raw) or raw[i] is None else str(raw[i]).strip()
        out.append(row)
    return out


def _brand_spreadsheet_id(brand: str, cfg: dict | None) -> str:
    entry = ((cfg or {}).get("brand_sheets") or {}).get(brand) or {}
    return str(entry.get("spreadsheet_id") or "")


def load_pushed_keywords(
    brand: str,
    cfg: dict | None = None,
    xlsx_path: str | Path | None = None,
    limit: int = 0,
) -> list[dict]:
    """브랜드의 `밀려남` 키워드 목록.

    반환: `[{"keyword": "...", "cafe": "..."}]` — 시트 순서 유지, 키워드 기준 중복 제거.
    `xlsx_path`를 주면 네트워크를 쓰지 않고 로컬 엑셀을 읽는다.
    """
    if xlsx_path:
        rows = rows_from_xlsx(xlsx_path)
    else:
        sid = _brand_spreadsheet_id(brand, cfg)
        if not sid:
            raise SourceError(f"브랜드 시트를 찾을 수 없습니다: {brand}")
        url = gviz_csv_url(sid, sheet=EXPOSURE_SHEET, headers=0)
        rows = _drop_password_columns(fetch_csv(url))

    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if _pick(row, _STATUS_HEADERS) != PUSHED_STATUS:
            continue
        keyword = _pick(row, _KEYWORD_HEADERS)
        if not keyword:
            continue
        key = _norm(keyword)
        if key in seen:
            continue
        seen.add(key)
        out.append({"keyword": keyword, "cafe": _pick(row, _CAFE_HEADERS)})
        if limit and len(out) >= limit:
            break
    return out


__all__ = [
    "EXPOSURE_SHEET",
    "PUSHED_STATUS",
    "PASSWORD_HEADERS",
    "load_pushed_keywords",
    "rows_from_xlsx",
]
