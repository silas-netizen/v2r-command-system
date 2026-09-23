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


def test_run_cycle_new_relevant_keywords_raise_eligible(tmp_path):
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

    assert result["new_collected"] == 1
    assert result["adopted"] == 1
    assert result["adoption_rate"] == 1.0
    assert result["capped"] is False


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

    router = FakeRouter(relevance=3)  # 전부 무관 처리

    import v2r.knowledge.keyword_relevance as kr_mod

    def fake_score_batch_codex(brand, keywords, summary, exe="", cwd=None):
        return ([{"keyword": kw, "relevance": 3, "rationale": ""} for kw in keywords], "fake-model")

    orig = kr_mod.score_batch_codex
    kr_mod.score_batch_codex = fake_score_batch_codex
    try:
        fill.fill_until_target(
            "브랜드", db, guides_dir, router, fake_fetch,
            target=10000, data_dir=data_dir, seed_limit=1, guide_seed_n=1, max_rounds=20,
        )
    finally:
        kr_mod.score_batch_codex = orig

    data = fill.load_progress(fill.progress_path(data_dir))
    assert data["브랜드"]["status"] == "시드고갈"
    assert data["브랜드"]["low_adoption_streak"] >= fill.LOW_ADOPTION_STREAK_LIMIT
