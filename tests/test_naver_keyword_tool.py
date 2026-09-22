"""브랜드 키워드 발굴 — 응답 파싱/BFS/연관도/저장/명령 테스트.

docs/reports/keyword-program-plan-2026-09-22.md A1~A3 구현.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from v2r.command.parser import parse_korean_command
from v2r.knowledge.naver_keyword_tool import (
    DEFAULT_TARGET,
    KeywordRow,
    compute_relevance,
    discover,
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


def test_compute_relevance_scales_with_depth():
    guide = ["착색", "미백"]
    assert compute_relevance("전혀 다른 낱말", guide, depth=1) == 1
    assert compute_relevance("전혀 다른 낱말", guide, depth=2) == 2
    assert compute_relevance("전혀 다른 낱말", guide, depth=5) == 3  # 3에서 잘림
    assert compute_relevance("전혀 다른 낱말", guide, depth=0) == 1  # 최소 1


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
    calls: list[str] = []

    def fetch(keyword: str, depth: int) -> list[KeywordRow]:
        calls.append(keyword)
        return graph.get(keyword, [])

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
    assert "착색" in calls and "미백" in calls and "색소침착" in calls

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
    assert calls == ["a"]
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
