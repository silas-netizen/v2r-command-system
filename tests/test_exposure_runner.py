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


def _cfg():
    return {"exposed_recheck_hours": 6, "pushed_min_gap_hours": 12}


def test_priority_tier_노출완_주기지나면_1순위():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    last_checked = {"키워드": {"checked_at": "2026-09-24T02:00:00+00:00", "status": "exposed"}}
    tier, _ = exposure_priority.priority_tier({"keyword": "키워드"}, last_checked, set(), _cfg(), 10.0, now)
    assert tier == 1


def test_priority_tier_노출완_주기전이면_제외():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    last_checked = {"키워드": {"checked_at": "2026-09-24T10:00:00+00:00", "status": "exposed"}}
    tier, _ = exposure_priority.priority_tier({"keyword": "키워드"}, last_checked, set(), _cfg(), 10.0, now)
    assert tier == 99


def test_priority_tier_시트G열_노출완은_미검사여도_1순위():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    tier, _ = exposure_priority.priority_tier({"keyword": "시트노출완", "sheet_status": "노출완"}, {}, set(), _cfg(), 10.0, now)
    assert tier == 1
    fresh = {"시트노출완": {"checked_at": "2026-09-24T11:00:00+00:00", "status": "pushed"}}
    tier, _ = exposure_priority.priority_tier({"keyword": "시트노출완", "sheet_status": "노출완"}, fresh, set(), _cfg(), 10.0, now)
    assert tier == 99


def test_priority_tier_최근발행이_2순위():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    last_checked = {"키워드": {"checked_at": "2026-09-23T00:00:00+00:00", "status": "pushed"}}
    tier, _ = exposure_priority.priority_tier({"keyword": "키워드"}, last_checked, {"키워드"}, _cfg(), 10.0, now)
    assert tier == 2


def test_priority_tier_미확인과_밀려남은_3순위_최소간격():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    assert exposure_priority.priority_tier({"keyword": "새키워드"}, {}, set(), _cfg(), 10.0, now)[0] == 3
    old = {"오래": {"checked_at": "2026-09-23T00:00:00+00:00", "status": "pushed"}}
    fresh = {"방금": {"checked_at": "2026-09-24T11:00:00+00:00", "status": "pushed"}}
    assert exposure_priority.priority_tier({"keyword": "오래"}, old, set(), _cfg(), 10.0, now)[0] == 3
    assert exposure_priority.priority_tier({"keyword": "방금"}, fresh, set(), _cfg(), 10.0, now)[0] == 99


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

def _insert_publication(conn, url, created_at, status="done"):
    from v2r.store.db import now_iso as _now_iso

    conn.execute(
        "INSERT INTO publications (source_key, row_number, content_hash, status, stage,"
        " source_id, url, account, cafe, menu_id, scheduled_at, created_at, updated_at, board)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("소스", 1, url, status, None, "s1", url, "acc", "마이카페", "", "", created_at, _now_iso(), ""),
    )


def test_publications_recent_article_ids_창안이면_포함(tmp_path):
    rt = make_runtime(tmp_path)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    # 2시간 전(정확히 창 중앙) — 포함
    _insert_publication(
        rt.conn,
        "https://cafe.naver.com/ca-fe/cafes/111/articles/222?query=1",
        "2026-09-24T10:00:00+00:00",
    )
    ids = exposure_priority._publications_recent_article_ids(rt.conn, now, [2.0, 6.0, 24.0])
    assert ids == {"222"}


def test_publications_recent_article_ids_창밖이면_제외(tmp_path):
    rt = make_runtime(tmp_path)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    _insert_publication(rt.conn, "https://cafe.naver.com/ca-fe/cafes/111/articles/333", "2026-09-24T00:00:00+00:00")
    ids = exposure_priority._publications_recent_article_ids(rt.conn, now, [2.0, 6.0, 24.0])
    assert ids == set()


def test_publications_recent_article_ids_실패상태는_제외(tmp_path):
    rt = make_runtime(tmp_path)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    _insert_publication(
        rt.conn, "https://cafe.naver.com/ca-fe/cafes/111/articles/444", "2026-09-24T10:00:00+00:00", status="failed"
    )
    ids = exposure_priority._publications_recent_article_ids(rt.conn, now, [2.0, 6.0, 24.0])
    assert ids == set()


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
    assert out == {"다이어트보조제"}


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
    assert out == set()
    assert called["n"] == 0


