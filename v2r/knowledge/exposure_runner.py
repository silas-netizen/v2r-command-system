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
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def update_worker_state(
    repo_root: str | Path,
    worker_id: int,
    *,
    processed_delta: int = 0,
    last_keyword: str | None = None,
    resting_until: float | None = None,
    started_at: str | None = None,
) -> dict:
    """작업자 상태 갱신(다른 프로세스와 동시에 써도 마지막 쓰기가 이긴다 —
    작업자별 키가 나뉘어 있어 충돌해도 서로 덮어쓰지 않는다)."""
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
    elapsed_h = max(
        1e-6,
        (time.time() - _to_epoch(w.get("started_at", now_iso_utc()))) / 3600.0,
    )
    w["rate_per_hour"] = round(int(w.get("processed", 0)) / elapsed_h, 1)
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


def _finalize_row(rt: Any, brand: str, item: dict, row: Any) -> None:
    """판정을 확정해 DB append + 시트 배치 큐(기존 함수 호출만)."""
    from v2r.knowledge.keyword_exposure import _enqueue_sheet_row, write_exposure_csv
    from v2r.store import keyword_exposure_store as store

    store.save(rt.conn, row.as_row())
    # `next_priority_batch`의 last_checked(DB)는 캐시하지 않고 매번 새로 읽으므로
    # 이 저장은 다음 조회에 바로 반영된다(같은 키워드 연속 재선택 방지, 단위
    # 시험 `test_next_priority_batch_같은_키워드_연속_두번_안뽑힘` 참고).
    # universe 캐시(시트·발굴 CSV, `exposure_priority.invalidate_universe_cache`)는
    # 검사 결과와 무관해 여기서 비우지 않는다 — 매 건 저장마다 비우면 배치
    # 캐시 효과가 사라진다.
    _enqueue_sheet_row(rt, brand, item, row)
    try:
        write_exposure_csv(rt, brand)
    except Exception as exc:  # pragma: no cover - 방어용
        log.warning("노출 CSV 갱신 실패(%s): %s", brand, exc)


def _latest_status(conn: Any, brand: str, keyword: str) -> str:
    # checked_at은 초 단위라 같은 초 안에 두 번 저장되면 값이 같을 수 있다
    # (테스트, 또는 확인 즉시 재확인) — rowid로 동률을 깬다(나중에 쓴 쪽 우선).
    row = conn.execute(
        "SELECT status FROM keyword_exposure WHERE brand = ? AND keyword = ? "
        "ORDER BY checked_at DESC, rowid DESC LIMIT 1",
        (brand, keyword),
    ).fetchone()
    return str(row["status"]) if row else ""


