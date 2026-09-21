"""요금제(구독) 길 — Claude Code CLI 백엔드와 길 폴백 테스트.

실제 `claude.exe`를 부르지 않는다. 실행기(`runner`)와 anthropic 클라이언트를
전부 대역으로 갈아 끼우고, **어떤 문자열이 넘어갔는지**만 본다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from v2r.llm.plan_backend import (
    PlanBackend,
    PlanError,
    PlanLimit,
    PlanNotLoggedIn,
    parse_result,
)
from v2r.llm.router import (
    DEFAULT_BACKEND_ORDER,
    LLMRouter,
    normalize_backend_order,
    prompt_sha256,
)

# --- CLI 결과 JSON 파싱 -------------------------------------------

#: 실제 CLI가 돌려준 모양 그대로 (2026-09-21 실측)
OK_JSON = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 1,
    "session_id": "eacf6fcb",
    "total_cost_usd": 0.002778,
    "result": '{"title": "제목", "body": "본문"}',
    "usage": {
        "input_tokens": 2,
        "output_tokens": 3,
        "cache_creation_input_tokens": 686,
        "cache_read_input_tokens": 0,
    },
}

NOT_LOGGED_IN_JSON = {
    "type": "result",
    "is_error": True,
    "terminal_reason": "api_error",
    "result": "Not logged in · Please run /login",
    "usage": {},
    "total_cost_usd": 0,
}

LIMIT_JSON = {
    "type": "result",
    "is_error": True,
    "result": "Claude usage limit reached. Please try again later.",
    "usage": {},
}


def _runner(payload, code: int = 0, err: bytes = b""):
    """`(argv, stdin, cwd, env)`를 기록하고 정해진 JSON을 돌려주는 대역."""
    seen: dict = {}

    def run(argv, stdin, cwd, env):
        seen["argv"] = list(argv)
        seen["stdin"] = stdin
        seen["cwd"] = cwd
        seen["env"] = dict(env)
        # 임시 시스템 프롬프트 파일은 호출이 끝나면 지워지므로 지금 읽어 둔다
        if "--system-prompt-file" in argv:
            path = argv[argv.index("--system-prompt-file") + 1]
            with open(path, encoding="utf-8") as fp:
                seen["system"] = fp.read()
        raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return code, raw, err

    run.seen = seen  # type: ignore[attr-defined]
    return run


def _backend(tmp_path, payload, **kw) -> PlanBackend:
    backend = PlanBackend(work_dir=tmp_path, runner=_runner(payload, **kw))
    backend.exe = "claude.exe"  # 실행 파일 찾기를 건너뛴다
    return backend


def test_parse_result_reads_body_and_usage():
    body, info = parse_result(json.dumps(OK_JSON))
    assert json.loads(body) == {"title": "제목", "body": "본문"}
    assert info["usage"]["cache_creation_input_tokens"] == 686
    # 요금제 안이라 우리가 더 내는 돈은 0이다 (CLI가 적어 준 값은 참고용)
    assert info["cost_usd"] == 0.0
    assert info["reported_cost_usd"] == pytest.approx(0.002778)


def test_parse_result_classifies_not_logged_in():
    with pytest.raises(PlanNotLoggedIn):
        parse_result(json.dumps(NOT_LOGGED_IN_JSON))


def test_parse_result_classifies_limit():
    with pytest.raises(PlanLimit):
        parse_result(json.dumps(LIMIT_JSON))


def test_parse_result_rejects_garbage():
    with pytest.raises(PlanError):
        parse_result("이건 JSON이 아닙니다")


# --- 호출 모양 ----------------------------------------------------
def test_user_prompt_goes_through_stdin(tmp_path):
    backend = _backend(tmp_path, OK_JSON)
    backend.complete("claude-sonnet-5", "지침 가나다", "키워드 리포좀비타민C")
    seen = backend.runner.seen
    # 한글이 명령줄이 아니라 stdin으로, UTF-8 그대로 간다
    assert seen["stdin"] == "키워드 리포좀비타민C".encode("utf-8")
    assert "키워드 리포좀비타민C" not in " ".join(seen["argv"])
    assert seen["env"]["PYTHONIOENCODING"] == "utf-8"


def test_argv_has_the_options_we_verified(tmp_path):
    backend = _backend(tmp_path, OK_JSON)
    backend.complete("claude-sonnet-5", "지침", "요청")
    argv = backend.runner.seen["argv"]
    assert "-p" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--input-format") + 1] == "text"
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--disallowedTools") + 1] == "*"
    assert argv[argv.index("--setting-sources") + 1] == ""
    # `--bare`는 구독 로그인을 안 읽어 요금제 길이 깨진다 (2026-09-21 실측)
    assert "--bare" not in argv


def test_system_prompt_goes_to_a_file_verbatim(tmp_path):
    written: dict = {}

    def run(argv, stdin, cwd, env):
        path = argv[argv.index("--system-prompt-file") + 1]
        written["text"] = open(path, encoding="utf-8").read()
        return 0, json.dumps(OK_JSON).encode("utf-8"), b""

    backend = PlanBackend(work_dir=tmp_path, runner=run)
    backend.exe = "claude.exe"
    system = "브랜드 규칙\n줄바꿈도 그대로\t탭도"
    backend.complete("claude-sonnet-5", system, "요청")
    assert written["text"] == system


def test_api_key_is_removed_from_child_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-something")
    backend = _backend(tmp_path, OK_JSON)
    backend.complete("claude-sonnet-5", "지침", "요청")
    # 키가 남아 있으면 구독이 아니라 돈 나가는 API 길로 붙는다
    assert "ANTHROPIC_API_KEY" not in backend.runner.seen["env"]


def test_plan_usage_is_counted_with_zero_cost(tmp_path):
    from v2r.llm.router import estimate_cost

    backend = _backend(tmp_path, OK_JSON)
    usage: dict = {}
    backend.complete("claude-sonnet-5", "지침", "요청", usage_out=usage)
    plan = usage["by_backend"]["plan"]
    assert plan["calls"] == 1
    assert plan["cache_creation_input_tokens"] == 686
    assert plan["cost_usd"] == 0.0
    # 단가표를 태우는 자리(by_model)에는 안 들어간다 → 비용 0
    assert "by_model" not in usage
    assert estimate_cost(usage) == 0.0


# --- 길 차례 / 폴백 -----------------------------------------------
class _FakeMessages:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        block = type("Block", (), {"type": "text", "text": self.text})()
        return type("Resp", (), {"content": [block], "stop_reason": "end_turn"})()


class _FakeClient:
    def __init__(self, text: str):
        self.messages = _FakeMessages(text)


def _router(tmp_path, plan_payload, order=("plan", "batch", "api"), api_text="API 응답"):
    backend = _backend(tmp_path, plan_payload)
    return LLMRouter(
        api_key="k",
        client=_FakeClient(api_text),
        backend_order=order,
        plan=backend,
        data_dir=tmp_path,
    )


def test_normalize_backend_order():
    assert normalize_backend_order(["plan", "api"]) == ("plan", "api")
    assert normalize_backend_order("plan, api") == ("plan", "api")
    assert normalize_backend_order(["엉뚱한길"]) == DEFAULT_BACKEND_ORDER
    assert normalize_backend_order(None) == DEFAULT_BACKEND_ORDER
    assert normalize_backend_order(["api", "api"]) == ("api",)


def test_plan_first_then_api_is_untouched(tmp_path):
    router = _router(tmp_path, OK_JSON)
    out = router.complete("brand_body", "지침", "요청")
    assert json.loads(out) == {"title": "제목", "body": "본문"}
    assert router.last_call["backend"] == "plan"
    # 요금제로 됐으면 API는 건드리지 않는다
    assert router._client.messages.calls == []


def test_batch_backend_is_skipped_for_now(tmp_path):
    router = _router(tmp_path, NOT_LOGGED_IN_JSON, order=("batch", "api"))
    assert router.complete("brand_body", "지침", "요청") == "API 응답"
    assert router.last_call["backend"] == "api"


def test_not_logged_in_falls_back_to_api_and_never_retries(tmp_path, caplog):
    router = _router(tmp_path, NOT_LOGGED_IN_JSON)
    with caplog.at_level("WARNING"):
        assert router.complete("brand_body", "지침", "요청") == "API 응답"
        assert router.complete("brand_body", "지침", "요청2") == "API 응답"
    assert router.last_call["backend"] == "api"
    # 요금제 길은 첫 번에만 해 보고 그 뒤로는 아예 안 부른다
    assert router._plan_disabled is True
    assert router.plan_backend().runner.seen["stdin"] == "요청".encode("utf-8")
    # 경고는 딱 한 번
    assert sum("로그인되어 있지 않아" in r.message for r in caplog.records) == 1


@pytest.fixture
def _no_wait(monkeypatch):
    """한도 재시도 30초를 기다리지 않게 한다."""
    import time

    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    return slept


def test_limit_locks_plan_only_after_a_second_try(tmp_path, _no_wait):
    """한도 문구를 **두 번** 봐야 잠근다 (2026-09-22 오탐 대책)."""
    from v2r.llm.router import PLAN_LIMIT_RETRY_SECONDS

    router = _router(tmp_path, LIMIT_JSON)
    assert router.complete("brand_body", "지침", "요청") == "API 응답"
    # 30초 기다렸고, CLI를 두 번 불렀다
    assert _no_wait == [PLAN_LIMIT_RETRY_SECONDS]
    lock = json.loads((tmp_path / "plan_lock.json").read_text(encoding="utf-8"))
    assert "usage limit reached" in lock["first_error"]
    alert = next((tmp_path.parent / "docs" / "reports" / "alerts").glob("plan-lock-*.md"))
    text = alert.read_text(encoding="utf-8")
    # 알림에 claude 오류 원문이 그대로 들어간다
    assert "claude 오류 원문" in text
    assert "Claude usage limit reached. Please try again later." in text
    assert "plan_lock.json" in text


def test_one_off_limit_message_does_not_lock(tmp_path, _no_wait):
    """1차는 한도, 2차는 성공 → 잠그지 않고 요금제 길을 그대로 쓴다."""
    answers = [LIMIT_JSON, OK_JSON]

    def run(argv, stdin, cwd, env):
        return 0, json.dumps(answers.pop(0)).encode("utf-8"), b""

    backend = PlanBackend(work_dir=tmp_path, runner=run)
    backend.exe = "claude.exe"
    router = LLMRouter(
        api_key="k",
        client=_FakeClient("API 응답"),
        backend_order=("plan", "api"),
        plan=backend,
        data_dir=tmp_path,
    )
    out = router.complete("brand_body", "지침", "요청")
    assert json.loads(out) == {"title": "제목", "body": "본문"}
    assert router.last_call["backend"] == "plan"
    assert not (tmp_path / "plan_lock.json").exists()


def test_transient_error_text_is_not_read_as_a_limit():
    """'please try again' 같은 흔한 일시 오류로는 잠그지 않는다."""
    for text in ("Error: something went wrong, please try again", "stream rate too slow"):
        payload = dict(LIMIT_JSON, result=text)
        with pytest.raises(PlanError) as got:
            parse_result(json.dumps(payload))
        assert not isinstance(got.value, PlanLimit)


def test_timeout_is_not_a_limit(tmp_path):
    """응답이 늦은 것은 한도가 아니다 — 5시간 잠그면 안 된다."""
    import subprocess

    def run(argv, stdin, cwd, env):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    backend = PlanBackend(work_dir=tmp_path, runner=run)
    backend.exe = "claude.exe"
    with pytest.raises(PlanError) as got:
        backend.complete("claude-sonnet-5", "지침", "요청")
    assert not isinstance(got.value, PlanLimit)


def test_limit_locks_plan_for_five_hours(tmp_path, _no_wait):
    router = _router(tmp_path, LIMIT_JSON)
    assert router.complete("brand_body", "지침", "요청") == "API 응답"
    lock = json.loads((tmp_path / "plan_lock.json").read_text(encoding="utf-8"))
    until = datetime.fromisoformat(lock["until"])
    locked_at = datetime.fromisoformat(lock["locked_at"])
    assert lock["hours"] == 5
    assert timedelta(hours=4, minutes=59) < (until - locked_at) < timedelta(hours=5, minutes=1)
    assert router.plan_locked_until() is not None


def test_locked_plan_is_skipped_without_calling_cli(tmp_path):
    router = _router(tmp_path, OK_JSON)
    router.lock_plan("한도")
    assert router.complete("brand_body", "지침", "요청") == "API 응답"
    # CLI를 아예 부르지 않았다
    assert router.plan_backend().runner.seen == {}


def test_expired_lock_lets_plan_run_again(tmp_path):
    router = _router(tmp_path, OK_JSON)
    (tmp_path / "plan_lock.json").write_text(
        json.dumps({"until": (datetime.now().astimezone() - timedelta(minutes=1)).isoformat()}),
        encoding="utf-8",
    )
    assert router.plan_locked_until() is None
    router.complete("brand_body", "지침", "요청")
    assert router.last_call["backend"] == "plan"


def test_env_variable_forces_one_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("V2R_LLM_BACKEND", "api")
    router = _router(tmp_path, OK_JSON)
    assert router.complete("brand_body", "지침", "요청") == "API 응답"
    assert router.plan_backend().runner.seen == {}


def test_forced_plan_does_not_fall_back_to_api(tmp_path):
    router = _router(tmp_path, LIMIT_JSON)
    router.force_backend = "plan"
    from v2r.llm.anthropic import LLMDisabled

    with pytest.raises(LLMDisabled):
        router.complete("brand_body", "지침", "요청")
    assert router._client.messages.calls == []


# --- 프롬프트 동일성 (핵심) ---------------------------------------
def test_both_backends_get_byte_identical_prompts(tmp_path):
    """같은 (system, user)면 요금제 길과 API 길에 **똑같은 글자**가 간다."""
    system = "브랜드 규칙\n- 한 줄 40자\n- 이모지 금지 🙂"
    user = "키워드: 리포좀비타민C\n카페: 씨씨앙"

    plan_router = _router(tmp_path / "plan", OK_JSON, order=("plan",))
    plan_router.complete("brand_body", system, user)
    seen = plan_router.plan_backend().runner.seen
    # 임시 파일은 호출이 끝나며 지워지므로 대역이 그때 읽어 둔 값을 쓴다
    plan_system = seen["system"]
    plan_user = seen["stdin"].decode("utf-8")

    api_router = _router(tmp_path / "api", OK_JSON, order=("api",))
    api_router.complete("brand_body", system, user)
    sent = api_router._client.messages.calls[0]
    api_system = sent["system"][0]["text"]
    api_user = sent["messages"][0]["content"]

    assert plan_system == api_system == system
    assert plan_user == api_user == user
    # 그래서 지문도 같다
    assert (
        plan_router.last_call["prompt_sha256"]
        == api_router.last_call["prompt_sha256"]
        == prompt_sha256(system, user)
    )
    assert plan_router.last_call["backend"] == "plan"
    assert api_router.last_call["backend"] == "api"


def test_prompt_sha256_is_sensitive_to_the_boundary():
    # 이어 붙이기만 하면 ("가나","다") 와 ("가","나다")가 같아져 버린다
    assert prompt_sha256("가나", "다") != prompt_sha256("가", "나다")
