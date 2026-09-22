"""브랜드 키워드 발굴 — 응답 파싱/BFS/연관도/저장/명령 테스트.

docs/reports/keyword-program-plan-2026-09-22.md A1~A3 구현.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from v2r.command.parser import parse_korean_command
from v2r.knowledge.naver_keyword_tool import (
    DEFAULT_TARGET,
    SEED_BATCH_SIZE,
    KeywordRow,
    compute_relevance,
    discover,
    parse_keyword_download_rows,
    parse_keyword_response,
)
from v2r.store import keyword_discovery_store as store

# 네이버 키워드 도구 XHR 응답을 흉내 낸 고정 샘플(실 필드명 그대로: relKeyword,
# monthlyPcQcCnt, monthlyMobileQcCnt, compIdx)
SAMPLE_RESPONSE = {
    "keywordList": [
        {
            "relKeyword": "얼굴 착색",
            "monthlyPcQcCnt": "1200",
            "monthlyMobileQcCnt": "3400",
            "compIdx": "높음",
        },
        {
            "relKeyword": "미백 크림 추천",
            "monthlyPcQcCnt": "< 10",
            "monthlyMobileQcCnt": "50",
            "compIdx": "낮음",
        },
        {"relKeyword": "", "monthlyPcQcCnt": "10", "monthlyMobileQcCnt": "10"},  # 빈 키워드는 버림
        {"relKeyword": "색소침착 원인", "monthlyPcQcCnt": 300, "monthlyMobileQcCnt": 700},
    ]
}


def test_parse_keyword_response_basic():
    rows = parse_keyword_response(SAMPLE_RESPONSE)
    assert [r.keyword for r in rows] == ["얼굴 착색", "미백 크림 추천", "색소침착 원인"]
    first = rows[0]
    assert first.pc == 1200
    assert first.mobile == 3400
    assert first.total == 4600
    # "< 10" 표기는 5로 대략화
    assert rows[1].pc == 5
    assert rows[1].mobile == 50


def test_parse_keyword_response_malformed():
    assert parse_keyword_response({}) == []
    assert parse_keyword_response({"keywordList": "not-a-list"}) == []
    assert parse_keyword_response(None) == []  # type: ignore[arg-type]


def test_compute_relevance_direct_overlap_is_zero():
    guide = ["착색", "미백", "색소침착"]
    assert compute_relevance("얼굴 착색 고민", guide, depth=3) == 0
    assert compute_relevance("색소침착 원인", guide, depth=1) == 0


def test_compute_relevance_partial_text_overlap_is_one():
    # "색소침착"과 2글자 이상 겹치는 "색소" 부분 일치 -> 1
    guide = ["색소침착"]
    assert compute_relevance("색소 관리법", guide, depth=1) == 1


def test_compute_relevance_scales_with_depth_when_no_text_overlap():
    guide = ["착색", "미백"]
    assert compute_relevance("전혀 다른 낱말", guide, depth=0) == 2  # 최소 2
    assert compute_relevance("전혀 다른 낱말", guide, depth=1) == 2
    assert compute_relevance("전혀 다른 낱말", guide, depth=2) == 3
    assert compute_relevance("전혀 다른 낱말", guide, depth=5) == 3  # 3에서 잘림


def test_store_save_many_dedup_and_summary(tmp_path):
    db_path = tmp_path / "우아덤.sqlite"
    conn = store.open_db(db_path)
    try:
        n = store.save_many(
            conn,
            [
                {"keyword": "얼굴 착색", "pc": 100, "mobile": 200, "source_seed": "착색", "depth": 1, "relevance": 0},
                {"keyword": "얼굴 착색", "pc": 999, "mobile": 999, "source_seed": "x", "depth": 2, "relevance": 1},
                {"keyword": "미백 관리", "pc": 10, "mobile": 20, "source_seed": "착색", "depth": 1, "relevance": 1},
            ],
        )
        assert n == 2  # 중복 키워드는 무시(최초 값 유지)
        assert store.count(conn) == 2
        summ = store.summary(conn)
        assert summ["count"] == 2
        assert summ["total_search_volume"] == 100 + 200 + 10 + 20
        assert summ["relevance"] == {0: 1, 1: 1}
    finally:
        conn.close()


def _fake_fetch_factory(graph: dict[str, list[KeywordRow]]):
    calls: list[list[str]] = []

    def fetch(keywords: list[str], depth: int) -> list[KeywordRow]:
        calls.append(list(keywords))
        out: list[KeywordRow] = []
        for kw in keywords:
            out.extend(graph.get(kw, []))
        return out

    return fetch, calls


def test_discover_bfs_tail_chasing_and_target_cap(tmp_path):
    # 씨앗 "착색" -> "미백" -> "색소침착" -> "관리법" 꼬리 물기
    graph = {
        "착색": [KeywordRow("미백", 10, 20), KeywordRow("착색 원인", 5, 5)],
        "미백": [KeywordRow("색소침착", 30, 40)],
        "색소침착": [KeywordRow("관리법", 1, 1)],
        "착색 원인": [],
        "관리법": [],
    }
    fetch, calls = _fake_fetch_factory(graph)
    db_path = tmp_path / "우아덤.sqlite"
    stats = discover(
        "우아덤",
        ["착색"],
        guide_keywords=["착색"],
        fetch_fn=fetch,
        db_path=db_path,
        target=100,
        sleep_fn=lambda _s: None,
    )
    assert stats.collected == 4  # 미백, 착색 원인, 색소침착, 관리법
    flat_calls = [kw for batch in calls for kw in batch]
    assert "착색" in flat_calls and "미백" in flat_calls and "색소침착" in flat_calls

    conn = store.open_db(db_path)
    try:
        kws = set(store.all_keywords(conn))
    finally:
        conn.close()
    assert kws == {"미백", "착색 원인", "색소침착", "관리법"}


def test_discover_stops_on_target(tmp_path):
    graph = {"a": [KeywordRow("b", 1, 1), KeywordRow("c", 1, 1), KeywordRow("d", 1, 1)]}
    fetch, calls = _fake_fetch_factory(graph)
    stats = discover(
        "brand",
        ["a"],
        guide_keywords=[],
        fetch_fn=fetch,
        db_path=tmp_path / "brand.sqlite",
        target=2,
        sleep_fn=lambda _s: None,
    )
    assert stats.collected == 2
    assert stats.stopped_reason == "목표 개수에 도달했습니다"


def test_discover_stops_on_fetch_error(tmp_path):
    def fetch(keyword: str, depth: int):
        raise RuntimeError("네이버 검색 차단/캡차로 보입니다")

    stats = discover(
        "brand",
        ["a"],
        guide_keywords=[],
        fetch_fn=fetch,
        db_path=tmp_path / "brand.sqlite",
        target=100,
        sleep_fn=lambda _s: None,
    )
    assert stats.collected == 0
    assert "차단" in stats.stopped_reason


def test_discover_session_cap_without_rest_stops(tmp_path):
    graph = {"a": [KeywordRow("b", 1, 1)], "b": [KeywordRow("c", 1, 1)]}
    fetch, calls = _fake_fetch_factory(graph)
    stats = discover(
        "brand",
        ["a"],
        guide_keywords=[],
        fetch_fn=fetch,
        db_path=tmp_path / "brand.sqlite",
        target=100,
        session_cap=1,
        rest_fn=None,
        sleep_fn=lambda _s: None,
    )
    assert calls == [["a"]]
    assert "세션 조회 상한" in stats.stopped_reason


# --- 명령 파서 ---------------------------------------------------------------
def test_parse_keyword_discovery_command():
    spec = parse_korean_command("우아덤 키워드 발굴 1000개")
    assert spec is not None
    assert spec.task == "keyword_discovery"
    assert spec.brand == "우아덤"
    assert spec.count == 1000


def test_parse_keyword_discovery_status_command():
    spec = parse_korean_command("키워드 발굴 현황")
    assert spec is not None
    assert spec.task == "keyword_discovery_status"


def test_parse_keyword_discovery_no_count_uses_default_later():
    spec = parse_korean_command("코숨핏 키워드 발굴")
    assert spec is not None
    assert spec.task == "keyword_discovery"
    assert spec.brand == "코숨핏"
    assert spec.count == 0  # 처리기가 DEFAULT_TARGET으로 채운다


def test_default_target_is_ten_thousand():
    assert DEFAULT_TARGET == 10_000


def test_seed_batch_size_is_five():
    assert SEED_BATCH_SIZE == 5


def test_discover_batches_seeds_by_five(tmp_path):
    # 씨앗 7개 -> 첫 조회 5개, 둘째 조회 2개로 나뉘어야 한다
    seeds = [f"씨앗{i}" for i in range(7)]
    fetch, calls = _fake_fetch_factory({})
    discover(
        "brand",
        seeds,
        guide_keywords=[],
        fetch_fn=fetch,
        db_path=tmp_path / "brand.sqlite",
        target=100,
        sleep_fn=lambda _s: None,
    )
    assert len(calls) == 2
    assert len(calls[0]) == 5
    assert len(calls[1]) == 2
    assert set(calls[0]) | set(calls[1]) == set(seeds)


def test_discover_does_not_mix_depths_in_one_batch(tmp_path):
    # 씨앗 6개(깊이 0) -> 1번째 조회는 5개(깊이0), 새 키워드 1개가 나오면
    # 남은 씨앗 1개(깊이0)와 새 키워드(깊이1)를 한 조회에 섞지 않는다
    seeds = [f"씨앗{i}" for i in range(6)]
    graph = {"씨앗0": [KeywordRow("새키워드", 1, 1)]}
    fetch, calls = _fake_fetch_factory(graph)
    discover(
        "brand",
        seeds,
        guide_keywords=[],
        fetch_fn=fetch,
        db_path=tmp_path / "brand.sqlite",
        target=100,
        sleep_fn=lambda _s: None,
    )
    # 2번째 조회(depth 0 나머지 1개)와 3번째 조회(depth 1, 새키워드)가 섞이지 않아야 한다
    assert calls[1] == ["씨앗5"]
    assert calls[2] == ["새키워드"]


# --- 다운로드(xlsx) 파싱 -----------------------------------------------------
# 실측(2026-09-23): "전체 다운로드" xlsx는 1~2행이 헤더, 3행부터 데이터.
# A=연관키워드, B=월간검색수(PC), C=월간검색수(모바일) ... H=경쟁정도
SAMPLE_DOWNLOAD_ROWS = [
    ("연관키워드", "월간검색수 ", None, "월평균클릭수 ", None, "월평균클릭률 ", None, "경쟁정도", "월평균노출 광고수"),
    (None, "월간검색수(PC)", "월간검색수(모바일)", "월평균클릭수(PC)", "월평균클릭수(모바일)", "월평균클릭률(PC)", "월평균클릭률(모바일)", None, None),
    ("가려움증", "650", "3,250", "0.5", "15.2", "0.09%", "0.5%", "높음", "10"),
    ("종류", "230", "870", 0, "3", "-", "0.37%", "중간", "8"),
    (None, None, None, None, None, None, None, None, None),  # 빈 줄은 건너뜀
    ("건성습진", "< 10", "170", "0.5", "2.2", "0.9%", "1.37%", "높음", "10"),
]


def test_parse_keyword_download_rows():
    rows = parse_keyword_download_rows(SAMPLE_DOWNLOAD_ROWS)
    assert [r.keyword for r in rows] == ["가려움증", "종류", "건성습진"]
    assert rows[0].pc == 650
    assert rows[0].mobile == 3250
    assert rows[0].comp_idx == "높음"
    assert rows[2].pc == 5  # "< 10" -> 5로 대략화


def test_parse_keyword_download_rows_empty():
    assert parse_keyword_download_rows([]) == []
    assert parse_keyword_download_rows(SAMPLE_DOWNLOAD_ROWS[:2]) == []  # 헤더만
