"""브랜드 키워드 발굴 — 네이버 검색광고 키워드 도구(꼬리 물기 BFS).

설계: `docs/reports/keyword-program-plan-2026-09-22.md` A1~A3.

목표: 브랜드 씨앗 키워드(시트 `노출 현황` 탭 키워드 + 브랜드 논리 낱말)를
네이버 검색광고 키워드 도구(https://ads.naver.com/manage/ad-accounts/<계정>/sa/tool/keyword-planner)
에 넣어 연관 키워드 + 월간 검색량(PC/모바일)을 받고, 새로 나온 키워드를 다시
씨앗으로 넣는 꼬리 물기(BFS)로 브랜드당 최대 `DEFAULT_TARGET`(10,000)개까지 모은다.

로그인: `v2r/warehouse/naver_session.py`의 영속 프로필을 그대로 연다.
**절대 로그아웃·재로그인하지 않는다** — 세션이 풀려 있으면 조회 없이 멈추고 보고한다.

저장: `data/keywords/<브랜드>.sqlite` (표 `keywords`: keyword PK, pc, mobile,
total, source_seed, depth, relevance, collected_at). 연관도(`relevance`)는
0(직접, 브랜드 논리 낱말과 겹침)~3(BFS 깊이가 먼 간접 키워드).

이 모듈은 브라우저 자동화(`fetch_related_keywords`)와 순수 로직(응답 파싱,
BFS, 연관도 계산, 시트/가이드 씨앗 추출)을 분리한다 — 순수 로직은 브라우저
없이 테스트한다.
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: 브랜드당 목표 키워드 수
DEFAULT_TARGET = 10_000
#: BFS 한 단계에서 더 파고들 최대 깊이(너무 멀어지면 연관도가 낮아 의미가 옅다)
DEFAULT_MAX_DEPTH = 6
#: 한 세션(브라우저 열려있는 동안) 조회 상한 — 이후 휴식
DEFAULT_SESSION_CAP = 200
#: 세션 상한 도달 시 쉬는 시간(초)
DEFAULT_REST_SEC = 15 * 60
#: 조회 사이 대기(초) — 네이버 차단 방지
MIN_DELAY_SEC = 3.0
MAX_DELAY_SEC = 6.0
#: 한 번 조회에 넣을 수 있는 씨앗 개수(실측 2026-09-23: 최대 5개, 줄바꿈 구분)
SEED_BATCH_SIZE = 5

#: 네이버 검색광고 키워드 도구 페이지 (계정 번호는 브랜드마다 다를 수 있어 인자로 받는다)
KEYWORD_PLANNER_URL = (
    "https://ads.naver.com/manage/ad-accounts/{account_id}/sa/tool/keyword-planner"
)
#: 씨앗 입력창 placeholder(실측 2026-09-23)
SEED_TEXTAREA_PLACEHOLDER_HINT = "한줄에 하나씩"
#: 조회 버튼 정확한 텍스트(실측 2026-09-23) — 이 텍스트로만 정확히 찾는다.
#: 페이지에 "광고 만들기"·"전체추가"·"바로추가"·"월간 예상 실적 보기" 등 광고
#: 계정에 영향을 줄 수 있는 버튼이 함께 있으니 **절대 건드리지 않는다.**
SEARCH_BUTTON_TEXT = "조회하기"
#: 결과 표 전체를 받는 다운로드 버튼 텍스트 조각(표를 긁는 대신 이걸 받아 파싱)
DOWNLOAD_BUTTON_TEXT_HINT = "전체 다운로드"
#: 절대 클릭하지 않는 버튼들(광고 계정에 영향)
FORBIDDEN_BUTTON_TEXTS = ("광고 만들기", "전체추가", "바로추가", "월간 예상 실적 보기")

#: "< 10" 같은 표기 → 대략값(5)로. 네이버 키워드 도구는 10 미만을 이렇게 감춘다.
_RE_UNDER_TEN = re.compile(r"^\s*[<＜]\s*10\s*$")


# --- 순수 로직: 응답 파싱 ----------------------------------------------------
def _to_count(value: Any) -> int:
    """월간 검색량 값을 정수로. `< 10`은 5, 빈 값/오류는 0."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    s = str(value).strip().replace(",", "")
    if not s:
        return 0
    if _RE_UNDER_TEN.match(s):
        return 5
    try:
        return max(0, int(float(s)))
    except ValueError:
        return 0


@dataclass
class KeywordRow:
    keyword: str
    pc: int
    mobile: int
    comp_idx: str = ""

    @property
    def total(self) -> int:
        return self.pc + self.mobile


