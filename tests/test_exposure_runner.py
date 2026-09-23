"""노출 확인 러너 — 스크롤 조기 종료·상태 파일·우선순위 큐 순서.

설계: docs/reports/exposure-speed-plan-2026-09-23.md. 판정 규칙(keyword_exposure.py)
은 손대지 않았으므로 여기선 러너 쪽 새 로직만 검증한다.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from v2r.knowledge import exposure_priority, exposure_runner
from v2r.knowledge import keyword_exposure as ke
from v2r.store import keyword_exposure_store as store
from v2r.store.db import now_iso

from tests.test_engine import make_runtime


def test_should_stop_scrolling_카드없으면_최대까지():
    # 카드가 한 번도 안 잡히면 마지막 라운드까지 절대 멈추지 않는다
    heights = [100, 100, 100]
    assert not exposure_runner.should_stop_scrolling(heights, False, 2, max_rounds=20, same_height_rounds=2)


def test_should_stop_scrolling_카드있고_높이_연속동일하면_중단():
    heights = [100, 200, 300, 300]
    assert exposure_runner.should_stop_scrolling(heights, True, 3, max_rounds=20, same_height_rounds=2)


def test_should_stop_scrolling_카드있어도_아직_변하는중이면_계속():
    heights = [100, 200, 300]
    assert not exposure_runner.should_stop_scrolling(heights, True, 2, max_rounds=20, same_height_rounds=2)


def test_should_stop_scrolling_마지막라운드면_무조건_중단():
    heights = [100]
    assert exposure_runner.should_stop_scrolling(heights, False, 19, max_rounds=20, same_height_rounds=2)


def test_state_file_round_trip(tmp_path):
    exposure_runner.update_worker_state(tmp_path, 0, processed_delta=3, last_keyword="키워드1")
    exposure_runner.update_worker_state(tmp_path, 1, processed_delta=1, last_keyword="키워드2")
    state = exposure_runner._load_state(tmp_path)
    assert state["workers"]["0"]["processed"] == 3
    assert state["workers"]["0"]["last_keyword"] == "키워드1"
    assert state["workers"]["1"]["processed"] == 1
    assert exposure_runner.is_alive(tmp_path)


def test_is_alive_없으면_false(tmp_path):
    assert not exposure_runner.is_alive(tmp_path)


def test_전체작업자_동시휴식이면_전역정지_기록(tmp_path):
    now = 1_000_000.0
    exposure_runner.update_worker_state(tmp_path, 0, processed_delta=1)
    exposure_runner.update_worker_state(tmp_path, 1, processed_delta=1)
    # 작업자 0만 쉬는 중 — 아직 전역 정지 아님
    exposure_runner.update_worker_state(tmp_path, 0, resting_until=now + 900)
    assert exposure_runner.maybe_set_global_pause(tmp_path, 2, 30, now=now) is None
    assert exposure_runner.global_pause_remaining(tmp_path, now=now) == 0.0

    # 작업자 1도 쉬는 중 — 이제 전원 동시 휴식, 전역 30분 정지 기록
    exposure_runner.update_worker_state(tmp_path, 1, resting_until=now + 900)
    until = exposure_runner.maybe_set_global_pause(tmp_path, 2, 30, now=now)
    assert until == now + 30 * 60
    assert exposure_runner.global_pause_remaining(tmp_path, now=now) == 30 * 60
    assert exposure_runner.global_pause_remaining(tmp_path, now=now + 31 * 60) == 0.0


def test_전역정지_이미있으면_연장안함(tmp_path):
    now = 2_000_000.0
    exposure_runner.update_worker_state(tmp_path, 0, resting_until=now + 900)
    exposure_runner.update_worker_state(tmp_path, 1, resting_until=now + 900)
    first = exposure_runner.maybe_set_global_pause(tmp_path, 2, 30, now=now)
    # 5분 뒤 다시 불러도(다른 작업자가 또 휴식 조건에 걸림) 처음 정지 시각을 유지한다
    second = exposure_runner.maybe_set_global_pause(tmp_path, 2, 30, now=now + 300)
    assert first == second


def test_전역정지_작업자_상태모르면_설정안함(tmp_path):
    exposure_runner.update_worker_state(tmp_path, 0, resting_until=time.time() + 900)
    # 작업자 1은 아직 상태 파일에 없음 — 전원 휴식인지 알 수 없으므로 정지 안 걸림
    assert exposure_runner.maybe_set_global_pause(tmp_path, 2, 30) is None


def test_priority_tier_미확인이_1순위():
    now = datetime.now(timezone.utc)
    cfg = {"exposed_recheck_hours": 24, "pushed_recheck_hours": 48, "pushed_low_recheck_hours": 168}
    tier, _ = exposure_priority.priority_tier({"keyword": "새키워드"}, {}, set(), cfg, 10.0, now)
    assert tier == 1


def test_priority_tier_최근발행이_2순위():
    now = datetime.now(timezone.utc)
    cfg = {"exposed_recheck_hours": 24, "pushed_recheck_hours": 48, "pushed_low_recheck_hours": 168}
    last_checked = {"최근키워드": {"checked_at": (now - timedelta(hours=100)).isoformat(), "status": "pushed"}}
    tier, _ = exposure_priority.priority_tier(
        {"keyword": "최근키워드", "volume": 0}, last_checked, {"최근키워드"}, cfg, 10.0, now
    )
    assert tier == 2


def test_priority_tier_노출완_주기전이면_제외():
    now = datetime.now(timezone.utc)
    cfg = {"exposed_recheck_hours": 24, "pushed_recheck_hours": 48, "pushed_low_recheck_hours": 168}
    last_checked = {"키워드": {"checked_at": (now - timedelta(hours=1)).isoformat(), "status": "exposed"}}
    tier, _ = exposure_priority.priority_tier({"keyword": "키워드"}, last_checked, set(), cfg, 10.0, now)
    assert tier == 99


def test_priority_tier_노출완_주기지나면_3순위():
    now = datetime.now(timezone.utc)
    cfg = {"exposed_recheck_hours": 24, "pushed_recheck_hours": 48, "pushed_low_recheck_hours": 168}
    last_checked = {"키워드": {"checked_at": (now - timedelta(hours=25)).isoformat(), "status": "exposed"}}
    tier, _ = exposure_priority.priority_tier({"keyword": "키워드"}, last_checked, set(), cfg, 10.0, now)
    assert tier == 3


def test_priority_tier_밀려남_상위검색량이_4순위_하위가_5순위():
    now = datetime.now(timezone.utc)
    cfg = {"exposed_recheck_hours": 24, "pushed_recheck_hours": 48, "pushed_low_recheck_hours": 168}
    last_checked = {
        "상위": {"checked_at": (now - timedelta(hours=50)).isoformat(), "status": "pushed"},
        "하위": {"checked_at": (now - timedelta(hours=200)).isoformat(), "status": "pushed"},
    }
    tier_top, _ = exposure_priority.priority_tier({"keyword": "상위", "volume": 100}, last_checked, set(), cfg, 10.0, now)
    tier_low, _ = exposure_priority.priority_tier({"keyword": "하위", "volume": 1}, last_checked, set(), cfg, 10.0, now)
    assert tier_top == 4
    assert tier_low == 5


def test_next_priority_batch_같은_키워드_연속_두번_안뽑힘(tmp_path, monkeypatch):
    """2026-09-24 지시 — 옛 next_cycle_batch가 같은 키워드를 4분 간격으로
    두 번 뽑던 결함(더마팩토리·착상혈·양압기) 재발 방지. 검사 직후 저장한
    `checked_at`이 바로 다음 호출에 반영돼, 재검사 주기 전에는 다시 안 뽑힌다."""
    rt = make_runtime(tmp_path)
    universe = [
        {"keyword": "더마팩토리", "cafe": "마이카페", "article_url": "", "t0_status": "", "candidate_title_norm": "", "volume": 50},
        {"keyword": "착상혈", "cafe": "마이카페", "article_url": "", "t0_status": "", "candidate_title_norm": "", "volume": 30},
        {"keyword": "양압기", "cafe": "마이카페", "article_url": "", "t0_status": "", "candidate_title_norm": "", "volume": 10},
    ]
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: universe)

    picked_first = exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
    assert len(picked_first) == 1
    first_keyword = picked_first[0]["keyword"]

    # 러너가 실제로 하는 것처럼: 검사 직후 바로 DB에 결과를 저장한다.
    row = ke.ExposureRow("테스트브랜드", first_keyword, "마이카페", "", None, "pushed", now_iso())
    store.save(rt.conn, row.as_row())

    # 같은 목록으로 곧바로 다시 뽑아도 방금 검사한 키워드는 다시 안 나온다
    # (아직 재검사 주기(기본 48시간/7일)가 안 지났으므로 99등급 → 제외).
    for _ in range(len(universe) - 1):
        picked_next = exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
        assert picked_next, "미확인 키워드가 남아 있는 동안은 빈 배치면 안 된다"
        assert picked_next[0]["keyword"] != first_keyword
        row = ke.ExposureRow("테스트브랜드", picked_next[0]["keyword"], "마이카페", "", None, "pushed", now_iso())
        store.save(rt.conn, row.as_row())

    # 이제 전부 방금 검사됨 — 주기 전이라 아무것도 안 뽑힌다(연속 재검사 방지)
    assert exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1) == []


# =======================================================================
# 노출완 → 밀려남 2단계 확인 (2026-09-24, 비만도 계산기 23:31 일시 변동 사례)
# =======================================================================

def _fake_row(brand, keyword, status, cafe="마이카페"):
    return ke.ExposureRow(brand, keyword, cafe, "", None, status, now_iso())


def test_노출완에서_밀려남_첫관측은_보류만_DB안바뀜(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    brand, keyword = "테스트브랜드", "비만도계산기"
    store.save(rt.conn, _fake_row(brand, keyword, "exposed").as_row())

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, item, cfg: _fake_row(b, item["keyword"], "pushed"))
    item = {"keyword": keyword, "cafe": "마이카페", "t0_status": "", "volume": 0}

    result = exposure_runner.process_one(rt, object(), brand, item, {})
    assert result["status"] == "pending_confirm"

    assert exposure_runner._latest_status(rt.conn, brand, keyword) == "exposed"  # DB는 아직 안 바뀜
    pending = exposure_runner.get_pending(rt.settings.repo_root, brand, keyword)
    assert pending is not None and pending["item"]["keyword"] == keyword


def test_재확인에서도_밀려남이면_확정(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    brand, keyword = "테스트브랜드", "비만도계산기"
    store.save(rt.conn, _fake_row(brand, keyword, "exposed").as_row())
    item = {"keyword": keyword, "cafe": "마이카페", "t0_status": "", "volume": 0}

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg: _fake_row(b, i["keyword"], "pushed"))
    exposure_runner.process_one(rt, object(), brand, item, {})  # 1차: 보류

    result = exposure_runner.process_one(rt, object(), brand, item, {})  # 2차: 또 밀려남
    assert result["status"] == "pushed"
    assert result.get("confirmed") is True
    assert exposure_runner._latest_status(rt.conn, brand, keyword) == "pushed"
    assert exposure_runner.get_pending(rt.settings.repo_root, brand, keyword) is None


def test_재확인에서_다시_노출완이면_일시변동으로_취소(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    brand, keyword = "테스트브랜드", "비만도계산기"
    store.save(rt.conn, _fake_row(brand, keyword, "exposed").as_row())
    item = {"keyword": keyword, "cafe": "마이카페", "t0_status": "", "volume": 0}

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg: _fake_row(b, i["keyword"], "pushed"))
    exposure_runner.process_one(rt, object(), brand, item, {})  # 1차: 보류

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg: _fake_row(b, i["keyword"], "exposed"))
    result = exposure_runner.process_one(rt, object(), brand, item, {})  # 2차: 다시 노출완
    assert result.get("false_alarm_cleared") is True
    assert exposure_runner._latest_status(rt.conn, brand, keyword) == "exposed"
    assert exposure_runner.get_pending(rt.settings.repo_root, brand, keyword) is None


def test_due_pending_시간지나야만_뽑힘(tmp_path):
    item = {"keyword": "키워드", "cafe": "", "t0_status": "", "volume": 0}
    exposure_runner.set_pending(tmp_path, "브랜드", item, {"pending_confirm_min_minutes": 5, "pending_confirm_max_minutes": 5}, now=1000.0)
    assert exposure_runner.due_pending(tmp_path, now=1000.0) == []
    due = exposure_runner.due_pending(tmp_path, now=1000.0 + 301)
    assert len(due) == 1
    assert due[0]["item"]["keyword"] == "키워드"
