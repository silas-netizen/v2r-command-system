"""요금제 길 — 명령 해석, 세션 점검, 원고 JSON·검토 MD의 `backend` 표시 테스트."""

from __future__ import annotations

import json

from v2r.command.parser import describe_spec, parse_korean_command
from v2r.content import brand_writer as bw
from v2r.engine.sidecar import LIGHT_TASKS
from v2r.llm.plan_backend import PlanBackend

# --- 명령 해석 ----------------------------------------------------


def test_plan_token_sets_backend():
    spec = parse_korean_command("우아덤 원고 1개 요금제로 만들어줘")
    assert spec.task == "generate_brand"
    assert spec.brand == "우아덤"
    assert spec.count == 1
    assert spec.llm_backend == "plan"
    assert "요금제 길" in describe_spec(spec)


def test_api_token_sets_backend():
    spec = parse_korean_command("우아덤 원고 1개 api로 만들어줘")
    assert spec.task == "generate_brand"
    assert spec.llm_backend == "api"
    assert "API 길" in describe_spec(spec)


def test_batch_token_sets_backend():
    assert parse_korean_command("우아덤 원고 1개 배치로 만들어줘").llm_backend == "batch"


def test_no_token_leaves_backend_empty():
    spec = parse_korean_command("우아덤 원고 1개 만들어줘")
    assert spec.llm_backend == ""


def test_plan_wins_over_api_when_both_written():
    # `요금제로 api로`처럼 둘 다 적으면 요금제를 따른다 (돈 안 드는 쪽)
    assert parse_korean_command("우아덤 원고 요금제로 api로 만들어줘").llm_backend == "plan"


def test_plan_keepalive_command():
    spec = parse_korean_command("요금제 세션 점검")
    assert spec.task == "plan_keepalive"
    assert parse_korean_command("클로드 코드 로그인 유지").task == "plan_keepalive"


def test_claude_code_does_not_steal_web_keepalive():
    # `웹 세션 점검`(Claude·Make)은 그대로 웹이어야 한다
    assert parse_korean_command("웹 세션 점검").task == "web_keepalive"
    assert parse_korean_command("클로드 세션 점검").task == "web_keepalive"


def test_plan_keepalive_is_a_light_task():
    # 긴 발행 작업 뒤에 줄 서면 쓸모가 없어지는 일이라 사이드카가 직접 집는다
    assert "plan_keepalive" in LIGHT_TASKS


def test_schedule_has_the_daily_check():
    from v2r.config import load_yaml

    entries = load_yaml("schedule").get("entries") or []
    rows = [e for e in entries if e.get("command") == "요금제 세션 점검"]
    assert rows and rows[0]["time"] == "09:25"
    assert rows[0]["enabled"] is True
    # 예약 문장이 실제로 그 작업으로 해석되어야 예약이 헛돌지 않는다
    assert parse_korean_command(rows[0]["command"]).task == "plan_keepalive"


# --- 세션 점검 ----------------------------------------------------
def _session_backend(tmp_path) -> PlanBackend:
    backend = PlanBackend(work_dir=tmp_path)
    backend.exe = "claude.exe"
    return backend


def test_check_session_reads_auth_status_without_calling_the_model(tmp_path, monkeypatch):
    from v2r.llm import plan_session

    called: list = []
    monkeypatch.setattr(
        plan_session,
        "auth_status",
        lambda exe, **kw: {"loggedIn": True, "authMethod": "claude.ai"},
    )
    backend = _session_backend(tmp_path)
    backend.runner = lambda *a, **k: called.append(a) or (0, b"{}", b"")
    out = plan_session.check_session(tmp_path, backend=backend)
    assert out["ok"] is True
    assert out["logged_in"] is True
    assert out["auth_method"] == "claude.ai"
    assert out["notice"] == ""
    # 모델을 한 번도 부르지 않았다 (한도를 안 쓴다)
    assert called == []


def test_check_session_notices_logged_out(tmp_path, monkeypatch):
    from v2r.llm import plan_session

    monkeypatch.setattr(plan_session, "auth_status", lambda exe, **kw: {"loggedIn": False})
    out = plan_session.check_session(tmp_path, backend=_session_backend(tmp_path))
    assert out["ok"] is False
    assert out["logged_in"] is False
    assert "claude-cli-login.cmd" in out["notice"]


def test_check_session_deep_reports_limit(tmp_path, monkeypatch):
    from v2r.llm import plan_session

    monkeypatch.setattr(plan_session, "auth_status", lambda exe, **kw: {"loggedIn": True})
    backend = _session_backend(tmp_path)
    backend.runner = lambda *a: (
        0,
        json.dumps({"is_error": True, "result": "usage limit reached"}).encode("utf-8"),
        b"",
    )
    out = plan_session.check_session(tmp_path, backend=backend, deep=True)
    # 한도에 걸렸다는 건 모델까지 닿았다는 뜻 — 로그인은 살아 있다
    assert out["limited"] is True
    assert out["logged_in"] is True
    assert out["notice"] == ""


def test_check_session_without_exe(tmp_path):
    from v2r.llm import plan_session

    backend = PlanBackend(work_dir=tmp_path)
    backend.exe = ""
    out = plan_session.check_session(tmp_path, backend=backend)
    assert out["ok"] is False
    assert "실행 파일" in out["notice"]


# --- 원고 JSON / 검토 MD ------------------------------------------
def _manuscript() -> bw.Manuscript:
    return bw.Manuscript(
        title="제목",
        body="본문입니다.",
        cafe="씨씨앙",
        keyword="리포좀비타민C",
        manuscript_type="질문형",
        source="generated:우아덤",
        comments=bw._comment_nodes({}),
        content_hash="h",
    )


