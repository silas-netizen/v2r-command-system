"""노출 확인 러너 — 스크롤 조기 종료·상태 파일·우선순위 큐 순서.

설계: docs/reports/exposure-speed-plan-2026-09-23.md. 판정 규칙(keyword_exposure.py)
은 손대지 않았으므로 여기선 러너 쪽 새 로직만 검증한다.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from v2r.knowledge import exposure_priority, exposure_runner
from v2r.knowledge import keyword_exposure as ke
from v2r.store import exposure_queue_store as qstore
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


def _cfg():
    return {"exposed_recheck_hours": 6, "pushed_min_gap_hours": 12}


def test_priority_tier_노출완_주기지나면_1순위():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    last_checked = {"키워드": {"checked_at": "2026-09-24T02:00:00+00:00", "status": "exposed"}}
    tier, _ = exposure_priority.priority_tier({"keyword": "키워드"}, last_checked, {}, _cfg(), 10.0, now)
    assert tier == 1


def test_priority_tier_노출완_주기전이면_제외():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    last_checked = {"키워드": {"checked_at": "2026-09-24T10:00:00+00:00", "status": "exposed"}}
    tier, _ = exposure_priority.priority_tier({"keyword": "키워드"}, last_checked, {}, _cfg(), 10.0, now)
    assert tier == 99


def test_priority_tier_시트G열_노출완은_미검사여도_1순위():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    tier, _ = exposure_priority.priority_tier({"keyword": "시트노출완", "sheet_status": "노출완"}, {}, {}, _cfg(), 10.0, now)
    assert tier == 1
    fresh = {"시트노출완": {"checked_at": "2026-09-24T11:00:00+00:00", "status": "pushed"}}
    tier, _ = exposure_priority.priority_tier({"keyword": "시트노출완", "sheet_status": "노출완"}, fresh, {}, _cfg(), 10.0, now)
    assert tier == 99


def test_priority_tier_최근발행이_2순위():
    """2026-09-24 8차부터 `recent_publish_norm`은 {키워드: 발행시각} 매핑.
    발행 6시간 근방(±2시간) 창 안에서, 최소 간격(90분)도 지나고 이 창에서는
    아직 안 봤으면 2등급이다."""
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    pub_dt = now - timedelta(hours=6)  # 발행 6시간 전 — 6시간 창 활성
    last_checked = {"키워드": {"checked_at": "2026-09-23T00:00:00+00:00", "status": "pushed"}}
    recent = {"키워드": pub_dt}
    tier, _ = exposure_priority.priority_tier({"keyword": "키워드"}, last_checked, recent, _cfg(), 10.0, now)
    assert tier == 2


def test_priority_tier_최근발행_최소간격90분_안이면_제외():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    pub_dt = now - timedelta(hours=6)
    last_checked = {"키워드": {"checked_at": (now - timedelta(minutes=30)).isoformat(), "status": "pushed"}}
    recent = {"키워드": pub_dt}
    tier, _ = exposure_priority.priority_tier({"keyword": "키워드"}, last_checked, recent, _cfg(), 10.0, now)
    assert tier == 99, "최소 간격(90분) 안이면 창이 활성이어도 대상이 아니다"


def test_priority_tier_최근발행_같은창에서_이미검사했으면_제외():
    """8차 — 같은 키워드는 각 창(2h/6h/24h)에서 최대 1회만. 6시간 창이 활성인
    지금, 직전 검사도 그 6시간 창 범위(발행 후 4~8시간) 안이었으면 다시
    안 뽑힌다(다음 창인 24시간까지는 대상 아님)."""
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    pub_dt = now - timedelta(hours=6)
    # 직전 검사가 발행 5시간 뒤(같은 6시간 창 범위 안)였고, 최소 간격(90분)도 지남
    last_checked_at = pub_dt + timedelta(hours=5)
    last_checked = {"키워드": {"checked_at": last_checked_at.isoformat(), "status": "pushed"}}
    recent = {"키워드": pub_dt}
    tier, _ = exposure_priority.priority_tier({"keyword": "키워드"}, last_checked, recent, _cfg(), 10.0, now)
    assert tier == 99, "같은 창(6시간 근방)에서는 최대 1회만 검사해야 한다"


def test_priority_tier_최근발행_다른창이면_다시대상():
    """직전 검사가 2시간 창(발행 후 2시간 근방)에서 있었고, 지금은 6시간
    창이 활성이면(서로 다른 창) 다시 대상이어야 한다."""
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    pub_dt = now - timedelta(hours=6)
    last_checked_at = pub_dt + timedelta(hours=2)  # 2시간 창에서 검사됨
    last_checked = {"키워드": {"checked_at": last_checked_at.isoformat(), "status": "pushed"}}
    recent = {"키워드": pub_dt}
    tier, _ = exposure_priority.priority_tier({"keyword": "키워드"}, last_checked, recent, _cfg(), 10.0, now)
    assert tier == 2


def test_priority_tier_최근발행_창밖이면_2등급아님():
    """발행 후 12시간(2h/6h/24h 어느 창에도 안 걸침) — 2등급이 아니라
    3등급(밀려남·미확인) 규칙으로 넘어가야 한다."""
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    pub_dt = now - timedelta(hours=12)
    last_checked = {"키워드": {"checked_at": "2026-09-23T00:00:00+00:00", "status": "pushed"}}
    recent = {"키워드": pub_dt}
    tier, _ = exposure_priority.priority_tier({"keyword": "키워드"}, last_checked, recent, _cfg(), 10.0, now)
    assert tier == 3


def test_priority_tier_미확인과_밀려남은_3순위_최소간격():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    assert exposure_priority.priority_tier({"keyword": "새키워드"}, {}, {}, _cfg(), 10.0, now)[0] == 3
    old = {"오래": {"checked_at": "2026-09-23T00:00:00+00:00", "status": "pushed"}}
    fresh = {"방금": {"checked_at": "2026-09-24T11:00:00+00:00", "status": "pushed"}}
    assert exposure_priority.priority_tier({"keyword": "오래"}, old, {}, _cfg(), 10.0, now)[0] == 3
    assert exposure_priority.priority_tier({"keyword": "방금"}, fresh, {}, _cfg(), 10.0, now)[0] == 99


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

    # 러너가 실제로 하는 것처럼: 검사 직후 바로 DB에 결과를 저장하고,
    # last_checked 캐시(TTL 20초)도 즉시 갱신한다(2026-09-24 2차,
    # `_finalize_row`가 이렇게 함).
    row = ke.ExposureRow("테스트브랜드", first_keyword, "마이카페", "", None, "pushed", now_iso())
    store.save(rt.conn, row.as_row())
    exposure_priority.mark_checked(rt, "테스트브랜드", first_keyword, row.status, row.checked_at)

    # 같은 목록으로 곧바로 다시 뽑아도 방금 검사한 키워드는 다시 안 나온다
    # (아직 재검사 주기(기본 48시간/7일)가 안 지났으므로 99등급 → 제외).
    for _ in range(len(universe) - 1):
        picked_next = exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
        assert picked_next, "미확인 키워드가 남아 있는 동안은 빈 배치면 안 된다"
        assert picked_next[0]["keyword"] != first_keyword
        row = ke.ExposureRow("테스트브랜드", picked_next[0]["keyword"], "마이카페", "", None, "pushed", now_iso())
        store.save(rt.conn, row.as_row())
        exposure_priority.mark_checked(rt, "테스트브랜드", picked_next[0]["keyword"], row.status, row.checked_at)

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

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, item, cfg, **kw: _fake_row(b, item["keyword"], "pushed"))
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

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg, **kw: _fake_row(b, i["keyword"], "pushed"))
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

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg, **kw: _fake_row(b, i["keyword"], "pushed"))
    exposure_runner.process_one(rt, object(), brand, item, {})  # 1차: 보류

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg, **kw: _fake_row(b, i["keyword"], "exposed"))
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


# =======================================================================
# 최근 발행 키워드 — DB publications × 시트 F열(발행 URL) URL 대조 (2026-09-24)
# =======================================================================

def _insert_publication(conn, url, created_at, status="done", source_key="테스트브랜드"):
    from v2r.store.db import now_iso as _now_iso

    conn.execute(
        "INSERT INTO publications (source_key, row_number, content_hash, status, stage,"
        " source_id, url, account, cafe, menu_id, scheduled_at, created_at, updated_at, board)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source_key, 1, url, status, None, "s1", url, "acc", "마이카페", "", "", created_at, _now_iso(), ""),
    )


def test_publications_recent_pub_times_창안이면_포함(tmp_path):
    """2026-09-24 8차 — `_publications_recent_article_ids`는
    `_publications_recent_pub_times`로 바뀌었다(브랜드 필터 추가, 반환값도
    글 번호 집합 -> {글 번호: 발행 시각} 매핑). 창(2h/6h/24h) 판정은 더 이상
    여기서 하지 않는다 — `max_age_hours` 안의 모든 발행을 넓게 돌려주고,
    창 활성 여부는 `priority_tier`가 매번 새로 계산한다."""
    rt = make_runtime(tmp_path)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    _insert_publication(
        rt.conn,
        "https://cafe.naver.com/ca-fe/cafes/111/articles/222?query=1",
        "2026-09-24T10:00:00+00:00",
    )
    pub_times = exposure_priority._publications_recent_pub_times(rt.conn, "테스트브랜드", now, 26.0)
    assert set(pub_times) == {"222"}
    assert pub_times["222"] == datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)


def test_publications_recent_pub_times_범위밖이면_제외(tmp_path):
    rt = make_runtime(tmp_path)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    _insert_publication(rt.conn, "https://cafe.naver.com/ca-fe/cafes/111/articles/333", "2026-09-20T00:00:00+00:00")
    pub_times = exposure_priority._publications_recent_pub_times(rt.conn, "테스트브랜드", now, 26.0)
    assert pub_times == {}


def test_publications_recent_pub_times_실패상태는_제외(tmp_path):
    rt = make_runtime(tmp_path)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    _insert_publication(
        rt.conn, "https://cafe.naver.com/ca-fe/cafes/111/articles/444", "2026-09-24T10:00:00+00:00", status="failed"
    )
    pub_times = exposure_priority._publications_recent_pub_times(rt.conn, "테스트브랜드", now, 26.0)
    assert pub_times == {}


def test_publications_recent_pub_times_다른브랜드는_제외(tmp_path):
    """2026-09-24 8차 — 오탐 원인 수정: 다른 브랜드(또는 일상 글 —
    `v2r.engine.publish.brand_source_keys`가 정의하는 "브랜드 시트 이름이
    아닌 source_key")의 발행은 이 브랜드의 "최근 발행"에 안 섞인다."""
    rt = make_runtime(tmp_path)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    _insert_publication(
        rt.conn, "https://cafe.naver.com/ca-fe/cafes/111/articles/555", "2026-09-24T10:00:00+00:00",
        source_key="다른브랜드",
    )
    pub_times = exposure_priority._publications_recent_pub_times(rt.conn, "테스트브랜드", now, 26.0)
    assert pub_times == {}


def test_recent_publish_keywords_from_db_시트F열_URL대조로_H열키워드(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    _insert_publication(
        rt.conn,
        "https://cafe.naver.com/ca-fe/cafes/111/articles/999/",  # 끝 슬래시
        "2026-09-24T10:00:00+00:00",
    )
    fake_sheet_rows = [
        {
            "카페": "마이카페",
            "발행 URL": "https://cafe.naver.com/mycafe/999?ref=abc",  # 쿼리스트링 다름
            "노출 상태": "밀려남",
            "키워드": "다이어트보조제",
        },
        {
            "카페": "마이카페",
            "발행 URL": "https://cafe.naver.com/mycafe/555",
            "노출 상태": "밀려남",
            "키워드": "관련없는키워드",
        },
    ]
    monkeypatch.setattr(
        "v2r.knowledge.keyword_exposure._sheet_rows", lambda brand, cfg, xlsx_path: fake_sheet_rows
    )
    out = exposure_priority._recent_publish_keywords_from_db(rt, "테스트브랜드", now, [2.0, 6.0, 24.0])
    assert set(out) == {"다이어트보조제"}
    assert out["다이어트보조제"] == datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)


def test_recent_publish_keywords_from_db_대조안되면_빈집합(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    # publications가 비어 있으면 시트를 읽으러 가지도 않는다
    called = {"n": 0}

    def _boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("호출되면 안 됨")

    monkeypatch.setattr("v2r.knowledge.keyword_exposure._sheet_rows", _boom)
    out = exposure_priority._recent_publish_keywords_from_db(rt, "테스트브랜드", now, [2.0, 6.0, 24.0])
    assert out == {}
    assert called["n"] == 0


def test_recent_publish_keywords_article_index_휴리스틱_제거됨(tmp_path, monkeypatch):
    """2026-09-24 8차(코디네이터 지시, 7차 실측 후속) — `article_index`
    "제목 맨 앞 낱말" 휴리스틱을 완전히 제거했다. `article_index`는 카페의
    모든 글(V2R 발행이든 자사 카페 일상 글이든 브랜드 구분 없이)을 담고
    있어(`v2r/store/article_index.py`), 오늘 발행된 일상 글의 제목 앞
    낱말이 우연히 universe 키워드와 겹치면 그 키워드가 잘못 "최근 발행"
    으로 잡혔다(7차 실측: 초과 검사 31건 중 27건이 이 경로의 2등급
    오탐). `article_index`에 그럴듯한 매치가 있어도(`FakeArticleIndex`)
    이제 결과에 안 섞이고, DB `publications`(source_key=브랜드) × 시트
    F열 URL 대조 결과만 남아야 한다."""
    rt = make_runtime(tmp_path)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    _insert_publication(
        rt.conn, "https://cafe.naver.com/ca-fe/cafes/111/articles/777", "2026-09-24T10:00:00+00:00"
    )
    fake_sheet_rows = [
        {"카페": "마이카페", "발행 URL": "https://cafe.naver.com/mycafe/777", "키워드": "DB발행키워드"},
    ]
    monkeypatch.setattr(
        "v2r.knowledge.keyword_exposure._sheet_rows", lambda brand, cfg, xlsx_path: fake_sheet_rows
    )

    class FakeArticleIndex:
        def rows_for_cafe(self, cafe):
            return [{"title": "제목키워드 나머지 제목", "published_at": "2026-09-24T10:00:00+00:00"}]

    rt.article_index = FakeArticleIndex()
    out = exposure_priority._recent_publish_keywords(rt, "테스트브랜드", {"마이카페"}, now, [2.0, 6.0, 24.0])
    assert set(out) == {"db발행키워드"}, "article_index 휴리스틱 결과(제목키워드)는 더 이상 섞이면 안 된다"


# =======================================================================
# 작업자 간 배타(중복 검사 방지) — 2026-09-24 5차: 공유 큐(exposure_queue 표)
# `exposure_queue_store.claim_batch`/`claim_specific`/`mark_done`/
# `release_claim`이 옛 파일 기반 `claim_inflight`/`release_inflight`/
# `complete_inflight`를 대체한다.
# =======================================================================

def test_claim_specific_먼저_잡은쪽만_성공(tmp_path):
    rt = make_runtime(tmp_path)
    item = {"keyword": "키워드"}
    assert qstore.claim_specific(rt.conn, "브랜드", "키워드", "키워드", item, "worker0") is True
    # 만료 전에는 다른 작업자가 같은 키워드를 못 잡는다
    assert qstore.claim_specific(rt.conn, "브랜드", "키워드", "키워드", item, "worker1") is False


def test_release_claim_후_다시_잡을수있음(tmp_path):
    rt = make_runtime(tmp_path)
    item = {"keyword": "키워드"}
    qstore.claim_specific(rt.conn, "브랜드", "키워드", "키워드", item, "worker0")
    qstore.release_claim(rt.conn, "브랜드", "키워드")
    assert qstore.claim_specific(rt.conn, "브랜드", "키워드", "키워드", item, "worker1") is True


def test_claim_만료되면_다시_잡을수있음(tmp_path):
    rt = make_runtime(tmp_path)
    item = {"keyword": "키워드"}
    now0 = 1_000_000.0
    qstore.claim_specific(rt.conn, "브랜드", "키워드", "키워드", item, "worker0", now_epoch=now0)
    # TTL(180초) 전 — 여전히 막힘
    assert qstore.claim_specific(rt.conn, "브랜드", "키워드", "키워드", item, "worker1", now_epoch=now0 + 60) is False
    # TTL 지남(죽은 작업자로 간주) — 다른 작업자가 잡을 수 있음
    assert qstore.claim_specific(rt.conn, "브랜드", "키워드", "키워드", item, "worker1", now_epoch=now0 + 200) is True


def test_mark_done_후에는_claim_batch에_안뽑힘(tmp_path):
    rt = make_runtime(tmp_path)
    candidates = [(1, -1.0, "키워드", "키워드", {"keyword": "키워드"})]
    qstore.upsert_candidates(rt.conn, "브랜드", candidates)
    picked = qstore.claim_batch(rt.conn, "브랜드", "worker0", 1)
    assert len(picked) == 1
    qstore.mark_done(rt.conn, "브랜드", "키워드")
    assert qstore.claim_batch(rt.conn, "브랜드", "worker1", 1) == []


def test_claim_batch_동시선점_경쟁없음(tmp_path):
    """같은 키워드를 두 "작업자"가 동시에 claim_batch로 뽑아도 한쪽만
    가져간다 — 5차의 핵심(원자적 UPDATE)."""
    rt = make_runtime(tmp_path)
    candidates = [(1, -1.0, "키워드", "키워드", {"keyword": "키워드"})]
    qstore.upsert_candidates(rt.conn, "브랜드", candidates)
    first = qstore.claim_batch(rt.conn, "브랜드", "worker0", 1)
    second = qstore.claim_batch(rt.conn, "브랜드", "worker1", 1)
    assert len(first) == 1
    assert second == []


# =======================================================================
# universe 캐시(TTL) — exposure-queue-cache-2026-09-24.md
# =======================================================================


def _universe_fixture():
    return [
        {"keyword": "더마팩토리", "cafe": "마이카페", "article_url": "", "t0_status": "", "candidate_title_norm": "", "volume": 50},
        {"keyword": "착상혈", "cafe": "마이카페", "article_url": "", "t0_status": "", "candidate_title_norm": "", "volume": 30},
    ]


def test_universe_캐시_TTL안에는_한번만_조회(tmp_path, monkeypatch):
    exposure_priority.invalidate_universe_cache(None)
    rt = make_runtime(tmp_path)
    calls = {"n": 0}

    def fake_universe(rt_, brand):
        calls["n"] += 1
        return _universe_fixture()

    monkeypatch.setattr(ke, "keyword_universe", fake_universe)

    exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
    exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
    exposure_priority.queue_counts(rt, "테스트브랜드")
    assert calls["n"] == 1, "TTL 안이면 keyword_universe는 한 번만 불려야 한다"


def test_universe_캐시_TTL지나면_다시_조회(tmp_path, monkeypatch):
    """프로세스 메모리 캐시가 TTL 지나 만료되면 다시 조회해야 한다 — 4차에서
    추가된 파일 캐시(작업자 프로세스 간 공유)는 이 시험의 관심사가 아니므로
    비활성화해(항상 미스로) 순수 프로세스 캐시 동작만 본다.

    2026-09-24 6차 — 큐가 이미 한 번 채워진 뒤(콜드 스타트 아님) TTL이
    지나 갱신할 때는 백그라운드 스레드로 넘어간다(코디네이터 지시 —
    갱신하는 동안 호출자를 막지 않기 위해). 시험에서는 `threading.Thread`를
    "바로 그 자리에서 실행"하는 가짜로 바꿔 동기적으로 검증한다."""

    class _SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    exposure_priority.invalidate_universe_cache(None)
    monkeypatch.setattr(exposure_priority, "_read_universe_file_cache", lambda rt_, brand, ttl: None)
    monkeypatch.setattr(exposure_priority.threading, "Thread", _SyncThread)
    rt = make_runtime(tmp_path)
    monkeypatch.setattr(exposure_priority, "_open_refresh_runtime", lambda: rt)
    # 가짜 스레드가 끝나며 rt.close()를 부르면 시험이 이어서 쓰는 rt.conn이
    # 닫혀 버리니, 이 시험 안에서는 close를 무시한다.
    monkeypatch.setattr(rt, "close", lambda: None)
    calls = {"n": 0}

    def fake_universe(rt_, brand):
        calls["n"] += 1
        return _universe_fixture()

    monkeypatch.setattr(ke, "keyword_universe", fake_universe)

    now0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(exposure_priority.time, "time", lambda: 1_000_000.0)
    exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1, now=now0)
    assert calls["n"] == 1

    # 기본 TTL(600초, 4차에서 120→600으로 상향) + 여유 지남 — 다시 조회해야 한다
    # (백그라운드 스레드가 가짜 동기 스레드라 이 호출 안에서 바로 끝난다)
    monkeypatch.setattr(exposure_priority.time, "time", lambda: 1_000_000.0 + 650.0)
    exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1, now=now0)
    assert calls["n"] == 2


