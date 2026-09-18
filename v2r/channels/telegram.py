"""텔레그램 채널. 허용 chat_id 목록이 있어야만 동작한다."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .base import IncomingCommand

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TIMEOUT = 30.0
LONG_POLL = 20


class TelegramChannel:
    """텔레그램 봇 폴링 채널. 메시지로 셸을 실행하지 않는다."""

    name = "telegram"

    def __init__(
        self,
        token: str,
        allowed_chat_ids: set[str] | None = None,
        data_dir: Path | None = None,
        client: httpx.Client | None = None,
        allowed_user_ids: set[str] | None = None,
    ) -> None:
        self.token = (token or "").strip()
        self.allowed_chat_ids = {str(c).strip() for c in (allowed_chat_ids or set()) if str(c).strip()}
        # 비어 있으면 발신자 제한 없음(허용 방 전체). 그룹방이면 지정 권장.
        self.allowed_user_ids = {
            str(u).strip() for u in (allowed_user_ids or set()) if str(u).strip()
        }
        self.data_dir = Path(data_dir) if data_dir else _default_data_dir()
        self._client = client
        # 토큰과 허용 목록이 둘 다 있어야 활성
        self.enabled = bool(self.token and self.allowed_chat_ids)

    # --- 내부 도구 -------------------------------------------------
    @property
    def _offset_path(self) -> Path:
        return self.data_dir / "telegram_offset.json"

    def _load_offset(self) -> int:
        try:
            raw = json.loads(self._offset_path.read_text(encoding="utf-8"))
            return int(raw.get("offset", 0))
        except Exception:
            return 0

    def _save_offset(self, offset: int) -> None:
        try:
            self._offset_path.parent.mkdir(parents=True, exist_ok=True)
            self._offset_path.write_text(
                json.dumps({"offset": int(offset)}), encoding="utf-8"
            )
        except OSError as exc:  # 저장 실패해도 동작은 계속
            log.warning("텔레그램 offset 저장 실패: %s", exc)

    def _mask(self, text: str) -> str:
        """메시지에서 봇 토큰을 지운다(안전 불변식 2: 토큰을 로그에 남기지 않음)."""
        return text.replace(self.token, "***") if self.token else text

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        url = f"{API_BASE}/bot{self.token}/{method}"
        try:
            if self._client is not None:
                resp = self._client.post(url, json=payload, timeout=TIMEOUT)
            else:
                with httpx.Client(timeout=TIMEOUT) as client:
                    resp = client.post(url, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            # 예외 메시지에 봇 토큰이 담긴 URL이 통째로 들어가므로 상태코드만 남긴다
            log.warning("텔레그램 %s HTTP %s", method, exc.response.status_code)
            return None
        except Exception as exc:
            log.warning("텔레그램 %s 호출 실패: %s", method, self._mask(str(exc)))
            return None
        if not isinstance(data, dict) or not data.get("ok"):
            log.warning("텔레그램 %s 응답 오류", method)
            return None
        return data

    # --- 채널 규약 -------------------------------------------------
    def poll(self) -> list[IncomingCommand]:
        """getUpdates로 새 메시지를 읽는다. 허용되지 않은 방은 버린다."""
        if not self.enabled:
            return []
        offset = self._load_offset()
        payload: dict[str, Any] = {"timeout": LONG_POLL, "allowed_updates": ["message"]}
        if offset:
            payload["offset"] = offset
        data = self._call("getUpdates", payload)
        if data is None:
            return []

        commands: list[IncomingCommand] = []
        max_update_id = offset - 1
        for update in data.get("result") or []:
            update_id = int(update.get("update_id", 0))
            max_update_id = max(max_update_id, update_id)
            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            chat_id = str((message.get("chat") or {}).get("id", ""))
            sender_id = str((message.get("from") or {}).get("id", ""))
            if not text or not chat_id:
                continue
            if chat_id not in self.allowed_chat_ids:
                log.warning("허용되지 않은 텔레그램 chat_id 무시: %s", chat_id)
                continue
            if self.allowed_user_ids and sender_id not in self.allowed_user_ids:
                log.warning("허용되지 않은 텔레그램 발신자 무시: %s", sender_id)
                continue
            commands.append(
                IncomingCommand(
                    channel=self.name,
                    sender_id=sender_id,
                    chat_id=chat_id,
                    text=text,
                    raw=update,
                )
            )
        if max_update_id >= offset:
            self._save_offset(max_update_id + 1)
        return commands

    def send(self, chat_id: str, text: str) -> bool:
        """sendMessage로 보고를 보낸다."""
        if not self.enabled:
            return False
        chat_id = str(chat_id)
        if chat_id not in self.allowed_chat_ids:
            log.warning("허용되지 않은 텔레그램 chat_id 발신 차단: %s", chat_id)
            return False
        data = self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
        return data is not None

    def broadcast(self, text: str) -> int:
        """허용된 모든 chat_id에 보고를 보낸다."""
        return sum(1 for chat_id in sorted(self.allowed_chat_ids) if self.send(chat_id, text))


def _default_data_dir() -> Path:
    """설정의 data 폴더. 설정 로드 실패 시 저장소 기준 기본값."""
    try:
        from ..config import get_settings

        return get_settings().data_dir
    except Exception:
        return Path(__file__).resolve().parent.parent.parent / "data"
