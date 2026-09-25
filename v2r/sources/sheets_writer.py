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
import re
import time
from pathlib import Path
from typing import Any

import httpx

from v2r.knowledge import keyword_relevance as _kr_mod
from v2r.sources.keyword_list import _norm

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

#: 2026-09-25 사고(실행기 재시작 중 5개 브랜드 시트 2탭 훼손) 재발 방지 가드.
#: A1(첫 열 헤더)이 이 문자열이 아니면 그 탭은 우리가 아는 "노출 현황" 탭이
#: 아니거나 이미 훼손된 상태이므로 어떤 쓰기도 하지 않는다.
_EXPECTED_A1 = "카페"
#: 한 번의 `시트 키워드 반영` 실행에서 붙일 수 있는 최대 행 수(브랜드 무관 상한).
MAX_ROWS_PER_SYNC = 1000
#: 시트 백업 디렉터리(동기화 시작 전 항상 전체 export를 여기 저장한다).
SHEET_BACKUP_DIR = "data/sheet_backups"


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


def _dismiss_modal(page: Any) -> str:
    """떠 있는 알림 모달(예: '지정한 범위가 시트 크기를 초과합니다')을 닫고 그 문구를 돌려준다."""
    try:
        dl = page.locator(".modal-dialog")
        for i in range(dl.count()):
            if dl.nth(i).is_visible():
                text = dl.nth(i).inner_text()[:120].replace("\n", " | ")
                dl.nth(i).locator("button, [role=button]").first.click()
                page.wait_for_timeout(300)
                return text
    except Exception:  # noqa: BLE001
        pass
    return ""


def _nav_to(page: Any, cell: str) -> str:
    """이름 상자로 `cell`에 이동하고, 이동 뒤 이름 상자 값을 돌려준다(모달이 뜨면 닫는다).

    2026-09-25 사고: 이름 상자에 초점이 없는 채로 타이핑이 시작되면(그리드가 다른
    동시 작업으로 바쁠 때 재현) 키 입력이 그대로 활성 셀에 리터럴 텍스트로
    들어간다(A1에 "A1265" 문자열이 박힌 사고). 타이핑 전 Escape로 어떤 편집
    상태도 확실히 비운 뒤에만 이름 상자를 클릭·입력한다.
    """
    page.keyboard.press("Escape")
    page.click("#t-name-box")
    page.keyboard.press("Control+A")
    page.keyboard.type(cell)
    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    if _dismiss_modal(page):
        return ""
    try:
        return str(page.input_value("#t-name-box") or "").strip().upper()
    except Exception:  # noqa: BLE001
        return ""


def _grid_row_count(page: Any, hi_hint: int = 1000) -> int:
    """격자의 마지막 행 번호를 이름 상자 이동 시도로 이진 탐색한다(모달이 뜨면 범위 밖).

    사고 2026-09-23: Ctrl+End 로 어림하다가(이름 상자에 초점이 있어 1행으로 읽힘) 팥순이
    탭 2~3024행에 빈 행 3,023개를 끼워 넣었다. 이제 어림하지 않고 정확히 잰다.
    """
    lo, hi = 1, max(2, int(hi_hint))
    while _nav_to(page, f"A{hi}") == f"A{hi}":
        lo = hi
        hi *= 2
        if hi > 5_000_000:
            return lo
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _nav_to(page, f"A{mid}") == f"A{mid}":
            lo = mid
        else:
            hi = mid
    return lo