def test_recent_publish_keywords_합집합_article_index와_DB(tmp_path, monkeypatch):
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
    assert out == {"제목키워드", "db발행키워드"}


# =======================================================================
# 작업자 간 배타(중복 검사 방지) — 2026-09-24 코디네이터 지적
# =======================================================================

def test_claim_inflight_먼저_잡은쪽만_성공(tmp_path):
    assert exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드", worker_id=0) is True
    # 만료 전에는 다른 작업자가 같은 키워드를 못 잡는다
    assert exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드", worker_id=1) is False
    assert exposure_runner.is_inflight(tmp_path, "브랜드", "키워드") is True


def test_claim_inflight_release후_다시_잡을수있음(tmp_path):
    exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드", worker_id=0)
    exposure_runner.release_inflight(tmp_path, "브랜드", "키워드")
    assert exposure_runner.is_inflight(tmp_path, "브랜드", "키워드") is False
    assert exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드", worker_id=1) is True


def test_claim_inflight_만료되면_다시_잡을수있음(tmp_path):
    now0 = 1_000_000.0
    exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드", worker_id=0, now=now0)
    # TTL(180초) 전 — 여전히 막힘
    assert exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드", worker_id=1, now=now0 + 60) is False
    # TTL 지남(죽은 작업자로 간주) — 다른 작업자가 잡을 수 있음
    assert exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드", worker_id=1, now=now0 + 200) is True


def test_claim_inflight_다른_키워드는_서로_안막음(tmp_path):
    assert exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드A", worker_id=0) is True
    assert exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드B", worker_id=1) is True


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
    exposure_priority.invalidate_universe_cache(None)
    rt = make_runtime(tmp_path)
    calls = {"n": 0}

    def fake_universe(rt_, brand):
        calls["n"] += 1
        return _universe_fixture()

    monkeypatch.setattr(ke, "keyword_universe", fake_universe)

    now0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(exposure_priority.time, "time", lambda: 1_000_000.0)
    exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1, now=now0)
    assert calls["n"] == 1

    # 기본 TTL(120초) + 여유 지남 — 다시 조회해야 한다
    monkeypatch.setattr(exposure_priority.time, "time", lambda: 1_000_000.0 + 200.0)
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


def test_universe_캐시_last_checked는_mark_checked로_즉시반영(tmp_path, monkeypatch):
    """universe(시트) 캐시와 정렬 캐시가 TTL 안이어도, `mark_checked`로 갱신한
    키워드는 last_checked 캐시(TTL 20초)와 무관하게 같은 키워드가 바로 다시
    뽑히지 않아야 한다(2026-09-24 2차 — last_checked도 캐시하되 즉시 반영)."""
    exposure_priority.invalidate_universe_cache(None)
    exposure_priority.invalidate_last_checked_cache(None)
    exposure_priority.invalidate_sorted_cache(None)
    rt = make_runtime(tmp_path)
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: _universe_fixture())

    picked = exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
    assert len(picked) == 1
    first_keyword = picked[0]["keyword"]

    row = ke.ExposureRow("테스트브랜드", first_keyword, "마이카페", "", None, "pushed", now_iso())
    store.save(rt.conn, row.as_row())
    exposure_priority.mark_checked(rt, "테스트브랜드", first_keyword, row.status, row.checked_at)

    picked2 = exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
    assert len(picked2) == 1
    assert picked2[0]["keyword"] != first_keyword