def test_universe_캐시_브랜드마다_따로(tmp_path, monkeypatch):
    exposure_priority.invalidate_universe_cache(None)
    rt = make_runtime(tmp_path)
    calls = []

    def fake_universe(rt_, brand):
        calls.append(brand)
        return _universe_fixture()

    monkeypatch.setattr(ke, "keyword_universe", fake_universe)

    exposure_priority.next_priority_batch(rt, "브랜드A", n=1)
    exposure_priority.next_priority_batch(rt, "브랜드B", n=1)
    exposure_priority.next_priority_batch(rt, "브랜드A", n=1)
    assert calls == ["브랜드A", "브랜드B"], "브랜드별로 캐시가 따로 유지돼야 한다"


def test_next_priority_batch_공유큐_선점이_같은키워드_다시안뽑음(tmp_path, monkeypatch):
    """2026-09-24 5차 — 정렬은 공유 큐(`exposure_queue` 표)에서 한 번만
    계산되고, `next_priority_batch`는 그 표에서 원자적으로 선점(claim)한다.
    같은 프로세스가 연속으로 불러도(같은 worker_id든 다르든) 이미 선점된
    (완료 전) 키워드는 다시 안 뽑혀야 한다 — 프로세스별 캐시가 아니라 DB
    표 자체가 "누가 뭘 가져갔는지"의 유일한 진실이므로."""
    exposure_priority.invalidate_universe_cache(None)
    rt = make_runtime(tmp_path)
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: _universe_fixture())

    picked = exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
    assert len(picked) == 1
    first_keyword = picked[0]["keyword"]

    picked2 = exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
    assert len(picked2) == 1
    assert picked2[0]["keyword"] != first_keyword


