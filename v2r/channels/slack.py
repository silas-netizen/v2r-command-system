"""슬랙 채널. 수신은 허용 채널 목록이 반드시 필요하다(전체 허용 금지)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .base import IncomingCommand

log = logging.getLogger(__name__)

API_BASE = "https://slack.com/api"
TIMEOUT = 30.0


def _ts(value: str) -> float:
    """슬랙 `ts`를 수치로. 문자열 비교는 자릿수가 바뀌면 틀린다."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class SlackChannel:
    """슬랙 봇 채널. webhook만 있으면 발신 전용으로 동작한다."""

    name = "slack"

    def __init__(
        self,
        bot_token: str = "",
        allowed_channel_ids: set[str] | None = None,
        webhook_url: str = "",
        data_dir: Path | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        self.allowed_channel_ids = {
            str(c).strip() for c in (allowed_channel_ids or set()) if str(c).strip()
        }
        self.webhook_url = (webhook_url or "").strip()
        self.data_dir = Path(data_dir) if data_dir else _default_data_dir()
        self._client = client
        # 수신은 봇 토큰 + 허용 채널 목록이 둘 다 있을 때만
        self.can_receive = bool(self.bot_token and self.allowed_channel_ids)
        self.webhook_only = not self.can_receive and bool(self.webhook_url)
        self.enabled = self.can_receive or bool(self.webhook_url)

    # --- 내부 도구 -------------------------------------------------
    @property
    def _cursor_path(self) -> Path:
        return self.data_dir / "slack_cursor.json"

    def _load_cursors(self) -> dict[str, str]:
        try:
            raw = json.loads(self._cursor_path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _save_cursors(self, cursors: dict[str, str]) -> None:
        try:
            self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
            self._cursor_path.write_text(
                json.dumps(cursors, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("슬랙 cursor 저장 실패: %s", exc)

    def _request(self, method: str, **kwargs: Any) -> httpx.Response | None:
        try:
            if self._client is not None:
                return self._client.request(method=kwargs.pop("verb", "GET"), timeout=TIMEOUT, **kwargs)
            with httpx.Client(timeout=TIMEOUT) as client:
                return client.request(method=kwargs.pop("verb", "GET"), timeout=TIMEOUT, **kwargs)
        except Exception as exc:
            log.warning("슬랙 %s 요청 실패: %s", method, exc)
            return None

    def _api(self, api_method: str, *, verb: str, params: dict | None = None, payload: dict | None = None) -> dict | None:
        headers = {"Authorization": f"Bearer {self.bot_token}"}
        kwargs: dict[str, Any] = {"url": f"{API_BASE}/{api_method}", "headers": headers, "verb": verb}
        if params:
            kwargs["params"] = params
        if payload is not None:
            kwargs["json"] = payload
            headers["Content-Type"] = "application/json; charset=utf-8"
        resp = self._request(api_method, **kwargs)
        if resp is None:
            return None
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("슬랙 %s 응답 해석 실패: %s", api_method, exc)
            return None
        if not isinstance(data, dict) or not data.get("ok"):
            log.warning("슬랙 %s 응답 오류: %s", api_method, (data or {}).get("error"))
            return None
        return data

    # --- 채널 규약 -------------------------------------------------
    def poll(self) -> list[IncomingCommand]:
        """허용 채널들의 새 메시지만 읽는다. 봇 메시지는 제외."""
        if not self.can_receive:
            return []
        cursors = self._load_cursors()
        commands: list[IncomingCommand] = []
        for channel_id in sorted(self.allowed_channel_ids):
            params: dict[str, Any] = {"channel": channel_id, "limit": 50}
            last_ts = cursors.get(channel_id)
            if last_ts:
                params["oldest"] = last_ts
            data = self._api("conversations.history", verb="GET", params=params)
            if data is None:
                continue
            newest = last_ts or "0"
            for message in reversed(data.get("messages") or []):
                ts = str(message.get("ts", "0"))
                if _ts(ts) > _ts(newest):
                    newest = ts
                if last_ts and _ts(ts) <= _ts(last_ts):
                    continue
                if message.get("bot_id") or message.get("subtype"):
                    continue  # 봇/시스템 메시지는 명령으로 보지 않는다
                text = (message.get("text") or "").strip()
                if not text:
                    continue
                commands.append(
                    IncomingCommand(
                        channel=self.name,
                        sender_id=str(message.get("user", "")),
                        chat_id=channel_id,
                        text=text,
                        raw=message,
                    )
                )
            cursors[channel_id] = newest
        self._save_cursors(cursors)
        return commands

    def send(self, chat_id: str, text: str) -> bool:
        """chat.postMessage(또는 webhook 전용 모드에서는 webhook)로 보고."""
        if self.can_receive:
            chat_id = str(chat_id)
            if chat_id not in self.allowed_channel_ids:
                log.warning("허용되지 않은 슬랙 채널 발신 차단: %s", chat_id)
                return False
            data = self._api(
                "chat.postMessage", verb="POST", payload={"channel": chat_id, "text": text}
            )
            return data is not None
        return self._send_webhook(text)

    def _send_webhook(self, text: str) -> bool:
        if not self.webhook_url:
            return False
        resp = self._request("webhook", url=self.webhook_url, json={"text": text}, verb="POST")
        if resp is None:
            return False
        return 200 <= resp.status_code < 300

    def broadcast(self, text: str) -> int:
        """허용된 모든 채널(또는 webhook)에 보고."""
        if self.can_receive:
            return sum(1 for cid in sorted(self.allowed_channel_ids) if self.send(cid, text))
        return 1 if self._send_webhook(text) else 0


def _default_data_dir() -> Path:
    """설정의 data 폴더. 설정 로드 실패 시 저장소 기준 기본값."""
    try:
        from ..config import get_settings

        return get_settings().data_dir
    except Exception:
        return Path(__file__).resolve().parent.parent.parent / "data"
