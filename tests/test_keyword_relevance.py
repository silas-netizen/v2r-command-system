"""키워드-브랜드 연관도 재산정(`v2r.knowledge.keyword_relevance`) 시험."""

from __future__ import annotations

import json
import sqlite3

import pytest

from v2r.knowledge import keyword_relevance as kr


def _make_db(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE keywords (
            keyword TEXT PRIMARY KEY,
            pc INTEGER NOT NULL DEFAULT 0,
            mobile INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            source_seed TEXT NOT NULL DEFAULT '',
            depth INTEGER NOT NULL DEFAULT 0,
            relevance INTEGER NOT NULL DEFAULT 0,
            collected_at TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO keywords (keyword, total, collected_at) VALUES (?, ?, '2026-09-23')",
        rows,
    )
    conn.commit()
    conn.close()


class FakeRouter:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def complete(self, purpose, system, user, max_tokens=1200):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# --- 마이그레이션 ---------------------------------------------------


def test_migrate_adds_columns_once(tmp_path):
    db = tmp_path / "brand.sqlite"
    _make_db(db, [("키워드1", 100)])
    added = kr.migrate_path(db)
    assert set(added) == set(kr.MIGRATION_COLUMNS)

    added_again = kr.migrate_path(db)
    assert added_again == []


# --- 프롬프트/파싱 ---------------------------------------------------


def test_build_user_prompt_numbers_keywords():
    prompt = kr.build_user_prompt(["a", "b"])
    assert prompt == "1. a\n2. b"


def test_parse_response_ok():
    keywords = ["감기약", "이비인후과"]
    raw = json.dumps(
        [
            {"keyword": "감기약", "relevance": 0, "rationale": "직접 제품군"},
            {"keyword": "이비인후과", "relevance": 3, "rationale": "무관"},
        ],
        ensure_ascii=False,
    )
    out = kr.parse_response(raw, keywords)
    assert out[0] == {
        "keyword": "감기약",
        "relevance": 0,
        "rationale": "직접 제품군",
        "bridge_rationale": "",
    }
    # relevance=3(당위성)이면 rationale은 그대로 두고 bridge는 비어 있을 수 있다
    assert out[1]["relevance"] == 3
    assert out[1]["rationale"] == "무관"


def test_parse_response_reads_bridge_for_relevance_3():
    raw = json.dumps(
        [
            {
                "keyword": "대상포진",
                "relevance": 3,
                "rationale": "면역 저하 국면",
                "bridge": "독감으로 몸살을 앓으면 면역이 떨어져 여성 건강 관리가 더 중요해진다",
            },
            {"keyword": "치과", "relevance": 4, "rationale": "", "bridge": "억지로 만든 다리"},
        ],
        ensure_ascii=False,
    )
    out = kr.parse_response(raw, ["대상포진", "치과"])
    assert out[0]["relevance"] == 3
    assert out[0]["bridge_rationale"] == "독감으로 몸살을 앓으면 면역이 떨어져 여성 건강 관리가 더 중요해진다"
    # relevance=4(무관)면 rationale·bridge 모두 강제로 빈 문자열
    assert out[1]["relevance"] == 4
    assert out[1]["rationale"] == ""
    assert out[1]["bridge_rationale"] == ""


def test_default_batch_size_is_50():
    assert kr.DEFAULT_BATCH_SIZE == 50


def test_score_batch_uses_higher_max_tokens():
    calls = {}

    class RecordingRouter:
        def complete(self, purpose, system, user, max_tokens=1200):
            calls["max_tokens"] = max_tokens
            return json.dumps([{"keyword": "a", "relevance": 0, "rationale": "직접"}])

    kr.score_batch(RecordingRouter(), "브랜드", ["a"], "요약")
    assert calls["max_tokens"] == kr.DEFAULT_MAX_TOKENS == 8000


def test_parse_response_count_mismatch():
    raw = json.dumps([{"keyword": "a", "relevance": 0, "rationale": "x"}])
    with pytest.raises(kr.RelevanceParseError):
        kr.parse_response(raw, ["a", "b"])


def test_parse_response_keyword_mismatch():
    # 편집 거리가 커서(전혀 다른 단어) 오타 허용 범위를 넘는 경우
    raw = json.dumps([{"keyword": "완전히다른단어", "relevance": 0, "rationale": "x"}])
    with pytest.raises(kr.RelevanceParseError):
        kr.parse_response(raw, ["감기약전용키워드"])


def test_parse_response_bad_relevance_range():
    raw = json.dumps([{"keyword": "a", "relevance": 9, "rationale": "x"}])
    with pytest.raises(kr.RelevanceParseError):
        kr.parse_response(raw, ["a"])


def test_parse_response_not_json_array():
    with pytest.raises(kr.RelevanceParseError):
        kr.parse_response("이건 그냥 텍스트", ["a"])


def test_parse_response_accepts_typo_corrected_keyword():
    """모델이 오타를 "교정"해 돌려줘도(예: 클랜징폼 → 클렌징폼) 위치로 대응해 받아들인다."""
    keywords = ["클랜징폼", "리포즘글루타치온", "때타월"]
    raw = json.dumps(
        [
            {"keyword": "클렌징폼", "relevance": 1, "rationale": "근접"},
            {"keyword": "리포솜글루타치온", "relevance": 2, "rationale": "확장"},
            {"keyword": "때타올", "relevance": 3, "rationale": "당위성", "bridge": "다리"},
        ],
        ensure_ascii=False,
    )
    out = kr.parse_response(raw, keywords)
    # 응답의 철자가 아니라 기대 키워드 그대로 저장한다
    assert [r["keyword"] for r in out] == keywords
    assert [r["relevance"] for r in out] == [1, 2, 3]


def test_parse_response_no_keyword_field_matches_by_position():
    keywords = ["a", "b"]
    raw = json.dumps(
        [
            {"relevance": 0, "rationale": "직접"},
            {"relevance": 4, "rationale": ""},
        ],
        ensure_ascii=False,
    )
    out = kr.parse_response(raw, keywords)
    assert [r["keyword"] for r in out] == ["a", "b"]


def test_parse_response_rejects_wildly_different_keyword():
    """편집 거리가 큰(전혀 다른 단어) 경우는 여전히 실패해야 한다."""
    raw = json.dumps([{"keyword": "완전히다른키워드입니다", "relevance": 0, "rationale": "x"}])
    with pytest.raises(kr.RelevanceParseError):
        kr.parse_response(raw, ["감기약"])


# --- score_batch 재시도 ------------------------------------------------


def test_score_batch_retries_then_succeeds():
    good = json.dumps([{"keyword": "a", "relevance": 1, "rationale": "근접"}], ensure_ascii=False)
    router = FakeRouter(["엉망인 응답", good])
    out = kr.score_batch(router, "브랜드", ["a"], "요약", retries=2)
    assert out[0]["relevance"] == 1
    assert router.calls == 2


def test_score_batch_gives_up_after_retries():
    router = FakeRouter(["나쁨1", "나쁨2", "나쁨3"])
    with pytest.raises(kr.RelevanceParseError):
        kr.score_batch(router, "브랜드", ["a"], "요약", retries=2)
    assert router.calls == 3


# --- score_brand / DB 반영 ----------------------------------------------


def test_score_brand_writes_scores_and_progress(tmp_path):
    db = tmp_path / "브랜드.sqlite"
    _make_db(db, [("키워드1", 100), ("키워드2", 50)])

    guides = tmp_path / "guides"
    guides.mkdir()
    (guides / "브랜드.md").write_text(
        "# 브랜드 정리본\n- 브랜드/제품: 테스트 제품\n[역할]\n타깃 설명 문장\n[절대 규칙]\n",
        encoding="utf-8",
    )

    response = json.dumps(
        [
            {"keyword": "키워드1", "relevance": 0, "rationale": "직접"},
            {"keyword": "키워드2", "relevance": 2, "rationale": "확장"},
        ],
        ensure_ascii=False,
    )
    router = FakeRouter([response])
    progress = tmp_path / "relevance_progress.json"

    result = kr.score_brand(
        router, "브랜드", db, guides, batch_size=100, progress_path=progress
    )
    assert result == {"scored": 2, "failed_batches": 0}

    conn = sqlite3.connect(str(db))
    rows = dict(conn.execute("SELECT keyword, relevance_llm FROM keywords").fetchall())
    conn.close()
    assert rows["키워드1"] == 0
    assert rows["키워드2"] == 2

    data = kr.load_progress(progress)
    assert data["브랜드"]["status"] == "done"
    assert data["브랜드"]["scored"] == 2


def test_score_brand_leaves_failed_batch_keywords_pending(tmp_path):
    """묶음이 포기되면 scored_at을 건드리지 않아 다음 회차에 다시 대상이 된다."""
    db = tmp_path / "브랜드.sqlite"
    _make_db(db, [("키워드1", 100), ("키워드2", 50)])

    guides = tmp_path / "guides"
    guides.mkdir()
    (guides / "브랜드.md").write_text("- 브랜드/제품: 테스트", encoding="utf-8")

    router = FakeRouter(["나쁨1", "나쁨2", "나쁨3"])  # RETRY_COUNT=2 → 3회 모두 실패
    result = kr.score_brand(router, "브랜드", db, guides, progress_path=None)
    assert result == {"scored": 0, "failed_batches": 1}

    conn = sqlite3.connect(str(db))
    unscored = conn.execute(
        "SELECT COUNT(*) FROM keywords WHERE scored_at = ''"
    ).fetchone()[0]
    conn.close()
    assert unscored == 2  # 실패한 묶음의 키워드가 그대로 남아, 다음 회차 pending_keywords 대상


def test_score_brand_skips_already_scored(tmp_path):
    db = tmp_path / "브랜드.sqlite"
    _make_db(db, [("키워드1", 100)])
    kr.migrate_path(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE keywords SET relevance_llm=1, scored_at='2026-09-23T00:00:00'"
    )
    conn.commit()
    conn.close()

    guides = tmp_path / "guides"
    guides.mkdir()
    (guides / "브랜드.md").write_text("- 브랜드/제품: 테스트", encoding="utf-8")

    router = FakeRouter([])
    result = kr.score_brand(router, "브랜드", db, guides, progress_path=None)
    assert result == {"scored": 0, "failed_batches": 0}
    assert router.calls == 0


# --- primary_brand 중복 배정 --------------------------------------------


def test_assign_primary_brand_picks_lower_relevance(tmp_path):
    db_a = tmp_path / "A.sqlite"
    db_b = tmp_path / "B.sqlite"
    _make_db(db_a, [("공통키워드", 10), ("A전용", 5)])
    _make_db(db_b, [("공통키워드", 10), ("B전용", 5)])
    for db in (db_a, db_b):
        kr.migrate_path(db)

    conn_a = sqlite3.connect(str(db_a))
    conn_a.execute("UPDATE keywords SET relevance_llm=2, scored_at='x' WHERE keyword='공통키워드'")
    conn_a.execute("UPDATE keywords SET relevance_llm=0, scored_at='x' WHERE keyword='A전용'")
    conn_a.commit()
    conn_a.close()

    conn_b = sqlite3.connect(str(db_b))
    conn_b.execute("UPDATE keywords SET relevance_llm=0, scored_at='x' WHERE keyword='공통키워드'")
    conn_b.execute("UPDATE keywords SET relevance_llm=1, scored_at='x' WHERE keyword='B전용'")
    conn_b.commit()
    conn_b.close()

    counts = kr.assign_primary_brand({"A": db_a, "B": db_b})
    assert counts["B"] == 2  # 공통키워드(B가 이김) + B전용
    assert counts["A"] == 1  # A전용

    conn_a = sqlite3.connect(str(db_a))
    assert conn_a.execute(
        "SELECT primary_brand FROM keywords WHERE keyword='공통키워드'"
    ).fetchone()[0] == ""
    conn_a.close()

    conn_b = sqlite3.connect(str(db_b))
    assert conn_b.execute(
        "SELECT primary_brand FROM keywords WHERE keyword='공통키워드'"
    ).fetchone()[0] == "B"
    conn_b.close()


# --- 현황 ---------------------------------------------------------------


# --- Codex 교차 검증 ----------------------------------------------------


def test_crosscheck_rows_flags_needs_review_and_picks_conservative():
    claude_rows = [
        {"keyword": "감기약", "relevance": 0, "rationale": "직접"},
        {"keyword": "이비인후과", "relevance": 1, "rationale": "근접"},
        {"keyword": "서울", "relevance": 0, "rationale": "직접(과다판정 의심)"},
    ]
    codex_rows = [
        {"keyword": "감기약", "relevance": 0, "rationale": "직접"},
        {"keyword": "이비인후과", "relevance": 2, "rationale": "확장"},
        {"keyword": "서울", "relevance": 3, "rationale": ""},
    ]
    merged = kr.crosscheck_rows(claude_rows, codex_rows)
    by_kw = {r["keyword"]: r for r in merged}

    assert by_kw["감기약"]["needs_review"] is False
    assert by_kw["감기약"]["final_relevance"] == 0

    assert by_kw["이비인후과"]["needs_review"] is False  # gap=1 < NEEDS_REVIEW_GAP
    assert by_kw["이비인후과"]["final_relevance"] == 1

    assert by_kw["서울"]["needs_review"] is True  # gap=3
    assert by_kw["서울"]["final_relevance"] == 3  # 보수적으로 큰(무관) 값 채택


def test_agreement_rate():
    merged = [
        {"keyword": "a", "relevance": 0, "relevance_codex": 0},
        {"keyword": "b", "relevance": 0, "relevance_codex": 1},
        {"keyword": "c", "relevance": 0, "relevance_codex": 3},
    ]
    assert kr.agreement_rate(merged) == round(2 / 3, 4)


def test_manuscript_eligible_requires_both_models_within_range():
    # 2026-09-24: MANUSCRIPT_MAX_RELEVANCE가 2->3으로 넓어졌다(당위성 포함).
    assert kr.manuscript_eligible({"relevance": 2, "relevance_codex": 2}) is True
    assert kr.manuscript_eligible({"relevance": 2, "relevance_codex": 3}) is True
    assert kr.manuscript_eligible({"relevance": 2, "relevance_codex": 4}) is False
    assert kr.manuscript_eligible({"relevance": 4, "relevance_codex": 0}) is False
    assert kr.manuscript_eligible({"relevance": 1, "relevance_codex": None}) is True


def test_is_manuscript_target_excludes_unrelated_and_needs_review():
    # 0에서 3(당위성 포함)이고 needs_review가 아니면 원고 대상.
    assert kr.is_manuscript_target(
        {"relevance_llm": 3, "relevance_codex": 3, "needs_review": 0}
    ) is True
    assert kr.is_manuscript_target(
        {"relevance_llm": 4, "relevance_codex": 3, "needs_review": 0}
    ) is False
    assert kr.is_manuscript_target(
        {"relevance_llm": 2, "relevance_codex": 4, "needs_review": 0}
    ) is False
    assert kr.is_manuscript_target(
        {"relevance_llm": 2, "relevance_codex": 2, "needs_review": 1}
    ) is False
    assert kr.is_manuscript_target(
        {"relevance_llm": 0, "relevance_codex": None, "needs_review": 0}
    ) is True
    # sqlite3.Row처럼 매핑 접근만 지원하는 대상에도 동작해야 한다
    assert kr.is_manuscript_target(
        {"relevance": 1, "relevance_codex": None, "needs_review": 0}
    ) is True  # relevance_llm이 없으면 relevance로 대체


def test_write_crosscheck_persists_final_scores(tmp_path):
    db = tmp_path / "브랜드.sqlite"
    _make_db(db, [("k1", 1)])
    kr.migrate_path(db)
    merged = [
        {"keyword": "k1", "relevance": 0, "relevance_codex": 3, "needs_review": True, "final_relevance": 3}
    ]
    conn = sqlite3.connect(str(db))
    kr.write_crosscheck(conn, merged)
    row = conn.execute(
        "SELECT relevance_llm, relevance_codex, needs_review FROM keywords WHERE keyword='k1'"
    ).fetchone()
    conn.close()
    assert row == (3, 3, 1)


def test_crosscheck_brand_processes_already_scored_keywords(tmp_path, monkeypatch):
    db = tmp_path / "브랜드.sqlite"
    _make_db(db, [("k1", 10), ("k2", 5)])
    kr.migrate_path(db)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE keywords SET relevance_llm=0, rationale='직접', scored_at='x' WHERE keyword='k1'")
    conn.execute("UPDATE keywords SET relevance_llm=1, rationale='근접', scored_at='x' WHERE keyword='k2'")
    conn.commit()
    conn.close()

    guides = tmp_path / "guides"
    guides.mkdir()
    (guides / "브랜드.md").write_text("- 브랜드/제품: 테스트", encoding="utf-8")

    def fake_score_batch_codex(brand, keywords, summary, exe=""):
        return (
            [{"keyword": kw, "relevance": 0, "rationale": "동의"} for kw in keywords],
            "gpt-6-astra",
        )

    monkeypatch.setattr(kr, "score_batch_codex", fake_score_batch_codex)
    result = kr.crosscheck_brand("브랜드", db, guides, batch_size=100)
    assert result == {"checked": 2, "failed_batches": 0}

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT relevance_codex FROM keywords WHERE keyword='k1'"
    ).fetchone()
    conn.close()
    assert row[0] == 0