def test_next_priority_batch_mark_done_후에도_큐갱신TTL안이면_다시_안뽑힘(tmp_path, monkeypatch):
    """2026-09-24 5차 — 검사를 마쳐 `mark_done`을 불러도, 큐 갱신 TTL(기본
    600초) 안에서는 그 키워드가 다시 정렬 대상으로 들어오지 않는다(등급
    규칙상 아직 재검사 주기가 안 지났으므로 — `mark_done`은 그저 "이번
    선점을 끝냈다"는 표시일 뿐, 등급 재계산은 다음 갱신 때 한다)."""
    exposure_priority.invalidate_universe_cache(None)
    rt = make_runtime(tmp_path)
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: _universe_fixture())

    picked = exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
    first_keyword = picked[0]["keyword"]

    from v2r.knowledge.keyword_exposure import _norm
    from v2r.store import exposure_queue_store as qstore

    qstore.mark_done(rt.conn, "테스트브랜드", _norm(first_keyword))
    row = ke.ExposureRow("테스트브랜드", first_keyword, "마이카페", "", None, "pushed", now_iso())
    store.save(rt.conn, row.as_row())

    picked2 = exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
    assert picked2, "미확인 키워드가 남아 있는 동안은 빈 배치면 안 된다"
    assert picked2[0]["keyword"] != first_keyword, "DB 직전 확인이 캐시와 무관하게 즉시 반영해야 한다"


