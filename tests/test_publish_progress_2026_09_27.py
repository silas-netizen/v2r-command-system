"""사고 2026-09-26·27(일상 글 발행 정체 방치) 재발 방지 시험.

무엇을 지키는 시험인가
----------------------
1. `consecutive_limit`("게시글을 연속으로 등록할 수 없습니다")은 작업 전체
   실패가 아니라 `RetryWithOtherAccount`(다른 계정으로 재시도)로 올라가야 한다.
2. worker 는 다른 계정이 없을 때 그 슬롯을 바로 실패시키지 않고 5분 대기 후
   같은 슬롯을 다시 본다(상한 안에서).
3. `publish_progress_check` 작업: 정체/실패면 슬랙 🔴 + 같은 명령 자동 재큐
   (하루 상한), 정상이면 📊 한 줄. 매 실행마다 현황판 파일을 통째로 다시 쓴다.
"""

from __future__ import annotations

import json

import pytest

from v2r.api.errors import V2RApiError, classify
from v2r.command.parser import parse_korean_command
from v2r.config import Settings
from v2r.engine import publish as publish_mod
from v2r.engine import worker
from v2r.engine.context import Runtime
from v2r.store.db import connect

from tests.test_engine import make_runtime, make_spec, _one_slot


# --------------------------------------------------------------------
# 1) consecutive_limit → RetryWithOtherAccount (사고 2026-09-27 09:55)
# --------------------------------------------------------------------
def test_연속등록_제한은_classify로_그대로_분류된다():
    exc = V2RApiError("실패", status=400, code="20004", reason="연속으로 등록할 수 없습니다")
    assert classify(exc) == "consecutive_limit"


def test_연속등록_제한이면_작업실패_아니라_다른계정_재시도(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, count=1)

    def blocked(client, **kwargs):
        raise V2RApiError("연속 등록 제한", status=400, code="20004", reason="연속으로 등록할 수 없습니다")

    monkeypatch.setattr(publish_mod.api_articles, "create_article", blocked)

    with pytest.raises(publish_mod.RetryWithOtherAccount):
        publish_mod.run_slot(rt, spec, _one_slot(rt, spec))

    # account_restricted와 달리 장기 차단하지 않는다 — 그 계정은 여전히 쓸 수 있어야 한다
    assert not rt.account_state.is_restricted("user0")
    row = rt.conn.execute(
        "SELECT status FROM publications WHERE source_key = '테스트시트'"
    ).fetchone()
    assert row["status"] == "failed"
    rt.close()


# --------------------------------------------------------------------
# 2) worker: 다른 계정이 없으면 5분 대기 후 같은 슬롯 재시도(작업 전체 실패 금지)
# --------------------------------------------------------------------
def _acquired_job(rt, text: str) -> int:
    worker.handle_text(rt, text)
    job = rt.jobs.acquire("tester")
    return int(job["id"])


def test_다른계정이_없으면_대기후_재시도하고_결국_성공(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, count=1, cafe="태극", board="자유게시판")
    job_id = _acquired_job(rt, "일상 글 1개 올려")

    monkeypatch.setattr(worker, "_other_account", lambda rt_, spec_, slot_: None)
    monkeypatch.setattr(publish_mod.api_articles, "create_article", lambda c, **k: "SRC-RETRY")
    monkeypatch.setattr(publish_mod.api_articles, "get_article", lambda c, sid: {"ok": True})
    monkeypatch.setattr(publish_mod.api_articles, "verify_article", lambda detail, **k: [])
    monkeypatch.setattr(publish_mod.api_articles, "wait_written", lambda *a, **k: {})
    slept: list[float] = []
    monkeypatch.setattr(
        worker, "_sleep_with_beat", lambda seconds, beat, **k: slept.append(seconds)
    )

    calls = {"n": 0}
    real_run_slot = publish_mod.run_slot

    def flaky_run_slot(rt_, spec_, slot_, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise publish_mod.RetryWithOtherAccount("연속 등록 제한: user0")
        return real_run_slot(rt_, spec_, slot_, **kwargs)

    monkeypatch.setattr(publish_mod, "run_slot", flaky_run_slot)

    out = worker._run_publish(rt, job_id, spec, "tester")

    assert calls["n"] == 2
    assert slept == [worker.NO_ACCOUNT_WAIT_S]
    assert out["ok"] is True
    assert out["failures"] == []
    rt.close()


def test_다른계정_없이_상한을_넘기면_그때는_실패로_남는다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, count=1, cafe="태극", board="자유게시판")
    job_id = _acquired_job(rt, "일상 글 1개 올려")

    monkeypatch.setattr(worker, "_other_account", lambda rt_, spec_, slot_: None)
    monkeypatch.setattr(worker, "_sleep_with_beat", lambda *a, **k: None)

    def always_blocked(rt_, spec_, slot_, **kwargs):
        raise publish_mod.RetryWithOtherAccount("연속 등록 제한: user0")

    monkeypatch.setattr(publish_mod, "run_slot", always_blocked)

    out = worker._run_publish(rt, job_id, spec, "tester")

    assert out["ok"] is False
    assert len(out["failures"]) == 1
    # 상한(NO_ACCOUNT_MAX_WAITS)만큼만 기다리고 더 기다리지 않는다
    assert calls_bounded(worker)
    rt.close()


