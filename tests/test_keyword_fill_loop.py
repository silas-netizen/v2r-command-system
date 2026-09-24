"""`v2r.knowledge.keyword_fill_loop` — 네트워크 없이 가짜 도구/LLM으로 검증."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from v2r.knowledge import keyword_fill_loop as fill
from v2r.knowledge import keyword_relevance as kr
from v2r.store import keyword_discovery_store as kd_store


class FakeRouter:
    """`complete(purpose, system, user, max_tokens)` — relevance 0을 전부 준다."""

    def __init__(self, relevance: int = 0):
        self.relevance = relevance
        self.calls = 0

    def complete(self, purpose, system, user, max_tokens=4000):
        self.calls += 1
        # keyword_relevance.build_user_prompt 는 "1. 키워드" 줄들
        lines = [l for l in user.splitlines() if l.strip()]
        kws = [l.split(". ", 1)[1] for l in lines]
        out = [{"keyword": kw, "relevance": self.relevance, "rationale": "테스트"} for kw in kws]
        return json.dumps(out, ensure_ascii=False)


def _seed_db(path: Path, eligible_kw="기존키워드", total=1000, extra_eligible=0):
    conn = kd_store.open_db(path)
    kr.migrate(conn)
    fill.migrate_fill_columns(conn)
    rows = [
        {"keyword": eligible_kw, "pc": total, "mobile": 0, "total": total, "source_seed": "", "depth": 0, "relevance": 0},
    ]
    for i in range(extra_eligible):
        rows.append(
            {"keyword": f"{eligible_kw}{i}", "pc": total - i, "mobile": 0, "total": total - i,
             "source_seed": "", "depth": 0, "relevance": 0}
        )
    kd_store.save_many(conn, rows)
    stamp = "2026-09-24T00:00:00+09:00"
    for r in rows:
        conn.execute(
            "UPDATE keywords SET relevance_llm=0, relevance_codex=0, needs_review=0, scored_at=? WHERE keyword=?",
            (stamp, r["keyword"]),
        )
    conn.commit()
    conn.close()


def test_migrate_fill_columns_adds_seeded_at(tmp_path):
    db = tmp_path / "b.sqlite"
    conn = kd_store.open_db(db)
    added = fill.migrate_fill_columns(conn)
    assert "seeded_at" in added
    # 두 번째는 아무것도 더하지 않는다
    assert fill.migrate_fill_columns(conn) == []
    conn.close()


def test_eligible_count_and_select_seed_keywords(tmp_path):
    db = tmp_path / "b.sqlite"
    _seed_db(db, extra_eligible=3)
    conn = kd_store.open_db(db)
    kr.migrate(conn)
    fill.migrate_fill_columns(conn)
    assert fill.eligible_count(conn) == 4
    seeds = fill.select_seed_keywords(conn, limit=2)
    assert len(seeds) == 2
    # 검색량 내림차순 — 처음 시드된 것(가장 큰 total)부터
    assert seeds[0] == "기존키워드"
    conn.close()


def test_select_seed_keywords_excludes_already_seeded(tmp_path):
    db = tmp_path / "b.sqlite"
    _seed_db(db, extra_eligible=2)
    conn = kd_store.open_db(db)
    fill.migrate_fill_columns(conn)
    fill.mark_seeded(conn, ["기존키워드"])
    seeds = fill.select_seed_keywords(conn, limit=10)
    assert "기존키워드" not in seeds
    assert len(seeds) == 2
    conn.close()


def test_auto_score_in_fill_loop_defaults_off():
    # 사용자 지시 2026-09-25: score-worker/codex-worker 분리 구조로 바뀌면서
    # 채우기 순환의 자체 채점은 기본적으로 꺼져 있어야 한다(수집·저장만).
    assert fill.AUTO_SCORE_IN_FILL_LOOP is False


def test_run_cycle_does_not_self_score_by_default(tmp_path):
    db = tmp_path / "b.sqlite"
    guides_dir = tmp_path / "guides"
    guides_dir.mkdir()
    (guides_dir / "브랜드.md").write_text("- 브랜드/제품: 테스트제품\n", encoding="utf-8")
    _seed_db(db, extra_eligible=0)

    def fake_fetch(seeds, depth):
        return [{"keyword": f"{seeds[0]}-연관1", "pc": 10, "mobile": 5}]

    def _boom(*a, **kw):
        raise AssertionError("AUTO_SCORE_IN_FILL_LOOP=False면 채점을 부르면 안 된다")

    router = FakeRouter(relevance=0)
    router.complete = _boom  # 채점 호출이 있으면 여기서 터진다
    result = fill.run_cycle("브랜드", db, guides_dir, router, fake_fetch, seed_limit=50)

    assert result["new_collected"] == 2
    assert result["adopted"] == 0  # 채점 안 했으니 원고대상으로 올라가지 않는다


def test_run_cycle_new_relevant_keywords_raise_eligible(tmp_path, monkeypatch):
    # 2026-09-25부터 채우기 순환은 기본적으로 자체 채점을 끄고(score_worker/codex_worker가
    # 별도로 처리) 수집만 한다(AUTO_SCORE_IN_FILL_LOOP=False). 이 시험은 자체 채점 경로
    # 자체는 여전히 지원돼야 하므로 그 경로를 켜서 확인한다.
    monkeypatch.setattr(fill, "AUTO_SCORE_IN_FILL_LOOP", True)
    db = tmp_path / "b.sqlite"
    guides_dir = tmp_path / "guides"
    guides_dir.mkdir()
    (guides_dir / "브랜드.md").write_text("- 브랜드/제품: 테스트제품\n", encoding="utf-8")
    _seed_db(db, extra_eligible=0)

    def fake_fetch(seeds, depth):
        return [{"keyword": f"{seeds[0]}-연관1", "pc": 10, "mobile": 5}]

    router = FakeRouter(relevance=0)
    # crosscheck 는 codex 를 부르므로 find_codex 가 빈 문자열이면 실패 배치로 처리된다.
    # 그러면 relevance_codex 가 계속 NULL이라 eligible 조건(둘 다 0~2)을 못 만족한다.
    # 그래서 crosscheck_brand 를 우회하도록 score_batch_codex 를 몽키패치한다.
    import v2r.knowledge.keyword_relevance as kr_mod

    def fake_score_batch_codex(brand, keywords, summary, exe="", cwd=None):
        return (
            [{"keyword": kw, "relevance": 0, "rationale": ""} for kw in keywords],
            "fake-model",
        )

    orig = kr_mod.score_batch_codex
    kr_mod.score_batch_codex = fake_score_batch_codex
    try:
        result = fill.run_cycle(
            "브랜드", db, guides_dir, router, fake_fetch, seed_limit=50,
        )
    finally:
        kr_mod.score_batch_codex = orig

    # 원고대상 시드 1개 + 정리본(정규식 대체) 시드 1개 → 각 1개씩 신규
    assert result["new_collected"] == 2
    assert result["adopted"] == 2
    assert result["adoption_rate"] == 1.0
    assert result["capped"] is False
    assert set(result["by_source"]) == {fill.SOURCE_ELIGIBLE, fill.SOURCE_GUIDE}
    assert result["by_source"][fill.SOURCE_ELIGIBLE]["adoption_rate"] == 1.0


def test_run_cycle_respects_cap(tmp_path):
    db = tmp_path / "b.sqlite"
    _seed_db(db, extra_eligible=0)
    router = FakeRouter()

    def fake_fetch(seeds, depth):
        raise AssertionError("cap에 걸리면 조회를 하면 안 된다")

    result = fill.run_cycle("브랜드", db, tmp_path, router, fake_fetch, cap=1)
    assert result["capped"] is True


def test_adoption_rate_and_progress_file(tmp_path):
    path = tmp_path / "fill_progress.json"
    fill.update_fill_progress(path, "브랜드", status="running", eligible=5, round=1)
    data = fill.load_progress(path)
    assert data["브랜드"]["eligible"] == 5
    assert data["브랜드"]["status"] == "running"
    fill.update_fill_progress(path, "브랜드", status="done", eligible=10000, round=2)
    data = fill.load_progress(path)
    assert data["브랜드"]["status"] == "done"
    assert data["브랜드"]["round"] == 2


def test_fill_until_target_stops_on_low_adoption_streak(tmp_path):
    db = tmp_path / "b.sqlite"
    guides_dir = tmp_path / "guides"
    guides_dir.mkdir()
    (guides_dir / "브랜드.md").write_text("- 브랜드/제품: 테스트제품\n", encoding="utf-8")
    _seed_db(db, extra_eligible=0)
    data_dir = tmp_path / "data"

    calls = {"n": 0}

    def fake_fetch(seeds, depth):
        calls["n"] += 1
        # 매번 새 키워드를 만들지만 관련도가 3(무관)이라 채택률이 0이 되게 한다
        return [{"keyword": f"무관{calls['n']}", "pc": 10, "mobile": 5}]

    router = FakeRouter(relevance=4)  # 전부 무관(4) 처리

    import v2r.knowledge.keyword_relevance as kr_mod

    def fake_score_batch_codex(brand, keywords, summary, exe="", cwd=None):
        return ([{"keyword": kw, "relevance": 4, "rationale": ""} for kw in keywords], "fake-model")

    orig = kr_mod.score_batch_codex
    kr_mod.score_batch_codex = fake_score_batch_codex
    ac_calls = {"n": 0}

    def fake_autocomplete(kw):
        # 회차마다 새 자동완성 시드가 나오게 해 시드가 마르지 않도록 한다
        ac_calls["n"] += 1
        return [f"자동완성시드{ac_calls['n']}"]

    try:
        fill.fill_until_target(
            "브랜드", db, guides_dir, router, fake_fetch,
            target=10000, data_dir=data_dir, seed_limit=1, guide_seed_n=1, max_rounds=20,
            autocomplete_fn=fake_autocomplete,
        )
    finally:
        kr_mod.score_batch_codex = orig

    data = fill.load_progress(fill.progress_path(data_dir))
    assert data["브랜드"]["status"] == "시드고갈"
    assert data["브랜드"]["low_adoption_streak"] >= fill.LOW_ADOPTION_STREAK_LIMIT


def test_gather_seeds_dedupes_and_records_sources(tmp_path):
    db = tmp_path / "b.sqlite"
    guides_dir = tmp_path / "guides"
    guides_dir.mkdir()
    (guides_dir / "브랜드.md").write_text("- 브랜드/제품: 테스트제품\n", encoding="utf-8")
    _seed_db(db, extra_eligible=2)
    conn = kd_store.open_db(db)
    kr.migrate(conn)
    fill.migrate_fill_columns(conn)
    fill.record_seeds(conn, [("이미쓴시드", fill.SOURCE_RELATED)])

    seeds = fill.gather_seeds(
        conn, "브랜드", guides_dir, router=None,
        autocomplete_fn=lambda kw: [f"{kw} 자동", "기존키워드", "이미쓴시드", "공통후보"],
        related_fn=lambda kw: ["공통후보", f"{kw} 연관"],
        seed_limit=2, expand_top_n=2,
    )
    terms = [s for s, _ in seeds]
    assert len(terms) == len(set(terms))  # 중복 없음
    assert "기존키워드" in terms  # 원고대상 시드로는 허용
    assert "이미쓴시드" not in terms  # fill_seeds에 있으면 제외
    by = {}
    for s, t in seeds:
        by.setdefault(t, []).append(s)
    assert by[fill.SOURCE_ELIGIBLE] == ["기존키워드", "기존키워드0"]
    assert by[fill.SOURCE_GUIDE] == ["테스트제품"]
    assert "공통후보" in by[fill.SOURCE_AUTOCOMPLETE]
    assert "공통후보" not in by.get(fill.SOURCE_RELATED, [])  # 먼저 나온 출처에만
    conn.close()


def test_expand_seed_terms_round_robin_cap():
    out = fill.expand_seed_terms(["a", "b"], lambda kw: [f"{kw}1", f"{kw}2", f"{kw}3"], cap=4)
    assert out == ["a1", "b1", "a2", "b2"]


def test_source_stats_groups_by_source(tmp_path):
    db = tmp_path / "b.sqlite"
    conn = kd_store.open_db(db)
    kr.migrate(conn)
    fill.migrate_fill_columns(conn)
    kd_store.save_many(conn, [
        {"keyword": "a", "pc": 1, "mobile": 0}, {"keyword": "b", "pc": 1, "mobile": 0},
        {"keyword": "c", "pc": 1, "mobile": 0},
    ], now="2026-09-24T01:00:00")
    conn.execute("UPDATE keywords SET seed_source_type='자동완성' WHERE keyword IN ('a','b')")
    conn.execute("UPDATE keywords SET seed_source_type='연관검색' WHERE keyword='c'")
    conn.execute("UPDATE keywords SET scored_at='x'")
    conn.execute("UPDATE keywords SET relevance_llm=1, relevance_codex=1, needs_review=0 WHERE keyword='a'")
    conn.execute("UPDATE keywords SET relevance_llm=4, relevance_codex=4, needs_review=0 WHERE keyword IN ('b','c')")
    conn.commit()
    stats = fill.source_stats(conn, "2026-09-24T00:00:00")
    assert stats["자동완성"] == {"new": 2, "adopted": 1, "adoption_rate": 0.5}
    assert stats["연관검색"] == {"new": 1, "adopted": 0, "adoption_rate": 0.0}
    conn.close()


def test_parse_autocomplete_and_related():
    from v2r.knowledge import keyword_exposure as ke

    items = ke.parse_autocomplete_items({"items": [[["팥베개", "x"], ["팥베개 효능"]], [["팥베개"]]]})
    assert items == ["팥베개", "팥베개 효능"]
    html = (
        '<a href="?where=nexearch&amp;sm=tab_clk.ndT&amp;query=%ED%97%88%EB%A6%AC%EB%B2%A0%EA%B0%9C">허리베...</a>'
        '<a href="?where=nexearch&amp;sm=tab_clk.ndT&amp;query=%ED%97%88%EB%A6%AC%EB%B2%A0%EA%B0%9C">중복</a>'
        '<a href="?sm=tab_jum&amp;query=%EB%8B%A4%EB%A5%B8">다른</a>'
    )
    assert ke.parse_related_searches(html) == ["허리베개"]


def test_eligible_follows_is_manuscript_target(tmp_path):
    """원고 대상 판정은 keyword_relevance.is_manuscript_target(엄격판, 2026-09-24)과 같아야 한다.

    - k0: 0/0, needs_review 없음 -> 대상
    - k1: 3/3, bridge_rationale 있음 -> 대상(당위성 논리 있는 3)
    - k2: 3/4 -> codex 범위 밖 -> 제외
    - k3: 4/3 -> llm 범위 밖 -> 제외
    - k4: 2/2, needs_review -> 제외
    - k5: 1/None -> codex 교차 검증 전 -> 제외(2026-09-24 엄격화: 예전엔 통과)
    """
    db = tmp_path / "b.sqlite"
    conn = kd_store.open_db(db)
    kr.migrate(conn)
    fill.migrate_fill_columns(conn)
    kd_store.save_many(conn, [{"keyword": f"k{i}", "pc": 1, "mobile": 0} for i in range(6)])
    cases = [
        ("k0", 0, 0, 0, ""),
        ("k1", 3, 3, 0, "다리 논리 한 줄"),
        ("k2", 3, 4, 0, "다리 논리 한 줄"),
        ("k3", 4, 3, 0, "다리 논리 한 줄"),
        ("k4", 2, 2, 1, ""),
        ("k5", 1, None, 0, ""),
    ]
    for kw, llm, codex, review, bridge in cases:
        conn.execute(
            "UPDATE keywords SET relevance_llm=?, relevance_codex=?, needs_review=?,"
            " bridge_rationale=?, scored_at='x' WHERE keyword=?",
            (llm, codex, review, bridge, kw),
        )
    conn.commit()
    rows = conn.execute("SELECT * FROM keywords ORDER BY keyword").fetchall()
    expected = [bool(kr.is_manuscript_target(r)) for r in rows]
    assert [fill.is_eligible_row(r) for r in rows] == expected
    assert fill.eligible_count(conn) == sum(expected) == 2
    assert fill.manuscript_max_relevance() == kr.MANUSCRIPT_MAX_RELEVANCE == 3
    conn.close()


def test_stop_file_halts_loop_before_next_round(tmp_path):
    db = tmp_path / "b.sqlite"
    guides_dir = tmp_path / "guides"
    guides_dir.mkdir()
    (guides_dir / "브랜드.md").write_text("- 브랜드/제품: 테스트제품\n", encoding="utf-8")
    _seed_db(db, extra_eligible=0)
    data_dir = tmp_path / "data"
    fill.stop_path(data_dir).parent.mkdir(parents=True, exist_ok=True)
    fill.stop_path(data_dir).write_text("stop", encoding="utf-8")

    def fake_fetch(seeds, depth):
        raise AssertionError("정지 파일이 있으면 조회하면 안 된다")

    fill.fill_until_target("브랜드", db, guides_dir, FakeRouter(), fake_fetch, data_dir=data_dir, max_rounds=3)
    data = fill.load_progress(fill.progress_path(data_dir))
    assert data["브랜드"]["status"] == "정지(STOP 파일)"


def test_retry_db_locked_retries_then_succeeds():
    calls = {"n": 0}
    waits = []

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert fill._retry_db_locked(fn, "테스트", sleep_fn=waits.append) == "ok"
    assert calls["n"] == 3 and len(waits) == 2


def test_retry_db_locked_reraises_other_errors():
    def fn():
        raise sqlite3.OperationalError("no such table")

    with pytest.raises(sqlite3.OperationalError):
        fill._retry_db_locked(fn, "테스트", sleep_fn=lambda s: None)


def test_default_cap_raised_to_200000():
    assert fill.DEFAULT_CAP == 200_000


def test_purge_confirmed_irrelevant_removes_and_records(tmp_path):
    db = tmp_path / "b.sqlite"
    conn = kd_store.open_db(db)
    kr.migrate(conn)
    fill.migrate_fill_columns(conn)
    kd_store.save_many(conn, [
        {"keyword": "무관1", "pc": 1, "mobile": 0}, {"keyword": "대상1", "pc": 1, "mobile": 0},
    ])
    conn.execute(
        "UPDATE keywords SET relevance_llm=4, relevance_codex=4, needs_review=0, scored_at='x'"
        " WHERE keyword='무관1'"
    )
    conn.execute(
        "UPDATE keywords SET relevance_llm=0, relevance_codex=0, needs_review=0, scored_at='x'"
        " WHERE keyword='대상1'"
    )
    conn.commit()

    purged = fill.purge_confirmed_irrelevant(conn, "브랜드")
    assert purged == 1
    remaining = {row[0] for row in conn.execute("SELECT keyword FROM keywords")}
    assert remaining == {"대상1"}
    norms = fill.rejected_keyword_norms(conn)
    assert fill.normalize_keyword("무관1") in norms
    conn.close()


def test_gather_seeds_skips_rejected_keywords(tmp_path):
    db = tmp_path / "b.sqlite"
    guides_dir = tmp_path / "guides"
    guides_dir.mkdir()
    (guides_dir / "브랜드.md").write_text("- 브랜드/제품: 테스트제품\n", encoding="utf-8")
    _seed_db(db, extra_eligible=0)
    conn = kd_store.open_db(db)
    kr.migrate(conn)
    fill.migrate_fill_columns(conn)
    conn.execute(
        "INSERT INTO rejected_keywords (keyword_norm, keyword, brand, reason, at)"
        " VALUES (?, ?, ?, ?, ?)",
        (fill.normalize_keyword("테스트제품"), "테스트제품", "브랜드", "무관", "x"),
    )
    conn.commit()

    seeds = fill.gather_seeds(conn, "브랜드", guides_dir, router=None, seed_limit=2)
    terms = [s for s, _ in seeds]
    assert "테스트제품" not in terms
    conn.close()


def test_rotate_eligible_keywords_advances_cursor(tmp_path):
    db = tmp_path / "b.sqlite"
    _seed_db(db, extra_eligible=3)  # 기존키워드, 기존키워드0..2 -> 4개 원고 대상
    conn = kd_store.open_db(db)
    kr.migrate(conn)
    fill.migrate_fill_columns(conn)

    first = fill.rotate_eligible_keywords(conn, "자동완성", 2)
    second = fill.rotate_eligible_keywords(conn, "자동완성", 2)
    assert len(first) == 2 and len(second) == 2
    assert first != second  # 다음 회차는 다른 구간
    conn.close()


def test_derived_seed_terms_uses_router(tmp_path):
    guides_dir = tmp_path / "guides"
    guides_dir.mkdir()
    (guides_dir / "브랜드.md").write_text("정리본 내용", encoding="utf-8")

    class FakeDerivedRouter:
        def complete(self, purpose, system, user, max_tokens=1500):
            return json.dumps(["파생어1", "파생어2"], ensure_ascii=False)

    terms = fill.derived_seed_terms("브랜드", guides_dir, FakeDerivedRouter(), ["기존키워드"], n=10)
    assert terms == ["파생어1", "파생어2"]


def test_derived_seed_terms_empty_without_router_or_base():
    assert fill.derived_seed_terms("브랜드", ".", None, ["a"]) == []
    assert fill.derived_seed_terms("브랜드", ".", object(), []) == []


def test_weighted_cap_scales_with_adoption_rate():
    base = 10
    assert fill.weighted_cap(base, "자동완성", None) == base
    low = fill.weighted_cap(base, "자동완성", {"자동완성": 0.0})
    high = fill.weighted_cap(base, "자동완성", {"자동완성": 1.0})
    assert low < base < high


def test_gather_seeds_includes_derived_source(tmp_path):
    db = tmp_path / "b.sqlite"
    guides_dir = tmp_path / "guides"
    guides_dir.mkdir()
    (guides_dir / "브랜드.md").write_text("- 브랜드/제품: 테스트제품\n", encoding="utf-8")
    _seed_db(db, extra_eligible=0)
    conn = kd_store.open_db(db)
    kr.migrate(conn)
    fill.migrate_fill_columns(conn)

    class FakeDerivedRouter:
        def complete(self, purpose, system, user, max_tokens=1500):
            if purpose == "keyword_fill_seed_derived":
                return json.dumps(["파생후보1"], ensure_ascii=False)
            return json.dumps([], ensure_ascii=False)

    seeds = fill.gather_seeds(conn, "브랜드", guides_dir, FakeDerivedRouter(), seed_limit=1)
    by = {}
    for s, t in seeds:
        by.setdefault(t, []).append(s)
    assert "파생후보1" in by.get(fill.SOURCE_DERIVED, [])
    conn.close()
