"""노출 확인 속도 개선 — 상주 브라우저 작업자 + 스크롤 조기 종료 + 우선순위 큐.

설계: `docs/reports/exposure-speed-plan-2026-09-23.md`.
이 모듈은 새 파일이다 — `v2r/knowledge/keyword_exposure.py`(판정 규칙, 다른
일꾼이 마무리 중)는 고치지 않고 그 함수들을 **호출만** 한다:
`resolve_search_query`, `judge_keyword_exposure`(카드 파싱·판정), `_enqueue_sheet_row`,
`keyword_exposure_store.save`. 판정 규칙 자체(카페 카드 대표/서브 구분, 댓글2
식별어 위치, 시트 A/G/J/K/L 갱신)는 그 모듈이 그대로 갖고 있다.

작업자 1개 = 브라우저 1개 상주(익명 컨텍스트, `data/naver_cookies.json` 있으면
로그인 상태 사용). 키워드마다 페이지만 새로 열어 통합검색 → **스크롤 조기
종료**(카페 카드가 로드되고 문서 높이가 `early_exit_same_height_rounds`회 연속
같으면 중단, 카페 카드가 하나도 없으면 `max_scroll_rounds`까지) → 최종 DOM을
`judge_keyword_exposure(dom_html=...)`에 넘겨 판정(기존 함수) → DB append → 시트
배치 큐. 다음 키워드는 우선순위 큐(`exposure_priority.next_priority_batch`)에서
뽑는다.

실행: `python -m v2r.knowledge.exposure_runner --worker-id 0 --brands 팥순이,코숨핏`
(브랜드를 안 주면 `known_brands`). 상태는 `data/exposure_runner_state.json`
(작업자별 처리 수·마지막 키워드·휴식 상태·시간당 속도), 로그는 `logs/exposure-runner-<n>.log`.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATE_FILE = "exposure_runner_state.json"


# =======================================================================
# 스크롤 조기 종료 — 순수 함수로 분리해 테스트 가능하게 한다.
# =======================================================================

def should_stop_scrolling(
    heights: list[int],
    has_cafe_cards: bool,
    round_index: int,
    max_rounds: int,
    same_height_rounds: int,
) -> bool:
    """지금까지의 문서 높이 이력으로 스크롤을 멈출지.

    카페 카드가 하나도 없으면(`has_cafe_cards=False`) `max_rounds`까지 계속한다
    (예전 동작 유지 — 카드가 늦게 붙는 화면을 놓치지 않기 위해). 카드가 있으면
    최근 `same_height_rounds`개 높이가 모두 같을 때 멈춘다.
    """
    if round_index + 1 >= max_rounds:
        return True
    if not has_cafe_cards:
        return False
    if len(heights) < same_height_rounds:
        return False
    tail = heights[-same_height_rounds:]
    return len(set(tail)) == 1


def fetch_integrated_search_dom_resident(
    context: Any,
    query: str,
    max_rounds: int = 20,
    wait_sec: float = 0.4,
    same_height_rounds: int = 2,
) -> str:
    """상주 브라우저 컨텍스트로 통검 페이지를 열어 조기 종료 스크롤 후 최종 DOM.

    `keyword_exposure.fetch_integrated_search_dom`과 달리 매번 브라우저를 켜지
    않고, 호출자가 이미 연 `context`(브라우저 1개, 페이지만 새로)를 받는다.
    """
    from urllib.parse import quote

    from v2r.knowledge.keyword_exposure import INTEGRATED_SEARCH_URL, serp

    url = INTEGRATED_SEARCH_URL.format(query=quote(query))
    page = context.new_page()
    try:
        page.goto(url, timeout=15000, wait_until="domcontentloaded")
        heights: list[int] = []
        for i in range(max_rounds):
            for sel in ("a.api_more", "a.more", "button.api_more"):
                try:
                    loc = page.locator(sel)
                    if loc.count() and loc.first.is_visible():
                        loc.first.click(timeout=1000)
                except Exception:
                    pass
            page.mouse.wheel(0, 20000)
            page.wait_for_timeout(int(wait_sec * 1000))
            height = page.evaluate("document.body.scrollHeight")
            heights.append(int(height))
            html_now = page.content()
            has_cards = bool(serp.extract_cafe_cards(html_now))
            if should_stop_scrolling(heights, has_cards, i, max_rounds, same_height_rounds):
                break
        return page.content()
    finally:
        page.close()


def _atomic_write_json(path: Path, data: dict) -> None:
    """여러 작업자 프로세스가 같은 파일에 동시에 쓸 수 있어 임시 파일명을
    프로세스별로 고유하게 만든다(2026-09-24 발견 — 고정된 `.json.tmp` 이름을
    공유해서 작업자 5개 중 1개가 "다른 프로세스가 파일을 사용 중" PermissionError로
    죽었다). `os.replace` 자체는 원자적이지만, Windows에서 마침 다른 프로세스가
    같은 임시 파일을 쓰는 찰나와 겹치면 잠깐 실패할 수 있어 짧게 재시도한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    last_exc: Exception | None = None
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:  # pragma: no cover - Windows 파일 잠금 경합
            last_exc = exc
            time.sleep(0.05)
    if last_exc is not None:
        raise last_exc


# =======================================================================
# 작업자 상태 파일
# =======================================================================

def state_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / "data" / STATE_FILE


