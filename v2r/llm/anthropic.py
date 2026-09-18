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
    usage_out: dict | None = None,
) -> str:
    """messages.create 한 번 호출하고 본문 텍스트만 돌려준다.

    `usage_out`을 주면 입력/출력 토큰 수를 그 dict에 더한다(비용 집계용).
    """
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if usage_out is not None:
        usage = getattr(response, "usage", None)
        usage_out["input_tokens"] = usage_out.get("input_tokens", 0) + int(
            getattr(usage, "input_tokens", 0) or 0
        )
        usage_out["output_tokens"] = usage_out.get("output_tokens", 0) + int(
            getattr(usage, "output_tokens", 0) or 0
        )
        usage_out["calls"] = usage_out.get("calls", 0) + 1
    if getattr(response, "stop_reason", None) == "refusal":
        raise LLMDisabled("모델이 요청을 거절했습니다")
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts).strip()