def test_is_due_now_직전확인(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    item = {"keyword": "키워드", "cafe": "마이카페", "t0_status": "", "volume": 0}
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: [item])

    # 검사 이력이 없으면 항상 검사해도 된다
    assert exposure_priority.is_due_now(rt, "테스트브랜드", item) is True

    row = ke.ExposureRow("테스트브랜드", "키워드", "마이카페", "", None, "pushed", now_iso())
    store.save(rt.conn, row.as_row())
    # 방금(밀려남 최소 간격 12시간 안) 검사됨 — 지금은 건너뛰어야 한다
    assert exposure_priority.is_due_now(rt, "테스트브랜드", item) is False


def test_invalidate_universe_cache_비우면_다시조회(tmp_path, monkeypatch):
    """`invalidate_universe_cache`는 (5차부터) `_universe_bundle`의 프로세스
    캐시만 비운다 — 공유 큐 갱신 여부는 `exposure_priority.
    maybe_refresh_queue`(TTL 기반)가 따로 결정하므로, 이 시험은
    `_universe_bundle`을 직접 불러 확인한다."""
    exposure_priority.invalidate_universe_cache(None)
    monkeypatch.setattr(exposure_priority, "_read_universe_file_cache", lambda rt_, brand, ttl: None)
    rt = make_runtime(tmp_path)
    calls = {"n": 0}

    def fake_universe(rt_, brand):
        calls["n"] += 1
        return _universe_fixture()

    monkeypatch.setattr(ke, "keyword_universe", fake_universe)
    cfg = {"universe_cache_sec": 600, "top_volume_percentile": 0.7, "recent_publish_hours": [2, 6, 24]}
    now = datetime.now(timezone.utc)

    exposure_priority._universe_bundle(rt, "테스트브랜드", cfg, now)
    assert calls["n"] == 1
    exposure_priority.invalidate_universe_cache(rt, "테스트브랜드")
    exposure_priority._universe_bundle(rt, "테스트브랜드", cfg, now)
    assert calls["n"] == 2


