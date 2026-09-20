"""용도별 모델 라우터 (DESIGN §7). 키가 없으면 해당 기능만 끄고 나머지는 동작한다."""

from __future__ import annotations

import json
import logging
from typing import Any

from .anthropic import LLMDisabled, create_message, make_client

log = logging.getLogger(__name__)

# 용도 -> 모델 ID
MODELS: dict[str, str] = {
    "ambiguous_command": "claude-haiku-4-5",
    "daily_comment": "claude-haiku-4-5",
    #: 감시견 Tier 1 — 규칙표에 없는 오류 문구 1건 분류 (하루 상한 있음)
    "error_diagnosis": "claude-haiku-4-5",
    "promo_comment": "claude-sonnet-5",
    "daily_adapt": "claude-sonnet-5",
    # 브랜드(제휴) 바이럴 원고. 지침의 모델 분리(본문/댓글)를 그대로 따른다.
    # 2026-09-19 품질 시험: 본문도 잠시 Sonnet으로 내린다(되돌리려면 claude-opus-5).
    "brand_body": "claude-sonnet-5",
    "brand_comments": "claude-sonnet-5",
}

DEFAULT_MODEL = "claude-haiku-4-5"

#: 생각(thinking)을 꺼야 하는 용도. 최신 모델은 기본이 adaptive라 글쓰기 용도에서
#: max_tokens를 전부 생각에 써 버리고 본문이 비어 돌아온다.
NO_THINKING_PURPOSES = frozenset({"brand_body", "brand_comments"})
THINKING_DISABLED = {"type": "disabled"}

#: 백만 토큰당 미국 달러 단가. **2026-09 기준 추정, 실제 요금표 확인 필요.**
#: cache_write는 입력의 1.25배, cache_read는 입력의 0.1배라는 공개 비율을 따랐다.
PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    # 2026-09 기준 추정, 실제 요금표 확인 필요
    "claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.10},
    "claude-opus-5": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
}

#: 표에 없는 모델에 쓰는 기본 단가 (Sonnet 기준)
DEFAULT_PRICE = PRICES_USD_PER_MTOK["claude-sonnet-5"]

__all__ = [
    "MODELS",
    "LLMRouter",
    "LLMDisabled",
    "extract_json",
    "estimate_cost",
    "PRICES_USD_PER_MTOK",
]


def _cost_one(usage: dict, model: str) -> float:
    price = PRICES_USD_PER_MTOK.get(model, DEFAULT_PRICE)
    return (
        int(usage.get("input_tokens", 0) or 0) * price["input"]
        + int(usage.get("output_tokens", 0) or 0) * price["output"]
        + int(usage.get("cache_creation_input_tokens", 0) or 0) * price["cache_write"]
        + int(usage.get("cache_read_input_tokens", 0) or 0) * price["cache_read"]
    ) / 1_000_000


def estimate_cost(usage: dict | None, model: str = "") -> float:
    """토큰 누계를 달러로 어림한다 (2026-09 기준 추정, 실제 요금표 확인 필요).

    `usage`에 모델별 누계(`by_model`)가 들어 있으면 모델마다 제 단가를 적용해
    더한다. 없으면 `model` 하나의 단가로 계산한다.
    """
    if not usage:
        return 0.0
    per_model = usage.get("by_model")
    if isinstance(per_model, dict) and per_model:
        return round(sum(_cost_one(u, name) for name, u in per_model.items()), 6)
    return round(_cost_one(usage, model), 6)


def extract_json(text: str) -> dict | list:
    """텍스트에서 첫 JSON 객체/배열을 꺼낸다."""
    if not text:
        raise ValueError("모델 응답이 비어 있습니다")
    start = -1
    for idx, ch in enumerate(text):
        if ch in "{[":
            start = idx
            break
    if start < 0:
        raise ValueError("모델 응답에서 JSON을 찾지 못했습니다")

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start : idx + 1])
    raise ValueError("모델 응답의 JSON이 끝나지 않았습니다")


class LLMRouter:
    """용도 이름으로 모델을 골라 호출한다."""

    def __init__(self, api_key: str = "", client: Any | None = None) -> None:
        self.api_key = (api_key or "").strip()
        self._client = client
        self.enabled = bool(self._client) or bool(self.api_key)
        #: 이 라우터로 쓴 토큰 누계 (비용 보고용)
        self.usage: dict[str, int] = {}

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "LLMRouter":
        """설정에서 키를 읽어 라우터를 만든다."""
        if settings is None:
            from ..config import get_settings

            settings = get_settings()
        return cls(api_key=getattr(settings, "anthropic_api_key", "") or "")

    def estimated_cost(self) -> float:
        """이 라우터로 쓴 토큰의 어림 비용(USD)."""
        return estimate_cost(self.usage)

    def model_for(self, purpose: str) -> str:
        """용도에 해당하는 모델 ID."""
        return MODELS.get(purpose, DEFAULT_MODEL)

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = make_client(self.api_key)
        return self._client

    def complete(self, purpose: str, system: str, user: str, max_tokens: int = 1200) -> str:
        """모델 호출 후 텍스트 반환. 키가 없으면 LLMDisabled."""
        if not self.enabled:
            raise LLMDisabled("ANTHROPIC_API_KEY가 없어 모델 기능을 사용할 수 없습니다")
        client = self._ensure_client()
        model = self.model_for(purpose)
        log.debug("LLM 호출 용도=%s 모델=%s", purpose, model)
        return create_message(
            client,
            model,
            system,
            user,
            max_tokens=max_tokens,
            usage_out=self.usage,
            thinking=THINKING_DISABLED if purpose in NO_THINKING_PURPOSES else None,
        )

    def complete_json(
        self, purpose: str, system: str, user: str, max_tokens: int = 1200
    ) -> dict | list:
        """모델 응답에서 첫 JSON 객체/배열을 꺼내 반환."""
        return extract_json(self.complete(purpose, system, user, max_tokens=max_tokens))