def _ensure_grid_rows(page: Any, last_row: int, max_loops: int = 12) -> None:
    """시트 격자가 `last_row`행까지 없으면 마지막 행 아래에 행을 끼워 넣어 늘린다.

    익명 편집 화면에는 API가 없으므로 UI로 한다: 마지막 N행을 이름 상자로 선택 →
    Shift+F10 컨텍스트 메뉴 → "아래에 행 N개 삽입"(선택 행 수만큼 늘어난다).
    현재 격자 크기는 모르므로 Ctrl+End(데이터 끝)를 하한으로 잡고, 목표 셀 이동이
    모달 없이 성공할 때까지 반복한다. 이미 충분하면 아무것도 하지 않는다.
    """
    probe = f"A{last_row}"
    if _nav_to(page, probe) == probe:
        return
    current = _grid_row_count(page, last_row)
    for _ in range(max_loops):
        # 2026-09-25 사고 가드(i): `current`(격자 마지막 행이라고 믿는 값)이 실제로
        # 격자 끝인지 삽입 직전에 다시 확인한다 — A{current}는 되고 A{current+1}은
        # 안 되어야(모달) 진짜 끝이다. 동시 작업 등으로 이진 탐색이 실제보다 작은
        # 값에 잘못 수렴하면(2026-09-25 뉴더미스 865행부터 빈 행 1,465개 삽입 사고의
        # 원인으로 의심) 여기서 즉시 중단하고 절대 삽입하지 않는다.
        if _nav_to(page, f"A{current}") != f"A{current}":
            raise SheetsWriteError(f"행 늘리기 중단: 격자 끝 재확인 실패(A{current})")
        if _nav_to(page, f"A{current + 1}") == f"A{current + 1}":
            raise SheetsWriteError(
                f"행 늘리기 중단: {current}행이 격자 끝이 아님(A{current + 1} 이동이 성공함) — "
                "이진 탐색 결과를 신뢰할 수 없어 삽입하지 않음"
            )
        n = max(1, min(current, 1000, last_row - current))
        sel = f"{current - n + 1}:{current}"
        if _nav_to(page, sel) != sel.upper():
            raise SheetsWriteError(f"행 늘리기 실패: {sel} 선택이 안 됨")
        page.keyboard.press("Shift+F10")
        page.wait_for_timeout(700)
        items = page.locator("[role=menuitem]:visible")
        clicked = False
        for i in range(items.count()):
            text = items.nth(i).inner_text().strip()
            if "아래에" in text and "행" in text and "삽입" in text:
                items.nth(i).click()
                clicked = True
                break
        if not clicked:
            page.keyboard.press("Escape")
            raise SheetsWriteError("행 늘리기 실패: '아래에 행 N개 삽입' 메뉴가 없음")
        page.wait_for_timeout(1500)
        current += n
        log.info("시트 행 늘림: +%d행 → %d행", n, current)
        if current >= last_row and _nav_to(page, probe) == probe:
            return
    raise SheetsWriteError(f"행 늘리기 실패: {last_row}행까지 늘리지 못함(현재 {current})")


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

            # 붙여넣을 마지막 행이 시트 격자 밖이면 먼저 행을 늘린다 — 사고 2026-09-23:
            # 팥순이(3,654행)·코숨핏(449행) 탭은 격자가 데이터 크기로 잘려 있어 A3655/A450
            # 이동이 "지정한 범위가 시트 크기를 초과합니다" 모달로 막혔고, 그 상태로 붙여넣기가
            # A1(기본 선택)에 떨어져 헤더가 덮였다.
            col_letter, row1 = _parse_cell(cell)
            n_lines = tsv.count("\n") + 1 if tsv else 1
            _ensure_grid_rows(page, row1 + n_lines - 1)

            # 이름 상자에 셀 주소 입력 → Enter로 이동. 이동이 실제로 됐는지 이름 상자
            # 값을 다시 읽어 확인한다(이동이 씹히면 붙여넣기가 A1에 떨어진다).
            landed = False
            now_at = ""
            for _nav in range(3):
                now_at = _nav_to(page, cell)
                if now_at == cell.upper():
                    landed = True
                    break
                page.wait_for_timeout(600)
            if not landed:
                raise SheetsWriteError(f"셀 이동 실패({cell}): 이름 상자가 {now_at!r}에 머묾")

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