def test_migration_adds_bridge_rationale_idempotent(tmp_path):
    db = tmp_path / "브랜드.sqlite"
    _make_db(db, [("k1", 1)])
    added = kr.migrate_path(db)
    assert "bridge_rationale" in added
    conn = sqlite3.connect(str(db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(keywords)")}
    conn.close()
    assert "bridge_rationale" in cols
    assert kr.migrate_path(db) == []  # 이미 있으면 다시 더하지 않는다


def test_system_prompt_includes_bridge_vs_unrelated_examples():
    prompt = kr.build_system_prompt("우아덤", "요약")
    assert "당위성" in prompt
    assert "무관" in prompt
    for kw in ("대상포진", "독감", "위고비", "근처피부과", "사마귀"):
        assert kw in prompt
    for kw in ("피부과", "안과", "치과", "마운자로", "독감예방접종"):
        assert kw in prompt
    assert "bridge" in prompt


def test_pending_legacy_unrelated_rows_only_old_scale_3(tmp_path):
    db = tmp_path / "브랜드.sqlite"
    _make_db(db, [("k1", 100), ("k2", 50), ("k3", 10)])
    kr.migrate_path(db)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE keywords SET relevance_llm=3, scored_at='x' WHERE keyword='k1'")
    conn.execute("UPDATE keywords SET relevance_llm=0, scored_at='x' WHERE keyword='k2'")
    conn.execute("UPDATE keywords SET relevance_llm=3, relevance_codex=3, scored_at='x' WHERE keyword='k3'")
    conn.commit()
    rows = kr.pending_legacy_unrelated_rows(conn)
    conn.close()
    assert rows == ["k1", "k3"]  # 검색량 내림차순, k2(직접)는 대상 아님


def test_brand_status_distribution(tmp_path):
    db = tmp_path / "브랜드.sqlite"
    _make_db(db, [("k1", 1), ("k2", 1), ("k3", 1)])
    kr.migrate_path(db)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE keywords SET relevance_llm=0, scored_at='x' WHERE keyword='k1'")
    conn.execute("UPDATE keywords SET relevance_llm=3, scored_at='x' WHERE keyword='k2'")
    conn.commit()
    conn.close()

    status = kr.brand_status(db)
    assert status["total"] == 3
    assert status["unscored"] == 1
    assert status["distribution"][0] == 1
    assert status["distribution"][3] == 1
    assert status["distribution"][1] == 0