def process_one(
    rt: Any, context: Any, brand: str, item: dict, cfg: dict, executor: "concurrent.futures.ThreadPoolExecutor | None" = None
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
    """
    keyword = item["keyword"]
    prev_status = _latest_status(rt.conn, brand, keyword)
    pending = get_pending(rt.settings.repo_root, brand, keyword)

    row = judge_once(rt, context, brand, item, cfg, executor=executor)

    if row.status == "pushed" and (prev_status == "exposed" or pending is not None):
        if pending is None:
            # 노출완 → 밀려남 첫 관측 — 바로 확정하지 않고 대기만 남긴다(DB 미기록).
            set_pending(rt.settings.repo_root, brand, item, cfg)
            return {"status": "pending_confirm", "keyword": keyword}
        # 대기 중이던 키워드의 재확인 — 이번에도 밀려남이면 확정.
        clear_pending(rt.settings.repo_root, brand, keyword)
        _finalize_row(rt, brand, item, row)
        return {"status": row.status, "keyword": keyword, "rank": row.rank, "confirmed": True}

    if pending is not None:
        # 대기 중이었는데 이번엔 밀려남이 아님(exposed로 되돌아옴) — 일시 변동,
        # 대기만 지우고 DB는 안 건드린다(직전 exposed 행이 이미 최신).
        clear_pending(rt.settings.repo_root, brand, keyword)
        if row.status != "exposed":
            _finalize_row(rt, brand, item, row)
        return {"status": row.status, "keyword": keyword, "rank": row.rank, "false_alarm_cleared": True}

    _finalize_row(rt, brand, item, row)
    return {"status": row.status, "keyword": keyword, "rank": row.rank}


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
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


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
# 작업자 간 배타(중복 검사 방지) — 2026-09-24 코디네이터 지적
#
# 작업자 3개가 각자 독립된 rt.conn으로 next_priority_batch를 부르니, 셋 다
# 아직 아무도 저장하기 전에 같은(등급 1위) 키워드를 동시에 집어 실제로
# 같은 키워드를 2~3번 따로 검색하는 낭비가 있었다(예: 02:30:16/19/19 "치루수술
# 회복기간" 3연속). 검사 시작 직전 "진행 중" 표시를 남기고, 배치에서 이미
# 진행 중인 항목은 건너뛴다. 여러 프로세스가 동시에 파일을 읽고 쓰는 경합
# 자체는 파일 잠금(디렉터리 생성 기반, 원자적)으로 막는다.
# =======================================================================

INFLIGHT_FILE = "exposure_inflight.json"
#: 이 시간(초)이 지난 진행 중 표시는 죽은 작업자의 것으로 보고 무시한다
#: (한 건 검사에 보통 10~40초, 넉넉히 잡음).
INFLIGHT_TTL_SECONDS = 180


def inflight_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / "data" / INFLIGHT_FILE


def _inflight_lock_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / "data" / (INFLIGHT_FILE + ".lock")


def _with_inflight_lock(repo_root: str | Path, fn: Any, timeout: float = 2.0) -> Any:
    """`os.mkdir`은 원자적이라(먼저 만든 프로세스만 성공) 여러 프로세스 간
    간이 잠금으로 쓴다. 최대 `timeout`초 재시도, 그래도 못 잡으면(죽은 잠금
    의심) 강제로 지우고 한 번 더 시도한다."""
    lock_dir = _inflight_lock_dir(repo_root)
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            if time.time() >= deadline:
                try:
                    os.rmdir(lock_dir)
                except OSError:
                    pass
                continue
            time.sleep(0.02)
    try:
        return fn()
    finally:
        try:
            os.rmdir(lock_dir)
        except OSError:
            pass


def _load_inflight(repo_root: str | Path) -> dict:
    p = inflight_path(repo_root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_inflight(repo_root: str | Path, data: dict) -> None:
    p = inflight_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _inflight_key(brand: str, keyword: str) -> str:
    return f"{brand}|{keyword}"


def is_inflight(repo_root: str | Path, brand: str, keyword: str, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    entry = _load_inflight(repo_root).get(_inflight_key(brand, keyword))
    if not entry:
        return False
    return (now - float(entry.get("claimed_at", 0))) < INFLIGHT_TTL_SECONDS


def claim_inflight(
    repo_root: str | Path, brand: str, keyword: str, worker_id: int, now: float | None = None
) -> bool:
    """지금부터 이 키워드를 이 작업자가 검사한다고 표시. 다른 작업자가 이미
    (만료 전) 진행 중이면 `False`(이 작업자는 다른 후보를 골라야 함)."""
    now = now if now is not None else time.time()
    key = _inflight_key(brand, keyword)

    def _do() -> bool:
        data = _load_inflight(repo_root)
        existing = data.get(key)
        if existing and (now - float(existing.get("claimed_at", 0))) < INFLIGHT_TTL_SECONDS:
            return False
        data[key] = {"worker_id": worker_id, "claimed_at": now}
        _write_inflight(repo_root, data)
        return True

    return _with_inflight_lock(repo_root, _do)


def release_inflight(repo_root: str | Path, brand: str, keyword: str) -> None:
    key = _inflight_key(brand, keyword)

    def _do() -> None:
        data = _load_inflight(repo_root)
        data.pop(key, None)
        _write_inflight(repo_root, data)

    _with_inflight_lock(repo_root, _do)


# =======================================================================
# 작업자 메모리 큐 — n=1로 매번 우선순위 조회하던 것을 배치로 바꾼다
# (docs/reports/exposure-speed-2026-09-24.md 6절). 한 번에 여러 개 뽑아
# 작업자 메모리에 두고 하나씩 소비하며, `claim_inflight`로 선점 실패한
# 항목은 건너뛴다. 큐가 비거나(선점 실패로 다 걸러진 경우 포함) TTL이
# 지나면 `exposure_priority.next_priority_batch`를 다시 부른다.
# =======================================================================


class WorkerQueue:
    """브랜드 하나의 우선순위 배치를 메모리에 들고 하나씩 내준다."""

    def __init__(self, batch_size: int = 10, ttl_sec: float = 120.0):
        self.batch_size = max(1, int(batch_size))
        self.ttl_sec = float(ttl_sec)
        self.items: list[dict] = []
        self.brand: str | None = None
        self.expires_at: float = 0.0

    def _needs_refill(self, brand: str, now: float) -> bool:
        return brand != self.brand or now >= self.expires_at or not self.items

    def refill_if_needed(self, rt: Any, brand: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if not self._needs_refill(brand, now):
            return
        from v2r.knowledge import exposure_priority

        t0 = time.perf_counter()
        self.items = list(exposure_priority.next_priority_batch(rt, brand, n=self.batch_size))
        log.info(
            "타이밍 우선순위조회 브랜드=%s sheet_read=%.2fs 배치=%s",
            brand, time.perf_counter() - t0, len(self.items),
        )
        self.brand = brand
        self.expires_at = now + self.ttl_sec

    def take(self, rt: Any, brand: str, worker_id: int, now: float | None = None) -> dict | None:
        """큐에서 `claim_inflight`로 선점에 성공하는 첫 항목을 꺼내 돌려준다.

        선점 실패한 항목(다른 작업자가 방금 집음)은 버리고 다음 항목을
        시도한다. 큐가 (선점 실패로) 다 비면 새로 조회하지 않고 `None`을
        돌려준다 — 호출자가 잠시 쉬었다 다음 틱에 다시 부르면 그때
        TTL·빈 큐 조건으로 재조회된다."""
        self.refill_if_needed(rt, brand, now)
        while self.items:
            candidate = self.items.pop(0)
            if claim_inflight(rt.settings.repo_root, brand, candidate["keyword"], worker_id):
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

                # 밀려남 2차 확인 대기 중인 키워드가 때(5~10분) 됐으면 그걸 먼저
                # 처리한다 — 우선순위 등급과 무관하게 시간이 생명인 재확인.
                # 두 경로 모두 claim_inflight로 같은 키워드를 다른 작업자와
                # 동시에 집지 않게 막는다(2026-09-24 — 작업자 3개가 저장 전에
                # 같은 1등급 키워드를 2~3번 중복 검사하던 문제 수정).
                brand = None
                item = None
                due = due_pending(rt.settings.repo_root)
                for entry in due:
                    if claim_inflight(rt.settings.repo_root, entry["brand"], entry["item"]["keyword"], worker_id):
                        brand, item = entry["brand"], entry["item"]
                        break
                if item is None:
                    brand = brand_list[brand_idx % len(brand_list)]
                    brand_idx += 1
                    # 배치를 메모리 큐에 두고 하나씩 소비한다(매번 n=1로 조회하던
                    # 것을 바꿈 — 조회 자체도 브랜드별 TTL 캐시를 쓴다).
                    item = worker_queue.take(rt, brand, worker_id)
                    if item is None:
                        time.sleep(2.0)
                        continue

                try:
                    result = process_one(rt, context, brand, item, cfg, executor=confirm_executor)
                finally:
                    release_inflight(rt.settings.repo_root, brand, item["keyword"])
                update_worker_state(rt.settings.repo_root, worker_id, processed_delta=1, last_keyword=item["keyword"])
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
    "inflight_path",
    "is_inflight",
    "claim_inflight",
    "release_inflight",
    "WorkerQueue",
    "run_worker",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