# =======================================================================
# WorkerQueue — n=1 매번 조회를 배치로 바꿈
# =======================================================================


def test_worker_queue_배치를_하나씩_소비(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    batch = [{"keyword": f"키워드{i}", "cafe": "마이카페"} for i in range(3)]
    calls = {"n": 0}

    def fake_batch(rt_, brand, n, worker_id="0", now=None):
        calls["n"] += 1
        return list(batch)

    monkeypatch.setattr(exposure_priority, "next_priority_batch", fake_batch)

    wq = exposure_runner.WorkerQueue(batch_size=3, ttl_sec=120.0)
    seen = []
    for _ in range(3):
        item = wq.take(rt, "테스트브랜드", worker_id=0)
        assert item is not None
        seen.append(item["keyword"])

    assert seen == ["키워드0", "키워드1", "키워드2"]
    assert calls["n"] == 1, "큐가 남아 있는 동안은 다시 조회하지 않아야 한다"


def test_worker_queue_비면_다시조회(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    calls = {"n": 0}

    def fake_batch(rt_, brand, n, worker_id="0", now=None):
        calls["n"] += 1
        return [{"keyword": f"배치{calls['n']}", "cafe": "마이카페"}]

    monkeypatch.setattr(exposure_priority, "next_priority_batch", fake_batch)

    wq = exposure_runner.WorkerQueue(batch_size=1, ttl_sec=120.0)
    item1 = wq.take(rt, "테스트브랜드", worker_id=0)
    item2 = wq.take(rt, "테스트브랜드", worker_id=0)
    assert item1["keyword"] != item2["keyword"]
    assert calls["n"] == 2


def test_worker_queue_TTL지나면_다시조회(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    calls = {"n": 0}

    def fake_batch(rt_, brand, n, worker_id="0", now=None):
        calls["n"] += 1
        return [{"keyword": f"배치{calls['n']}-{i}", "cafe": "마이카페"} for i in range(5)]

    monkeypatch.setattr(exposure_priority, "next_priority_batch", fake_batch)

    wq = exposure_runner.WorkerQueue(batch_size=5, ttl_sec=60.0)
    wq.take(rt, "테스트브랜드", worker_id=0, now=1_000_000.0)
    assert calls["n"] == 1
    # 아직 큐에 항목이 남아 있어도 TTL이 지났으면 다시 조회한다
    wq.take(rt, "테스트브랜드", worker_id=0, now=1_000_000.0 + 61.0)
    assert calls["n"] == 2


# =======================================================================
# 2026-09-24 5차 — WorkerQueue의 직전 확인(is_due_now) 건너뛰기.
#
# 선점 실패("다른 작업자가 먼저 집음") 시나리오는 이제 `next_priority_batch`
# (내부적으로 `exposure_queue_store.claim_batch`, 원자적 sqlite 트랜잭션)의
# 몫이라(단위 시험은 위 `test_claim_batch_동시선점_경쟁없음`) WorkerQueue
# 자체에는 더 이상 "선점 실패" 개념이 없다. WorkerQueue.take()에 남은 유일한
# 필터는 검사 직전 DB 단건 확인(`is_due_now`, 3차부터 유지)이다.
# =======================================================================


def test_worker_queue_직전확인에서_걸리면_다음후보(tmp_path, monkeypatch):
    """다른 경로(예: 2단계 확인 재검사)로 방금(최소 간격 안) 검사돼 DB에
    저장된 키워드가 배치에 섞여 있어도(공유 큐 갱신이 그 사이 안 됐다고
    흉내), take()가 직전 DB 확인에서 걸러 다음 후보로 넘어가야 한다."""
    rt = make_runtime(tmp_path)
    row = ke.ExposureRow("테스트브랜드", "방금검사됨", "마이카페", "", None, "pushed", now_iso())
    store.save(rt.conn, row.as_row())

    batch = [
        {"keyword": "방금검사됨", "cafe": "마이카페", "volume": 0},
        {"keyword": "새것", "cafe": "마이카페", "volume": 0},
    ]
    monkeypatch.setattr(exposure_priority, "next_priority_batch", lambda rt_, brand, n, worker_id="0", now=None: list(batch))

    wq = exposure_runner.WorkerQueue(batch_size=2, ttl_sec=120.0)
    item = wq.take(rt, "테스트브랜드", worker_id=0)
    assert item["keyword"] == "새것"
    assert wq._state("테스트브랜드").stale_skipped == 1


def test_update_worker_state_중복률_집계(tmp_path):
    exposure_runner.update_worker_state(tmp_path, 0, processed_delta=1, duplicate=False)
    exposure_runner.update_worker_state(tmp_path, 0, processed_delta=1, duplicate=True)
    exposure_runner.update_worker_state(tmp_path, 1, processed_delta=1, duplicate=False)

    state = exposure_runner._load_state(tmp_path)
    w0 = state["workers"]["0"]
    assert w0["checked_count"] == 2
    assert w0["duplicate_count"] == 1
    assert w0["duplicate_rate"] == 0.5

    totals = state["duplicate_totals"]
    assert totals["checked_count"] == 3
    assert totals["duplicate_count"] == 1
    assert round(totals["duplicate_rate"], 4) == round(1 / 3, 4)


def test_update_worker_state_min_gap_violation_집계_분모공유(tmp_path):
    """2026-09-24 7차 — `duplicate`·`min_gap_violation`을 같은 호출에 함께
    넘기면 `checked_count`(분모)를 이중으로 세지 않는다."""
    exposure_runner.update_worker_state(tmp_path, 0, processed_delta=1, duplicate=False, min_gap_violation=True)
    exposure_runner.update_worker_state(tmp_path, 0, processed_delta=1, duplicate=True, min_gap_violation=False)

    state = exposure_runner._load_state(tmp_path)
    w0 = state["workers"]["0"]
    assert w0["checked_count"] == 2, "duplicate·min_gap_violation을 같이 넘겨도 분모는 한 번만 는다"
    assert w0["duplicate_count"] == 1
    assert w0["min_gap_violation_count"] == 1
    assert w0["min_gap_violation_rate"] == 0.5

    totals = state["duplicate_totals"]
    assert totals["checked_count"] == 2
    assert totals["min_gap_violation_count"] == 1


def test_process_one_최소간격안_재검사는_min_gap_violation_True(tmp_path, monkeypatch):
    """2026-09-24 7차 — 옛 `duplicate`(최소 간격 문턱값)는 이제
    `min_gap_violation`으로 이름이 바뀌었다. 등급 규칙 재사용
    (`priority_tier`)으로 판정하므로 `keyword_universe`를 몽키패치해
    "최근 발행" 예외가 아닌 평범한 3등급 키워드로 만든다."""
    rt = make_runtime(tmp_path)
    brand, keyword = "테스트브랜드", "키워드"
    store.save(rt.conn, ke.ExposureRow(brand, keyword, "마이카페", "", None, "pushed", now_iso()).as_row())
    item = {"keyword": keyword, "cafe": "마이카페", "t0_status": "", "volume": 0}
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand_: [item])

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg, **kw: _fake_row(b, i["keyword"], "pushed"))
    result = exposure_runner.process_one(rt, object(), brand, item, {})
    assert result["min_gap_violation"] is True


def test_process_one_주기지난_재검사는_min_gap_violation_False(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    brand, keyword = "테스트브랜드", "키워드"
    old_checked = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
    store.save(rt.conn, ke.ExposureRow(brand, keyword, "마이카페", "", None, "pushed", old_checked).as_row())
    item = {"keyword": keyword, "cafe": "마이카페", "t0_status": "", "volume": 0}
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand_: [item])

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg, **kw: _fake_row(b, i["keyword"], "pushed"))
    result = exposure_runner.process_one(rt, object(), brand, item, {})
    assert result["min_gap_violation"] is False