def copy_row_format(
    spreadsheet_id: str,
    gid: str | int,
    src_row: int,
    dst_first: int,
    dst_last: int,
    *,
    n_cols: int = 15,
) -> None:
    """`src_row`(서식·데이터 확인 드롭다운이 있는 기존 행, 보통 2행)의 서식을
    `dst_first`~`dst_last`행에 복사한다. 값은 절대 건드리지 않는다.

    헤드리스로: (a) 이름 상자로 `A{src}:{col}{src}` 선택 → Ctrl+C, (b) 대상 범위
    선택 → 컨텍스트 메뉴 "선택하여 붙여넣기" → "서식만 붙여넣기", (c) 다시 같은
    범위 선택 → "선택하여 붙여넣기" → "데이터 확인만 붙여넣기"(둘 다 있어야
    드롭다운 칩+배경색이 옮겨진다). "값 붙여넣기"류 메뉴는 절대 클릭하지 않는다.
    """
    from playwright.sync_api import sync_playwright

    last_col = _col_letter(n_cols - 1)
    src_range = f"A{src_row}:{last_col}{src_row}"
    dst_range = f"A{dst_first}:{last_col}{dst_last}"

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=True)
        try:
            # 실측(2026-09-24): 클립보드 권한을 안 주면 Ctrl+C가 조용히 실패해서(예외
            # 없음) "서식만"/"데이터 확인만" 메뉴 클릭은 정상 진행되고 메뉴도 닫히지만
            # 실제로 붙일 클립보드 내용이 없어 아무 것도 안 바뀐다(장으뜸 탭에서 재현—
            # 큰 범위건 한 행이건 산발적으로 실패). origin에 clipboard-read/write 권한을
            # 미리 부여한다.
            context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
            page = context.new_page()
            page.goto(EDIT_URL.format(sid=spreadsheet_id, gid=gid), wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector("#t-name-box", timeout=45000)
            page.wait_for_timeout(1000)

            if _nav_to(page, src_range) != src_range.upper():
                raise SheetsWriteError(f"서식 원본 선택 실패: {src_range}")
            page.keyboard.press("Control+C")
            page.wait_for_timeout(400)

            if _nav_to(page, dst_range) != dst_range.upper():
                raise SheetsWriteError(f"서식 대상 선택 실패: {dst_range}")

            # 실측(2026-09-24): 하위 메뉴 항목 표기는 "서식만 붙여넣기"가 아니라
            # "서식만"(단축키 Ctrl+Alt+V), "데이터 확인만"이다 — 긴 문구로 찾으면
            # 못 찾는다. 부분 문자열로 맞춘다.
            _paste_special(page, "서식만", fallback_keys="Control+Alt+V")
            page.wait_for_timeout(600)

            # 데이터 확인(드롭다운) 규칙은 서식 붙여넣기에 안 따라오므로 별도로 다시 한다.
            # 실측(2026-09-24): 첫 붙여넣기 뒤 "marching ants"(복사 표시)가 풀려서
            # 두 번째 선택하여 붙여넣기가 조용히 빈 클립보드에 대고 실행되는 경우가
            # 있었다(장으뜸 탭에서 재현) — 원본을 다시 Ctrl+C한다.
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            if _nav_to(page, src_range) != src_range.upper():
                raise SheetsWriteError(f"서식 원본 재선택 실패: {src_range}")
            page.keyboard.press("Control+C")
            page.wait_for_timeout(400)
            if _nav_to(page, dst_range) != dst_range.upper():
                raise SheetsWriteError(f"서식 대상 재선택 실패: {dst_range}")
            _paste_special(page, "데이터 확인만")
            page.wait_for_timeout(600)
        finally:
            browser.close()


def _paste_special(page: Any, menu_text: str, *, fallback_keys: str | None = None) -> None:
    """Shift+F10 컨텍스트 메뉴 → "선택하여 붙여넣기" 하위 메뉴 → `menu_text` 클릭.

    하위 메뉴 항목은 상위 메뉴를 hover/click 한 뒤에야 DOM에 나타나므로
    `[role=menuitem]:visible`을 두 번 조회한다(부모 클릭 전/후).

    실측(2026-09-24): 평범한 `.click()`은 가끔(특히 장으뜸 탭에서) 메뉴가 안 닫히고
    조용히 무시됐다(예외도 없이 서식·데이터 확인이 반영 안 됨) — `force=True`로
    다시 시도하고, 메뉴가 실제로 닫혔는지(`[role=menuitem]:visible` 개수가 메뉴바
    수준으로 줄었는지)까지 확인한 뒤에야 성공으로 본다.
    """
    page.keyboard.press("Shift+F10")
    page.wait_for_timeout(600)
    items = page.locator("[role=menuitem]:visible")
    baseline = items.count()  # 컨텍스트 메뉴 열기 전 상단 메뉴바 수(대조용)
    parent_clicked = False
    for i in range(items.count()):
        text = items.nth(i).inner_text().strip()
        if "선택하여 붙여넣기" in text:
            items.nth(i).click(force=True)
            page.wait_for_timeout(500)
            parent_clicked = True
            break
    if not parent_clicked:
        page.keyboard.press("Escape")
        if fallback_keys:
            page.keyboard.press(fallback_keys)
            return
        raise SheetsWriteError("'선택하여 붙여넣기' 메뉴를 찾지 못함")

    sub_items = page.locator("[role=menuitem]:visible")
    target_idx = None
    for i in range(sub_items.count()):
        text = sub_items.nth(i).inner_text().strip()
        if menu_text in text:
            target_idx = i
            break
    if target_idx is None:
        page.keyboard.press("Escape")
        if fallback_keys:
            page.keyboard.press(fallback_keys)
            return
        raise SheetsWriteError(f"'{menu_text}' 메뉴를 찾지 못함")

    for attempt in range(2):
        page.locator("[role=menuitem]:visible").nth(target_idx).click(force=True)
        page.wait_for_timeout(500)
        still_open = page.locator("[role=menuitem]:visible").count()
        if still_open <= baseline + 1:  # 메뉴가 닫혀 상단 메뉴바 수준으로 줄었으면 성공
            return
        page.wait_for_timeout(400)  # 첫 시도가 안 먹혔으면 한 번 더
    raise SheetsWriteError(f"'{menu_text}' 클릭이 반영되지 않음(메뉴가 안 닫힘)")


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


def _last_data_row(table: list[list[str]]) -> int:
    """값이 하나라도 있는 마지막 행 번호(1-based). 비어 있으면 0, 헤더만 있으면 1."""
    last = 0
    for i, row in enumerate(table, 1):
        if any(str(c).strip() for c in row):
            last = i
    return last


def _check_a1_ok(table: list[list[str]]) -> str:
    """A1이 `_EXPECTED_A1`("카페")이 아니면 빈 문자열이 아닌 오류 메시지를 돌려준다.

    2026-09-25 사고: 이름 상자 이동이 씹혀 A1에 셀 주소 문자열("A1265")이
    그대로 들어간 채로 다음 붙여넣기가 계속 진행됐다. 모든 쓰기 진입점은 쓰기
    전에 이걸 먼저 확인하고, 실패하면 아무 것도 쓰지 않고 즉시 중단한다.
    """
    if not table or not table[0]:
        return "시트가 비어 있음(A1 확인 불가)"
    a1 = str(table[0][0]).strip()
    if a1 != _EXPECTED_A1:
        return f"A1 훼손 의심 — 예상 {_EXPECTED_A1!r}, 실제 {a1!r}(이 탭은 쓰기 금지, 복구 필요)"
    return ""


def _backup_sheet_csv(
    brand: str, table: list[list[str]], *, repo_root: str | Path = ".", tag: str = "before_sync"
) -> Path:
    """전체 export CSV를 타임스탬프 파일로 저장한다(동기화 시작 전 항상 호출).

    2026-09-25 사고 이후 지시(v) — 되돌릴 수 있어야 하므로 쓰기 시작 전 백업은
    선택이 아니라 필수다.
    """
    from datetime import datetime

    out_dir = Path(repo_root) / SHEET_BACKUP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = out_dir / f"{brand}_노출현황_{tag}_{ts}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerows(table)
    return path


_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})")


