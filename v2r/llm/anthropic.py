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


def system_blocks(system: Any, cache: bool = True) -> list[dict]:
    """`system` 문자열(또는 블록 목록)을 캐시 표시가 붙은 블록 목록으로 바꾼다.

    프롬프트 캐싱: 매번 똑같이 들어가는 **고정 부분**(규칙·지침·예시)을 system에
    담고 `cache_control`을 달아 두면, 두 번째 호출부터는 그 부분의 입력 토큰을
    1/10 값으로 읽는다. 키워드처럼 매번 바뀌는 부분은 user 쪽에 둔다.
    """
    if isinstance(system, list):
        return [dict(b) for b in system]
    text = system or ""
    block: dict = {"type": "text", "text": text}
    if cache and text:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def create_message(
    client: Any,
    model: str,
    system: Any,
    user: str,
    max_tokens: int = 1200,
    usage_out: dict | None = None,
    thinking: dict | None = None,
    cache_system: bool = True,
) -> str:
    """messages.create 한 번 호출하고 본문 텍스트만 돌려준다.

    `usage_out`을 주면 입력/출력 토큰 수를 그 dict에 더한다(비용 집계용).

    `thinking`: 생각(thinking) 설정. Sonnet 5 같은 최신 모델은 **기본이 adaptive**라
    `max_tokens`를 생각에 다 써 버리고 텍스트 블록이 하나도 안 오는 일이 생긴다
    (원고 생성이 "모델 응답이 비어 있습니다"로 줄줄이 실패했다). 글쓰기처럼
    생각이 필요 없는 용도는 `{"type": "disabled"}`를 넘긴다. Make 지침도 전부
    thinking을 disabled로 두고 있다.
    """
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_blocks(system, cache_system),
        "messages": [{"role": "user", "content": user}],
    }
    if thinking is not None:
        kwargs["thinking"] = thinking
    response = client.messages.create(**kwargs)
    if usage_out is not None:
        usage = getattr(response, "usage", None)
        usage_out["input_tokens"] = usage_out.get("input_tokens", 0) + int(
            getattr(usage, "input_tokens", 0) or 0
        )
        usage_out["output_tokens"] = usage_out.get("output_tokens", 0) + int(
            getattr(usage, "output_tokens", 0) or 0
        )
        for field in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            usage_out[field] = usage_out.get(field, 0) + int(getattr(usage, field, 0) or 0)
        usage_out["calls"] = usage_out.get("calls", 0) + 1
        # 모델별 누계 (비용 추정용). 용도마다 모델이 달라 합산 단가가 다르다.
        per = usage_out.setdefault("by_model", {})
        slot = per.setdefault(
            model,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "calls": 0,
            },
        )
        slot["input_tokens"] += int(getattr(usage, "input_tokens", 0) or 0)
        slot["output_tokens"] += int(getattr(usage, "output_tokens", 0) or 0)
        slot["cache_read_input_tokens"] += int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        slot["cache_creation_input_tokens"] += int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        slot["calls"] += 1
    if getattr(response, "stop_reason", None) == "refusal":
        raise LLMDisabled("모델이 요청을 거절했습니다")
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts).strip()