def test_next_priority_batch_mark_checked_없이도_DB직전확인으로_즉시반영(tmp_path, monkeypatch):
    """2026-09-24 3차 — 재실측(중복 13%, 브랜드 고정 시 67%)에서 프로세스별
    캐시(last_checked 포함)가 다른 프로세스의 검사 결과를 못 보는 게 근본
    원인으로 드러났다. 그래서 `next_priority_batch`는 배치에 넣을 후보마다
    캐시가 아니라 DB 단건 조회(`store.latest_for_keyword`)로 항상 최종
    확인한다 — `mark_checked`를 안 불러도(다른 프로세스가 저장한 것처럼)
    바로 반영돼야 한다."""
    exposure_priority.invalidate_universe_cache(None)
    exposure_priority.invalidate_last_checked_cache(None)
    exposure_priority.invalidate_sorted_cache(None)
    rt = make_runtime(tmp_path)
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: _universe_fixture())

    picked = exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
    first_keyword = picked[0]["keyword"]
    # last_checked 캐시(사람이 안 볼 뿐 여전히 존재)를 먼저 채워 둔다(TTL 20초 시작)
    exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)

    # 다른 작업자 프로세스가 이 키워드를 검사해 저장한 것처럼 흉내낸다
    # (mark_checked를 일부러 안 부름 — 다른 프로세스면 애초에 이 캐시에
    # 손댈 방법이 없다).
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
    exposure_priority.invalidate_universe_cache(None)
    rt = make_runtime(tmp_path)
    calls = {"n": 0}

    def fake_universe(rt_, brand):
        calls["n"] += 1
        return _universe_fixture()

    monkeypatch.setattr(ke, "keyword_universe", fake_universe)

    exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
    exposure_priority.invalidate_universe_cache(rt, "테스트브랜드")
    exposure_priority.next_priority_batch(rt, "테스트브랜드", n=1)
    assert calls["n"] == 2


# =======================================================================
# WorkerQueue — n=1 매번 조회를 배치로 바꿈
# =======================================================================