def _norm_cell(value: Any) -> str:
    """검증용 정규화 — 시트가 숫자를 자동 서식(천 단위 콤마, 12345.0)해서 돌려주거나
    앞뒤 공백·개행 종류가 달라지는 것을 같은 값으로 본다 (사고 2026-09-23: P1/Q1 합계와
    검색량 열이 이 차이로 1,000회 넘게 "검증 불일치"로 찍혔다)."""
    s = str(value if value is not None else "").strip().replace("\r\n", "\n")
    m = _DATE_RE.match(s)
    if m:  # 시트가 날짜를 "2026-09-24 0:31:50"처럼 앞 0 없이 돌려준다 (2026-09-24)
        y, mo, d, h, mi, se = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d} {int(h):02d}:{mi}:{se}"
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
    # 2026-09-24: 공백·대소문자 차이로 같은 키워드가 새 행으로 붙던 사고(811개 중복)
    # 방지 — 키 대조는 항상 `_norm`(keyword_exposure._norm과 동일 규칙) 정규화로 한다.
    existing_keys: dict[str, int] = {}
    for i, row in enumerate(table):
        if i == 0:
            continue
        norm_key = _norm(row[key_idx]) if key_idx < len(row) else ""
        if norm_key:
            existing_keys.setdefault(norm_key, i + 1)  # 1-based data row, 첫 일치만

    written = 0
    to_append: list[list[str]] = []
    seen_new: set[str] = set()  # append 목록 안 정규화 중복 제거
    for row in rows:
        key = str(row.get(key_column, ""))
        norm_key = _norm(key)
        values = [str(row.get(h, "")) for h in hdr]
        if norm_key in existing_keys:
            # existing_keys[norm_key]는 이미 헤더를 포함한 1-based 시트 행 번호다
            # (예: 헤더=1행, 첫 데이터 행=2행) — 여기서 다시 +1 하면 한 행 밀려
            # 써지는 버그였다(2026-09-24 시험에서 발견, 정규화 중복 수정과 함께 고침).
            row1 = existing_keys[norm_key]
            res = _write_verified(spreadsheet_id, gid, _cell_ref(row1, "A"), [values], repo_root)
            if res.get("mode") == "sheets":
                written += 1
        elif norm_key and norm_key not in seen_new:
            seen_new.add(norm_key)
            to_append.append(values)
        elif not norm_key:
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
    copy_format: bool = True,
    format_src_row: int = 2,
) -> dict[str, Any]:
    """시트 끝(마지막 데이터 행 다음)에 행을 append한다.

    `rows`는 리스트의 리스트(값 순서 그대로) 또는 딕셔너리(헤더명 필요) 모두
    받는다. 200행 단위로 나눠 paste한다. `copy_format`(기본 True)이면 값을 쓴
    뒤 `format_src_row`(기본 2행 — 드롭다운이 확인된 기존 행)의 서식·데이터
    확인 규칙을 새로 추가된 행 범위에 복사한다(값은 그대로 유지).
    """
    try:
        table = _read_export_csv(spreadsheet_id, gid)
    except Exception as exc:
        return {"written": 0, "mode": "csv_only", "error": str(exc)}
    a1_err = _check_a1_ok(table)
    if a1_err:
        return {"written": 0, "mode": "csv_only", "error": a1_err}
    hdr = header or (table[0] if table else [])
    value_rows: list[list[str]] = []
    for row in rows:
        if isinstance(row, dict):
            value_rows.append([str(row.get(h, "")) for h in hdr])
        else:
            value_rows.append([str(v) for v in row])
    if not value_rows:
        return {"written": 0, "mode": "sheets"}

    # 2026-09-24 사고: export CSV는 서식·데이터 확인만 있는 빈 행도 돌려주므로 len(table)은
    # 격자 끝이지 데이터 끝이 아니다 — 우아덤 탭에서 4,947행 뒤 빈 행 8,979개를 건너뛰어
    # 13,926행부터 붙였다. 마지막 '값이 있는 행' 다음에 붙인다.
    start_row1 = _last_data_row(table) + 1
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
        if res.get("mode") != "sheets":
            # 2026-09-25 사고 가드(ii): 검증 불일치(붙인 행 수와 실제 반영이 다름)가
            # 나면 다음 chunk로 넘어가지 말고 즉시 멈춘다 — 이전에는 계속 진행해
            # 어긋난 자리 위에 chunk가 계속 쌓이며 훼손이 커졌다.
            errors.append(f"{cell} 이후 chunk 중단(검증 실패)")
            break
    out: dict[str, Any] = {"written": written, "mode": "sheets" if written == len(value_rows) else "csv_only"}
    if errors:
        out["error"] = "; ".join(errors)
    if copy_format and written and format_src_row < start_row1:
        try:
            copy_row_format(
                spreadsheet_id, gid, format_src_row, start_row1, start_row1 + len(value_rows) - 1,
                n_cols=max(len(hdr), 1) or 15,
            )
            out["format_copied"] = True
        except Exception as exc:  # noqa: BLE001 — 서식 실패는 값 쓰기 성공을 무효화하지 않는다
            out["format_error"] = str(exc)
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
    # 2026-09-24: 공백·대소문자 차이(예 "수면테이프"/"수면 테이프")를 같은 키로 보도록
    # `_norm` 정규화로 대조한다. 여러 행이 일치하면(정규화 중복이 이미 시트에 있는 경우)
    # 전부 갱신한다(한 행만 갱신하면 나머지는 계속 안 맞는 값으로 남는다).
    norm_target = _norm(key_value)
    row1s = [
        i + 1
        for i, row in enumerate(table)
        if key_col0 < len(row) and _norm(row[key_col0]) == norm_target and norm_target
    ]
    if not row1s:
        return {"written": 0, "mode": "csv_only", "error": f"키를 찾지 못함: {key_value}"}

    written = 0
    errors: list[str] = []
    for row1 in row1s:
        for col_letter, value in updates.items():
            cell = _cell_ref(row1, col_letter.upper())
            res = _write_verified(spreadsheet_id, gid, cell, [[str(value)]], repo_root)
            if res.get("mode") == "sheets":
                written += 1
            elif res.get("error"):
                errors.append(f"{cell}: {res['error']}")
    out = {
        "written": written,
        "mode": "sheets" if written == len(updates) * len(row1s) else "csv_only",
        "row": row1s[0],
        "rows": row1s,
    }
    if errors:
        out["error"] = "; ".join(errors)
    return out


