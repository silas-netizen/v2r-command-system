"""로컬 엑셀/CSV 행 적재 (결정 3·7, 2026-09-19).

`warehouse/inbox/sheets/*각색*.xlsx`는 **자사 카페 일상 글**(이미 각색해 둔 글) 모음이다.
브랜드 원고가 아니다. 원본 종류 이름은 `xlsx_daily`이며 `publish_daily`의 일상 글 풀로 들어간다.

두 가지 헤더 배치를 A~F열만 읽어서 처리한다.

| 파일 | A | B | C | D | E | F |
|---|---|---|---|---|---|---|
| `각색_전체_*` | 카페명 | 게시판명 | 게시판링크 | 작성계정 | 각색제목 | 각색본문 |
| `각색_<카페>_*` | 카페명 | 게시판명 | 각색제목 | 각색본문 | 등록시간 | 상태 |

`등록시간`이 차 있거나 `상태`에 완료/발행이 들어 있는 행은 이미 올라간 글이라 건너뛴다.
`source_key`는 파일 stem, `row_number`는 엑셀 실제 행 번호(헤더 다음이 2)다.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

#: 원본 종류 이름
KIND = "xlsx_daily"

#: 등록 대상 파일 패턴 (`각색`이 이름에 든 xlsx)
XLSX_GLOB = "*각색*.xlsx"
#: 기본 폴더
DEFAULT_XLSX_DIR = "warehouse/inbox/sheets"

#: 이미 발행된 것으로 보는 상태 문자열
DONE_STATUS_WORDS = ("완료", "발행", "등록됨")

_PAREN = re.compile(r"\(([^()]+)\)")
_LINK = re.compile(r"^https?://", re.IGNORECASE)


def discover_xlsx(directory: str | Path) -> list[dict]:
    """폴더에서 `*각색*.xlsx`를 찾아 원본 항목 목록으로 만든다.

    항목 형식: `{name: <파일 stem>, kind: "xlsx_daily", path: <절대 경로 문자열>}`
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
    """파일 이름 마지막 괄호 안 문자열 (legacy `brand_from_sheet_title`).

    현재 인박스의 파일들은 브랜드 원고가 아니라 일상 글이라 쓰이지 않는다.
    """
    found = _PAREN.findall(Path(str(name or "")).stem)
    return found[-1].strip() if found else ""


def _norm(text) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _cells(row: dict) -> list[str]:
    """행을 열 순서대로 문자열 목록으로."""
    return [("" if v is None else str(v).strip()) for v in row.values()]


def sniff_layout(header_keys: list[str]) -> str:
    """헤더를 보고 배치 이름을 정한다: `full`(각색_전체) 또는 `cafe`(카페별)."""
    norms = [_norm(k) for k in header_keys]
    if _norm("게시판링크") in norms or _norm("작성계정") in norms:
        return "full"
    if _norm("상태") in norms or _norm("등록시간") in norms:
        return "cafe"
    # 헤더가 없거나 다르면 열 개수로 추정
    return "full" if len(norms) >= 6 and _norm("각색제목") not in norms[:3] else "cafe"


def _is_published(registered_at: str, status: str) -> bool:
    """등록시간/상태로 이미 올라간 글인지 판단."""
    if registered_at.strip():
        return True
    text = status or ""
    return any(word in text for word in DONE_STATUS_WORDS) or bool(_LINK.match(text))


def parse_daily_xlsx_rows(
    rows: list[dict], source: str = "", cafes_cfg: dict | None = None
) -> list:
    """A~F열만 읽어 일상 글 원고 목록으로. 이미 발행된 행은 건너뛴다."""
    from v2r.content.manuscript import Manuscript, content_hash
    from v2r.content.sanitize import strip_emoji
    from v2r.sources.sheets import _canonical_cafe

    if not rows:
        return []
    layout = sniff_layout(list(rows[0].keys()))
    out = []
    for index, row in enumerate(rows, start=2):
        cells = (_cells(row) + [""] * 6)[:6]
        if layout == "full":
            cafe, board, _link, account, title, body = cells
            registered_at = status = ""
        else:
            cafe, board, title, body, registered_at, status = cells
            account = ""
        if not title and not body:
            continue
        if _is_published(registered_at, status):
            continue
        # 이모지는 읽을 때 지운다 → 모의 실행에도 실제로 올라갈 깨끗한 글이 보인다.
        # 다만 `content_hash`는 **지우기 전 원문**으로 계산한다. 해시는 "이 글을 이미
        # 올렸는가"를 판단하는 열쇠라, 규칙이 바뀌었다고 해시가 달라지면 이미 발행한
        # 글이 새 글로 보여 두 번 올라간다.
        raw_hash = content_hash(title, body)
        title, body = strip_emoji(title), strip_emoji(body)
        out.append(
            Manuscript(
                title=title,
                body=body,
                cafe=_canonical_cafe(cafe, cafes_cfg),
                board=board,
                account=account,
                source=source,
                source_row=index,
                images_enabled=False,  # 일상 글은 사진 없음
                content_hash=raw_hash,
            )
        )
    return out


def parse_xlsx_entry(entry: dict, cafes_cfg: dict | None = None):
    """xlsx 원본 1건 → 일상 글 원고 목록."""
    rows = load_xlsx_rows(entry.get("path") or "")
    return parse_daily_xlsx_rows(rows, source=str(entry.get("name") or ""), cafes_cfg=cafes_cfg)


def load_xlsx_rows(path: str | Path, sheet: str | None = None) -> list[dict]:
    """엑셀 첫 행을 헤더로 읽어 dict 행 목록 반환."""
    from openpyxl import load_workbook

    wb = load_workbook(Path(path), data_only=True, read_only=True)
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
    "discover_xlsx",
    "load_csv_rows",
    "load_xlsx_rows",
    "parse_daily_xlsx_rows",
    "parse_xlsx_entry",
    "sniff_layout",
]