def test_process_one_최근발행_다른창이고_최소간격지나면_위반아님(tmp_path, monkeypatch):
    """2026-09-24 8차 — 7차 실측(10:20 이후 창)에서 중복 초과 검사 31건 중
    27건이 "최근 발행"(2등급) 경로였는데, 그중 대부분은 `article_index`
    오탐(위 `test_recent_publish_keywords_article_index_휴리스틱_제거됨`)
    이었다. 8차부터는 2등급도 (a) 창(2h/6h/24h)마다 최대 1회, (b) 최소
    90분 간격을 지킨다 — 직전 검사가 **다른 창**이었고 90분도 지났으면
    위반이 아니다."""
    rt = make_runtime(tmp_path)
    brand, keyword = "테스트브랜드", "키워드"
    now = datetime.now(timezone.utc)
    pub_dt = now - timedelta(hours=6)  # 지금은 6시간 창이 활성
    prev_checked_at = pub_dt + timedelta(hours=2)  # 직전 검사는 2시간 창(다른 창)이었음
    store.save(rt.conn, ke.ExposureRow(brand, keyword, "마이카페", "", None, "pushed", prev_checked_at.isoformat()).as_row())
    item = {"keyword": keyword, "cafe": "마이카페", "t0_status": "", "volume": 0}
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand_: [item])
    monkeypatch.setattr(exposure_priority, "_recent_publish_keywords", lambda *a, **kw: {"키워드": pub_dt})

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg, **kw: _fake_row(b, i["keyword"], "pushed"))
    result = exposure_runner.process_one(rt, object(), brand, item, {})
    assert result["min_gap_violation"] is False


