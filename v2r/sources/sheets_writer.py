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
        browser = launch_chromium(p, headless=True)
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
            if _norm_cell(actual) != _norm_cell(val):
                return False
    return True


def _norm_cell(value: Any) -> str:
    """검증용 정규화 — 시트가 숫자를 자동 서식(천 단위 콤마, 12345.0)해서 돌려주거나
    앞뒤 공백·개행 종류가 달라지는 것을 같은 값으로 본다 (사고 2026-09-23: P1/Q1 합계와
    검색량 열이 이 차이로 1,000회 넘게 "검증 불일치"로 찍혔다)."""
    s = str(value if value is not None else "").strip().replace("\r\n", "\n")
    t = s.replace(",", "")
    try:
        f = float(t)
        if f == int(f):
            return str(int(f))
        return repr(f)
    except ValueError:
        return s


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
    # 2026-09-23 실측: 1,000행을 한 번에 붙여넣으면 690행쯤에서 잘려 검증이 늘 실패했다
    # (같은 자리에 3번 다시 붙여넣기만 반복). 200행씩 나누면 전부 붙는다.
    CHUNK = 200
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


# --------------------------------------------------------------------------
# 브랜드 설정(config/brands.yaml) — spreadsheet_id 조회
# --------------------------------------------------------------------------

DEFAULT_BRANDS_CONFIG = "config/brands.yaml"
DEFAULT_KEYWORDS_DIR = "data/keywords"

#: 시트 두 번째 탭(노출 현황류) 이름 — 명령 로그·보고서 표기용
EXPOSURE_TAB_NAME = "노출 현황"

#: `relevance_llm` 값 -> 본문 분류 라벨(0=가장 직접적, 값이 커질수록 느슨해진다는
#: 기존 연관도 재산정 스케일 전제. `data/keywords/<브랜드>.sqlite`를 만드는
#: keyword_relevance.py 쪽 스케일이 바뀌면 이 매핑도 같이 바꿔야 한다.)
RELEVANCE_LABELS = {0: "직접", 1: "근접", 2: "확장"}
IRRELEVANT_LABEL = "무관"
IRRELEVANT_NOTE = "연관도 무관(자동)"


def _load_brands_config(repo_root: str | Path = ".", config_path: str = DEFAULT_BRANDS_CONFIG) -> dict:
    import yaml  # type: ignore

    path = Path(repo_root) / config_path
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_spreadsheet_id(
    brand: str, repo_root: str | Path = ".", config_path: str = DEFAULT_BRANDS_CONFIG
) -> str | None:
    """`config/brands.yaml`에서 브랜드의 시트 ID를 찾는다.

    `spreadsheet_id:` 키를 우선 보고, 없으면 기존 `sheets: [id, ...]`의
    첫 값을 쓴다(둘 다 결국 config/sources.yaml `brand_sheets`와 같은 ID).
    """
    cfg = _load_brands_config(repo_root, config_path)
    entry = (cfg.get("brands") or {}).get(brand)
    if not entry:
        return None
    if entry.get("spreadsheet_id"):
        return str(entry["spreadsheet_id"])
    sheets = entry.get("sheets") or []
    return str(sheets[0]) if sheets else None


def list_configured_brands(repo_root: str | Path = ".", config_path: str = DEFAULT_BRANDS_CONFIG) -> list[str]:
    cfg = _load_brands_config(repo_root, config_path)
    return list((cfg.get("brands") or {}).keys())


# --------------------------------------------------------------------------
# 시트 탭 목록(gid) — "두 번째 탭"을 이름 없이 찾기 위해
# --------------------------------------------------------------------------


