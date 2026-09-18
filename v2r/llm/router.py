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
    "promo_comment": "claude-sonnet-5",
    "daily_adapt": "claude-sonnet-5",
}

DEFAULT_MODEL = "claude-haiku-4-5"

__all__ = ["MODELS", "LLMRouter", "LLMDisabled", "extract_json"]


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

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "LLMRouter":
        """설정에서 키를 읽어 라우터를 만든다."""
        if settings is None:
            from ..config import get_settings

            settings = get_settings()
        return cls(api_key=getattr(settings, "anthropic_api_key", "") or "")

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
        return create_message(client, model, system, user, max_tokens=max_tokens)

    def complete_json(
        self, purpose: str, system: str, user: str, max_tokens: int = 1200
    ) -> dict | list:
        """모델 응답에서 첫 JSON 객체/배열을 꺼내 반환."""
        return extract_json(self.complete(purpose, system, user, max_tokens=max_tokens))