def test_process_one_최근발행_같은창이면_위반(tmp_path, monkeypatch):
    """8차 — 직전 검사가 지금과 **같은 창**(6시간 근방)이었으면, 최소 간격
    (90분)을 지켰어도 `min_gap_violation`이어야 한다(같은 창 최대 1회)."""
    rt = make_runtime(tmp_path)
    brand, keyword = "테스트브랜드", "키워드"
    now = datetime.now(timezone.utc)
    pub_dt = now - timedelta(hours=6)
    prev_checked_at = pub_dt + timedelta(hours=5)  # 같은 6시간 창 범위(4~8시간) 안
    store.save(rt.conn, ke.ExposureRow(brand, keyword, "마이카페", "", None, "pushed", prev_checked_at.isoformat()).as_row())
    item = {"keyword": keyword, "cafe": "마이카페", "t0_status": "", "volume": 0}
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand_: [item])
    monkeypatch.setattr(exposure_priority, "_recent_publish_keywords", lambda *a, **kw: {"키워드": pub_dt})

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg, **kw: _fake_row(b, i["keyword"], "pushed"))
    result = exposure_runner.process_one(rt, object(), brand, item, {})
    assert result["min_gap_violation"] is True


def test_process_one_duplicate는_이번실행_시작시각_이후만(tmp_path, monkeypatch):
    """2026-09-24 7차 — `duplicate`는 이제 "이 러너 실행(run_started_at
    이후) 안에서 두 번째 이상 검사"만 뜻한다. 재시작 전(run_started_at
    이전) 검사는 아무리 직전이어도 중복으로 안 센다."""
    rt = make_runtime(tmp_path)
    brand, keyword = "테스트브랜드", "키워드"
    item = {"keyword": keyword, "cafe": "마이카페", "t0_status": "", "volume": 0}
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand_: [item])
    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg, **kw: _fake_row(b, i["keyword"], "pushed"))

    # 재시작 전 검사 하나만 있음 — run_started_at을 그 이후로 잡으면 중복 아님
    old_checked = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store.save(rt.conn, ke.ExposureRow(brand, keyword, "마이카페", "", None, "pushed", old_checked).as_row())
    run_started_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    result = exposure_runner.process_one(rt, object(), brand, item, {}, run_started_at=run_started_at)
    assert result["duplicate"] is False, "재시작 이전 검사는 이번 실행의 중복이 아니다"

    # 이번 실행 안에서 방금 또 검사됨 — 이제는 중복
    result2 = exposure_runner.process_one(rt, object(), brand, item, {}, run_started_at=run_started_at)
    assert result2["duplicate"] is True, "이번 실행 시작 이후 검사가 있으면 다음 검사는 중복이다"


def test_process_one_run_started_at_없으면_duplicate_항상_False(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    brand, keyword = "테스트브랜드", "키워드"
    item = {"keyword": keyword, "cafe": "마이카페", "t0_status": "", "volume": 0}
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand_: [item])
    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg, **kw: _fake_row(b, i["keyword"], "pushed"))
    store.save(rt.conn, ke.ExposureRow(brand, keyword, "마이카페", "", None, "pushed", now_iso()).as_row())

    result = exposure_runner.process_one(rt, object(), brand, item, {})
    assert result["duplicate"] is False


# =======================================================================
# 2026-09-24 4차 — 브랜드 순환 중에도 배치가 실제로 소비되는지(실측 8:
# "소비=1 ... 남은채로재조회=9"가 매번 나오던 결함), universe 파일 캐시
# =======================================================================


def test_worker_queue_브랜드순환해도_배치가_유지됨(tmp_path, monkeypatch):
    """5개 브랜드를 매 반복 바꿔 가며 take()해도, 각 브랜드 배치(10개)가
    실제로 다 소비될 때까지 재조회하지 않아야 한다(2026-09-24 4차 —
    이전엔 브랜드가 바뀔 때마다 큐를 통째로 버려 사실상 매번 재조회했다)."""
    rt = make_runtime(tmp_path)
    calls: dict[str, int] = {}

    def fake_batch(rt_, brand, n, worker_id="0", now=None):
        calls[brand] = calls.get(brand, 0) + 1
        return [{"keyword": f"{brand}-{calls[brand]}-{i}", "cafe": "마이카페", "volume": 0} for i in range(n)]

    monkeypatch.setattr(exposure_priority, "next_priority_batch", fake_batch)

    wq = exposure_runner.WorkerQueue(batch_size=10, ttl_sec=600.0)
    brands = ["브랜드A", "브랜드B", "브랜드C", "브랜드D", "브랜드E"]
    # 브랜드를 매번 바꿔 가며 각 브랜드당 10개씩(배치 크기만큼) 소비한다
    for _round in range(10):
        for b in brands:
            item = wq.take(rt, b, worker_id=0)
            assert item is not None

    # 브랜드마다 배치(10개)가 정확히 한 번씩만 조회됐어야 한다(10라운드×10개=100건을
    # 배치 1번으로 다 소비) — 이전 결함이면 브랜드당 10회(라운드마다) 조회됐을 것.
    assert calls == {b: 1 for b in brands}


def test_universe_bundle_파일캐시로_프로세스간_공유(tmp_path, monkeypatch):
    """2026-09-24 4차 — 작업자 프로세스 A가 만든 universe 파일 캐시
    (`data/exposure_universe_cache/<브랜드>.json`)를, 프로세스 메모리 캐시가
    없는(A와는 다른 프로세스라고 흉내낸) 다른 호출도 네트워크 없이 재사용해야
    한다."""
    rt = make_runtime(tmp_path)
    calls = {"n": 0}

    def fake_universe(rt_, brand):
        calls["n"] += 1
        return [{"keyword": "키워드", "cafe": "마이카페", "volume": 0}]

    monkeypatch.setattr(ke, "keyword_universe", fake_universe)
    exposure_priority.invalidate_universe_cache(None)

    # 1차: 실제로 읽고 파일 캐시에 씀
    bundle1 = exposure_priority._universe_bundle(
        rt, "테스트브랜드", {"universe_cache_sec": 600, "universe_file_cache_sec": 600}, datetime.now(timezone.utc)
    )
    assert calls["n"] == 1
    cache_file = exposure_priority._universe_file_cache_path(rt, "테스트브랜드")
    assert cache_file.exists()

    # 2차: 프로세스 메모리 캐시를 비워(다른 프로세스인 것처럼) 다시 불러도
    # 파일 캐시가 있으면 keyword_universe를 다시 안 부른다
    exposure_priority.invalidate_universe_cache(rt, "테스트브랜드")
    bundle2 = exposure_priority._universe_bundle(
        rt, "테스트브랜드", {"universe_cache_sec": 600, "universe_file_cache_sec": 600}, datetime.now(timezone.utc)
    )
    assert calls["n"] == 1, "파일 캐시가 있으면 네트워크(keyword_universe)를 다시 안 불러야 한다"
    assert bundle2["universe"] == bundle1["universe"]