def parse_keyword_response(data: dict[str, Any]) -> list[KeywordRow]:
    """네이버 키워드 도구 XHR JSON(`keywordstool` API 모양) → `KeywordRow` 목록.

    실제 응답은 `{"keywordList": [{"relKeyword": "...", "monthlyPcQcCnt": "...",
    "monthlyMobileQcCnt": "...", "compIdx": "..."}, ...]}` 꼴(네이버 검색광고
    공식 API 필드명). 필드가 없거나 형태가 다르면 그 행만 건너뛴다.
    """
    if not isinstance(data, dict):
        return []
    items = data.get("keywordList")
    if not isinstance(items, list):
        return []
    out: list[KeywordRow] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kw = str(item.get("relKeyword") or "").strip()
        if not kw:
            continue
        pc = _to_count(item.get("monthlyPcQcCnt"))
        mobile = _to_count(item.get("monthlyMobileQcCnt"))
        comp = str(item.get("compIdx") or "").strip()
        out.append(KeywordRow(keyword=kw, pc=pc, mobile=mobile, comp_idx=comp))
    return out


#: 다운로드 xlsx 헤더는 2행(1행: 큰 분류, 2행: PC/모바일 세부) — 데이터는 3행부터.
#: A=연관키워드, B=월간검색수(PC), C=월간검색수(모바일), H=경쟁정도(있으면).
_DOWNLOAD_HEADER_ROWS = 2


def parse_keyword_download_rows(rows: list[tuple[Any, ...]]) -> list[KeywordRow]:
    """`전체 다운로드` xlsx를 openpyxl로 읽은 행(튜플) 목록 → `KeywordRow` 목록.

    실측(2026-09-23): 1~2행은 헤더(연관키워드 / 월간검색수 PC·모바일 / ...),
    3행부터 데이터. 검색수는 "3,250" 같은 천단위 콤마 문자열.
    """
    out: list[KeywordRow] = []
    for row in rows[_DOWNLOAD_HEADER_ROWS:]:
        if not row or not row[0]:
            continue
        kw = str(row[0]).strip()
        if not kw:
            continue
        pc = _to_count(row[1] if len(row) > 1 else None)
        mobile = _to_count(row[2] if len(row) > 2 else None)
        comp = str(row[7]).strip() if len(row) > 7 and row[7] else ""
        out.append(KeywordRow(keyword=kw, pc=pc, mobile=mobile, comp_idx=comp))
    return out