def _load_state(repo_root: str | Path) -> dict:
    p = state_path(repo_root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(repo_root: str | Path, state: dict) -> None:
    p = state_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(p, state)


def update_worker_state(
    repo_root: str | Path,
    worker_id: int,
    *,
    processed_delta: int = 0,
    last_keyword: str | None = None,
    resting_until: float | None = None,
    started_at: str | None = None,
    duplicate: bool | None = None,
    min_gap_violation: bool | None = None,
) -> dict:
    """작업자 상태 갱신(다른 프로세스와 동시에 써도 마지막 쓰기가 이긴다 —
    작업자별 키가 나뉘어 있어 충돌해도 서로 덮어쓰지 않는다).

    2026-09-24 3차 — `duplicate`(이번 처리 건이 중복 재검사였는지, `True`/
    `False`)를 넘기면 작업자별·전체 중복률(`duplicate_rate`)을 같이 집계해
    `data/exposure_runner_state.json`에 남긴다.

    2026-09-24 7차(코디네이터 지시) — `duplicate`의 뜻이 바뀌었다: 재시작
    이전 검사까지 세던 옛 정의(53%까지 나와 운영 지표로 못 썼다, 6-5절) 대신
    "이 러너 실행 안에서 같은 (브랜드,키워드)를 두 번 이상 검사했는가"만
    센다(`process_one._is_duplicate_since_run_start`). 별도로
    `min_gap_violation`(그 순간 등급 규칙상 아직 재검사 대상이 아니었는지,
    `process_one._min_gap_violation`)도 넘기면 같은 방식으로
    `min_gap_violation_count`/`min_gap_violation_rate`를 집계한다 — 둘 다
    같은 `checked_count` 분모를 공유한다(한 번의 처리 건에 대해 두 지표를
    같이 넘기므로 분모를 이중으로 세지 않는다)."""
    state = _load_state(repo_root)
    workers = state.setdefault("workers", {})
    w = workers.setdefault(str(worker_id), {"processed": 0, "started_at": started_at or now_iso_utc()})
    if processed_delta:
        w["processed"] = int(w.get("processed", 0)) + processed_delta
    if last_keyword is not None:
        w["last_keyword"] = last_keyword
        w["last_at"] = now_iso_utc()
    if resting_until is not None:
        w["resting_until"] = resting_until
    if duplicate is not None or min_gap_violation is not None:
        w["checked_count"] = int(w.get("checked_count", 0)) + 1
        if duplicate is not None:
            w["duplicate_count"] = int(w.get("duplicate_count", 0)) + (1 if duplicate else 0)
            w["duplicate_rate"] = round(w["duplicate_count"] / max(1, w["checked_count"]), 4)
        if min_gap_violation is not None:
            w["min_gap_violation_count"] = int(w.get("min_gap_violation_count", 0)) + (1 if min_gap_violation else 0)
            w["min_gap_violation_rate"] = round(w["min_gap_violation_count"] / max(1, w["checked_count"]), 4)
    elapsed_h = max(
        1e-6,
        (time.time() - _to_epoch(w.get("started_at", now_iso_utc()))) / 3600.0,
    )
    w["rate_per_hour"] = round(int(w.get("processed", 0)) / elapsed_h, 1)
    if duplicate is not None or min_gap_violation is not None:
        totals = state.setdefault(
            "duplicate_totals",
            {"duplicate_count": 0, "min_gap_violation_count": 0, "checked_count": 0},
        )
        totals["checked_count"] = int(totals.get("checked_count", 0)) + 1
        if duplicate is not None:
            totals["duplicate_count"] = int(totals.get("duplicate_count", 0)) + (1 if duplicate else 0)
            totals["duplicate_rate"] = round(totals["duplicate_count"] / max(1, totals["checked_count"]), 4)
        if min_gap_violation is not None:
            totals["min_gap_violation_count"] = int(totals.get("min_gap_violation_count", 0)) + (
                1 if min_gap_violation else 0
            )
            totals["min_gap_violation_rate"] = round(
                totals["min_gap_violation_count"] / max(1, totals["checked_count"]), 4
            )
    state["updated_at"] = now_iso_utc()
    _write_state(repo_root, state)
    return w


def maybe_set_global_pause(
    repo_root: str | Path, expected_workers: int, rest_minutes: float, now: float | None = None
) -> float | None:
    """전체 작업자(0..expected_workers-1)가 지금 동시에 휴식 중이면 순환
    상태 파일에 `global_paused_until`(30분 정지)을 기록한다.

    작업자 하나라도 상태를 모르거나(아직 상태 파일에 없음) 쉬는 중이 아니면
    아무것도 안 쓰고 `None`을 돌려준다. 이미 전역 정지가 걸려 있으면(아직 안
    지났으면) 그대로 둔다(연장하지 않음 — 매번 15분 휴식할 때마다 30분씩
    밀리지 않게).
    """
    now = now if now is not None else time.time()
    state = _load_state(repo_root)
    workers = state.get("workers", {})
    if expected_workers <= 0:
        return None
    for i in range(expected_workers):
        w = workers.get(str(i))
        if not w:
            return None
        resting_until = w.get("resting_until")
        if not resting_until or float(resting_until) <= now:
            return None
    existing = state.get("global_paused_until")
    if existing and float(existing) > now:
        return float(existing)
    global_until = now + rest_minutes * 60
    state["global_paused_until"] = global_until
    state["global_paused_at"] = now_iso_utc()
    _write_state(repo_root, state)
    log.warning("노출 러너: 작업자 전원 동시 휴식 — 전체 %s분 정지 기록", rest_minutes)
    return global_until


def global_pause_remaining(repo_root: str | Path, now: float | None = None) -> float:
    """지금부터 전역 정지가 끝날 때까지 남은 초(0이면 정지 아님)."""
    now = now if now is not None else time.time()
    state = _load_state(repo_root)
    until = state.get("global_paused_until")
    if not until:
        return 0.0
    remaining = float(until) - now
    return remaining if remaining > 0 else 0.0


def is_alive(repo_root: str | Path, stale_seconds: float = 120.0) -> bool:
    """러너가 살아 있는지(사이드카 `cycle_tick` 중복 방지용) — 상태 파일이
    최근에 갱신됐으면 산다고 본다."""
    state = _load_state(repo_root)
    updated = state.get("updated_at")
    if not updated:
        return False
    dt = _to_epoch(updated)
    return (time.time() - dt) < stale_seconds


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_epoch(iso: str) -> float:
    try:
        s = str(iso).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return time.time()


# =======================================================================
# 작업자 루프
# =======================================================================

def judge_once(
    rt: Any, context: Any, brand: str, item: dict, cfg: dict, executor: "concurrent.futures.ThreadPoolExecutor | None" = None
) -> "ExposureRow":  # noqa: F821
    """키워드 하나 조기 종료 스크롤 → 기존 판정 함수. DB/시트에는 아직 안 쓴다
    (2차 확인 대기 로직이 `process_one`에서 저장 여부를 결정한다).

    2026-09-24 실측 중 발견한 버그 — `judge_keyword_exposure`(카드 후보 확정
    단계, `confirm_our_article_detail`)는 후보 글을 열 때 **자기 것만의**
    `with sync_playwright()`를 새로 연다(`fetch_article_html`/`fetch_article_text`).
    이 함수를 상주 브라우저를 쥔 스레드(메인 스레드, `run_worker`가 이미
    `with sync_playwright()` 안에 있음)에서 그대로 부르면 Playwright sync API가
    같은 스레드 안의 중첩 호출을 막아 "Playwright Sync API inside the asyncio
    loop" 예외를 던진다 — 모든 후보 확인이 조용히 실패해 노출완이 전부
    밀려남으로 오판정되는 심각한 결함이었다(30분 실측, 103건 전부 pushed,
    unknown 0). 판정 함수 자체(`judge_keyword_exposure`)는 그대로 두고, 그
    호출만 별도 스레드(`executor`)로 옮겨 스레드 충돌을 피한다."""
    from v2r.knowledge.keyword_exposure import ExposureRow, judge_keyword_exposure, now_iso, resolve_search_query

    keyword = item["keyword"]
    # 2026-09-24 코디네이터 지시 — 병목 파악용 구간별 타이밍(자동완성/스크롤/
    # 후보 확인). "타이밍" 태그로 로그에 남기고, 실측 뒤 grep으로 집계한다.
    t0 = time.perf_counter()
    query = resolve_search_query(keyword)
    t1 = time.perf_counter()
    cookies_path = Path(rt.settings.repo_root) / "data" / "naver_cookies.json"
    try:
        html = fetch_integrated_search_dom_resident(
            context,
            query,
            max_rounds=int(cfg.get("max_scroll_rounds", 20)),
            wait_sec=float(cfg.get("scroll_wait_sec", 0.4)),
            same_height_rounds=int(cfg.get("early_exit_same_height_rounds", 2)),
        )
    except Exception as exc:
        log.warning("페이지 로드 실패(%s): %s", keyword, exc)
        return ExposureRow(brand, keyword, item.get("cafe", ""), "", None, "unknown", now_iso(), item.get("t0_status", ""), query)
    t2 = time.perf_counter()

    judge_kwargs = dict(
        cookies_path=cookies_path,
        dom_html=html,
        search_query=query,
        article_index=getattr(rt, "article_index", None),
    )
    if executor is not None:
        verdict = executor.submit(judge_keyword_exposure, rt, brand, keyword, **judge_kwargs).result()
    else:
        verdict = judge_keyword_exposure(rt, brand, keyword, **judge_kwargs)
    t3 = time.perf_counter()
    log.info(
        "타이밍 %s autocomplete=%.2fs scroll=%.2fs confirm=%.2fs total=%.2fs",
        keyword, t1 - t0, t2 - t1, t3 - t2, t3 - t0,
    )
    return ExposureRow(
        brand,
        keyword,
        item.get("cafe", ""),
        verdict["matched_url"],
        verdict["rank"],
        verdict["status"],
        now_iso(),
        item.get("t0_status", ""),
        verdict["search_query"],
        verdict.get("rank_overall"),
    )


# =======================================================================
# 2026-09-24 6차 — `write_exposure_csv` 스로틀·백그라운드화.
#
# 실측(08:13 재시작 후, 시트가 우아덤 4,959→12,549행·코숨핏 254→4,854행으로
# 커진 뒤): 작업자당 52건/시(실측 11의 560건/시 대비 급락), 같은 작업자의
# 연속 검사 사이 간격 중앙값이 90~120초였다 — 실제 검색+판정(자동완성+
# 스크롤+확인)은 여전히 평균 9~10초였는데도. 원인은 `_finalize_row`가 검사
# **1건마다 동기로** `write_exposure_csv`(keyword_universe 전체 재순회 +
# `store.latest_by_keyword` 전체 이력 + 1만 행 정렬 + CSV 전량 재작성)를
# 불렀기 때문 — 시트가 작을 때는 안 보이던 비용이 브랜드당 1만 행대에서
# 메인 검사 루프를 초 단위로 막는 병목이 됐다(가설 중 "시트 쓰기 잠금
# 대기"·"네이버 응답"은 로그로 배제 — `_enqueue_sheet_row`의 시트 배치
# 반영은 원래 별도 스레드라 안 막았고, 자동완성·스크롤·확인 각 구간 평균도
# 크게 안 늘었다). 브랜드당 이 초(기본 60초)에 한 번만, 그것도 전용
# Runtime을 연 별도 스레드에서 갱신하도록 바꿨다(정렬 큐 백그라운드 갱신,
# b393cd4와 같은 패턴).
# =======================================================================

CSV_WRITE_MIN_INTERVAL_SEC = 60.0

_CSV_WRITE_LOCK = threading.Lock()
#: 브랜드 -> 마지막으로 백그라운드 CSV 갱신을 "시작한" monotonic 시각.
_CSV_LAST_WRITE_MONO: dict[str, float] = {}


def _open_csv_runtime() -> Any:
    """`_write_exposure_csv_in_background` 전용 — 메인 스레드(작업자)의
    `rt.conn`을 다른 스레드와 공유하지 않도록 이 스레드만의 새 `Runtime`을
    연다(`exposure_priority._refresh_queue_in_background`와 같은 패턴).

    2026-09-25 9차 — `skip_schema_init=True`: 스키마는 메인 작업자가 이미
    만들어 뒀다. 이 스레드가 브랜드마다 최대 60초에 한 번씩 새 `Runtime`을
    여는데, 매번 `init_schema`(executescript)까지 다시 돌리면 다른 연결의
    `BEGIN IMMEDIATE` 쓰기 트랜잭션과 부딪혀 "database is locked"(busy_timeout
    5000ms 초과)로 작업자가 죽는 원인이 됐다 — 21시간 가동 중 6개 중 4개가
    이렇게 죽어 2개만 남아 그 2개가 서로 중복 재검사를 냈다(실측 9차)."""
    from v2r.engine.context import Runtime

    return Runtime.open(skip_schema_init=True)


def _write_exposure_csv_in_background(brand: str) -> None:
    from v2r.knowledge.keyword_exposure import write_exposure_csv

    thread_rt = None
    try:
        thread_rt = _open_csv_runtime()
        write_exposure_csv(thread_rt, brand)
    except Exception as exc:  # pragma: no cover - 방어용(백그라운드라 예외를 삼킴)
        log.warning("노출 CSV 갱신 실패(%s): %s", brand, exc)
    finally:
        if thread_rt is not None:
            try:
                thread_rt.close()
            except Exception:
                pass


def maybe_write_exposure_csv(brand: str, min_interval_sec: float = CSV_WRITE_MIN_INTERVAL_SEC) -> bool:
    """브랜드당 `min_interval_sec` 안에 이미 갱신을 시작했으면 건너뛰고,
    아니면 백그라운드 스레드에서 `write_exposure_csv`를 돌린다. 시작했으면
    `True`(시험용 — 실제 완료 여부는 보장 안 함)."""
    now = time.monotonic()
    with _CSV_WRITE_LOCK:
        last = _CSV_LAST_WRITE_MONO.get(brand, 0.0)
        if now - last < min_interval_sec:
            return False
        _CSV_LAST_WRITE_MONO[brand] = now
    threading.Thread(
        target=_write_exposure_csv_in_background,
        args=(brand,),
        daemon=True,
        name=f"exposure-csv-{brand}",
    ).start()
    return True


def _finalize_row(rt: Any, brand: str, item: dict, row: Any) -> None:
    """판정을 확정해 DB append + 시트 배치 큐(기존 함수 호출만)."""
    from v2r.knowledge.exposure_priority import mark_checked
    from v2r.knowledge.keyword_exposure import _enqueue_sheet_row
    from v2r.store import keyword_exposure_store as store

    store.save(rt.conn, row.as_row())
    # 2026-09-24 2차 — last_checked(DB)도 이제 TTL(기본 20초) 캐시한다. 저장
    # 직후 `mark_checked`로 이 키워드만 즉시 갱신해, 캐시가 아직 안 지났어도
    # 같은 키워드가 연속으로 다시 뽑히지 않게 한다(단위 시험
    # `test_next_priority_batch_같은_키워드_연속_두번_안뽑힘` 참고).
    # universe·정렬 캐시(시트·발굴 CSV, `invalidate_universe_cache`)는 검사
    # 결과와 무관해 여기서 비우지 않는다 — 매 건 저장마다 비우면 배치 캐시
    # 효과가 사라진다.
    mark_checked(rt, brand, item["keyword"], row.status, row.checked_at)
    _enqueue_sheet_row(rt, brand, item, row)
    # 2026-09-24 6차 — 매 건 동기 호출을 스로틀 + 백그라운드로(위 참고).
    maybe_write_exposure_csv(brand)


def _latest_status(conn: Any, brand: str, keyword: str) -> str:
    # checked_at은 초 단위라 같은 초 안에 두 번 저장되면 값이 같을 수 있다
    # (테스트, 또는 확인 즉시 재확인) — rowid로 동률을 깬다(나중에 쓴 쪽 우선).
    row = conn.execute(
        "SELECT status FROM keyword_exposure WHERE brand = ? AND keyword = ? "
        "ORDER BY checked_at DESC, rowid DESC LIMIT 1",
        (brand, keyword),
    ).fetchone()
    return str(row["status"]) if row else ""


def _is_duplicate_since_run_start(prev_row: Any, run_started_at: "datetime | None", *, exempt: bool) -> bool:
    """2026-09-24 7차 — 코디네이터 지시로 `duplicate`의 정의를 바꿨다.

    옛 정의(3~6차, `_is_duplicate_recheck`)는 "직전 검사로부터 최소 간격
    규칙보다 일찍 다시 봤는지"였는데, 이게 **재시작 전에 본 적 있는 키워드**
    까지 중복으로 셌다 — 오늘 하루 여러 번 재시작해 실측한 세션에서는
    `duplicate_rate`가 53%까지 나와(6-5절) 운영 지표로 못 썼다.

    새 정의: "이 러너 실행(작업자 프로세스가 시작된 시각 이후) 안에서 같은
    (브랜드, 키워드)를 두 번 이상 검사했는가" — `prev_row`(직전 검사 행)가
    있고 그 `checked_at`이 이 실행의 시작 시각(`run_started_at`) 이후면
    참이다. 재시작 이전 검사는 `prev_row`가 있어도 `checked_at`이
    `run_started_at`보다 이르므로 중복으로 안 센다. `exempt=True`(2단계
    확인 대기 재확인)면 의도된 재검사이므로 항상 거짓."""
    if exempt or prev_row is None or run_started_at is None:
        return False
    from v2r.knowledge.exposure_priority import _parse_iso

    prev_dt = _parse_iso(str(prev_row["checked_at"] or ""))
    if prev_dt is None:
        return False
    return prev_dt >= run_started_at


def _min_gap_violation(rt: Any, brand: str, item: dict, prev_row: Any, now: "datetime", cfg: dict, *, exempt: bool) -> bool:
    """2026-09-24 7차 — "최소 간격(6시간/12시간) 안 재검사" 지표. 옛
    `_is_duplicate_recheck`처럼 문턱값만 보지 않고, **같은 우선순위 규칙
    (`exposure_priority.priority_tier`)을 그대로 재사용**해 "이번 검사가
    그 순간의 등급 규칙상 실제로 대상이었는지"를 판정한다 — 2등급(최근
    발행)은 규칙상 원래 최소 간격이 없으므로(설계 그대로, 7차에서도 안
    바꿈) 몇 분 만에 다시 봐도 위반이 아니다. `prev_row`가 없거나(첫 검사)
    2단계 확인 대기 재확인(`exempt`)이면 위반이 아니다."""
    if exempt or prev_row is None:
        return False
    from v2r.knowledge.exposure_priority import _universe_bundle, priority_tier
    from v2r.knowledge.keyword_exposure import _norm

    priority_cfg = cfg.get("priority", {}) if isinstance(cfg.get("priority"), dict) else {}
    last_checked = {
        _norm(item.get("keyword", "")): {
            "checked_at": str(prev_row["checked_at"] or ""), "status": str(prev_row["status"] or "")
        }
    }
    try:
        bundle = _universe_bundle(rt, brand, priority_cfg, now)
    except Exception:  # pragma: no cover - 방어용(시트 조회 실패 시 위반 아님으로)
        return False
    tier, _age = priority_tier(item, last_checked, bundle["recent_norm"], priority_cfg, bundle["vol_threshold"], now)
    return tier >= 99


def process_one(
    rt: Any,
    context: Any,
    brand: str,
    item: dict,
    cfg: dict,
    executor: "concurrent.futures.ThreadPoolExecutor | None" = None,
    run_started_at: "datetime | None" = None,
) -> dict:
    """키워드 하나 검사 + 노출완→밀려남 2단계 확인(아래 참고) + DB/시트 반영.

    2026-09-24 코디네이터 지시(비만도 계산기 23:31 일시 변동 사례) — 직전
    판정이 `exposed`였는데 이번에 `pushed`가 나오면 **바로 확정하지 않는다**.
    `data/exposure_pending_confirm.json`에 `pending_confirm`으로만 기록해 두고
    5~10분 뒤(`pending_confirm_min_minutes`~`pending_confirm_max_minutes`,
    설정 기본값) 같은 키워드를 다시 확인한 결과가 **또** `pushed`일 때만 DB·
    시트를 밀려남으로 바꾼다. 재확인에서 `exposed`가 나오면(일시 변동) 대기를
    지우고 아무 것도 바꾸지 않는다 — 직전 `exposed` 행이 이미 최신이라 그대로
    유지된다. 판정 규칙 자체(`judge_keyword_exposure`)는 그대로 호출만 한다.

    2026-09-24 7차 — 반환 딕셔너리의 `duplicate`는 이제 "이 러너 실행 안에서
    두 번째 이상 검사"만 뜻한다(`_is_duplicate_since_run_start`, `run_started_at`
    필요 — 안 주면 항상 `False`). 별도 필드 `min_gap_violation`을 추가해
    "그 순간 등급 규칙상 아직 대상이 아니었는지"를 따로 판정한다
    (`_min_gap_violation`, 등급 2/최근 발행은 원래 간격이 없어 위반이 아님).
    상태 파일에 각각 집계된다(`update_worker_state`, `run_worker` 참고).
    """
    from v2r.store import keyword_exposure_store as store

    keyword = item["keyword"]
    prev_row = store.latest_for_keyword(rt.conn, brand, keyword)
    prev_status = str(prev_row["status"]) if prev_row is not None else ""
    pending = get_pending(rt.settings.repo_root, brand, keyword)
    exempt = pending is not None

    row = judge_once(rt, context, brand, item, cfg, executor=executor)
    duplicate = _is_duplicate_since_run_start(prev_row, run_started_at, exempt=exempt)
    min_gap_violation = _min_gap_violation(
        rt, brand, item, prev_row, datetime.now(timezone.utc), cfg, exempt=exempt
    )

    if row.status == "pushed" and (prev_status == "exposed" or pending is not None):
        if pending is None:
            # 노출완 → 밀려남 첫 관측 — 바로 확정하지 않고 대기만 남긴다(DB 미기록).
            set_pending(rt.settings.repo_root, brand, item, cfg)
            return {
                "status": "pending_confirm", "keyword": keyword,
                "duplicate": duplicate, "min_gap_violation": min_gap_violation,
            }
        # 대기 중이던 키워드의 재확인 — 이번에도 밀려남이면 확정.
        clear_pending(rt.settings.repo_root, brand, keyword)
        _finalize_row(rt, brand, item, row)
        return {
            "status": row.status, "keyword": keyword, "rank": row.rank, "confirmed": True,
            "duplicate": duplicate, "min_gap_violation": min_gap_violation,
        }

    if pending is not None:
        # 대기 중이었는데 이번엔 밀려남이 아님(exposed로 되돌아옴) — 일시 변동,
        # 대기만 지우고 DB는 안 건드린다(직전 exposed 행이 이미 최신).
        clear_pending(rt.settings.repo_root, brand, keyword)
        if row.status != "exposed":
            _finalize_row(rt, brand, item, row)
        return {
            "status": row.status, "keyword": keyword, "rank": row.rank, "false_alarm_cleared": True,
            "duplicate": duplicate, "min_gap_violation": min_gap_violation,
        }

    _finalize_row(rt, brand, item, row)
    return {
        "status": row.status, "keyword": keyword, "rank": row.rank,
        "duplicate": duplicate, "min_gap_violation": min_gap_violation,
    }


# =======================================================================
# 노출완→밀려남 2단계 확인 대기열
# =======================================================================

PENDING_CONFIRM_FILE = "exposure_pending_confirm.json"


def pending_confirm_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / "data" / PENDING_CONFIRM_FILE


def _load_pending_all(repo_root: str | Path) -> dict:
    p = pending_confirm_path(repo_root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_pending_all(repo_root: str | Path, data: dict) -> None:
    p = pending_confirm_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(p, data)


def _pending_key(brand: str, keyword: str) -> str:
    return f"{brand}|{keyword}"


def get_pending(repo_root: str | Path, brand: str, keyword: str) -> dict | None:
    return _load_pending_all(repo_root).get(_pending_key(brand, keyword))


def set_pending(repo_root: str | Path, brand: str, item: dict, cfg: dict, now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    min_m = float(cfg.get("pending_confirm_min_minutes", 5))
    max_m = float(cfg.get("pending_confirm_max_minutes", 10))
    due_at = now + random.uniform(min_m, max_m) * 60
    data = _load_pending_all(repo_root)
    entry = {"brand": brand, "item": item, "since": now, "due_at": due_at, "first_status": "pushed"}
    data[_pending_key(brand, item["keyword"])] = entry
    _write_pending_all(repo_root, data)
    return entry


def clear_pending(repo_root: str | Path, brand: str, keyword: str) -> None:
    data = _load_pending_all(repo_root)
    data.pop(_pending_key(brand, keyword), None)
    _write_pending_all(repo_root, data)


def due_pending(repo_root: str | Path, now: float | None = None) -> list[dict]:
    """지금 재확인할 때가 된(`due_at` 지남) 대기 목록."""
    now = now if now is not None else time.time()
    data = _load_pending_all(repo_root)
    return [e for e in data.values() if float(e.get("due_at", 0)) <= now]


# =======================================================================
# 작업자 간 배타(중복 검사 방지) — 2026-09-24 5차: 공유 큐로 대체
#
# 1~3차는 파일 기반 `claim_inflight`/`complete_inflight`(mkdir 잠금 +
# `exposure_inflight.json`)로 "진행 중" 표시를 남겼다. 4차에서 작업자별
# 정렬 캐시를 키우자 그 표시만으로는 부족해졌고(정렬 캐시 자체가 프로세스별
# 경쟁을 낳음), 5차에서 정렬·선점을 통째로 공유 sqlite 표
# (`exposure_queue`, `v2r/store/exposure_queue_store.py`)로 옮기면서 이
# 파일 기반 선점 표시는 더 이상 필요 없어져 제거했다 — 선점은 이제
# `exposure_priority.next_priority_batch`(배치 선점, 내부적으로
# `exposure_queue_store.claim_batch`)와, 2단계 확인 대기 재확인용
# `exposure_queue_store.claim_specific`이 표의 `claimed_by`/`claimed_at`
# 칸으로 직접 한다. 검사 정상 완료는 `exposure_queue_store.mark_done`,
# 실패(예외)는 `release_claim`을 쓴다(아래 `run_worker` 참고).
# =======================================================================


# =======================================================================
# 작업자 메모리 큐 — n=1로 매번 우선순위 조회하던 것을 배치로 바꾼다
# (docs/reports/exposure-speed-2026-09-24.md 6절). 한 번에 여러 개 뽑아
# 작업자 메모리에 두고 하나씩 소비하며, `claim_inflight`로 선점 실패한
# 항목은 건너뛴다. 큐가 비거나(선점 실패로 다 걸러진 경우 포함) TTL이
# 지나면 `exposure_priority.next_priority_batch`를 다시 부른다.
# =======================================================================


class _BrandQueueState:
    __slots__ = ("items", "expires_at", "consumed", "stale_skipped")

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.expires_at: float = 0.0
        self.consumed = 0
        self.stale_skipped = 0


class WorkerQueue:
    """브랜드별 배치를 메모리에 들고 하나씩 내준다.

    2026-09-24 4차(실측 8) — 작업자가 매 반복마다 브랜드를 바꿔 도는데(중복
    재검사 방지를 위해 브랜드 고정을 되돌린 ab32ce5), 이전 구현은 브랜드
    하나짜리 큐 한 개만 들고 있어 브랜드가 바뀔 때마다(사실상 매번) 배치를
    통째로 버리고 새로 조회했다 — 로그 실측으로 확인(`타이밍 큐소비`가
    항상 "소비=1 ... 남은채로재조회=9"). 브랜드별로 큐를 따로 둬서, 5개
    브랜드를 순환해도 각 브랜드의 배치(기본 10개)가 실제로 다 소비될 때까지
    유지되게 고쳤다.

    2026-09-24 5차 — `next_priority_batch`가 이제 공유 큐(`exposure_queue`
    표)에서 **이미 이 작업자 앞으로 선점(claim)까지 끝낸** 항목을 돌려준다
    (`exposure_priority.next_priority_batch`/`exposure_queue_store.
    claim_batch`, 원자적 sqlite 트랜잭션). 그래서 이 클래스는 더 이상
    `claim_inflight`를 부르지 않는다 — 배치를 받은 시점에 이미 선점이
    끝나 있다. `take()`는 검사 직전 DB 단건 확인(`is_due_now`, 3차 그대로
    유지)만 한 번 더 해서, 큐 갱신(최대 `queue_refresh_sec`, 기본 600초
    전) 이후 이 키워드가 그 사이 다른 경로(2단계 확인 재검사 등)로 이미
    검사됐으면 건너뛴다."""

    def __init__(self, batch_size: int = 10, ttl_sec: float = 120.0):
        self.batch_size = max(1, int(batch_size))
        self.ttl_sec = float(ttl_sec)
        self._by_brand: dict[str, _BrandQueueState] = {}

    def _state(self, brand: str) -> _BrandQueueState:
        return self._by_brand.setdefault(brand, _BrandQueueState())

    def _needs_refill(self, state: _BrandQueueState, now: float) -> bool:
        return now >= state.expires_at or not state.items

    def refill_if_needed(self, rt: Any, brand: str, worker_id: int, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        state = self._state(brand)
        if not self._needs_refill(state, now):
            return
        from v2r.knowledge import exposure_priority

        if state.expires_at > 0:
            log.info(
                "타이밍 큐소비 브랜드=%s 소비=%s 직전확인제외=%s 남은채로재조회=%s",
                brand, state.consumed, state.stale_skipped, len(state.items),
            )
        t0 = time.perf_counter()
        state.items = list(
            exposure_priority.next_priority_batch(rt, brand, n=self.batch_size, worker_id=str(worker_id))
        )
        log.info(
            "타이밍 큐조회 브랜드=%s round_trip=%.2fs 배치=%s",
            brand, time.perf_counter() - t0, len(state.items),
        )
        state.expires_at = now + self.ttl_sec
        state.consumed = 0
        state.stale_skipped = 0

    def take(self, rt: Any, brand: str, worker_id: int, now: float | None = None) -> dict | None:
        """이 브랜드 큐(이미 이 작업자 앞으로 선점된 배치)에서, 검사 직전 DB
        단건 확인(`exposure_priority.is_due_now`, 2026-09-24 3차)까지
        통과하는 첫 항목을 꺼내 돌려준다.

        직전 확인에서 "이미 검사됐다"고 나온 항목은 선점을 풀고(
        `exposure_queue_store.release_claim`) 다음 항목을 시도한다. 이
        브랜드 큐가 다 비면 새로 조회하지 않고 `None`을 돌려준다 — 호출자가
        다른 브랜드로 넘어갔다가 다음에 이 브랜드로 돌아오면 그때 TTL·빈 큐
        조건으로 재조회된다.

        2026-09-25 9차 — 실제로 넘기기(반환) 직전에
        `exposure_queue_store.renew_claim`으로 선점 시각을 "지금"으로
        되돌린다. 배치(기본 10개)를 순서대로 처리하다 보면 뒤쪽 항목은
        선점된 지 몇 분 지나서야 처리되는데, 그게 `CLAIM_TTL_SECONDS`
        (180초)를 넘으면 DB 쪽에서는 이미 "죽은 작업자 것"으로 보여 다른
        작업자가 같은 키워드를 또 선점해 동시에 검사하는 사고가 났다
        (실측 9차 — 21시간 가동 중 두 작업자가 같은 키워드를 초 단위로
        209번 동시 검사)."""
        from v2r.knowledge import exposure_priority
        from v2r.knowledge.keyword_exposure import _norm
        from v2r.store import exposure_queue_store as qstore

        self.refill_if_needed(rt, brand, worker_id, now)
        state = self._state(brand)
        while state.items:
            candidate = state.items.pop(0)
            keyword_norm = _norm(candidate.get("keyword", ""))
            qstore.renew_claim(rt.conn, brand, keyword_norm, str(worker_id))
            if not exposure_priority.is_due_now(rt, brand, candidate):
                qstore.release_claim(rt.conn, brand, keyword_norm)
                state.stale_skipped += 1
                continue
            state.consumed += 1
            return candidate
        return None


def run_worker(worker_id: int, brands: list[str] | None = None, max_iterations: int | None = None) -> None:
    """작업자 1개 메인 루프 — 브라우저 1개 상주, 브랜드를 돌며 우선순위 큐에서 계속 뽑는다."""
    from playwright.sync_api import sync_playwright

    from v2r.engine.context import Runtime
    from v2r.knowledge import exposure_priority
    from v2r.knowledge.keyword_exposure import known_brands, launch_chromium

    rt = Runtime.open()
    cfg = exposure_priority.load_config(rt.settings.repo_root)
    brand_list = brands or known_brands(rt)
    if not brand_list:
        log.warning("작업자 %s: 브랜드 없음, 종료", worker_id)
        return

    delay_min = float(cfg.get("delay_min_sec", 6))
    delay_max = float(cfg.get("delay_max_sec", 10))
    block_streak_limit = int(cfg.get("worker_block_streak_limit", 3))
    rest_minutes = float(cfg.get("worker_rest_minutes", 15))
    expected_workers = int(cfg.get("workers", 2))
    global_rest_minutes = float(cfg.get("global_block_rest_minutes", 30))

    cookies_path = Path(rt.settings.repo_root) / "data" / "naver_cookies.json"
    storage_state = str(cookies_path) if cookies_path.exists() else None

    update_worker_state(rt.settings.repo_root, worker_id, processed_delta=0)

    # 2026-09-24 7차 — "이 러너 실행 안에서 중복"을 재기 위한 기준 시각.
    # 프로세스가 실제로 시작된 순간(재시작 시각)이며, 상태 파일에 남아 있을
    # 수 있는 예전 `started_at`(재사용될 수 있음)과는 별개로 항상 지금
    # 새로 잰다.
    run_started_at = datetime.now(timezone.utc)

    priority_cfg = cfg.get("priority", {}) if isinstance(cfg.get("priority"), dict) else {}
    worker_queue = WorkerQueue(
        batch_size=int(priority_cfg.get("batch_size", 10)),
        ttl_sec=float(priority_cfg.get("universe_cache_sec", 120)),
    )

    unknown_streak = 0
    brand_idx = 0
    iterations = 0
    # 후보 글 상세 확인(judge_keyword_exposure 내부)은 자기만의 sync_playwright()를
    # 새로 연다 — 이 스레드(메인, 상주 브라우저 보유)에서 그대로 부르면 중첩
    # 호출로 실패한다(judge_once 문서 참고). 전담 스레드 1개로 격리한다.
    confirm_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix=f"exposure-confirm-{worker_id}"
    )
    with sync_playwright() as pw:
        browser = launch_chromium(pw, headless=True)
        context = browser.new_context(
            storage_state=storage_state,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 950},
        )
        try:
            while max_iterations is None or iterations < max_iterations:
                # 전체 작업자가 동시에 휴식 중이면(다른 작업자가 이미 전역
                # 정지를 기록했을 수 있음) 그 정지가 끝날 때까지 이 작업자도 쉰다.
                remaining = global_pause_remaining(rt.settings.repo_root)
                if remaining > 0:
                    log.warning("작업자 %s: 전체 정지 중, %.0f초 남음", worker_id, remaining)
                    time.sleep(min(remaining, 60.0))
                    continue

                iterations += 1
                from v2r.knowledge.keyword_exposure import _norm as _norm_kw
                from v2r.store import exposure_queue_store as qstore

                # 밀려남 2차 확인 대기 중인 키워드가 때(5~10분) 됐으면 그걸 먼저
                # 처리한다 — 우선순위 등급과 무관하게 시간이 생명인 재확인.
                # 두 경로 모두 공유 큐(`exposure_queue` 표)의 원자적 선점으로
                # 같은 키워드를 다른 작업자와 동시에 집지 않게 막는다
                # (2026-09-24 5차 — 파일 기반 claim_inflight를 대체).
                brand = None
                item = None
                due = due_pending(rt.settings.repo_root)
                for entry in due:
                    e_brand, e_item = entry["brand"], entry["item"]
                    if qstore.claim_specific(
                        rt.conn, e_brand, _norm_kw(e_item["keyword"]), e_item["keyword"], e_item, str(worker_id)
                    ):
                        brand, item = e_brand, e_item
                        break
                if item is None:
                    # 2026-09-24 — 브랜드 고정(커밋 de22603)을 실측했더니 처리량은
                    # 늘었지만(약 1,012건/시) 267건 중 180건(67%)이 같은 키워드
                    # 중복 재검사였다(6-4절) — 되돌리고 브랜드를 계속 순환한다.
                    # 4차(캐시 TTL 600초·공유 파일)로도 중복이 19.4%까지 남았던
                    # 근본 원인(작업자 프로세스마다 따로 정렬해 앞쪽을 다툼)은
                    # 5차에서 공유 큐(`exposure_queue` 표 + 원자적 선점)로 고쳤다
                    # (docs/reports/exposure-queue-cache-2026-09-24.md "5차" 참고)
                    # — 브랜드 순환 자체는 그대로 유지한다(우선순위 규칙 일부이자
                    # 한 브랜드에 쏠리지 않게 하는 장치).
                    brand = brand_list[brand_idx % len(brand_list)]
                    brand_idx += 1
                    item = worker_queue.take(rt, brand, worker_id)
                    if item is None:
                        time.sleep(2.0)
                        continue

                try:
                    result = process_one(
                        rt, context, brand, item, cfg, executor=confirm_executor, run_started_at=run_started_at
                    )
                except Exception:
                    # 검사 자체가 실패(예외)했으면 선점을 완전히 풀어 다른
                    # 작업자가 바로 다시 집을 수 있게 한다 — "완료" 표시를
                    # 남기면 실제로 검사가 안 됐는데도 배제되어 버린다.
                    qstore.release_claim(rt.conn, brand, _norm_kw(item["keyword"]))
                    raise
                # 2026-09-24 5차 — 정상 완료는 공유 큐 표에 `done_at`을 남긴다
                # (`exposure_queue_store.mark_done`). 다음 갱신(기본 600초)
                # 때까지, 또는 다시 등급에 들 때까지 이 키워드가 배치에서
                # 빠진다.
                qstore.mark_done(rt.conn, brand, _norm_kw(item["keyword"]))
                update_worker_state(
                    rt.settings.repo_root, worker_id, processed_delta=1, last_keyword=item["keyword"],
                    duplicate=bool(result.get("duplicate")),
                    min_gap_violation=bool(result.get("min_gap_violation")),
                )
                unknown_streak = unknown_streak + 1 if result["status"] == "unknown" else 0
                if unknown_streak >= block_streak_limit:
                    resting_until = time.time() + rest_minutes * 60
                    update_worker_state(rt.settings.repo_root, worker_id, resting_until=resting_until)
                    log.warning("작업자 %s: 연속 %s건 미확인, %s분 휴식", worker_id, unknown_streak, rest_minutes)
                    # 이 작업자를 쉬게 기록한 직후, 전체(설정된 작업자 수)가 다
                    # 동시에 쉬는 중인지 확인해 전역 30분 정지를 남긴다.
                    global_until = maybe_set_global_pause(
                        rt.settings.repo_root, expected_workers, global_rest_minutes
                    )
                    sleep_target = max(resting_until, global_until or 0.0)
                    time.sleep(max(0.0, sleep_target - time.time()))
                    unknown_streak = 0
                    update_worker_state(rt.settings.repo_root, worker_id, resting_until=None)
                time.sleep(random.uniform(delay_min, delay_max))
        finally:
            context.close()
            browser.close()
            rt.close()
    confirm_executor.shutdown(wait=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="노출 확인 작업자 1개")
    parser.add_argument("--worker-id", type=int, required=True)
    parser.add_argument("--brands", type=str, default="")
    parser.add_argument("--max-iterations", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    brands = [b.strip() for b in args.brands.split(",") if b.strip()] or None
    run_worker(args.worker_id, brands=brands, max_iterations=args.max_iterations)
    return 0


__all__ = [
    "should_stop_scrolling",
    "fetch_integrated_search_dom_resident",
    "state_path",
    "update_worker_state",
    "maybe_set_global_pause",
    "global_pause_remaining",
    "is_alive",
    "judge_once",
    "process_one",
    "pending_confirm_path",
    "get_pending",
    "set_pending",
    "clear_pending",
    "due_pending",
    "CSV_WRITE_MIN_INTERVAL_SEC",
    "maybe_write_exposure_csv",
    "WorkerQueue",
    "run_worker",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
