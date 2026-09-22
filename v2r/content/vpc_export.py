"""가상 PC 산출물 내보내기 (2026-09-23, `D:\\Users\\user\\Downloads\\인수인계.md` 형식 확정).

`가상pc 원고 내보내기` 명령이 만드는 두 산출물:

1. **브랜드 시트 형식**(`게시글 쓰기 원본` 탭과 같은 열 구성) — 브랜드별
   `data/export/<브랜드>_YYMMDD.xlsx`, 시트 이름 = 키워드. 열: A 키워드,
   B 본문(제목/본문 뒤에 `댓글1:`~`대댓글5:` 12개 블록, 대대댓글2는 `2.1:`,
   대대대댓글2는 `2.2:`), D 작성계정(우리 쪽엔 아직 계정 배정이 없어 빈칸),
   E 원고유형, F 완료 링크(발행 전이라 빈칸). C는 인수인계 문서에도 안 쓰는
   빈 열이라 비워 둔다.
2. **댓글 프로그램용 파일** — 인수인계 2절 그대로: 열 3개 고정(작성자 구분 /
   탐지할 댓글 / 댓글 내용), 행 순서
   댓글1→대댓글1→댓글2→대댓글2→2.1→2.2→댓글3→대댓글3→댓글4→대댓글4→댓글5
   →대댓글5→게시글 링크. 계정 배정표를 그대로 적용한다. 통합
   `data/export/댓글_YYMMDD.xlsx`(게시글마다 시트, 시트명 = 게시글번호,
   완료 링크가 없으면 키워드) + `data/export/댓글_YYMMDD_csv/<이름>.csv`
   (UTF-8 BOM, CRLF).

참고 스크립트(GitHub `silas-netizen/chat` 브랜치
`cursor/row77-comment-export-23d4`의 `댓글추출.py`)는 이 환경에서 접근 권한이
없어(비공개 저장소, `gh` 미설치) 가져오지 못했다 — 인수인계 문서 §1·§2의
규칙을 그대로 옮겨 구현했다.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from v2r.store.db import KST

#: 인수인계 §1 표 순서 그대로 — 우리 라벨(`brand_writer.COMMENT_LABELS`)과
#: 문서 표기(`2.1`/`2.2`)를 함께 적는다. `parent`는 "탐지할 댓글"(부모 댓글
#: 원문)을 찾을 라벨 — 1~5번 댓글(최상위)은 부모가 없다(빈칸).
COMMENT_ROWS: tuple[dict[str, str], ...] = (
    {"label": "댓글1", "doc": "댓글1", "parent": ""},
    {"label": "대댓글1", "doc": "대댓글1", "parent": "댓글1"},
    {"label": "댓글2", "doc": "댓글2", "parent": ""},
    {"label": "대댓글2", "doc": "대댓글2", "parent": "댓글2"},
    {"label": "대대댓글2", "doc": "2.1", "parent": "대댓글2"},
    {"label": "대대대댓글2", "doc": "2.2", "parent": "대대댓글2"},
    {"label": "댓글3", "doc": "댓글3", "parent": ""},
    {"label": "대댓글3", "doc": "대댓글3", "parent": "댓글3"},
    {"label": "댓글4", "doc": "댓글4", "parent": ""},
    {"label": "대댓글4", "doc": "대댓글4", "parent": "댓글4"},
    {"label": "댓글5", "doc": "댓글5", "parent": ""},
    {"label": "대댓글5", "doc": "대댓글5", "parent": "댓글5"},
)

#: 인수인계 §2 계정 배정표(고정값). 대댓글1~5·2.2(후기형)은 시트 D열
#: 작성계정을 쓴다(우리 쪽엔 아직 계정 배정이 없어 빈칸으로 남는다).
FIXED_ACCOUNTS = {
    "댓글1": "taboprou",
    "댓글2": "mizupph",
    "댓글3": "urffero",
    "댓글4": "pdillar",
    "댓글5": "hushnane",
}
#: 2.1/2.2는 원고유형(질문형/후기형)에 따라 갈린다. `writer`는 "시트 D열
#: 작성계정"을 뜻한다.
CONDITIONAL_ACCOUNTS = {
    ("대대댓글2", "질문형"): "mizupph",
    ("대대댓글2", "후기형"): "reaicia",
    ("대대대댓글2", "질문형"): "reaicia",
    ("대대대댓글2", "후기형"): "writer",
}

#: 댓글 원문에서 제거하는 문구(인수인계 §2)
_TEST_NOTICE_RE = re.compile(r"\(테스트\s*입니다\.?\)")
#: 자리표시 댓글 패턴 — `테스트1`, `테스트11`처럼 "테스트"+숫자만인 것만.
#: "테스트기"처럼 실제 문장 속 낱말은 걸리지 않는다(전체 일치만 본다).
_PLACEHOLDER_RE = re.compile(r"^테스트\d*$")


def _clean_comment_text(text: str) -> str:
    return _TEST_NOTICE_RE.sub("", text or "").strip()


def _is_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER_RE.match((text or "").strip()))


def _account_for(label: str, mtype: str, writer_account: str) -> str:
    """댓글 위치(라벨)·원고유형·작성계정으로 계정 배정표를 적용한다."""
    if label in FIXED_ACCOUNTS:
        return FIXED_ACCOUNTS[label]
    if label in ("대댓글1", "대댓글2", "대댓글3", "대댓글4", "대댓글5"):
        return writer_account
    key = (label, mtype or "질문형")
    acc = CONDITIONAL_ACCOUNTS.get(key, "")
    if acc == "writer":
        return writer_account
    return acc


def _load_manuscript(row: Any) -> dict | None:
    path = row["manuscript_path"]
    if not path or not Path(path).exists():
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _comments_map(data: dict) -> dict[str, str]:
    return {
        (c.get("label") or "").replace(" ", ""): c.get("text", "")
        for c in (data.get("comments") or [])
    }


def build_body_block(data: dict) -> str:
    """브랜드 시트 B열: 제목/본문 뒤에 12개 댓글 블록(`댓글1:`~`2.2:`)."""
    comments = _comments_map(data)
    lines = [data.get("title", ""), "", data.get("body", "")]
    for row in COMMENT_ROWS:
        lines.append("")
        lines.append(f"{row['doc']}: {comments.get(row['label'], '')}")
    return "\n".join(lines)


def export_brand_sheets(rt: Any, rows: list[Any], out_root: Path) -> dict:
    """브랜드별 `<브랜드>_YYMMDD.xlsx`(시트 이름 = 키워드)."""
    import openpyxl

    by_brand: dict[str, list[Any]] = {}
    for row in rows:
        by_brand.setdefault(row["brand"], []).append(row)

    today = datetime.now(KST).strftime("%y%m%d")
    written: dict[str, str] = {}
    for brand, brand_rows in by_brand.items():
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for row in brand_rows:
            data = _load_manuscript(row)
            if data is None:
                continue
            keyword = row["keyword"]
            ws = wb.create_sheet(title=keyword[:31] or "키워드")
            ws.append(["키워드", "본문", "", "작성계정", "원고유형", "완료 링크"])
            ws.append([keyword, build_body_block(data), "", "", row["mtype"] or "", ""])
        if not wb.sheetnames:
            continue
        out_root.mkdir(parents=True, exist_ok=True)
        path = out_root / f"{brand}_{today}.xlsx"
        wb.save(str(path))
        written[brand] = str(path)
    return written


def _post_rows(data: dict, mtype: str, writer_account: str, link: str) -> list[tuple[str, str, str]]:
    """게시글 한 건 → (작성자 구분, 탐지할 댓글, 댓글 내용) 행 목록(링크 행 포함)."""
    comments = {k: _clean_comment_text(v) for k, v in _comments_map(data).items()}
    out: list[tuple[str, str, str]] = []
    for row in COMMENT_ROWS:
        label = row["label"]
        text = comments.get(label, "")
        parent_text = comments.get(row["parent"], "") if row["parent"] else ""
        account = _account_for(label, mtype, writer_account)
        out.append((account, parent_text, text))
    out.append((link, "", ""))  # 게시글 URL(마지막 행)
    return out


def _post_number(link: str, fallback: str) -> str:
    """완료 링크의 마지막 숫자(게시글번호). 링크가 없으면 키워드로 대체."""
    if link:
        m = re.search(r"(\d+)(?:/)?$", link.strip())
        if m:
            return m.group(1)
    return fallback


def export_comment_program(rt: Any, rows: list[Any], out_root: Path) -> dict:
    """통합 `댓글_YYMMDD.xlsx` + `댓글_YYMMDD_csv/<이름>.csv`."""
    import openpyxl

    today = datetime.now(KST).strftime("%y%m%d")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    csv_dir = out_root / f"댓글_{today}_csv"

    sheet_names: list[str] = []
    excluded: list[str] = []
    no_link: list[str] = []
    for row in rows:
        data = _load_manuscript(row)
        if data is None:
            continue
        comments_raw = [c.get("text", "") for c in (data.get("comments") or [])]
        if comments_raw and all(_is_placeholder(t) for t in comments_raw):
            excluded.append(row["keyword"])
            continue
        link = ""  # 아직 발행 전이라 완료 링크가 없다(인수인계 §2 "링크 없음")
        name = _post_number(link, row["keyword"])
        if not link:
            no_link.append(row["keyword"])
        lines = _post_rows(data, row["mtype"] or "", "", link)

        sheet_title = name[:31] or "게시글"
        base_title, i = sheet_title, 2
        while base_title in sheet_names:
            base_title = f"{sheet_title[:28]}_{i}"
            i += 1
        sheet_title = base_title
        sheet_names.append(sheet_title)

        ws = wb.create_sheet(title=sheet_title)
        ws.append(["작성자 구분", "탐지할 댓글", "댓글 내용"])
        for line in lines:
            ws.append(list(line))

        csv_dir.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(["작성자 구분", "탐지할 댓글", "댓글 내용"])
        for line in lines:
            writer.writerow(list(line))
        (csv_dir / f"{sheet_title}.csv").write_bytes(
            b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
        )

    xlsx_path = ""
    if wb.sheetnames:
        out_root.mkdir(parents=True, exist_ok=True)
        xlsx_path = str(out_root / f"댓글_{today}.xlsx")
        wb.save(xlsx_path)

    return {
        "path": xlsx_path,
        "csv_dir": str(csv_dir) if wb.sheetnames else "",
        "posts": len(sheet_names),
        "excluded": excluded,
        "no_link": no_link,
    }


__all__ = [
    "COMMENT_ROWS",
    "build_body_block",
    "export_brand_sheets",
    "export_comment_program",
]