def normalize_kst_timestamp(raw: str) -> str:
    """J(최종 편집 일시)에 쓸 시각을 항상 KST `YYYY-MM-DD HH:MM:SS`로 맞춘다.

    2026-09-23 사용자 지적 — 노출 순환(`keyword_exposure.cycle_tick`)이 DB의
    `checked_at`(ISO 8601, `now_iso()`가 만드는 `...+09:00` 꼴)을 그대로 J에
    써서 "2026-09-23T23:18:27+09:00" 같은 값이 들어갔다. `apply_exposure`가
    J를 쓸 때마다(어느 호출부에서 왔든) 이 함수를 거치게 해 형식을 고정한다.
    이미 `YYYY-MM-DD HH:MM:SS`(공백 구분) 형식이면 그대로 두고, ISO(`T`·시간대
    포함)면 KST로 변환해 같은 형식으로 바꾼다. 못 알아보는 형식은 원문 그대로
    (억지로 지우지 않는다).
    """
    from datetime import datetime

    from v2r.store.db import KST

    raw = str(raw or "").strip()
    if not raw:
        return raw
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}:\d{2}", raw):
        return raw  # 이미 원하는 형식(옛 데이터의 한 자리 시각도 그대로 둔다)
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return raw
    if dt.tzinfo is not None:
        dt = dt.astimezone(KST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def build_exposure_column_updates(
    *,
    status: str,
    checked_at_kst: str,
    cafe: str | None = None,
    volume: int | None = None,
    existing_i: str = "",
    integrated_search_url_fn: Any = None,
    keyword: str = "",
) -> dict[str, str]:
    """노출 검사 결과로 바꿀 열만(2026-09-23 사용자 최종 지시) — **A·G·J·K·L만**.

    B~F열(url·발행시간·작성자·비밀번호·발행URL)은 숨김 열이라 절대 건드리지
    않는다(읽지도 쓰지도 않음 — 특히 E 비밀번호는 이 함수도, 이 함수를 부르는
    쪽도 어디서도 다루지 않는다). H(키워드)도 불변. I(통합검색)는 시트에 이미
    값이 있으면(`existing_i`) 그대로 두고, 비어 있을 때만 채운다.

    - A(카페): `cafe`가 있을 때만(노출완으로 확정된 우리 글의 카페) — 밀려남
      (`cafe=None`)이면 이 열은 아예 updates에 안 넣는다(기존 값 유지).
    - G(노출 상태): 노출완/밀려남/미확인.
    - J(최종 편집 일시): 이번 검사 시각(KST).
    - K(키워드 검색량): `volume`이 있을 때만.
    - L(노출된 검색량): 노출완이면 K와 같은 값, 밀려남이면 0(미확인이면 안 건드림).
    """
    status_label = EXPOSURE_STATUS_LABEL.get(status, status)
    updates: dict[str, str] = {"G": status_label, "J": normalize_kst_timestamp(checked_at_kst)}
    if cafe:
        updates["A"] = cafe
    if not existing_i and integrated_search_url_fn:
        i_val = integrated_search_url_fn(keyword)
        if i_val:
            updates["I"] = i_val
    if volume is not None:
        updates["K"] = str(volume)
        if status_label == "노출완":
            updates["L"] = str(volume)
    # 밀려남이면 검색량을 몰라도 L은 0 (K는 모르면 건드리지 않음 — 2026-09-24)
    if status_label == "밀려남":
        updates["L"] = "0"
    return updates


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

#: G열(노출 상태) 표기 — 코드 -> 한국어(keyword_exposure.KOREAN_STATUS와 동일 값)
EXPOSURE_STATUS_LABEL = {"exposed": "노출완", "pushed": "밀려남", "unpublished": "미확인", "unknown": "미확인"}
#: 반대로 한국어 라벨이 그대로 들어와도 받아주기 위한 역매핑
KOREAN_STATUS_TO_CODE = {v: k for k, v in EXPOSURE_STATUS_LABEL.items()}

#: `relevance_llm` 값 -> 본문 분류 라벨(0=가장 직접적, 값이 커질수록 느슨해진다는
#: 기존 연관도 재산정 스케일 전제. `data/keywords/<브랜드>.sqlite`를 만드는
#: keyword_relevance.py 쪽 스케일이 바뀌면 이 매핑도 같이 바꿔야 한다.)
#: 2026-09-24: keyword_relevance.RELEVANCE_LABELS와 동일해야 하므로 거기서 가져온다
#: (0-3(무관 포함) → 0-4(당위성/무관 분리) 확장, 사용자 지시).
RELEVANCE_LABELS = _kr_mod.RELEVANCE_LABELS
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

    최종 원고 대상 판정은 `keyword_relevance.is_manuscript_target`로 한다(0에서 3,
    3=당위성 포함, 4=무관만 제외). 관련 열이 아예 없으면(아직 재산정 전) 건너뛴다.
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
    con.row_factory = sqlite3.Row
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(keywords)")}
        required = {"relevance_llm", "relevance_codex", "needs_review"}
        if not required.issubset(cols):
            return {"brand": brand, "skipped": True, "reason": f"미산정 열 없음: {required - cols}"}
        has_bridge = "bridge_rationale" in cols

        all_scored = con.execute(
            """
            select keyword, total, rationale, relevance_llm, relevance_codex, needs_review
            {bridge_col}
            from keywords
            where relevance_llm is not null
            order by total desc
            """.format(bridge_col=", bridge_rationale" if has_bridge else ", '' as bridge_rationale")
        ).fetchall()
        rows = [
            (r["keyword"], r["total"], r["rationale"], r["relevance_llm"], r["bridge_rationale"])
            for r in all_scored
            if _kr_mod.is_manuscript_target(r)
        ]
    finally:
        con.close()

    if not rows:
        return {"brand": brand, "skipped": False, "picked": 0, "appended": 0}

    gid = _second_tab_gid(sid)
    try:
        table = _read_export_csv(sid, gid)
    except Exception as exc:
        return {"brand": brand, "skipped": True, "reason": f"시트 읽기 실패: {exc}"}
    a1_err = _check_a1_ok(table)
    if a1_err:
        return {"brand": brand, "skipped": True, "reason": a1_err}
    # 2026-09-25 사고 가드(v): 쓰기 시작 전 전체 export를 항상 백업한다.
    _backup_sheet_csv(brand, table, repo_root=repo_root, tag="before_sync")
    header = table[0][:15] if table else [
        "카페", "url", "발행시간", "작성자 아이디", "작성자 비밀번호", "발행 URL",
        "노출 상태", "키워드", "통합검색", "최종 편집 일시", "키워드 검색량",
        "노출된 검색량", "비고", "본문 분류", "1~5순위 진입",
    ]
    # 2026-09-24: 공백·대소문자 차이로 같은 키워드가 중복 행으로 붙던 사고 방지 —
    # 이미 있는지 판정은 항상 `_norm` 정규화로 하고, append 목록 안에서도 정규화
    # 중복을 제거한다.
    existing = {_norm(r[7]) for r in table[1:] if len(r) > 7 and r[7].strip()}

    out_rows: list[dict[str, Any]] = []
    seen_new: set[str] = set()
    for kw, total, rationale, rel_llm, bridge_rationale in rows:
        norm_kw = _norm(kw)
        if not norm_kw or norm_kw in existing or norm_kw in seen_new:
            continue
        seen_new.add(norm_kw)
        d = {h: "" for h in header}
        d["노출 상태"] = "미확인"
        d["키워드"] = kw
        d["통합검색"] = _keyword_search_url(kw)
        d["키워드 검색량"] = f"{total:,}" if total else ""
        # M열 비고: 당위성(3)이면 연결 논리(bridge_rationale)를 우선 채우고,
        # 없으면 기존 rationale로 채운다(2026-09-24).
        d["비고"] = (bridge_rationale or "") if rel_llm == _kr_mod.RELEVANCE_BRIDGE else (rationale or "")
        d["본문 분류"] = RELEVANCE_LABELS.get(rel_llm, str(rel_llm))
        out_rows.append(d)

    if not out_rows:
        return {"brand": brand, "skipped": False, "picked": len(rows), "appended": 0, "reason": "이미 시트에 있음"}

    # 2026-09-25 사고 가드(vi): "확정분 - 시트 보유분"을 넘겨 붙이지 않는다(그날
    # 사고에서 확정분보다 훨씬 많은 행이 붙었다). 브랜드별 상한 = 이번에 새로
    # 붙일 후보 수(out_rows, 이미 정규화 중복 제거됨)이고, 그와 별개로 한 번의
    # 실행에서 MAX_ROWS_PER_SYNC(기본 1,000)행을 넘지 않는다.
    cap = min(len(out_rows), MAX_ROWS_PER_SYNC)
    capped = len(out_rows) > cap
    out_rows = out_rows[:cap]

    res = append_rows(sid, EXPOSURE_TAB_NAME, out_rows, header=header, gid=gid, repo_root=repo_root)
    out = {
        "brand": brand,
        "skipped": False,
        "picked": len(rows),
        "appended": res.get("written", 0),
        "mode": res.get("mode"),
        **({"error": res["error"]} if res.get("error") else {}),
    }
    if capped:
        out["capped_at"] = cap
    return out


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

    2026-09-23 사용자 최종 지시(스크린샷 확인 후 정정) — B~F열은 숨김 열이라
    **건드리지 않는다**(읽지도 로그에 남기지도 않는다 — 특히 E 비밀번호). 바꾸는
    열은 **A(우리 글이 확정된 카페, 노출완일 때만)·G(노출 상태)·J(검사 시각)·
    K(키워드 검색량)·L(노출된 검색량)** 뿐이고, I(통합검색)는 비어 있을 때만
    채운다. H(키워드)는 불변. `rows`의 각 항목: `keyword`(H열 매칭 키, 필수),
    `status`(exposed|pushed|unknown 또는 한국어 라벨), `edited_at`(이번 검사
    시각), `volume`(키워드 검색량), `cafe`(노출완으로 확정된 카페명 — 밀려남이면
    빈 채로 둬서 A를 안 바꾼다), `final_url`(I가 비어 있을 때 채울 통합검색 URL).

    쓰기는 `update_by_key`로 열마다 따로 쓴다(한 번에 A~L 전체를 붙여넣는
    대신) — B~F를 아예 읽지 않아도 되고(이름 상자로 A, G, J:K:L 몇 칸만
    옮겨 다니면 되니 왕복이 적다), E가 붙여넣기 버퍼에 실릴 일 자체가 없어
    더 안전하다(보고서 "시트 갱신 규칙" 절 비교 참고).

    `totals`(선택)는 `{"P1": ..., "Q1": ...}` 형태로 시트 1행 합계 셀에 쓴다.
    """
    sid = get_spreadsheet_id(brand, repo_root, config_path)
    if not sid:
        return {"brand": brand, "skipped": True, "reason": "config/brands.yaml에 spreadsheet_id 없음"}
    gid = _second_tab_gid(sid)

    try:
        table = _read_export_csv(sid, gid)
    except Exception as exc:
        return {"brand": brand, "written": 0, "rows": len(rows), "error": f"시트 읽기 실패: {exc}"}
    a1_err = _check_a1_ok(table)
    if a1_err:
        return {"brand": brand, "written": 0, "rows": len(rows), "error": a1_err}
    # I(통합검색, 인덱스 8)이 이미 있는지만 본다 — B~F(인덱스 1~5, E=비밀번호 포함)는
    # 이 딕셔너리에 담기지만 아래에서 절대 인덱스로 꺼내 쓰지 않는다(로그·기록 없음).
    by_keyword_i = {_norm(r[7]): (r[8] if len(r) > 8 else "") for r in table[1:] if len(r) > 7 and r[7]}

    written = 0
    errors: list[str] = []
    for row in rows:
        kw = row.get("keyword")
        if not kw:
            continue
        status = row.get("status", "")
        if status in KOREAN_STATUS_TO_CODE:
            status = KOREAN_STATUS_TO_CODE[status]
        updates = build_exposure_column_updates(
            status=status,
            checked_at_kst=row.get("edited_at", ""),
            cafe=row.get("cafe") or None,
            volume=row.get("volume"),
            existing_i=by_keyword_i.get(_norm(kw), ""),
            integrated_search_url_fn=lambda k: row.get("final_url") or "",
            keyword=str(kw),
        )
        res = update_by_key(sid, EXPOSURE_TAB_NAME, str(kw), updates, key_column="H", gid=gid, repo_root=repo_root)
        written += res.get("written", 0)
        if res.get("error"):
            errors.append(f"{kw}: {res['error']}")

    if totals:
        block = totals.get("block") if isinstance(totals, dict) else None
        if isinstance(block, dict) and block.get("rows"):
            # 이름 붙은 합계 표(P2:Q3 등)를 한 번에 쓴다 (사용자 지시 2026-09-23)
            res = _write_verified(sid, gid, str(block.get("cell") or "P2"), [[str(v) for v in r] for r in block["rows"]], repo_root)
            if res.get("mode") != "sheets":
                errors.append(f"{block.get('cell', 'P2')} 표: {res.get('error', '실패')}")
        for cell, value in totals.items():
            if cell == "block":
                continue
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
    "build_exposure_column_updates",
    "normalize_kst_timestamp",
    "EXPOSURE_STATUS_LABEL",
    "set_cell",
    "copy_row_format",
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