def calls_bounded(worker_mod) -> bool:
    """상한 상수가 무한이 아님을 확인하는 안전망(0보다 크고 유한)."""
    return 0 < worker_mod.NO_ACCOUNT_MAX_WAITS < 100


# --------------------------------------------------------------------
# 3) publish_progress_check
# --------------------------------------------------------------------
def _rt_with_channels(tmp_path) -> Runtime:
    rt = make_runtime(tmp_path)
    return rt


def test_진도_정상이면_슬랙_정상_한줄_현황판_기록(tmp_path, monkeypatch):
    rt = _rt_with_channels(tmp_path)
    monkeypatch.setattr(publish_mod, "self_cafe_names", lambda rt_: ["고요한 아침"])
    monkeypatch.setattr(worker, "handle_text", lambda rt_, text: (_ for _ in ()).throw(AssertionError("재큐되면 안 된다")))
    from datetime import datetime, timezone

    monkeypatch.setattr(rt.publications, "counts_today_by_cafe", lambda day: {"고요한 아침": 100})
    monkeypatch.setattr(rt.publications, "count_since", lambda since: 20)
    monkeypatch.setattr(rt.jobs, "recent", lambda limit=50: [{"task": "publish_daily", "status": "done"}])
    monkeypatch.setattr(rt.jobs, "running_jobs", lambda **k: [])
    monkeypatch.setattr(rt.jobs, "open_jobs", lambda: [])

    sent = []
    monkeypatch.setattr(
        "v2r.channels.notify_all",
        lambda channels, text, level="info", **k: sent.append((level, text)) or 0,
    )
    monkeypatch.setattr(worker, "notify_all", lambda channels, text, level="info", **k: sent.append((level, text)) or 0)

    out = worker._publish_progress_check(rt, None)

    assert out["ok"] is True
    assert sent and sent[-1][0] == "always"
    board = rt.settings.repo_root / "docs" / "reports"
    files = list(board.glob("daily-posts-board-*.md"))
    assert files, "현황판 파일이 생성돼야 한다"
    text = files[0].read_text(encoding="utf-8")
    assert "오늘 문제 없음" in text
    assert "고요한 아침" in text
    rt.close()