_STATS = {
    "backend": "plan",
    "body_backend": "plan",
    "comments_backend": "plan",
    "prompt_sha256": {"body": "a" * 64, "comments": "b" * 64},
    "attempts": 2,
    "estimated_usd": 0.0,
}


def test_to_dict_carries_backend_and_prompt_hash():
    data = bw.to_dict(_manuscript(), dict(_STATS))
    assert data["backend"] == "plan"
    assert data["prompt_sha256"] == {"body": "a" * 64, "comments": "b" * 64}
    # 요금제 길은 구독 안이라 추가 비용 0원
    assert data["cost_usd"] == 0.0


def test_to_dict_without_stats_has_no_backend():
    assert "backend" not in bw.to_dict(_manuscript())


def test_save_json_writes_backend(tmp_path):
    path = bw.save_json(_manuscript(), tmp_path / "m.json", dict(_STATS))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["backend"] == "plan"
    assert data["prompt_sha256"]["body"] == "a" * 64


def test_review_md_shows_backend(tmp_path):
    path = bw.write_review_md(
        [_manuscript()], tmp_path / "r.md", title="검토", stats=[dict(_STATS)]
    )
    text = path.read_text(encoding="utf-8")
    assert "생성 경로(backend): **plan**" in text
    assert "추가 비용 0원" in text
    assert "프롬프트 지문(sha256)" in text
    assert "aaaaaaaaaaaaaaaa" in text


def test_review_md_without_stats_is_unchanged(tmp_path):
    text = bw.write_review_md([_manuscript()], tmp_path / "r.md").read_text(encoding="utf-8")
    assert "생성 경로(backend)" not in text


def test_api_stats_keep_their_cost():
    stats = dict(_STATS, backend="api", estimated_usd=0.17)
    assert bw.to_dict(_manuscript(), stats)["cost_usd"] == 0.17
    assert "비용 발생" in "\n".join(bw.backend_lines(stats))


# --- 작업 실행(워커) ----------------------------------------------
class _BackendLLM:
    """길을 알려 주는 라우터 대역 (`last_call`만 흉내 낸다)."""

    def __init__(self, backend: str = "plan") -> None:
        from tests.test_brand_writer import FakeLLM

        self._inner = FakeLLM()
        self.backend = backend
        self.force_backend = ""
        self.usage = {"by_backend": {backend: {"calls": 0}}}
        self.last_call: dict = {}

    def complete_json(self, purpose, system, user, max_tokens=1200):
        from v2r.llm.router import prompt_sha256

        # 명령으로 길을 못박았으면 그 길이 쓰여야 한다
        backend = self.force_backend or self.backend
        self.last_call = {
            "backend": backend,
            "purpose": purpose,
            "model": "claude-sonnet-5",
            "prompt_sha256": prompt_sha256(system, user),
        }
        self.usage["by_backend"].setdefault(backend, {"calls": 0})["calls"] += 1
        return self._inner.complete_json(purpose, system, user, max_tokens)

    def model_for(self, purpose):
        return "claude-sonnet-5"


def _brand_runtime(tmp_path, monkeypatch, backend="plan"):
    from tests.test_brand_writer import KEYWORD
    from tests.test_engine import make_runtime

    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    rt._llm = _BackendLLM(backend)
    rt._llm_ready = True
    monkeypatch.setattr(
        "v2r.sources.keyword_list.load_pushed_keywords",
        lambda brand, cfg=None, xlsx_path=None, limit=0, conn=None: [
            {"keyword": KEYWORD, "cafe": "씨씨앙"}
        ],
    )
    return rt


def test_worker_counts_manuscripts_per_backend(tmp_path, monkeypatch):
    from v2r.engine import worker

    rt = _brand_runtime(tmp_path, monkeypatch, backend="plan")
    out = worker._generate_brand(rt, parse_korean_command("우아덤 원고 1개 만들어줘"))
    assert out["ok"] is True
    assert out["backends"] == {"plan": 1}
    # 요금제 길은 추가 비용 0원
    assert out["backend_costs_usd"]["plan"] == 0.0
    assert out["estimated_usd"] == 0.0
    assert "비용 0원" in out["message"]
    rt.close()


def test_worker_forces_the_backend_from_the_command(tmp_path, monkeypatch):
    from v2r.engine import worker

    rt = _brand_runtime(tmp_path, monkeypatch, backend="api")
    out = worker._generate_brand(
        rt, parse_korean_command("우아덤 원고 1개 요금제로 만들어줘")
    )
    assert out["llm_backend"] == "plan"
    assert out["backends"] == {"plan": 1}
    # 끝나고 나면 원래대로 돌려놓는다 (다음 작업에 번지지 않게)
    assert rt.llm.force_backend == ""
    rt.close()


def test_worker_review_md_and_json_show_the_backend(tmp_path, monkeypatch):
    from pathlib import Path

    from tests.test_brand_writer import KEYWORD
    from v2r.engine import worker

    rt = _brand_runtime(tmp_path, monkeypatch, backend="plan")
    out = worker._generate_brand(rt, parse_korean_command("우아덤 원고 1개 만들어줘"))
    text = Path(out["report"]).read_text(encoding="utf-8")
    assert "생성 경로(backend): **plan**" in text
    saved = json.loads(
        (
            tmp_path / "warehouse" / "manuscripts" / "generated" / "우아덤" / f"{KEYWORD}.json"
        ).read_text(encoding="utf-8")
    )
    assert saved["backend"] == "plan"
    # 본문·댓글 각각의 프롬프트 지문 (64자 sha256)
    assert len(saved["prompt_sha256"]["body"]) == 64
    assert len(saved["prompt_sha256"]["comments"]) == 64
    assert saved["prompt_sha256"]["body"] != saved["prompt_sha256"]["comments"]
    assert saved["cost_usd"] == 0.0
    rt.close()