def parse_keyword_download_file(path: str | Path) -> list[KeywordRow]:
    """`전체 다운로드` xlsx 파일 경로 → `KeywordRow` 목록."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    return parse_keyword_download_rows(rows)


# --- 씨앗 키워드 -------------------------------------------------------------
def seeds_from_sheet(
    brand: str, cfg: dict | None = None, xlsx_path: str | Path | None = None
) -> list[str]:
    """시트 `노출 현황` 탭 H열(키워드) — 기존 키워드를 씨앗으로."""
    from v2r.sources.keyword_list import (
        EXPOSURE_SHEET,
        _brand_spreadsheet_id,
        _drop_password_columns,
        _KEYWORD_HEADERS,
        _pick,
        rows_from_xlsx,
    )
    from v2r.sources.sheets import SourceError, fetch_csv, gviz_csv_url

    if xlsx_path:
        rows = rows_from_xlsx(xlsx_path)
    else:
        sid = _brand_spreadsheet_id(brand, cfg)
        if not sid:
            return []
        try:
            rows = _drop_password_columns(fetch_csv(gviz_csv_url(sid, sheet=EXPOSURE_SHEET, headers=0)))
        except SourceError:
            return []
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        kw = _pick(row, _KEYWORD_HEADERS).strip()
        if kw and kw not in seen:
            seen.add(kw)
            out.append(kw)
    return out


#: 정리본 md에서 핵심어를 뽑는 자리 — "브랜드/제품:" 줄, "결핍: A, B, C" 나열,
#: "네이버 카페에 A, B, C 결핍을 가진 타겟", "목록 상세(A, B, C)" 꼴을 모두 본다.
_RE_GUIDE_PRODUCT_LINE = re.compile(r"^-\s*브랜드/제품\s*:\s*(.+)$", re.M)
#: "결핍: 색소침착, 미백, 착색 등" — 콜론 뒤 나열
_RE_DEFICIT_COLON = re.compile(r"결핍\s*[:：]\s*([^\n]+)")
#: "네이버 카페에 착색, 미백, 색소침착 결핍을 가진 타겟" — "카페에(서)"와 "결핍" 사이
_RE_DEFICIT_BEFORE = re.compile(r"카페에(?:서)?\s*(.+?)\s*결핍")
#: "타겟 결핍 목록 상세(기미, 흑자, 검버섯, ...)" — "목록" 옆 괄호 안 나열만(이모티콘
#: 괄호처럼 "결핍"이라는 낱말이 같은 줄 앞쪽에만 있는 무관한 괄호는 거른다)
_RE_PAREN_LIST = re.compile(r"목록\s*상세[^(\n]*\(([^)]+)\)")
_RE_SPLIT = re.compile(r"[,·/、]+")
#: 나열 항목 끝의 "등"·순번("2.")·조사 찌꺼기 제거
_RE_TRAILING_ETC = re.compile(r"\s*등\s*$")
_RE_LEADING_NUM = re.compile(r"^\s*\d+[.)]\s*")
#: 낱말 양끝 따옴표·괄호 찌꺼기
_RE_STRIP_PUNCT = re.compile(r'^[\s"\'“”‘’(){}\[\]]+|[\s"\'“”‘’(){}\[\]]+$')


def _clean_word_list(raw: list[str]) -> list[str]:
    out: list[str] = []
    for w in raw:
        w = _RE_LEADING_NUM.sub("", str(w or "")).strip()
        w = _RE_TRAILING_ETC.sub("", w).strip()
        w = _RE_STRIP_PUNCT.sub("", w).strip()
        if w and 1 < len(w) <= 12 and not w.isdigit() and not re.fullmatch(r"[ㄱ-ㅎㅏ-ㅣ]+", w):
            out.append(w)
    return out


def extract_guide_keywords(brand: str, guides_dir: str | Path | None = None) -> list[str]:
    """`warehouse/guides/정리본/<브랜드>.md`에서 브랜드 논리 낱말을 뽑는다.

    "결핍: A, B, C" · "카페에 A, B, C 결핍을 가진 타겟" · "목록 상세(A, B, C)"
    세 형태를 모두 찾아 합친다(하나만 있어도 되고, 여러 개면 다 모은다 — 논리
    낱말이 많을수록 `compute_relevance`의 0~3 분포가 고르게 나온다). 파일이
    없거나 아무 패턴도 안 맞으면 `config/brands.yaml`의 `description`을 대신
    쓴다(둘 다 없으면 빈 목록).
    """
    base = Path(guides_dir) if guides_dir else Path("warehouse") / "guides" / "정리본"
    path = base / f"{brand}.md"
    words: list[str] = []
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="ignore")
        m = _RE_GUIDE_PRODUCT_LINE.search(text)
        if m:
            words.extend(_RE_SPLIT.split(m.group(1)))
        for pattern in (_RE_DEFICIT_COLON, _RE_DEFICIT_BEFORE, _RE_PAREN_LIST):
            for m in pattern.finditer(text):
                chunk = m.group(1)
                # 맞춤법/AI-티 제거 규칙 같은 무관한 줄(화살표·물결·과도하게 긴 나열)은 거른다
                if len(chunk) > 100 or "→" in chunk or "~" in chunk:
                    continue
                words.extend(_RE_SPLIT.split(chunk))
    words = _clean_word_list(words)
    if not words:
        try:
            import yaml

            cfg = yaml.safe_load(Path("config/brands.yaml").read_text(encoding="utf-8"))
            desc = ((cfg or {}).get("brands") or {}).get(brand, {}).get("description", "")
            words = _clean_word_list(re.split(r"[·,\-—]", desc))
        except Exception:  # noqa: BLE001
            pass
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


# --- 연관도 -------------------------------------------------------------
def compute_relevance(keyword: str, guide_keywords: list[str], depth: int) -> int:
    """연관도 0(직접)~3(멀음).

    실측(2026-09-23, 우아덤 1,000개): BFS가 한두 단계만에 목표에 도달하면 거의
    모든 키워드의 깊이가 같아져(예: 전부 1) 깊이만으로는 0~3이 고르게 안
    나온다. 그래서 텍스트 근접도를 한 단계 더 본다:

    0 = 브랜드 논리 낱말이 키워드에 그대로 들어있음(또는 반대로 포함됨) — 직접.
    1 = 논리 낱말과 2글자 이상 겹치는 부분(부분 일치)이 있음 — 꽤 가까움.
    2~3 = 텍스트로는 안 겹치고 BFS 깊이만 남음 — 깊이를 2~3으로 잘라 쓴다
    (깊이 0~1도 텍스트가 안 겹치면 최소 2로 본다 — 논리 낱말과 전혀 안
    겹치면 아무리 얕아도 "직접"은 아니라서).
    """
    kw = str(keyword or "")
    guides = [str(g or "").strip() for g in (guide_keywords or []) if str(g or "").strip()]
    for g in guides:
        if g in kw or kw in g:
            return 0
    for g in guides:
        for i in range(len(g) - 1):
            if g[i : i + 2] in kw:
                return 1
    return max(2, min(int(depth) + 1, 3))


#: "전체 다운로드"가 헤드리스에서 가끔 실패(다운로드 이벤트를 못 받음)하는 것으로
#: 실측됐다 — 같은 세션(같은 조회 결과)에서 이만큼 재시도한 뒤에도 안 되면 표를
#: DOM에서 직접 긁는 대안으로 넘어간다.
DOWNLOAD_RETRY_COUNT = 3
#: DOM에서 표를 긁을 때 볼 행 선택자 힌트(결과 표의 데이터 행). 실측(2026-09-23):
#: 결과 표는 `role=row`를 쓰는 그리드이고, 헤더 행 2개를 제외한 나머지가 데이터.
TABLE_ROW_SELECTOR = "[role='row']"


def _scrape_table_rows(page: Any) -> list[KeywordRow]:
    """`전체 다운로드`가 계속 실패할 때 결과 표를 DOM에서 직접 긁는 대안.

    각 행의 셀 텍스트를 순서대로 읽어 `parse_keyword_download_rows`와 같은
    열 배치(A=연관키워드, B=PC, C=모바일, ... H=경쟁정도)로 맞춘다.
    """
    rows_locator = page.locator(TABLE_ROW_SELECTOR)
    n = rows_locator.count()
    raw_rows: list[tuple[Any, ...]] = []
    for i in range(n):
        row = rows_locator.nth(i)
        cells = row.locator("[role='cell'], [role='gridcell'], td, th")
        cnt = cells.count()
        if cnt == 0:
            continue
        texts = [cells.nth(j).inner_text().strip() for j in range(cnt)]
        raw_rows.append(tuple(texts))
    # 헤더로 보이는 행(첫 칸이 "연관키워드" 등 숫자가 아닌 라벨) 앞부분을 건너뛴다.
    data_rows = [r for r in raw_rows if r and r[0] and not re.match(r"^(연관\s*키워드|번호|No\.?)$", r[0].strip())]
    return parse_keyword_download_rows([("", ""), *data_rows]) if False else parse_keyword_download_rows(
        [("_header1",), ("_header2",), *data_rows]
    )


# --- 브라우저 자동화 ------------------------------------------------------
#: 조회 XHR 응답을 기다리는 최대 시간(초) — 이 안에 `keywordList`가 든 JSON
#: 응답이 안 잡히면 DOM 표 긁기로 넘어간다.
RESPONSE_CAPTURE_TIMEOUT_SEC = 30.0


def _make_response_capture(page: Any) -> tuple[Callable[[Any], None], dict[str, Any]]:
    """`keywordList`가 든 JSON 응답을 가로채는 `page.on("response", ...)` 핸들러.

    지시(2026-09-23 01:22, 사용자 절대 규칙): 다운로드 버튼을 클릭하면 헤드리스
    에서 브라우저가 통째로 닫히는 버그가 재현됐다 — 다운로드 자체를 쓰지 않고
    조회 결과 XHR JSON을 직접 가로챈다. 정확한 엔드포인트 URL을 몰라도(문서화된
    바 없음) 응답 바디 모양(`{"keywordList": [...]}`, 네이버 검색광고 공식 API
    필드명)만 보고 판정한다 — 페이지의 다른 XHR과 안 헷갈린다.
    """
    captured: dict[str, Any] = {}

    def _on_response(response: Any) -> None:
        if "data" in captured:
            return
        try:
            ctype = (response.headers or {}).get("content-type", "")
        except Exception:  # noqa: BLE001
            ctype = ""
        if "json" not in ctype:
            return
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            return
        if isinstance(body, dict) and isinstance(body.get("keywordList"), list):
            captured["data"] = body

    return _on_response, captured


def fetch_related_keywords(
    page: Any,
    seeds: list[str],
    account_id: str,
    download_dir: str | Path | None = None,
    timeout_ms: int = 30000,
    download_retries: int = DOWNLOAD_RETRY_COUNT,
    download_click_timeout_ms: int = 30000,
    download_wait_timeout_ms: int = 30000,
    response_capture_timeout_sec: float = RESPONSE_CAPTURE_TIMEOUT_SEC,
    use_download_fallback: bool = False,
) -> list[KeywordRow]:
    """씨앗 키워드 최대 `SEED_BATCH_SIZE`(5)개를 한 번에 조회해 연관 키워드를 받는다.

    실측(2026-09-23, 사람이 직접 페이지를 열어 확인): 씨앗 입력창은 placeholder
    `"한줄에 하나씩 입력하세요.\\n(최대 5개까지)"`(줄바꿈으로 씨앗 구분, 최대 5개),
    조회 버튼은 정확히 `"조회하기"`.

    **2026-09-23 01:22 사용자 절대 규칙**: `"전체 다운로드"` 버튼을 클릭하면
    헤드리스에서 다운로드 저장 직전에 브라우저가 통째로 닫히는 버그가 병렬
    실행에서도 재현됐다(장으뜸 10회째). 그래서 기본 경로는 다운로드를 아예
    쓰지 않는다: `조회하기`를 누르기 전에 `page.on("response", ...)`를 걸어
    조회 XHR JSON 응답(`keywordList`가 든 응답)을 가로채고, 그게 안 잡히면
    결과 표를 DOM에서 직접 긁는다(`_scrape_table_rows`). `use_download_fallback`
    (기본 False)을 켜야만 마지막 수단으로 다운로드를 시도한다.

    **`FORBIDDEN_BUTTON_TEXTS`(광고 만들기·전체추가·바로추가·월간 예상 실적
    보기)는 절대 클릭하지 않는다** — 광고 계정 설정에 영향을 줄 수 있다.
    """
    if len(seeds) > SEED_BATCH_SIZE:
        raise ValueError(f"한 번에 최대 {SEED_BATCH_SIZE}개까지만 조회할 수 있습니다")

    url = KEYWORD_PLANNER_URL.format(account_id=account_id)
    if account_id not in (page.url or ""):
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

    ta = page.locator(f'textarea[placeholder*="{SEED_TEXTAREA_PLACEHOLDER_HINT}"]').first
    if ta.count() == 0:
        raise RuntimeError("씨앗 입력창을 찾지 못했습니다(페이지 구조 확인 필요)")
    ta.click()
    ta.fill("\n".join(str(s).strip() for s in seeds if str(s or "").strip()))
    page.wait_for_timeout(500)

    btn = page.get_by_text(SEARCH_BUTTON_TEXT, exact=True).first
    if btn.count() == 0:
        raise RuntimeError("'조회하기' 버튼을 찾지 못했습니다(페이지 구조 확인 필요)")
    if btn.get_attribute("disabled") is not None:
        raise RuntimeError("'조회하기' 버튼이 비활성 상태입니다(씨앗 입력 확인 필요)")

    on_response, captured = _make_response_capture(page)
    page.on("response", on_response)
    try:
        btn.click(timeout=10000)
        deadline = time.monotonic() + max(float(response_capture_timeout_sec), 0.0)
        while "data" not in captured and time.monotonic() < deadline:
            page.wait_for_timeout(200)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:  # noqa: BLE001
            pass

    if "data" in captured:
        rows = parse_keyword_response(captured["data"])
        if rows:
            return rows
        # keywordList가 있었지만 빈 배열 — 정말 결과가 없다는 뜻이라 그대로 반환

    # 응답 가로채기 실패(구조가 다르거나 늦음) — 결과 표를 DOM에서 직접 긁는다.
    page.wait_for_timeout(max(int(timeout_ms) - int(response_capture_timeout_sec * 1000), 0))
    try:
        rows = _scrape_table_rows(page)
        if rows:
            return rows
    except Exception as exc:  # noqa: BLE001
        log.warning("DOM 표 긁기 실패: %s", exc)

    if not use_download_fallback:
        return []

    # 마지막 수단(기본 꺼짐) — 다운로드는 헤드리스에서 브라우저를 닫히게 하는
    # 것으로 실측됐으니 offscreen/headful 호출에서만 켜서 쓴다.
    dl_btn = page.get_by_text(DOWNLOAD_BUTTON_TEXT_HINT, exact=False).first
    waited = 0
    poll = 1000
    while dl_btn.count() == 0 and waited < download_wait_timeout_ms:
        page.wait_for_timeout(poll)
        waited += poll
        dl_btn = page.get_by_text(DOWNLOAD_BUTTON_TEXT_HINT, exact=False).first
    if dl_btn.count() == 0:
        raise RuntimeError("'전체 다운로드' 버튼을 찾지 못했습니다(결과가 없거나 페이지 구조 변경)")

    tmp_dir = Path(download_dir) if download_dir else Path.cwd() / "data" / "keywords" / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    last_exc: Exception | None = None
    for attempt in range(1, max(int(download_retries), 1) + 1):
        tmp_path = tmp_dir / f"dl_{int(time.time() * 1000)}_{attempt}.xlsx"
        try:
            with page.expect_download(timeout=download_click_timeout_ms) as dl_info:
                dl_btn.click(timeout=download_click_timeout_ms)
            download = dl_info.value
            download.save_as(str(tmp_path))
            try:
                return parse_keyword_download_file(tmp_path)
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001 - 재시도
            last_exc = exc
            log.warning("전체 다운로드 실패(시도 %d/%d): %s", attempt, download_retries, exc)
            page.wait_for_timeout(1000)

    log.warning("전체 다운로드 %d회 모두 실패 — 표를 DOM에서 직접 긁습니다: %s", download_retries, last_exc)
    return _scrape_table_rows(page)


#: 실측(2026-09-23): 헤드리스(`headless=True`)로 이 페이지에서 "전체 다운로드"를
#: 받으면 다운로드 저장 직전에 브라우저가 통째로 닫히는 문제가 재현됐다(같은
#: 프로필을 화면에 띄운 상태로는 최소 1회 조회가 성공했다). 원인이 100% 확정될
#: 때까지는 화면 밖으로 창을 옮겨 띄우는 `offscreen` 모드를 기본으로 쓴다 —
#: 실제 화면에는 안 보이면서도 진짜 창이 있는 헤드풀 모드라 다운로드가 이전에
#: 성공했던 경로와 같다.
OFFSCREEN_ARGS = ["--window-position=-32000,-32000", "--window-size=1280,900"]


def open_keyword_tool_page(
    profile_dir: str | Path | None = None, headless: bool = False, offscreen: bool = True
):
    """네이버 로그인 프로필로 키워드 도구 창을 연다. 로그아웃/재로그인 절대 금지.

    `headless=True`면 완전 헤드리스로(다운로드가 불안정한 것으로 실측됐으니
    되도록 `offscreen`을 쓴다). `headless=False`고 `offscreen=True`(기본)면
    실제 창을 화면 밖(`OFFSCREEN_ARGS`)에 최소화해 띄운다 — 사람 눈에는
    안 보이지만 헤드리스보다 다운로드가 안정적이다.

    반환: `(playwright, context, page, logged_in)`. `logged_in`이 False면
    조회 없이 즉시 닫고 호출 쪽에서 중단해야 한다(로그인 자동 처리 금지).
    """
    from playwright.sync_api import sync_playwright

    from v2r.warehouse import naver_session

    path = Path(profile_dir) if profile_dir else naver_session.default_profile_dir()
    path.mkdir(parents=True, exist_ok=True)
    user_agent = naver_session.saved_user_agent(path)

    if headless:
        playwright, context, page = naver_session._launch(
            path, headless=True, user_agent=user_agent, purpose="keyword_discovery"
        )
    else:
        lock = naver_session.acquire_profile_lock("keyword_discovery", profile_dir=path)
        playwright = sync_playwright().start()
        kwargs: dict[str, Any] = dict(
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="ko-KR",
        )
        if user_agent:
            kwargs["user_agent"] = user_agent
        if offscreen:
            kwargs["args"] = OFFSCREEN_ARGS
        context = None
        last_exc: Exception | None = None
        for channel in ("chrome", "msedge", None):
            try:
                kw = dict(kwargs)
                if channel:
                    kw["channel"] = channel
                context = playwright.chromium.launch_persistent_context(str(path), **kw)
                break
            except Exception as exc:  # noqa: BLE001 - 다음 채널 시도
                last_exc = exc
        if context is None:
            playwright.stop()
            lock.release()
            raise RuntimeError(f"브라우저를 열 수 없습니다: {last_exc}")
        page = context.pages[0] if context.pages else context.new_page()
        try:
            context._v2r_profile_lock = lock  # type: ignore[attr-defined]
        except Exception:
            pass

    logged_in = naver_session.has_login_cookies(naver_session._cookies(context))
    return playwright, context, page, logged_in


# --- 저장소 ----------------------------------------------------------------
def _store():
    from v2r.store import keyword_discovery_store as store

    return store


# --- BFS 꼬리 물기 ---------------------------------------------------------
@dataclass
class DiscoveryStats:
    collected: int = 0
    queries: int = 0
    stopped_reason: str = ""


def discover(
    brand: str,
    seeds: list[str],
    guide_keywords: list[str],
    fetch_fn: Callable[[list[str], int], list[KeywordRow]],
    db_path: str | Path,
    target: int = DEFAULT_TARGET,
    max_depth: int = DEFAULT_MAX_DEPTH,
    session_cap: int = DEFAULT_SESSION_CAP,
    batch_size: int = SEED_BATCH_SIZE,
    delay_range: tuple[float, float] = (MIN_DELAY_SEC, MAX_DELAY_SEC),
    sleep_fn: Callable[[float], None] = time.sleep,
    rest_fn: Callable[[float], None] | None = None,
    rest_sec: float = DEFAULT_REST_SEC,
    on_progress: Callable[[DiscoveryStats], None] | None = None,
) -> DiscoveryStats:
    """씨앗 키워드로 꼬리 물기 BFS. 순수 로직 — 한 번에 최대 `batch_size`개 씨앗을

    묶어 `fetch_fn(keywords, depth) -> list[KeywordRow]`를 부른다(네이버 키워드
    도구가 한 조회당 최대 5개 씨앗을 받는다, 실측 2026-09-23). 같은 깊이인
    항목끼리만 묶는다(BFS 단계가 섞이지 않게). 새로 찾은 키워드의 `source_seed`는
    그 조회에 쓴 씨앗들을 콤마로 이어 적는다(어느 씨앗에서 왔는지 응답이 구분해
    주지 않는다).

    `fetch_fn`이 예외를 던지면(차단 등) 그 자리에서 멈추고 이유를 남긴다.
    한 세션에서 `session_cap`번 조회하면 `rest_fn`(주면)으로 쉬고 계속한다
    (테스트에서는 `rest_fn=None`이면 즉시 멈춘다 — 실제 실행기만 휴식을 건다).
    """
    store = _store()
    conn = store.open_db(db_path)
    stats = DiscoveryStats()
    try:
        existing = store.count(conn)
        stats.collected = existing
        seen: set[str] = set(store.all_keywords(conn))
        queue: list[tuple[str, int]] = []  # (keyword, depth)
        for s in seeds:
            s = str(s or "").strip()
            if s and s not in seen:
                queue.append((s, 0))
                seen.add(s)

        session_count = 0
        while queue and stats.collected < target:
            depth = queue[0][1]
            batch: list[str] = []
            while queue and len(batch) < batch_size and queue[0][1] == depth:
                batch.append(queue.pop(0)[0])
            source_seed = ",".join(batch)

            if session_count >= session_cap:
                if rest_fn is None:
                    stats.stopped_reason = "세션 조회 상한 도달(휴식 없음 — 테스트/일회성 호출)"
                    break
                rest_fn(rest_sec)
                session_count = 0
            try:
                related = fetch_fn(batch, depth)
            except Exception as exc:  # noqa: BLE001
                stats.stopped_reason = f"조회 실패(차단 가능성): {exc}"
                log.warning("키워드 도구 조회 중단(%s, 씨앗=%s): %s", brand, batch, exc)
                break
            stats.queries += 1
            session_count += 1

            rows_to_save = []
            for kr in related:
                if kr.keyword in seen:
                    continue
                seen.add(kr.keyword)
                relevance = compute_relevance(kr.keyword, guide_keywords, depth + 1)
                rows_to_save.append(
                    {
                        "keyword": kr.keyword,
                        "pc": kr.pc,
                        "mobile": kr.mobile,
                        "total": kr.total,
                        "source_seed": source_seed,
                        "depth": depth + 1,
                        "relevance": relevance,
                    }
                )
                if depth + 1 <= max_depth:
                    queue.append((kr.keyword, depth + 1))
                if stats.collected + len(rows_to_save) >= target:
                    break
            if rows_to_save:
                store.save_many(conn, rows_to_save)
                stats.collected += len(rows_to_save)
                if on_progress is not None:
                    on_progress(stats)  # 의도적으로 안 감쌈: TimeoutError 등으로 멈추게 할 수 있다

            if queue and stats.collected < target:
                sleep_fn(random.uniform(*delay_range))
        else:
            if not queue:
                stats.stopped_reason = stats.stopped_reason or "씨앗을 모두 소진했습니다"
            elif stats.collected >= target:
                stats.stopped_reason = stats.stopped_reason or "목표 개수에 도달했습니다"
    finally:
        conn.close()
    return stats


def db_path_for_brand(brand: str, data_dir: str | Path = "data") -> Path:
    p = Path(data_dir) / "keywords" / f"{brand}.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def recompute_relevance(db_path: str | Path, guide_keywords: list[str]) -> dict[int, int]:
    """이미 저장된 키워드의 연관도를 최신 `guide_keywords`로 다시 계산해 저장한다.

    가이드 낱말 목록을 나중에 보강했을 때(예: 정리본 추출 규칙 개선) 브라우저를
    다시 돌리지 않고 기존 행만 갱신하는 용도. 갱신 후 연관도 분포를 돌려준다.
    """
    store = _store()
    conn = store.open_db(db_path)
    try:
        rows = store.all_rows(conn)
        updates = [
            (r["keyword"], compute_relevance(r["keyword"], guide_keywords, r["depth"]))
            for r in rows
        ]
        store.update_relevance(conn, updates)
        return store.relevance_distribution(conn)
    finally:
        conn.close()


def run_for_brand(
    rt: Any,
    brand: str,
    target: int = DEFAULT_TARGET,
    headless: bool = True,
    offscreen: bool = True,
    account_id: str = "",
    profile_dir: str | Path | None = None,
    session_cap: int = DEFAULT_SESSION_CAP,
    rest_sec: float = DEFAULT_REST_SEC,
    delay_range: tuple[float, float] = (MIN_DELAY_SEC, MAX_DELAY_SEC),
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """실행기에서 부르는 진입점. 실 브라우저로 씨앗→BFS 전체를 돈다.

    기본값은 완전 헤드리스(`headless=True`, 2026-09-23) — 잠금 아래에서 재시도
    (`fetch_related_keywords`의 `download_retries`)와 DOM 직접 긁기 대안으로
    다운로드 실패 문제를 흡수한다. `headless=False`로 부르면 이전처럼 화면 밖
    창(`offscreen`)을 띄운다.

    `profile_dir`을 주면 그 프로필로 연다(예: 브랜드별 복제 프로필) — 기본은
    공유 프로필(`naver_session.default_profile_dir()`). 서로 다른 `profile_dir`은
    각자 독립된 잠금을 써서(`naver_session.lock_file_path`) 병렬로 돌 수 있다.
    복제 프로필은 기존 쿠키로만 쓰고, 로그인이 풀려 있으면 여기서도 로그인은
    시도하지 않고 즉시 실패로 보고한다(철칙: 재로그인 금지).
    """
    from v2r.warehouse import naver_session

    cfg = getattr(rt, "sources_cfg", None)
    repo = Path(rt.settings.repo_root)
    xlsx = repo / "data" / f"brand_sheet_{brand}.xlsx"
    seeds = seeds_from_sheet(brand, cfg, xlsx_path=str(xlsx) if xlsx.exists() else None)
    guide_words = extract_guide_keywords(brand, repo / "warehouse" / "guides" / "정리본")
    seeds = list(dict.fromkeys(seeds + guide_words))
    if not seeds:
        return {"ok": False, "error": f"{brand} 씨앗 키워드가 없습니다(시트·정리본 확인 필요)"}

    acct = account_id or "685753"
    pdir = profile_dir or naver_session.default_profile_dir()
    playwright, context, page, logged_in = open_keyword_tool_page(
        profile_dir=pdir, headless=headless, offscreen=offscreen,
    )
    try:
        log.info(
            "%s: 브라우저 열림(pid=%s, profile=%s, headless=%s, offscreen=%s)",
            brand, os.getpid(), pdir, headless, offscreen,
        )
        if not logged_in:
            return {
                "ok": False,
                "error": "네이버 로그인이 풀려 있습니다(로그인·재로그인은 하지 않습니다). "
                "scripts\\naver-login.cmd 로 직접 로그인해 주세요.",
            }

        download_dir = repo / "data" / "keywords" / "_tmp"

        #: 로그인 풀림·캡차·429·빈 결과가 연속으로 이만큼 나오면(단발성은 흡수)
        #: `RuntimeError`를 던져 `discover`를 멈춘다 — 그 브랜드 프로세스만 중단,
        #: 재로그인은 절대 시도하지 않는다. "브라우저가 닫혔다" 종류는 그 자체로는
        #: 세지 않고(아래) 브라우저를 이 프로세스 안에서만 다시 띄워 이어간다.
        consecutive_bad = 0
        relaunch_count = 0
        MAX_RELAUNCH = 3

        def _looks_blocked() -> bool:
            try:
                if naver_session._looks_logged_out(page):
                    return True
                body = page.inner_text("body")[:2000]
                return any(m in body for m in ("캡차", "자동입력 방지", "비정상적인 접근", "일시적으로 제한"))
            except Exception:  # noqa: BLE001
                return False

        def _is_closed_error(exc: Exception) -> bool:
            s = str(exc)
            return "has been closed" in s or "Target closed" in s or "Connection closed" in s

        def _reopen() -> bool:
            """이 프로세스가 연 자기 브라우저만 닫고 같은 프로필로 다시 연다(다른
            프로세스의 브라우저는 건드리지 않는다 — `context`/`playwright`는 이
            함수 지역 변수라 다른 프로세스와 아예 공유되지 않는다)."""
            nonlocal playwright, context, page, logged_in
            try:
                naver_session._close(playwright, context, page)
            except Exception:  # noqa: BLE001
                pass
            try:
                playwright, context, page, logged_in = open_keyword_tool_page(
                    profile_dir=pdir, headless=headless, offscreen=offscreen,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: 브라우저 재기동 실패: %s", brand, exc)
                return False
            log.info("%s: 브라우저 재기동 성공(로그인=%s)", brand, logged_in)
            return bool(logged_in)

        def _fetch(keywords: list[str], depth: int) -> list[KeywordRow]:
            nonlocal consecutive_bad, relaunch_count
            last_err: str | None = None
            try:
                rows = fetch_related_keywords(page, keywords, acct, download_dir=download_dir)
            except Exception as exc:  # noqa: BLE001 - 연속 횟수로 판단, 단발은 흡수
                rows = []
                last_err = str(exc)
                if _is_closed_error(exc) and relaunch_count < MAX_RELAUNCH:
                    relaunch_count += 1
                    log.warning(
                        "%s: 브라우저가 닫힘(시도 %d/%d) — 같은 프로필로 재기동 후 이어감: %s",
                        brand, relaunch_count, MAX_RELAUNCH, exc,
                    )
                    if _reopen():
                        try:
                            rows = fetch_related_keywords(page, keywords, acct, download_dir=download_dir)
                            last_err = None
                        except Exception as exc2:  # noqa: BLE001
                            rows = []
                            last_err = str(exc2)
            bad = (not rows) or last_err is not None or _looks_blocked()
            consecutive_bad = consecutive_bad + 1 if bad else 0
            if consecutive_bad >= 10:
                raise RuntimeError(
                    f"연속 10회 문제 감지(로그인풀림/캡차/429/빈결과) — 마지막 오류: {last_err}"
                )
            return rows

        db_path = db_path_for_brand(brand, repo / "data")

        def _rest(sec: float) -> None:
            time.sleep(sec)

        def _on_progress(stats: DiscoveryStats) -> None:
            if progress_cb is not None:
                progress_cb({"brand": brand, "collected": stats.collected, "queries": stats.queries})

        stats = discover(
            brand,
            seeds,
            guide_words,
            _fetch,
            db_path,
            target=target,
            session_cap=session_cap,
            delay_range=delay_range,
            rest_fn=_rest,
            rest_sec=rest_sec,
            on_progress=_on_progress,
        )
        if progress_cb is not None:
            try:
                progress_cb(
                    {
                        "brand": brand,
                        "collected": stats.collected,
                        "queries": stats.queries,
                        "stopped_reason": stats.stopped_reason,
                        "done": True,
                    }
                )
            except Exception:  # noqa: BLE001
                pass
    finally:
        naver_session._close(playwright, context, page)

    return {
        "ok": True,
        "brand": brand,
        "collected": stats.collected,
        "queries": stats.queries,
        "stopped_reason": stats.stopped_reason,
        "db_path": str(db_path),
    }


__all__ = [
    "DEFAULT_TARGET",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_SESSION_CAP",
    "DEFAULT_REST_SEC",
    "KeywordRow",
    "DiscoveryStats",
    "parse_keyword_response",
    "parse_keyword_download_rows",
    "parse_keyword_download_file",
    "SEED_BATCH_SIZE",
    "seeds_from_sheet",
    "extract_guide_keywords",
    "compute_relevance",
    "fetch_related_keywords",
    "open_keyword_tool_page",
    "discover",
    "db_path_for_brand",
    "recompute_relevance",
    "run_for_brand",
]
