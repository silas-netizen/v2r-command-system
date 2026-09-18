"""LLM 라우터와 명령 폴백 테스트."""

from __future__ import annotations

import pytest

from v2r.command.llm_fallback import interpret_with_llm
from v2r.llm.anthropic import LLMDisabled
from v2r.llm.prompts import guides_context
from v2r.llm.router import MODELS, LLMRouter, extract_json


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


def _router(text: str) -> LLMRouter:
    return LLMRouter(api_key="k", client=_FakeClient(text))


# --- 라우터 -------------------------------------------------------
def test_router_disabled_without_key():
    router = LLMRouter(api_key="")
    assert router.enabled is False
    with pytest.raises(LLMDisabled):
        router.complete("daily_comment", "시스템", "사용자")


def test_model_map_matches_design():
    assert MODELS["ambiguous_command"] == "claude-haiku-4-5"
    assert MODELS["daily_comment"] == "claude-haiku-4-5"
    assert MODELS["promo_comment"] == "claude-sonnet-5"
    assert MODELS["daily_adapt"] == "claude-sonnet-5"


def test_complete_uses_purpose_model():
    router = _router("좋은 글이네요")
    assert router.complete("promo_comment", "시스템", "사용자") == "좋은 글이네요"
    assert router._client.messages.calls[0]["model"] == "claude-sonnet-5"


def test_extract_json_object_and_array():
    assert extract_json('설명\n```json\n{"task": "status"}\n```') == {"task": "status"}
    assert extract_json('앞말 [{"title": "가"}] 뒷말') == [{"title": "가"}]
    assert extract_json('{"body": "중괄호 } 포함"}') == {"body": "중괄호 } 포함"}


def test_extract_json_failure():
    with pytest.raises(ValueError):
        extract_json("JSON이 없습니다")


def test_complete_json_via_router():
    router = _router('여기 있습니다: {"task": "status", "count": 3}')
    assert router.complete_json("ambiguous_command", "시스템", "사용자") == {
        "task": "status",
        "count": 3,
    }


def test_guides_context(tmp_path):
    folder = tmp_path / "★NEW 카페 바이럴★"
    folder.mkdir(parents=True)
    (folder / "우아덤 카페.md").write_text("# 우아덤\n지침 본문", encoding="utf-8")
    (tmp_path / "INDEX.md").write_text("# 목차", encoding="utf-8")

    context = guides_context(tmp_path)
    assert "지침 본문" in context
    assert "목차" not in context
    assert guides_context(tmp_path / "없음") == ""


# --- 명령 폴백 ----------------------------------------------------
def test_fallback_forces_dry_run():
    router = _router('{"task": "publish_daily", "count": 20, "dry_run": false}')
    spec = interpret_with_llm("일상 글 20개 적당히 올려줘", router)
    assert spec.task == "publish_daily"
    assert spec.count == 20
    assert spec.dry_run is True


def test_fallback_allows_real_run_when_stated():
    router = _router('{"task": "publish_daily", "count": 5}')
    spec = interpret_with_llm("일상 글 5개 실제 발행 해줘", router)
    assert spec.dry_run is False


def test_fallback_rejects_unknown_task():
    router = _router('{"task": "rm_rf", "notes": "셸 실행"}')
    with pytest.raises(ValueError, match="명령을 해석하지 못했습니다"):
        interpret_with_llm("서버 지워줘", router)


def test_fallback_rejects_non_json():
    router = _router("잘 모르겠습니다")
    with pytest.raises(ValueError, match="명령을 해석하지 못했습니다"):
        interpret_with_llm("뭔가 해줘", router)


def test_fallback_without_router():
    with pytest.raises(ValueError, match="명령을 해석하지 못했습니다"):
        interpret_with_llm("뭔가 해줘", LLMRouter(api_key=""))


def test_fallback_accepts_array_response():
    router = _router('[{"task": "status"}]')
    assert interpret_with_llm("어떻게 되고 있어", router).task == "status"


def test_make_client_without_key():
    from v2r.llm.anthropic import make_client

    with pytest.raises(LLMDisabled):
        make_client("")
