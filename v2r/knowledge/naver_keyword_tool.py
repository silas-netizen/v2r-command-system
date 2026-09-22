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

#: 네이버 검색광고 키워드 도구 페이지 (계정 번호는 브랜드마다 다를 수 있어 인자로 받는다)
KEYWORD_PLANNER_URL = (
    "https://ads.naver.com/manage/ad-accounts/{account_id}/sa/tool/keyword-planner"
)
#: 이 페이지가 실제로 호출하는 연관 키워드 XHR (경로에 이 조각이 들어있으면 잡는다)
KEYWORD_XHR_HINT = "keywordstool"

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


#: 정리본 md에서 핵심어를 뽑는 자리 — "브랜드/제품:" 줄과 첫 문단의 결핍/증상
#: 낱말(쉼표·가운뎃점 구분)을 쓴다.
_RE_GUIDE_PRODUCT_LINE = re.compile(r"^-\s*브랜드/제품\s*:\s*(.+)$", re.M)
#: "네이버 카페에 <A>, <B>, <C> 결핍을 가진 타겟" 처럼 나열된 결핍 낱말
_RE_GUIDE_TARGET_LINE = re.compile(r"타겟을?\s*대상으로")
_RE_SPLIT = re.compile(r"[,·/、]+")


def extract_guide_keywords(brand: str, guides_dir: str | Path | None = None) -> list[str]:
    """`warehouse/guides/정리본/<브랜드>.md`에서 브랜드 논리 낱말을 뽑는다.

    파일이 없거나 패턴이 안 맞으면 `config/brands.yaml`의 `description`을
    낱말 단위로 잘라 대신 쓴다(둘 다 없으면 빈 목록).
    """
    base = Path(guides_dir) if guides_dir else Path("warehouse") / "guides" / "정리본"
    path = base / f"{brand}.md"
    words: list[str] = []
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="ignore")
        m = _RE_GUIDE_PRODUCT_LINE.search(text)
        if m:
            words.extend(w.strip() for w in _RE_SPLIT.split(m.group(1)) if w.strip())
        # "착색, 미백, 색소침착 결핍을 가진 타겟" 꼴의 줄에서 결핍 낱말도 뽑는다
        for line in text.splitlines():
            if "결핍" in line and ("타겟" in line or "가진" in line):
                lead = line.split("결핍")[0]
                # 마지막 콤마 구분 나열만 취한다(문장 앞머리 잡동사니 제거)
                lead = lead.split("에")[-1].split("에서")[-1]
                words.extend(w.strip() for w in _RE_SPLIT.split(lead) if w.strip() and len(w.strip()) <= 12)
    if not words:
        try:
            import yaml

            cfg = yaml.safe_load(Path("config/brands.yaml").read_text(encoding="utf-8"))
            desc = ((cfg or {}).get("brands") or {}).get(brand, {}).get("description", "")
            words.extend(w.strip() for w in re.split(r"[·,\-—]", desc) if w.strip())
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

    브랜드 논리 낱말 중 하나가 키워드에 그대로 들어있으면(또는 반대로 키워드가
    논리 낱말에 들어있으면) 0. 아니면 BFS 깊이를 그대로 0~3으로 자른다(깊이
    0=씨앗 자체는 논리 낱말이 아니어도 직접 입력한 것이므로 1로 본다).
    """
    kw = str(keyword or "")
    for g in guide_keywords or []:
        g = str(g or "").strip()
        if g and (g in kw or kw in g):
            return 0
    return max(1, min(int(depth), 3))


# --- 브라우저 자동화 ------------------------------------------------------
def fetch_related_keywords(
    page: Any, seed: str, account_id: str, timeout_ms: int = 20000
) -> list[KeywordRow]:
    """씨앗 키워드 1개를 키워드 도구에 입력해 연관 키워드 XHR 응답을 잡는다.

    실 서비스 DOM은 바뀔 수 있으니 보수적으로 짠다: 씨앗 입력창에 값을 넣고
    조회 버튼을 누른 뒤, `KEYWORD_XHR_HINT`가 URL에 들어간 응답을 기다려 JSON을
    파싱한다. 셀렉터를 못 찾으면 `RuntimeError`.
    """
    url = KEYWORD_PLANNER_URL.format(account_id=account_id)
    if account_id not in (page.url or ""):
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

    captured: dict[str, Any] = {}

    def _on_response(resp: Any) -> None:
        try:
            if KEYWORD_XHR_HINT in (resp.url or "") and resp.status == 200:
                captured["data"] = resp.json()
        except Exception:  # noqa: BLE001
            pass

    page.on("response", _on_response)
    try:
        # 씨앗 입력창(정확한 셀렉터는 실제 페이지에 맞춰 조정 필요 — 우선 흔한
        # placeholder/aria-label 후보를 순서대로 시도한다)
        input_locator = None
        for sel in [
            'textarea[placeholder*="키워드"]',
            'input[placeholder*="키워드"]',
            'textarea',
        ]:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0:
                    input_locator = loc
                    break
            except Exception:  # noqa: BLE001
                continue
        if input_locator is None:
            raise RuntimeError("키워드 입력창을 찾지 못했습니다(페이지 구조 확인 필요)")

        # 실측(2026-09-22): 입력만으로는 조회 버튼이 `disabled` 상태로 남는다.
        # Enter를 눌러 키워드를 칩(chip)으로 확정해야 버튼이 활성화되는데, 페이지
        # JS 번들이 늦게 붙으면 첫 Enter가 씹힐 때가 있어 최대 3번 재시도한다.
        btn = None
        for sel in ['button:has-text("조회하기")', 'button:has-text("조회")', 'button[type="submit"]']:
            loc = page.locator(sel).first
            if loc.count() > 0:
                btn = loc
                break
        if btn is None:
            raise RuntimeError("조회 버튼을 찾지 못했습니다(페이지 구조 확인 필요)")

        enabled = False
        for attempt in range(3):
            input_locator.click()
            input_locator.fill(seed)
            input_locator.press("Enter")
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                try:
                    if btn.count() > 0 and btn.get_attribute("disabled") is None:
                        enabled = True
                        break
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(300)
            if enabled:
                break
        if not enabled:
            raise RuntimeError(f"'{seed}' 조회 버튼이 활성화되지 않았습니다(칩 등록 실패로 보임)")

        btn.click(timeout=10000)
        page.wait_for_timeout(timeout_ms)
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:  # noqa: BLE001
            pass

    data = captured.get("data")
    if data is None:
        raise RuntimeError(f"'{seed}' 조회 응답을 못 받았습니다(XHR 미포착)")
    return parse_keyword_response(data)


def open_keyword_tool_page(profile_dir: str | Path | None = None, headless: bool = False):
    """네이버 로그인 프로필로 키워드 도구 창을 연다. 로그아웃/재로그인 절대 금지.

    반환: `(playwright, context, page, logged_in)`. `logged_in`이 False면
    조회 없이 즉시 닫고 호출 쪽에서 중단해야 한다(로그인 자동 처리 금지).
    """
    from v2r.warehouse import naver_session

    path = Path(profile_dir) if profile_dir else naver_session.default_profile_dir()
    playwright, context, page = naver_session._launch(
        path, headless=headless, user_agent=naver_session.saved_user_agent(path)
    )
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
    fetch_fn: Callable[[str, int], list[KeywordRow]],
    db_path: str | Path,
    target: int = DEFAULT_TARGET,
    max_depth: int = DEFAULT_MAX_DEPTH,
    session_cap: int = DEFAULT_SESSION_CAP,
    delay_range: tuple[float, float] = (MIN_DELAY_SEC, MAX_DELAY_SEC),
    sleep_fn: Callable[[float], None] = time.sleep,
    rest_fn: Callable[[float], None] | None = None,
    rest_sec: float = DEFAULT_REST_SEC,
) -> DiscoveryStats:
    """씨앗 키워드로 꼬리 물기 BFS. 순수 로직 — `fetch_fn(keyword, depth) -> list[KeywordRow]`만 준다.

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
        queue: list[tuple[str, str, int]] = []  # (keyword, source_seed, depth)
        for s in seeds:
            s = str(s or "").strip()
            if s and s not in seen:
                queue.append((s, s, 0))
                seen.add(s)

        session_count = 0
        while queue and stats.collected < target:
            keyword, source_seed, depth = queue.pop(0)
            if session_count >= session_cap:
                if rest_fn is None:
                    stats.stopped_reason = "세션 조회 상한 도달(휴식 없음 — 테스트/일회성 호출)"
                    break
                rest_fn(rest_sec)
                session_count = 0
            try:
                related = fetch_fn(keyword, depth)
            except Exception as exc:  # noqa: BLE001
                stats.stopped_reason = f"조회 실패(차단 가능성): {exc}"
                log.warning("키워드 도구 조회 중단(%s, 씨앗=%s): %s", brand, keyword, exc)
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
                    queue.append((kr.keyword, source_seed, depth + 1))
                if stats.collected + len(rows_to_save) >= target:
                    break
            if rows_to_save:
                store.save_many(conn, rows_to_save)
                stats.collected += len(rows_to_save)

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


