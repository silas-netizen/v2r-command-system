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

def process_one(rt: Any, context: Any, brand: str, item: dict, cfg: dict) -> dict:
    """키워드 하나: 조기 종료 스크롤 → 기존 판정 함수 → DB/시트 큐. 반환은 판정 요약."""
    from v2r.knowledge.keyword_exposure import (
        ExposureRow,
        _enqueue_sheet_row,
        judge_keyword_exposure,
        now_iso,
        resolve_search_query,
    )
    from v2r.store import keyword_exposure_store as store

    keyword = item["keyword"]
    query = resolve_search_query(keyword)
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
        row = ExposureRow(brand, keyword, item.get("cafe", ""), "", None, "unknown", now_iso(), item.get("t0_status", ""), query)
        store.save(rt.conn, row.as_row())
        _enqueue_sheet_row(rt, brand, item, row)
        return {"status": "unknown", "keyword": keyword}

    verdict = judge_keyword_exposure(
        rt,
        brand,
        keyword,
        cookies_path=cookies_path,
        dom_html=html,
        search_query=query,
        article_index=getattr(rt, "article_index", None),
    )
    row = ExposureRow(
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
    store.save(rt.conn, row.as_row())
    _enqueue_sheet_row(rt, brand, item, row)
    try:
        from v2r.knowledge.keyword_exposure import write_exposure_csv

        write_exposure_csv(rt, brand)
    except Exception as exc:  # pragma: no cover - 방어용
        log.warning("노출 CSV 갱신 실패(%s): %s", brand, exc)
    return {"status": row.status, "keyword": keyword, "rank": row.rank}


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

    cookies_path = Path(rt.settings.repo_root) / "data" / "naver_cookies.json"
    storage_state = str(cookies_path) if cookies_path.exists() else None

    update_worker_state(rt.settings.repo_root, worker_id, processed_delta=0)

    unknown_streak = 0
    brand_idx = 0
    iterations = 0
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
                iterations += 1
                brand = brand_list[brand_idx % len(brand_list)]
                brand_idx += 1
                batch = exposure_priority.next_priority_batch(rt, brand, n=1)
                if not batch:
                    time.sleep(2.0)
                    continue
                item = batch[0]
                result = process_one(rt, context, brand, item, cfg)
                update_worker_state(rt.settings.repo_root, worker_id, processed_delta=1, last_keyword=item["keyword"])
                unknown_streak = unknown_streak + 1 if result["status"] == "unknown" else 0
                if unknown_streak >= block_streak_limit:
                    resting_until = time.time() + rest_minutes * 60
                    update_worker_state(rt.settings.repo_root, worker_id, resting_until=resting_until)
                    log.warning("작업자 %s: 연속 %s건 미확인, %s분 휴식", worker_id, unknown_streak, rest_minutes)
                    time.sleep(rest_minutes * 60)
                    unknown_streak = 0
                    update_worker_state(rt.settings.repo_root, worker_id, resting_until=None)
                time.sleep(random.uniform(delay_min, delay_max))
        finally:
            context.close()
            browser.close()
            rt.close()


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
    "is_alive",
    "process_one",
    "run_worker",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