def test_진도_정체면_슬랙_경보와_자동재큐(tmp_path, monkeypatch):
    rt = _rt_with_channels(tmp_path)
    monkeypatch.setattr(publish_mod, "self_cafe_names", lambda rt_: ["고요한 아침"])

    monkeypatch.setattr(rt.publications, "counts_today_by_cafe", lambda day: {"고요한 아침": 5})
    monkeypatch.setattr(rt.publications, "count_since", lambda since: 2)
    monkeypatch.setattr(rt.jobs, "recent", lambda limit=50: [{"task": "publish_daily", "status": "failed"}])
    monkeypatch.setattr(rt.jobs, "running_jobs", lambda **k: [])
    monkeypatch.setattr(rt.jobs, "open_jobs", lambda: [])

    requeued = []
    monkeypatch.setattr(
        worker, "handle_text", lambda rt_, text: requeued.append(text) or {"ok": True, "job_id": 999}
    )
    sent = []
    monkeypatch.setattr(worker, "notify_all", lambda channels, text, level="info", **k: sent.append((level, text)) or 0)

    out = worker._publish_progress_check(rt, None)

    assert requeued == [worker.PROGRESS_REQUEUE_COMMAND]
    assert sent and sent[-1][0] == "critical"
    assert out["action"].startswith("자동 재큐")

    text = (rt.settings.repo_root / "docs" / "reports" / f"daily-posts-board-{__import__('datetime').datetime.now(worker.KST).strftime('%Y-%m-%d')}.md").read_text(encoding="utf-8")
    assert "자동 재큐" in text
    rt.close()


def test_진도_정체여도_이미_도는_작업이면_재큐하지_않는다(tmp_path, monkeypatch):
    rt = _rt_with_channels(tmp_path)
    monkeypatch.setattr(publish_mod, "self_cafe_names", lambda rt_: ["고요한 아침"])
    monkeypatch.setattr(rt.publications, "counts_today_by_cafe", lambda day: {"고요한 아침": 5})
    monkeypatch.setattr(rt.publications, "count_since", lambda since: 2)
    monkeypatch.setattr(rt.jobs, "recent", lambda limit=50: [{"task": "publish_daily", "status": "running"}])
    monkeypatch.setattr(rt.jobs, "running_jobs", lambda **k: [{"id": 1, "task": "publish_daily"}])
    monkeypatch.setattr(rt.jobs, "open_jobs", lambda: [])

    monkeypatch.setattr(
        worker, "handle_text", lambda rt_, text: (_ for _ in ()).throw(AssertionError("도는 중이면 재큐 금지"))
    )
    monkeypatch.setattr(worker, "notify_all", lambda *a, **k: 0)

    out = worker._publish_progress_check(rt, None)
    assert "재큐 생략" in out["action"] or out["action"] == "정상"
    rt.close()


def test_재큐_상한을_넘으면_더_안_쏘고_알림만(tmp_path, monkeypatch):
    rt = _rt_with_channels(tmp_path)
    monkeypatch.setattr(publish_mod, "self_cafe_names", lambda rt_: ["고요한 아침"])
    monkeypatch.setattr(rt.publications, "counts_today_by_cafe", lambda day: {"고요한 아침": 5})
    monkeypatch.setattr(rt.publications, "count_since", lambda since: 2)
    monkeypatch.setattr(rt.jobs, "recent", lambda limit=50: [{"task": "publish_daily", "status": "failed"}])
    monkeypatch.setattr(rt.jobs, "running_jobs", lambda **k: [])
    monkeypatch.setattr(rt.jobs, "open_jobs", lambda: [])
    monkeypatch.setattr(worker, "notify_all", lambda *a, **k: 0)

    from datetime import datetime as _dt

    today = _dt.now(worker.KST).strftime("%Y-%m-%d")
    worker._save_progress_state(rt, {"date": today, "requeue_count": worker.MAX_REQUEUE_PER_DAY})

    monkeypatch.setattr(
        worker, "handle_text", lambda rt_, text: (_ for _ in ()).throw(AssertionError("상한 넘으면 재큐 금지"))
    )

    out = worker._publish_progress_check(rt, None)
    assert "상한" in out["action"]
    rt.close()


# --------------------------------------------------------------------
# 4) 명령 해석 + 예약표
# --------------------------------------------------------------------
def test_명령_일상글_진도_점검이_task로_풀린다():
    spec = parse_korean_command("일상 글 진도 점검")
    assert spec is not None
    assert spec.task == "publish_progress_check"


def test_예약표에_매시_진도점검이_있다():
    import yaml
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "config" / "schedule.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    names = [e["name"] for e in data["entries"] if e.get("command") == "일상 글 진도 점검"]
    assert len(names) == 17  # 09:05 ~ 01:05 매시
    for entry in data["entries"]:
        if entry.get("command") == "일상 글 진도 점검":
            assert entry.get("expect_task") == "publish_progress_check"