def run_for_brand(
    rt: Any,
    brand: str,
    target: int = DEFAULT_TARGET,
    headless: bool = False,
    account_id: str = "",
) -> dict[str, Any]:
    """실행기에서 부르는 진입점. 실 브라우저로 씨앗→BFS 전체를 돈다."""
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
    playwright, context, page, logged_in = open_keyword_tool_page(
        profile_dir=naver_session.default_profile_dir(), headless=headless
    )
    try:
        if not logged_in:
            return {
                "ok": False,
                "error": "네이버 로그인이 풀려 있습니다(로그인·재로그인은 하지 않습니다). "
                "scripts\\naver-login.cmd 로 직접 로그인해 주세요.",
            }

        def _fetch(keyword: str, depth: int) -> list[KeywordRow]:
            return fetch_related_keywords(page, keyword, acct)

        db_path = db_path_for_brand(brand, repo / "data")
        stats = discover(
            brand,
            seeds,
            guide_words,
            _fetch,
            db_path,
            target=target,
            rest_fn=lambda sec: time.sleep(sec),
        )
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
    "seeds_from_sheet",
    "extract_guide_keywords",
    "compute_relevance",
    "fetch_related_keywords",
    "open_keyword_tool_page",
    "discover",
    "db_path_for_brand",
    "run_for_brand",
]