def test_worker_queue_배치를_하나씩_소비(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    batch = [{"keyword": f"키워드{i}", "cafe": "마이카페"} for i in range(3)]
    calls = {"n": 0}

    def fake_batch(rt_, brand, n, now=None):
        calls["n"] += 1
        return list(batch)

    monkeypatch.setattr(exposure_priority, "next_priority_batch", fake_batch)

    wq = exposure_runner.WorkerQueue(batch_size=3, ttl_sec=120.0)
    seen = []
    for _ in range(3):
        item = wq.take(rt, "테스트브랜드", worker_id=0)
        assert item is not None
        seen.append(item["keyword"])
        exposure_runner.release_inflight(rt.settings.repo_root, "테스트브랜드", item["keyword"])

    assert seen == ["키워드0", "키워드1", "키워드2"]
    assert calls["n"] == 1, "큐가 남아 있는 동안은 다시 조회하지 않아야 한다"


def test_worker_queue_비면_다시조회(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    calls = {"n": 0}

    def fake_batch(rt_, brand, n, now=None):
        calls["n"] += 1
        return [{"keyword": f"배치{calls['n']}", "cafe": "마이카페"}]

    monkeypatch.setattr(exposure_priority, "next_priority_batch", fake_batch)

    wq = exposure_runner.WorkerQueue(batch_size=1, ttl_sec=120.0)
    item1 = wq.take(rt, "테스트브랜드", worker_id=0)
    exposure_runner.release_inflight(rt.settings.repo_root, "테스트브랜드", item1["keyword"])
    item2 = wq.take(rt, "테스트브랜드", worker_id=0)
    assert item1["keyword"] != item2["keyword"]
    assert calls["n"] == 2


def test_worker_queue_TTL지나면_다시조회(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    calls = {"n": 0}

    def fake_batch(rt_, brand, n, now=None):
        calls["n"] += 1
        return [{"keyword": f"배치{calls['n']}-{i}", "cafe": "마이카페"} for i in range(5)]

    monkeypatch.setattr(exposure_priority, "next_priority_batch", fake_batch)

    wq = exposure_runner.WorkerQueue(batch_size=5, ttl_sec=60.0)
    wq.take(rt, "테스트브랜드", worker_id=0, now=1_000_000.0)
    assert calls["n"] == 1
    # 아직 큐에 항목이 남아 있어도 TTL이 지났으면 다시 조회한다
    wq.take(rt, "테스트브랜드", worker_id=0, now=1_000_000.0 + 61.0)
    assert calls["n"] == 2


def test_worker_queue_선점실패한_항목은_건너뜀(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    batch = [{"keyword": "이미잡힘", "cafe": "마이카페"}, {"keyword": "새것", "cafe": "마이카페"}]
    monkeypatch.setattr(
        exposure_priority, "next_priority_batch", lambda rt_, brand, n, now=None: list(batch)
    )
    # 다른 작업자가 먼저 "이미잡힘"을 선점한 상태를 흉내낸다
    exposure_runner.claim_inflight(rt.settings.repo_root, "테스트브랜드", "이미잡힘", worker_id=9)

    wq = exposure_runner.WorkerQueue(batch_size=2, ttl_sec=120.0)
    item = wq.take(rt, "테스트브랜드", worker_id=0)
    assert item["keyword"] == "새것"


# =======================================================================
# 2026-09-24 3차 — 완료 표시(complete_inflight)·중복률 집계
# =======================================================================


def test_complete_inflight_완료후에도_TTL동안_is_inflight(tmp_path):
    now0 = 5_000_000.0
    exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드", worker_id=0, now=now0)
    exposure_runner.complete_inflight(tmp_path, "브랜드", "키워드", now=now0 + 1.0)
    # 완료됐지만 TTL(180초) 안이라 여전히 선점 중으로 취급된다
    assert exposure_runner.is_inflight(tmp_path, "브랜드", "키워드", now=now0 + 100.0) is True
    assert exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드", worker_id=1, now=now0 + 100.0) is False


def test_complete_inflight_TTL지나면_다시_잡을수있음(tmp_path):
    now0 = 6_000_000.0
    exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드", worker_id=0, now=now0)
    exposure_runner.complete_inflight(tmp_path, "브랜드", "키워드", now=now0 + 1.0)
    assert exposure_runner.is_inflight(tmp_path, "브랜드", "키워드", now=now0 + 200.0) is False
    assert exposure_runner.claim_inflight(tmp_path, "브랜드", "키워드", worker_id=1, now=now0 + 200.0) is True


def test_worker_queue_직전확인에서_걸리면_다음후보(tmp_path, monkeypatch):
    """다른 작업자가 방금(최소 간격 안) 검사해 DB에 저장해 둔 키워드가 배치에
    섞여 있어도(정렬 캐시가 그 사이 갱신 안 됐다고 흉내), take()가 직전
    DB 확인에서 걸러 다음 후보로 넘어가야 한다."""
    rt = make_runtime(tmp_path)
    row = ke.ExposureRow("테스트브랜드", "방금검사됨", "마이카페", "", None, "pushed", now_iso())
    store.save(rt.conn, row.as_row())

    batch = [
        {"keyword": "방금검사됨", "cafe": "마이카페", "volume": 0},
        {"keyword": "새것", "cafe": "마이카페", "volume": 0},
    ]
    monkeypatch.setattr(exposure_priority, "next_priority_batch", lambda rt_, brand, n, now=None: list(batch))

    wq = exposure_runner.WorkerQueue(batch_size=2, ttl_sec=120.0)
    item = wq.take(rt, "테스트브랜드", worker_id=0)
    assert item["keyword"] == "새것"
    assert wq._stale_skipped == 1
    # 직전 확인에서 걸린 "방금검사됨"은 선점이 다시 풀려 다른 작업자가 집을 수 있다
    assert exposure_runner.is_inflight(rt.settings.repo_root, "테스트브랜드", "방금검사됨") is False


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


def test_process_one_최소간격안_재검사는_duplicate_True(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    brand, keyword = "테스트브랜드", "키워드"
    store.save(rt.conn, ke.ExposureRow(brand, keyword, "마이카페", "", None, "pushed", now_iso()).as_row())
    item = {"keyword": keyword, "cafe": "마이카페", "t0_status": "", "volume": 0}

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg, **kw: _fake_row(b, i["keyword"], "pushed"))
    result = exposure_runner.process_one(rt, object(), brand, item, {})
    assert result["duplicate"] is True


def test_process_one_주기지난_재검사는_duplicate_False(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    brand, keyword = "테스트브랜드", "키워드"
    old_checked = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
    store.save(rt.conn, ke.ExposureRow(brand, keyword, "마이카페", "", None, "pushed", old_checked).as_row())
    item = {"keyword": keyword, "cafe": "마이카페", "t0_status": "", "volume": 0}

    monkeypatch.setattr(exposure_runner, "judge_once", lambda rt_, ctx, b, i, cfg, **kw: _fake_row(b, i["keyword"], "pushed"))
    result = exposure_runner.process_one(rt, object(), brand, item, {})
    assert result["duplicate"] is False