def _list_tabs(spreadsheet_id: str) -> list[tuple[str, int]]:
    """시트의 탭들을 `[(이름, gid), ...]`(왼쪽부터 순서대로)로 돌려준다."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(
                EDIT_URL.format(sid=spreadsheet_id, gid=0), wait_until="domcontentloaded", timeout=45000
            )
            page.wait_for_selector("#t-name-box", timeout=45000)
            page.wait_for_timeout(1000)
            names = page.eval_on_selector_all(".docs-sheet-tab", "els => els.map(e => e.innerText)")
            tabs: list[tuple[str, int]] = []
            for i, name in enumerate(names):
                page.click(f".docs-sheet-tab:nth-child({i + 1})")
                page.wait_for_timeout(400)
                m = page.url.split("gid=")
                gid = int(m[-1]) if len(m) > 1 else 0
                tabs.append((name, gid))
            return tabs
        finally:
            browser.close()


def _second_tab_gid(spreadsheet_id: str) -> int:
    """두 번째 탭(대개 "노출 현황"류)의 gid. 탭이 하나뿐이면 그 탭."""
    tabs = _list_tabs(spreadsheet_id)
    if len(tabs) >= 2:
        return tabs[1][1]
    if tabs:
        return tabs[0][1]
    return 0


# --------------------------------------------------------------------------
# 명령 `시트 키워드 반영 <브랜드|전체>`
# --------------------------------------------------------------------------


def _keyword_search_url(keyword: str) -> str:
    from urllib.parse import quote

    return "https://search.naver.com/search.naver?query=" + quote(keyword)


def sync_keywords_to_sheet(
    brand: str,
    *,
    repo_root: str | Path = ".",
    config_path: str = DEFAULT_BRANDS_CONFIG,
    keywords_dir: str = DEFAULT_KEYWORDS_DIR,
) -> dict[str, Any]:
    """`data/keywords/<브랜드>.sqlite`에서 최종 원고 대상 키워드를 골라 시트
    두 번째 탭에 append한다.

    최종 원고 대상 = `relevance_llm`·`relevance_codex` 둘 다 0~2이고
    `needs_review`가 아님. 두 열이 아예 없으면(아직 재산정 전) 건너뛴다.
    이미 시트 H열에 있는 키워드는 다시 넣지 않는다. 1,000행 단위(append_rows
    가 알아서 나눈다).
    """
    import sqlite3

    sid = get_spreadsheet_id(brand, repo_root, config_path)
    if not sid:
        return {"brand": brand, "skipped": True, "reason": "config/brands.yaml에 spreadsheet_id 없음"}

    db_path = Path(repo_root) / keywords_dir / f"{brand}.sqlite"
    if not db_path.exists():
        return {"brand": brand, "skipped": True, "reason": f"키워드 DB 없음: {db_path}"}

    con = sqlite3.connect(str(db_path))
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(keywords)")}
        required = {"relevance_llm", "relevance_codex", "needs_review"}
        if not required.issubset(cols):
            return {"brand": brand, "skipped": True, "reason": f"미산정 열 없음: {required - cols}"}

        rows = con.execute(
            """
            select keyword, total, rationale, relevance_llm
            from keywords
            where relevance_llm between 0 and 2
              and relevance_codex between 0 and 2
              and (needs_review is null or needs_review = 0)
            order by total desc
            """
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return {"brand": brand, "skipped": False, "picked": 0, "appended": 0}

    gid = _second_tab_gid(sid)
    try:
        table = _read_export_csv(sid, gid)
    except Exception as exc:
        return {"brand": brand, "skipped": True, "reason": f"시트 읽기 실패: {exc}"}
    header = table[0][:15] if table else [
        "카페", "url", "발행시간", "작성자 아이디", "작성자 비밀번호", "발행 URL",
        "노출 상태", "키워드", "통합검색", "최종 편집 일시", "키워드 검색량",
        "노출된 검색량", "비고", "본문 분류", "1~5순위 진입",
    ]
    existing = {r[7].strip() for r in table[1:] if len(r) > 7 and r[7].strip()}

    out_rows: list[dict[str, Any]] = []
    for kw, total, rationale, rel_llm in rows:
        if kw in existing:
            continue
        d = {h: "" for h in header}
        d["노출 상태"] = "미확인"
        d["키워드"] = kw
        d["통합검색"] = _keyword_search_url(kw)
        d["키워드 검색량"] = f"{total:,}" if total else ""
        d["비고"] = rationale or ""
        d["본문 분류"] = RELEVANCE_LABELS.get(rel_llm, str(rel_llm))
        out_rows.append(d)

    if not out_rows:
        return {"brand": brand, "skipped": False, "picked": len(rows), "appended": 0, "reason": "이미 시트에 있음"}

    res = append_rows(sid, EXPOSURE_TAB_NAME, out_rows, header=header, gid=gid, repo_root=repo_root)
    return {
        "brand": brand,
        "skipped": False,
        "picked": len(rows),
        "appended": res.get("written", 0),
        "mode": res.get("mode"),
        **({"error": res["error"]} if res.get("error") else {}),
    }


def sync_keywords_all(
    repo_root: str | Path = ".",
    config_path: str = DEFAULT_BRANDS_CONFIG,
    keywords_dir: str = DEFAULT_KEYWORDS_DIR,
) -> dict[str, Any]:
    """명령 `시트 키워드 반영 전체` — 설정된 모든 브랜드에 대해 실행."""
    results = {}
    for brand in list_configured_brands(repo_root, config_path):
        db_path = Path(repo_root) / keywords_dir / f"{brand}.sqlite"
        if not db_path.exists():
            continue
        results[brand] = sync_keywords_to_sheet(
            brand, repo_root=repo_root, config_path=config_path, keywords_dir=keywords_dir
        )
    return results


# --------------------------------------------------------------------------
# 노출 순환 연동 훅 — keyword_exposure.py가 호출하는 지점 (호출부는 그 파일
# 담당 일꾼이 붙인다. 이 모듈에는 훅 함수만 둔다.)
# --------------------------------------------------------------------------


def apply_exposure(
    brand: str,
    rows: list[dict[str, Any]],
    *,
    totals: dict[str, Any] | None = None,
    repo_root: str | Path = ".",
    config_path: str = DEFAULT_BRANDS_CONFIG,
) -> dict[str, Any]:
    """노출 확인 결과를 브랜드 시트 두 번째 탭에 반영한다.

    `rows`는 각 항목이 최소 `keyword`(H열 매칭 키)를 담고, 나머지는
    다음 중 있는 것만 반영한다:
      - `status`      -> G(노출 상태)
      - `final_url`   -> I(통합검색/최종 검색어 URL)
      - `edited_at`   -> J(최종 편집 일시)
      - `exposed_total` -> L(노출된 검색량)
      - `rank`        -> O(1~5순위 진입)

    `totals`(선택)는 `{"P1": ..., "Q1": ...}` 형태로 시트 1행 합계 셀에 쓴다.

    호출부: `v2r/knowledge/keyword_exposure.py`(다른 일꾼 담당, 이 함수는
    아직 어디서도 호출되지 않는다) — 노출 확인 주기가 끝나고 브랜드별 결과를
    모은 다음 `sheets_writer.apply_exposure(brand, rows, totals=...)`를
    호출하도록 그쪽에서 연결해야 한다. 자세한 위치는 보고서 참고.
    """
    sid = get_spreadsheet_id(brand, repo_root, config_path)
    if not sid:
        return {"brand": brand, "skipped": True, "reason": "config/brands.yaml에 spreadsheet_id 없음"}
    gid = _second_tab_gid(sid)

    col_map = {
        "status": "G",
        "final_url": "I",
        "edited_at": "J",
        "exposed_total": "L",
        "rank": "O",
    }

    written = 0
    errors: list[str] = []
    for row in rows:
        kw = row.get("keyword")
        if not kw:
            continue
        updates = {col_map[k]: v for k, v in row.items() if k in col_map and v is not None}
        if not updates:
            continue
        res = update_by_key(sid, EXPOSURE_TAB_NAME, str(kw), updates, key_column="H", gid=gid, repo_root=repo_root)
        written += res.get("written", 0)
        if res.get("error"):
            errors.append(f"{kw}: {res['error']}")

    if totals:
        for cell, value in totals.items():
            res = set_cell(sid, EXPOSURE_TAB_NAME, cell, value, gid=gid, repo_root=repo_root)
            if not res.get("written"):
                errors.append(f"{cell}: {res.get('error', '실패')}")

    out = {"brand": brand, "written": written, "rows": len(rows)}
    if errors:
        out["error"] = "; ".join(errors)
    return out


__all__ = [
    "SheetsWriteError",
    "DEFAULT_TOKEN_PATH",
    "has_credentials",
    "update_rows",
    "append_rows",
    "update_by_key",
    "set_cell",
    "get_spreadsheet_id",
    "list_configured_brands",
    "sync_keywords_to_sheet",
    "sync_keywords_all",
    "apply_exposure",
]


def launch_chromium(pw, headless: bool = True):
    """헤드리스 크로미움을 띄운다. 기본 헤드리스 셸이 "Executable doesn't exist"로 실패하면
    (2026-09-23 실행기 숨김 실행에서 재현) 전체 크로미움(chromium-*/chrome.exe)으로 대신 띄운다."""
    import os, glob
    try:
        return pw.chromium.launch(headless=headless)
    except Exception as exc:  # noqa: BLE001
        if "Executable doesn't exist" not in str(exc):
            raise
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    cands = sorted(glob.glob(os.path.join(base, "chromium-*", "chrome-win64", "chrome.exe")), reverse=True)
    if not cands:
        raise RuntimeError(f"크로미움 실행 파일을 찾지 못함: {base}")
    return pw.chromium.launch(headless=headless, executable_path=cands[0], args=["--headless=new"] if headless else None)
