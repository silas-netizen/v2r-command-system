"""장애 2026-09-20(10:20~10:45) 재발 방지 테스트.

세 가지를 고쳤다.

1. 감시 틱이 `int() argument ... not 'dict'` 로 터지던 것 — 발행 결과의
   집계 칸(`per_cafe`)과 모델 사용량(`usage["by_model"]`)이 **칸(dict)** 일 수
   있는데 그대로 `int()` 했다.
2. 긴 작업이 도는 동안 `data/serve_heartbeat.json` 이 낡던 것 — 심장박동을
   작업 사이에만 찍었다. 이제 리스를 연장하는 자리마다 같이 찍는다.
3. 슬롯 하나가 `uncertain/created` 로 10분 넘게 조용히 멈춰 있던 것 —
   등록 확인 폴링에 상한이 없었다. 이제 120초로 끊고 다음 슬롯으로 간다.

실제 API·실제 시계는 쓰지 않는다(가짜 클라이언트·가짜 시계).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from v2r.api import articles as api_articles
from v2r.content import duplicate
from v2r.content.manuscript import Manuscript
from v2r.engine import article_sync
from v2r.engine import monitor
from v2r.engine import publish as publish_mod
from v2r.engine import schedule as sched
from v2r.engine import worker
from v2r.engine.scheduler import KST

from tests.test_engine import make_runtime, make_spec

NOW = datetime(2026, 9, 20, 10, 40, tzinfo=KST)


# --------------------------------------------------------------------
# 1) 감시 집계가 어떤 모양이든 터지지 않는다
# --------------------------------------------------------------------
def _job(result: dict) -> dict:
    return {"id": 69, "task": "publish_daily", "result_json": json.dumps(result)}


def test_성공줄세기가_per_cafe_counts_모양을_받는다(tmp_path):
    """`worker.per_cafe_counts` 가 만드는 진짜 모양."""
    class _Slot:
        cafe = "마이 웨딩 드림"

    counts = worker.per_cafe_counts(
        [{"cafe": "고요한 아침"}, {"cafe": "고요한 아침"}], [(_Slot(), "실패")]
    )
    assert counts == {
        "고요한 아침": {"ok": 2, "fail": 0},
        "마이 웨딩 드림": {"ok": 0, "fail": 1},
    }
    assert monitor._success_rows(_job({"per_cafe": counts})) == 2


def test_성공줄세기가_prepare_per_cafe_모양을_받는다():
    """`publish.prepare_per_cafe` 쪽 칸({requested, already, planned})."""
    per_cafe = {
        "고요한 아침": {"requested": 5, "already": 1, "planned": 4},
        "마이 웨딩 드림": {"requested": 3, "already": 3, "planned": 0, "ok": 1},
    }
    assert monitor._success_rows(_job({"per_cafe": per_cafe})) == 1


def test_성공줄세기가_옛_숫자모양과_섞인모양도_센다():
    assert monitor._success_rows(_job({"per_cafe": {"가": 2, "나": 3}})) == 5
    assert monitor._success_rows(_job({"per_cafe": {"가": 2, "나": {"ok": 3}}})) == 5
    assert monitor._success_rows(_job({"per_cafe": {"가": None, "나": "2"}})) == 2
    assert monitor._success_rows(_job({"per_cafe": []})) == 0


def test_성공줄세기가_results_가_칸이어도_센다():
    results = {"고요한 아침": [{"status": "done"}, {"status": "failed"}]}
    assert monitor._success_rows(_job({"results": results})) == 1
    # 줄 모양을 모르면 있다고만 센다(다시 올려 중복 내는 것보다 안전하다)
    assert monitor._success_rows(_job({"results": ["SRC-1", "SRC-2"]})) == 2


def test_다시해볼줄세기가_칸모양_failures도_받는다():
    assert monitor._retryable_rows(_job({"failures": ["a", "b"]})) == 2
    assert monitor._retryable_rows(_job({"failures": {"가": "x", "나": "y"}})) == 2
    assert monitor._retryable_rows(_job({"failures": 3})) == 3
    assert monitor._retryable_rows(_job({})) == 0


def test_부분성공이면_재등록하지_않는다():
    job = _job({"per_cafe": {"고요한 아침": {"ok": 2, "fail": 1}}})
    reason = monitor.no_retry_reason(job)
    assert reason is not None and "성공 2건" in reason


class _FakeRouter:
    """usage 에 `by_model` **칸**이 섞여 있는 진짜 라우터 모양."""

    enabled = True

    def __init__(self) -> None:
        self.usage = {
            "input_tokens": 100,
            "output_tokens": 20,
            "calls": 1,
            "by_model": {"haiku": {"input_tokens": 100, "output_tokens": 20}},
        }

    def complete_json(self, *a, **k):
        self.usage["input_tokens"] += 50
        return {"rule": "unknown", "action": "human", "explain": "모름"}


def test_모델사용량에_칸이_섞여도_진단이_터지지_않는다(tmp_path):
    rt = make_runtime(tmp_path)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    rt.settings.config_dir = cfg_dir
    rt._llm = _FakeRouter()
    state: dict = {}
    out = monitor.diagnose(rt, state, "처음 보는 아주 이상한 오류 XYZ", NOW)
    assert out["rule"] == "unknown"
    box = monitor._daily(state, NOW)
    assert box["llm_calls"] == 1
    assert box["llm_tokens"] == 50  # 칸은 빼고 숫자만 세서 늘어난 만큼
    rt.close()


def test_usage_total_이_칸을_건너뛴다():
    assert monitor._usage_total(_FakeRouter()) == 120
    assert monitor._usage_total(object()) == 0


# --------------------------------------------------------------------
# 2) 긴 작업 동안에도 심장박동이 뛴다
# --------------------------------------------------------------------
def _beats(rt) -> float | None:
    return sched.heartbeat_age_seconds(rt)


def test_긴_대기중에도_심장박동_파일이_갱신된다(tmp_path):
    """가짜 시계로 10분을 쉬어도 심장박동은 방금 것이어야 한다."""
    rt = make_runtime(tmp_path)
    assert _beats(rt) is None  # 아직 아무도 찍지 않았다
    slept: list[float] = []
    worker._sleep_with_beat(600.0, lambda: None, slept.append, rt=rt)
    assert sum(slept) == 600.0
    age = _beats(rt)
    assert age is not None and age < sched.HEARTBEAT_STALE_SECONDS
    rt.close()


def test_요청제한_대기중에도_심장박동이_뛴다(tmp_path):
    rt = make_runtime(tmp_path)
    slept: list[float] = []
    assert worker._wait_for_rate_limit(rt, 300.0, lambda: None, slept.append) is True
    assert sum(slept) == 300.0
    age = _beats(rt)
    assert age is not None and age < sched.HEARTBEAT_STALE_SECONDS
    rt.close()


def test_긴_발행작업이_슬롯마다_심장박동을_찍는다(tmp_path, monkeypatch):
    """가짜 긴 작업: 슬롯 하나가 몇 분씩 걸려도 심장박동은 낡지 않는다."""
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, count=1)
    sched.write_heartbeat(rt, datetime.now(KST) - timedelta(seconds=586))
    stale = _beats(rt)
    assert stale is not None and stale > sched.HEARTBEAT_STALE_SECONDS

    def slow_create(client, **kwargs):
        worker._sleep_with_beat(300.0, lambda: None, lambda s: None, rt=rt)
        return "SRC-LONG"

    monkeypatch.setattr(publish_mod.api_articles, "create_article", slow_create)
    monkeypatch.setattr(publish_mod.api_articles, "get_article", lambda c, sid: {"ok": 1})
    monkeypatch.setattr(publish_mod.api_articles, "verify_article", lambda d, **k: [])
    monkeypatch.setattr(publish_mod.api_articles, "wait_written", lambda *a, **k: {})

    manuscripts = publish_mod.prepare_manuscripts(rt, spec)
    slot = publish_mod.plan(rt, spec, manuscripts)[0]
    out = publish_mod.run_slot(rt, spec, slot)
    assert out["status"] == "done"
    age = _beats(rt)
    assert age is not None and age < sched.HEARTBEAT_STALE_SECONDS
    rt.close()


def test_색인_동기화도_심장박동을_찍는다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = {"self_owned": [{"name": "고요한 아침", "cafe_id": 101}]}
    monkeypatch.setattr(article_sync, "_accounts", lambda rt_, cafe_id: ["user0"])
    monkeypatch.setattr(
        article_sync,
        "_fetch_written_articles",
        lambda rt_, cafe_id, login: [
            {"source_id": "s1", "title": "제목", "naver_login_id": login}
        ],
    )
    out = article_sync.sync_cafe_index(rt, "고요한 아침")
    assert out["rows"] == 1
    age = _beats(rt)
    assert age is not None and age < sched.HEARTBEAT_STALE_SECONDS
    rt.close()


# --------------------------------------------------------------------
# 3) 등록 확인 대기는 120초에서 끊고 다음 슬롯으로 간다
# --------------------------------------------------------------------
class _Clock:
    """가짜 단조 시계 — `sleep` 이 시간을 앞으로 민다."""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)


@pytest.fixture()
def fake_clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(api_articles.time, "sleep", clock.sleep)
    monkeypatch.setattr(api_articles.time, "monotonic", clock.monotonic)
    return clock


def _job_row(rt, spec) -> int:
    """이벤트를 달 수 있는 진짜 작업 1건(events 는 jobs 를 참조한다)."""
    return int(rt.jobs.enqueue(spec, f"stall-{id(spec)}"))


def _never_written(client, source_id):
    """서버가 끝내 "쓰는 중"만 돌려주는 상황(10:34 마이 웨딩 드림)."""
    return {"naver_cafe_article_history": {"status": "WRITING", "fail_reason": None}}


def test_등록확인_대기는_상한에서_끊긴다(fake_clock, monkeypatch):
    monkeypatch.setattr(api_articles, "get_article", _never_written)
    began = fake_clock.t
    with pytest.raises(Exception) as err:
        api_articles.wait_written(object(), "SRC-1", on_wait=None)
    assert "시간 초과" in str(err.value)
    assert fake_clock.t - began <= 130.0  # 상한 120초 + 마지막 폴링 한 번


def test_등록확인_대기는_30초마다_알린다(fake_clock, monkeypatch):
    monkeypatch.setattr(api_articles, "get_article", _never_written)
    seen: list[float] = []
    with pytest.raises(Exception):
        api_articles.wait_written(object(), "SRC-1", on_wait=seen.append)
    assert len(seen) >= 3  # 30 / 60 / 90 …
    assert all(s >= 30.0 for s in seen)


def test_등록확인이_안_되면_uncertain으로_두고_다음_슬롯으로_간다(
    tmp_path, monkeypatch, fake_clock
):
    rt = make_runtime(tmp_path)
    # 태극(테스트 카페)은 예약 없이 즉시 발행 → 등록 확인 폴링을 탄다
    spec = make_spec(cafe="태극", board="자유게시판", dry_run=False, count=2)
    job_id = _job_row(rt, spec)
    made: list[str] = []

    def fake_create(client, **kwargs):
        made.append(kwargs.get("title") or "")
        return f"SRC-{len(made)}"

    monkeypatch.setattr(publish_mod.api_articles, "create_article", fake_create)
    monkeypatch.setattr(publish_mod.api_articles, "get_article", _never_written)
    monkeypatch.setattr(publish_mod.api_articles, "verify_article", lambda d, **k: [])

    manuscripts = publish_mod.prepare_manuscripts(rt, spec)
    slots = publish_mod.plan(rt, spec, manuscripts)[:2]
    began = fake_clock.t
    outs = [publish_mod.run_slot(rt, spec, s, job_id=job_id) for s in slots]

    # 두 슬롯 다 돌았다 — 첫 슬롯이 뒤를 막지 않는다
    assert len(made) == 2
    assert [o["status"] for o in outs] == ["pending", "pending"]
    assert all("등록 확인 대기" in o["reason"] for o in outs)
    # 슬롯당 120초 남짓, 옛날처럼 30분(1,800초)을 붙잡지 않는다
    assert fake_clock.t - began <= 300.0
    # 줄은 미확정으로 남는다(reconcile 이 나중에 정리한다)
    left = rt.publications.list_uncertain()
    assert len(left) == 2
    assert {r["stage"] for r in left} == {"written_pending"}
    # 조용히 멈춰 있지 않다 — 30초마다 진행 이벤트가 남는다
    messages = [e["message"] for e in rt.events.recent(job_id, limit=100)]
    assert any(m.startswith("등록 확인 대기 3") for m in messages)
    rt.close()


def test_발행뒤_색인기록이_터져도_발행은_성공이다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, count=1)
    monkeypatch.setattr(publish_mod.api_articles, "create_article", lambda c, **k: "SRC-9")
    monkeypatch.setattr(publish_mod.api_articles, "get_article", lambda c, sid: {"ok": 1})
    monkeypatch.setattr(publish_mod.api_articles, "verify_article", lambda d, **k: [])
    monkeypatch.setattr(publish_mod.api_articles, "wait_written", lambda *a, **k: {})

    def boom(*a, **k):
        raise RuntimeError("색인 잠김")

    monkeypatch.setattr(publish_mod.article_sync, "record_published", boom)

    manuscripts = publish_mod.prepare_manuscripts(rt, spec)
    slot = publish_mod.plan(rt, spec, manuscripts)[0]
    job_id = _job_row(rt, spec)
    out = publish_mod.run_slot(rt, spec, slot, job_id=job_id)
    assert out["status"] == "done"
    assert rt.publications.by_source_id("SRC-9")["status"] == "done"
    messages = [e["message"] for e in rt.events.recent(job_id, limit=50)]
    assert any("색인 기록 실패" in m for m in messages)
    rt.close()


def test_중복관문_조회가_터져도_발행을_막지_않는다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, count=1)
    monkeypatch.setattr(publish_mod.api_articles, "create_article", lambda c, **k: "SRC-10")
    monkeypatch.setattr(publish_mod.api_articles, "get_article", lambda c, sid: {"ok": 1})
    monkeypatch.setattr(publish_mod.api_articles, "verify_article", lambda d, **k: [])
    monkeypatch.setattr(publish_mod.api_articles, "wait_written", lambda *a, **k: {})

    def boom(*a, **k):
        raise RuntimeError("색인 조회 실패")

    monkeypatch.setattr(publish_mod.duplicate, "is_duplicate_against_index", boom)

    manuscripts = publish_mod.prepare_manuscripts(rt, spec)
    slot = publish_mod.plan(rt, spec, manuscripts)[0]
    assert publish_mod.run_slot(rt, spec, slot)["status"] == "done"
    rt.close()


# --------------------------------------------------------------------
# 4) 중복 관문 앞거르기 — 1,300건에 difflib 를 다 돌리지 않는다
# --------------------------------------------------------------------
def _m(title: str) -> Manuscript:
    return Manuscript(
        title=title, body="본문입니다", account="user0", source="테스트시트", source_row=1
    )


@pytest.fixture()
def idx_rt(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.cafes_cfg = {
        "default_board": "자유게시판",
        "self_owned": [{"name": "고요한 아침", "cafe_id": 101}],
    }
    yield runtime
    runtime.close()


def test_앞거르기가_거의_같은_제목은_그대로_잡는다(idx_rt):
    old = "아이와 함께 주말 공원 나들이 다녀온 이야기입니다"
    new = "아이와 함께 주말 공원 나들이 다녀온 이야기입니다요"
    idx_rt.article_index.upsert(cafe_id=101, cafe="고요한 아침", source_id="s1", title=old)
    dup, why = duplicate.is_duplicate_against_index(idx_rt, _m(new), "고요한 아침")
    assert dup is True and "제목 유사" in why
    assert float(why.split(", ")[1].split(")")[0]) >= 0.95


def test_앞거르기는_길이가_많이_다르면_후보에서_뺀다():
    from v2r.store.article_index import normalize_title

    norm = normalize_title("주말 공원 나들이 다녀왔어요")
    rows = [
        ("주말 공원 나들이 다녀왔어요 그리고 아주 긴 뒷이야기까지 한참 더 적었습니다", "주말공원나들이다녀왔어요그리고아주긴뒷이야기까지한참더적었습니다"),
        ("가을 산책", "가을산책"),
        ("주말 공원 나들이 다녀왔어요!", normalize_title("주말 공원 나들이 다녀왔어요!")),
    ]
    got = duplicate.near_candidates(norm, rows)
    assert [t for t, _ in got] == ["주말 공원 나들이 다녀왔어요!"]
    assert duplicate.near_candidates("", rows) == []


def test_카페_제목목록은_한_번만_읽는다(idx_rt):
    for i in range(5):
        idx_rt.article_index.upsert(
            cafe_id=101, cafe="고요한 아침", source_id=f"s{i}", title=f"제목 {i}"
        )
    calls = {"n": 0}
    real = idx_rt.article_index.titles_for_cafe

    def counted(cafe):
        calls["n"] += 1
        return real(cafe)

    idx_rt.article_index.titles_for_cafe = counted  # type: ignore[assignment]
    for i in range(10):
        duplicate.is_duplicate_against_index(idx_rt, _m(f"완전히 다른 글 {i}"), "고요한 아침")
    assert calls["n"] == 1  # 10건을 검사해도 목록은 한 번만 읽는다

    # 새 글이 색인에 들어오면 캐시를 버리고 다시 읽는다
    duplicate.forget_cafe_titles(idx_rt, "고요한 아침")
    duplicate.is_duplicate_against_index(idx_rt, _m("또 다른 글"), "고요한 아침")
    assert calls["n"] == 2
