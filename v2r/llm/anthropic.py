"""Anthropic SDK 얇은 감싸개. 다른 모듈은 이 파일만 통해 SDK를 만진다."""

from __future__ import annotations

from typing import Any


class LLMDisabled(RuntimeError):
    """API 키가 없거나 SDK를 쓸 수 없어 모델 기능이 꺼진 상태."""


def make_client(api_key: str) -> Any:
    """Anthropic 클라이언트 생성. 키가 없으면 LLMDisabled."""
    if not api_key:
        raise LLMDisabled("ANTHROPIC_API_KEY가 없어 모델 기능을 사용할 수 없습니다")
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - 의존성 미설치 환경
        raise LLMDisabled("anthropic SDK가 설치되어 있지 않습니다") from exc
    return anthropic.Anthropic(api_key=api_key)


def create_message(
    client: Any,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 1200,
) -> str:
    """messages.create 한 번 호출하고 본문 텍스트만 돌려준다."""
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if getattr(response, "stop_reason", None) == "refusal":
        raise LLMDisabled("모델이 요청을 거절했습니다")
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts).strip()