def test_universe_cache_sec_기본값_600(tmp_path):
    cfg = exposure_priority.load_config(tmp_path)
    assert cfg["priority"]["universe_cache_sec"] == 600


# =======================================================================
# 2026-09-24 6차 — write_exposure_csv 스로틀·백그라운드화,
# exposure_queue 갱신 청크·워터마크 삭제(시트 1만 행대 실측 후)
# =======================================================================


def test_maybe_write_exposure_csv_스로틀(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(
        exposure_runner, "_write_exposure_csv_in_background", lambda brand: calls.__setitem__("n", calls["n"] + 1)
    )
    exposure_runner._CSV_LAST_WRITE_MONO.clear()

    started1 = exposure_runner.maybe_write_exposure_csv("테스트브랜드", min_interval_sec=60.0)
    started2 = exposure_runner.maybe_write_exposure_csv("테스트브랜드", min_interval_sec=60.0)
    assert started1 is True
    assert started2 is False, "60초 안에 다시 부르면 스레드를 또 띄우면 안 된다"


def test_maybe_write_exposure_csv_브랜드마다_따로(monkeypatch):
    monkeypatch.setattr(exposure_runner, "_write_exposure_csv_in_background", lambda brand: None)
    exposure_runner._CSV_LAST_WRITE_MONO.clear()

    assert exposure_runner.maybe_write_exposure_csv("브랜드A", min_interval_sec=60.0) is True
    assert exposure_runner.maybe_write_exposure_csv("브랜드B", min_interval_sec=60.0) is True


def test_upsert_candidates_청크로_나눠도_전부_들어감(tmp_path):
    """2026-09-24 6차 — 큰 브랜드(1만 행대)를 흉내내 청크 크기(여기선 3)보다
    많은 후보를 넣어도 전부 반영돼야 한다."""
    rt = make_runtime(tmp_path)
    candidates = [
        (3, float(-i), f"키워드{i}", f"키워드{i}", {"keyword": f"키워드{i}", "volume": i})
        for i in range(10)
    ]
    qstore.upsert_candidates(rt.conn, "테스트브랜드", candidates, chunk_size=3)
    counts = qstore.queue_counts(rt.conn, "테스트브랜드")
    assert counts["waiting"] == 10


def test_upsert_candidates_워터마크로_안쓰인_행_삭제(tmp_path):
    """진행 중인 선점이 아닌, 이번 갱신에 없는 키워드는 지워진다(워터마크
    삭제 — NOT IN 나열 없이 enqueued_at 기준)."""
    rt = make_runtime(tmp_path)
    first = [(3, -1.0, "옛키워드", "옛키워드", {"keyword": "옛키워드"})]
    qstore.upsert_candidates(rt.conn, "테스트브랜드", first, now_epoch=1_000_000.0)

    second = [(3, -1.0, "새키워드", "새키워드", {"keyword": "새키워드"})]
    qstore.upsert_candidates(rt.conn, "테스트브랜드", second, now_epoch=1_000_100.0)

    counts = qstore.queue_counts(rt.conn, "테스트브랜드")
    assert counts["total"] == 1, "이번 갱신에 없는 옛 행은 지워져야 한다"
    picked = qstore.claim_batch(rt.conn, "테스트브랜드", "worker0", 1, now_epoch=1_000_100.0)
    assert picked[0]["keyword"] == "새키워드"


def test_upsert_candidates_워터마크가_진행중인_선점은_안지움(tmp_path):
    """갱신 시점에 다른 작업자가 아직 검사 중인(선점, 미완료) 키워드는,
    이번 후보 목록에 없어도(예: 그 사이 시트에서 빠짐) 지워지면 안 된다 —
    검사 결과를 저장할 곳이 없어지기 때문."""
    rt = make_runtime(tmp_path)
    first = [(3, -1.0, "검사중", "검사중", {"keyword": "검사중"})]
    qstore.upsert_candidates(rt.conn, "테스트브랜드", first, now_epoch=2_000_000.0)
    qstore.claim_batch(rt.conn, "테스트브랜드", "worker0", 1, now_epoch=2_000_000.0)

    # 다음 갱신에는 "검사중"이 후보에 없다(등급이 바뀌었다고 흉내) —
    # 하지만 아직 완료(done_at) 전이고 선점 TTL(180초) 안이므로 지워지면 안 된다.
    qstore.upsert_candidates(rt.conn, "테스트브랜드", [], now_epoch=2_000_010.0)
    counts = qstore.queue_counts(rt.conn, "테스트브랜드")
    assert counts["claimed"] == 1, "진행 중인 선점은 갱신에도 살아 있어야 한다"


def test_do_refresh_queue_last_checked_캐시_건너뜀(tmp_path, monkeypatch):
    """2026-09-24 6차 — `_do_refresh_queue`는 매번 last_checked 캐시를
    무효화하고 새로 읽는다(방금 완료된 검사가 갱신에 안 보이는 걸 막기 위해)."""
    rt = make_runtime(tmp_path)
    item = {"keyword": "키워드", "cafe": "마이카페", "t0_status": "", "volume": 0}
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: [item])

    calls = {"n": 0}
    real_invalidate = exposure_priority.invalidate_last_checked_cache

    def spy_invalidate(rt_, brand=None):
        calls["n"] += 1
        return real_invalidate(rt_, brand)

    monkeypatch.setattr(exposure_priority, "invalidate_last_checked_cache", spy_invalidate)
    exposure_priority._do_refresh_queue(rt, "테스트브랜드", dict(exposure_priority._DEFAULT_PRIORITY), datetime.now(timezone.utc))
    assert calls["n"] == 1
