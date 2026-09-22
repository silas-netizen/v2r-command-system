"""시트 쓰기 인터페이스 — 로그인·OAuth·토큰 없이 구현 (2026-09-23).

브랜드 시트는 전부 "링크 있는 누구나 편집" 상태이므로, 구글 OAuth 대신
**헤드리스 Playwright**로 구글 시트 편집 화면을 직접 열어 쓴다.

방식:
1. 로그인하지 않은 새 브라우저 컨텍스트(사용자 프로필 재사용 금지)로
   `https://docs.google.com/spreadsheets/d/<id>/edit#gid=<gid>` 를 연다.
2. 이름 상자(Name box)에 대상 셀 주소를 입력해 커서를 옮긴다.
3. 선택된 셀에 TSV 문자열을 **paste 이벤트**로 주입한다 — 여러 행·열이
   한 번에 채워진다(구글 시트는 탭/개행이 든 클립보드 붙여넣기를 표 형태로
   해석한다).
4. "모든 변경사항이 드라이브에 저장됨" 표시를 기다린다.
5. `export?format=csv&gid=`(공개 CSV) 링크로 다시 읽어 값이 실제로
   반영됐는지 검증한다. 실패하면 최대 3회 재시도한다.

자격증명이 전혀 필요 없으므로 `has_credentials`는 그대로 두되 항상 `True`를
돌려준다(구 인터페이스 호환용). `update_rows`/`set_cell`/`append_rows`/
`update_by_key` 네 함수 시그니처는 기존 호출부와 호환되도록 유지한다.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

#: 구 인터페이스 호환용(더 이상 실제로 쓰이지 않는다 — OAuth 불필요)
DEFAULT_TOKEN_PATH = "data/google-oauth/token.json"

EXPORT_CSV = "https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"
#: 대체 CSV 경로 — 일부 시트에서 `export?format=csv`가 400을 돌려줘서 gviz로 폴백한다.
GVIZ_CSV = (
    "https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&gid={gid}"
)
EDIT_URL = "https://docs.google.com/spreadsheets/d/{sid}/edit#gid={gid}"

_RETRIES = 3
_LOCK_DIR = "data/locks"
_LOCK_TIMEOUT = 60.0  # 초 — 다른 프로세스가 같은 브랜드 시트를 쓰고 있으면 대기
_LOCK_STALE = 300.0  # 초 — 이보다 오래된 잠금 파일은 죽은 프로세스로 보고 지운다


class SheetsWriteError(RuntimeError):
    """시트 쓰기 실패."""


def has_credentials(repo_root: str | Path = ".", token_path: str = DEFAULT_TOKEN_PATH) -> bool:
    """호환용 — 이제 자격증명이 필요 없으므로 항상 `True`."""
    return True


# --------------------------------------------------------------------------
# 브랜드별 파일 잠금 (동시에 여러 프로세스가 같은 시트를 쓰지 않도록)
# --------------------------------------------------------------------------


class _BrandLock:
    """`spreadsheet_id` 하나당 잠금 파일 하나. `with`로 사용."""

    def __init__(self, spreadsheet_id: str, repo_root: str | Path = ".") -> None:
        self._path = Path(repo_root) / _LOCK_DIR / f"sheet-{spreadsheet_id}.lock"

    def __enter__(self) -> "_BrandLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = time.time() - self._path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > _LOCK_STALE:
                    self._path.unlink(missing_ok=True)
                    continue
                if time.monotonic() > deadline:
                    raise SheetsWriteError(f"시트 잠금 대기 시간 초과: {self._path}")
                time.sleep(0.5)

    def __exit__(self, *exc: Any) -> None:
        self._path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# CSV 검증
# --------------------------------------------------------------------------


def _read_export_csv(spreadsheet_id: str, gid: str | int = 0, timeout: float = 15.0) -> list[list[str]]:
    """공개 CSV 링크로 시트를 표 형태(행×열)로 읽는다.

    `export?format=csv`를 먼저 쓰고, 400(일부 시트에서 발생)이면 gviz CSV로
    폴백한다 — 둘 다 "링크 있는 누구나 편집" 시트에서 로그인 없이 읽힌다.
    """
    for url in (
        EXPORT_CSV.format(sid=spreadsheet_id, gid=gid),
        GVIZ_CSV.format(sid=spreadsheet_id, gid=gid),
    ):
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError:
            continue
        text = resp.content.decode("utf-8-sig", errors="replace")
        return list(csv.reader(io.StringIO(text)))
    raise SheetsWriteError(f"CSV 읽기 실패(export/gviz 모두): {spreadsheet_id} gid={gid}")


def _col_letter(idx0: int) -> str:
    """0-based 열 번호 → 'A', 'B', ... 'Z', 'AA' ..."""
    n = idx0 + 1
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell_ref(row1: int, col_letter: str) -> str:
    return f"{col_letter}{row1}"


def _parse_cell(cell: str) -> tuple[str, int]:
    """'H2' -> ('H', 2)"""
    i = 0
    while i < len(cell) and cell[i].isalpha():
        i += 1
    return cell[:i].upper(), int(cell[i:])


# --------------------------------------------------------------------------
# Playwright 저수준: 이름 상자 이동 + paste 주입
# --------------------------------------------------------------------------


def _write_tsv_at(spreadsheet_id: str, gid: str | int, cell: str, tsv: str) -> None:
    """`cell`에서 시작해 `tsv`(탭=열 구분, 개행=행 구분)를 붙여넣는다."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()  # 로그인 없는 새 컨텍스트
            page = context.new_page()
            # 구글 시트는 웹소켓/폴링으로 네트워크가 계속 바쁘므로 "networkidle"은
            # 절대 오지 않는다 — DOM 로드만 기다리고 이름 상자 등장을 따로 기다린다.
            page.goto(EDIT_URL.format(sid=spreadsheet_id, gid=gid), wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector("#t-name-box", timeout=45000)
            page.wait_for_timeout(1000)  # 그리드 초기화 여유

            # 이름 상자에 셀 주소 입력 → Enter로 이동
            page.click("#t-name-box")
            page.keyboard.press("Control+A")
            page.keyboard.type(cell)
            page.keyboard.press("Enter")
            page.wait_for_timeout(300)

            if tsv == "":
                # 빈 문자열은 paste 이벤트로 지워지지 않는다(클립보드에 아무 것도
                # 안 넣은 것과 같아서 무시됨) — Delete 키로 셀을 비운다.
                page.keyboard.press("Delete")
            else:
                # 활성 그리드 요소에 paste 이벤트로 TSV 주입
                page.evaluate(
                    """(text) => {
                        const dt = new DataTransfer();
                        dt.setData('text/plain', text);
                        const target = document.activeElement || document.body;
                        const evt = new ClipboardEvent('paste', {
                            bubbles: true, cancelable: true, clipboardData: dt,
                        });
                        target.dispatchEvent(evt);
                    }""",
                    tsv,
                )
            page.wait_for_timeout(800)

            # 저장 완료 대기: "모든 변경사항이 드라이브에 저장됨" 계열 텍스트
            try:
                page.wait_for_function(
                    """() => {
                        const el = document.querySelector('#docs-title-saving-status, .docs-title-saving-status');
                        if (!el) return true;
                        const t = el.textContent || '';
                        return !t.includes('저장 중') && !/Saving/i.test(t);
                    }""",
                    timeout=15000,
                )
            except Exception:
                page.wait_for_timeout(2000)
        finally:
            browser.close()


def _write_with_retry(spreadsheet_id: str, gid: str | int, cell: str, tsv: str) -> None:
    last_exc: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            _write_tsv_at(spreadsheet_id, gid, cell, tsv)
            return
        except Exception as exc:  # pragma: no cover - 네트워크/UI 문제
            last_exc = exc
            log.warning("시트 쓰기 시도 %d/%d 실패(%s): %s", attempt, _RETRIES, cell, exc)
            time.sleep(1.5 * attempt)
    raise SheetsWriteError(f"시트 쓰기 실패({cell}): {last_exc}") from last_exc


def _verify(
    spreadsheet_id: str, gid: str | int, start_row1: int, start_col_letter: str, expected: list[list[str]]
) -> bool:
    """CSV를 다시 읽어 `expected`(행×열)와 일치하는지 확인."""
    table = _read_export_csv(spreadsheet_id, gid)
    col0 = ord(start_col_letter) - ord("A")  # 단일 문자 열만 지원(A~Z)
    for r, row in enumerate(expected):
        row_idx = start_row1 - 1 + r
        if row_idx >= len(table):
            return False
        actual_row = table[row_idx]
        for c, val in enumerate(row):
            col_idx = col0 + c
            actual = actual_row[col_idx] if col_idx < len(actual_row) else ""
            if str(actual) != str(val):
                return False
    return True


def _write_verified(
    spreadsheet_id: str,
    gid: str | int,
    cell: str,
    rows: list[list[str]],
    repo_root: str | Path = ".",
) -> dict[str, Any]:
    """TSV로 쓰고 CSV로 검증(최대 `_RETRIES`회)."""
    col_letter, row1 = _parse_cell(cell)
    tsv = "\n".join("\t".join(str(v) for v in row) for row in rows)
    with _BrandLock(spreadsheet_id, repo_root):
        last_error = ""
        for attempt in range(1, _RETRIES + 1):
            try:
                _write_tsv_at(spreadsheet_id, gid, cell, tsv)
            except Exception as exc:
                last_error = str(exc)
                log.warning("쓰기 시도 %d/%d 실패: %s", attempt, _RETRIES, exc)
                time.sleep(1.5 * attempt)
                continue
            try:
                ok = _verify(spreadsheet_id, gid, row1, col_letter, rows)
            except Exception as exc:  # pragma: no cover - 검증 네트워크 문제
                ok = False
                last_error = str(exc)
            if ok:
                return {"written": len(rows), "mode": "sheets", "cell": cell}
            last_error = last_error or "검증 불일치"
            log.warning("검증 실패(%s) — 재시도 %d/%d", cell, attempt, _RETRIES)
        return {"written": 0, "mode": "csv_only", "error": last_error, "cell": cell}


# --------------------------------------------------------------------------
# 공개 인터페이스
# --------------------------------------------------------------------------


def update_rows(
    spreadsheet_id: str,
    sheet: str,
    rows: list[dict[str, Any]],
    *,
    key_column: str = "키워드",
    header: list[str] | None = None,
    gid: str | int = 0,
    repo_root: str | Path = ".",
    token_path: str = DEFAULT_TOKEN_PATH,
) -> dict[str, Any]:
    """`rows`(딕셔너리, 헤더명 그대로)를 `key_column` 기준으로 있으면 갱신·
    없으면 끝에 추가한다.

    `header`(열 이름 순서)를 안 주면 시트 1행을 CSV로 읽어 사용한다.
    """
    try:
        table = _read_export_csv(spreadsheet_id, gid)
    except Exception as exc:
        return {"written": 0, "mode": "csv_only", "error": str(exc)}
    if not table:
        return {"written": 0, "mode": "csv_only", "error": "빈 시트"}
    hdr = header or table[0]
    try:
        key_idx = hdr.index(key_column)
    except ValueError:
        return {"written": 0, "mode": "csv_only", "error": f"열 없음: {key_column}"}
    existing_keys = {row[key_idx]: i + 1 for i, row in enumerate(table) if i > 0}  # 1-based data row

    written = 0
    to_append: list[list[str]] = []
    for row in rows:
        key = str(row.get(key_column, ""))
        values = [str(row.get(h, "")) for h in hdr]
        if key in existing_keys:
            row1 = existing_keys[key] + 1  # +1: 헤더 포함 1-based
            res = _write_verified(spreadsheet_id, gid, _cell_ref(row1, "A"), [values], repo_root)
            if res.get("mode") == "sheets":
                written += 1
        else:
            to_append.append(values)
    if to_append:
        res = append_rows(spreadsheet_id, sheet, to_append, header=hdr, gid=gid, repo_root=repo_root)
        written += res.get("written", 0)
    return {"written": written, "mode": "sheets" if written else "csv_only"}


def append_rows(
    spreadsheet_id: str,
    sheet: str,
    rows: list[list[str]] | list[dict[str, Any]],
    *,
    header: list[str] | None = None,
    gid: str | int = 0,
    repo_root: str | Path = ".",
    token_path: str = DEFAULT_TOKEN_PATH,
) -> dict[str, Any]:
    """시트 끝(마지막 데이터 행 다음)에 행을 append한다.

    `rows`는 리스트의 리스트(값 순서 그대로) 또는 딕셔너리(헤더명 필요) 모두
    받는다. 1,000행 단위로 나눠 paste한다.
    """
    try:
        table = _read_export_csv(spreadsheet_id, gid)
    except Exception as exc:
        return {"written": 0, "mode": "csv_only", "error": str(exc)}
    hdr = header or (table[0] if table else [])
    value_rows: list[list[str]] = []
    for row in rows:
        if isinstance(row, dict):
            value_rows.append([str(row.get(h, "")) for h in hdr])
        else:
            value_rows.append([str(v) for v in row])
    if not value_rows:
        return {"written": 0, "mode": "sheets"}

    start_row1 = len(table) + 1
    written = 0
    errors: list[str] = []
    CHUNK = 1000
    for i in range(0, len(value_rows), CHUNK):
        chunk = value_rows[i : i + CHUNK]
        cell = _cell_ref(start_row1 + i, "A")
        res = _write_verified(spreadsheet_id, gid, cell, chunk, repo_root)
        written += res.get("written", 0)
        if res.get("error"):
            errors.append(res["error"])
    out: dict[str, Any] = {"written": written, "mode": "sheets" if written == len(value_rows) else "csv_only"}
    if errors:
        out["error"] = "; ".join(errors)
    return out


def update_by_key(
    spreadsheet_id: str,
    sheet: str,
    key_value: str,
    updates: dict[str, Any],
    *,
    key_column: str = "H",
    key_header: str | None = None,
    gid: str | int = 0,
    repo_root: str | Path = ".",
    token_path: str = DEFAULT_TOKEN_PATH,
) -> dict[str, Any]:
    """`key_column`(열 문자, 기본 H) 값이 `key_value`인 행을 찾아 `updates`
    (열 문자 -> 값, 예 `{"G": "확인", "I": "..."}`)를 그 행에 반영한다.

    노출 순환용: H열 키워드로 행을 찾아 G/I/J/L/O 등을 갱신하는 용도.
    """
    try:
        table = _read_export_csv(spreadsheet_id, gid)
    except Exception as exc:
        return {"written": 0, "mode": "csv_only", "error": str(exc)}
    key_col0 = ord(key_column.upper()) - ord("A")
    row1 = None
    for i, row in enumerate(table):
        if key_col0 < len(row) and row[key_col0] == key_value:
            row1 = i + 1
            break
    if row1 is None:
        return {"written": 0, "mode": "csv_only", "error": f"키를 찾지 못함: {key_value}"}

    written = 0
    errors: list[str] = []
    for col_letter, value in updates.items():
        cell = _cell_ref(row1, col_letter.upper())
        res = _write_verified(spreadsheet_id, gid, cell, [[str(value)]], repo_root)
        if res.get("mode") == "sheets":
            written += 1
        elif res.get("error"):
            errors.append(f"{cell}: {res['error']}")
    out = {"written": written, "mode": "sheets" if written == len(updates) else "csv_only", "row": row1}
    if errors:
        out["error"] = "; ".join(errors)
    return out


def set_cell(
    spreadsheet_id: str,
    sheet: str,
    cell: str,
    value: Any,
    *,
    gid: str | int = 0,
    repo_root: str | Path = ".",
    token_path: str = DEFAULT_TOKEN_PATH,
) -> dict[str, Any]:
    """셀 하나(P1/Q1 합계 등)를 쓴다."""
    res = _write_verified(spreadsheet_id, gid, cell, [[str(value)]], repo_root)
    return {"written": res.get("mode") == "sheets", "mode": res.get("mode"), **({"error": res["error"]} if res.get("error") else {})}


__all__ = [
    "SheetsWriteError",
    "DEFAULT_TOKEN_PATH",
    "has_credentials",
    "update_rows",
    "append_rows",
    "update_by_key",
    "set_cell",
]
