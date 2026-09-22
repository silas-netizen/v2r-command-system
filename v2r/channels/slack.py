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

    # --- 파일 전송(새 업로드 방식) -----------------------------------
    # 슬랙은 2024년 이후 files.upload를 없애고 3단계 업로드로 바꿨다:
    # 1) files.getUploadURLExternal 로 업로드 URL·file_id 발급
    # 2) 그 URL에 파일 바이트를 그대로 POST
    # 3) files.completeUploadExternal 로 채널에 게시(초기 댓글 포함)
    # 봇 토큰이 없으면(webhook 전용) 파일을 보낼 방법이 없어 0을 반환한다.
    def _upload_file(self, chat_id: str, path: Any, caption: str, *, mimetype: str) -> bool:
        if not self.bot_token:
            return False
        chat_id = str(chat_id)
        if chat_id not in self.allowed_channel_ids:
            log.warning("허용되지 않은 슬랙 채널 파일 전송 차단: %s", chat_id)
            return False
        file_path = Path(path)
        if not file_path.is_file():
            log.warning("보낼 파일이 없습니다: %s", file_path.name)
            return False
        try:
            data = file_path.read_bytes()
        except OSError as exc:
            log.warning("파일 읽기 실패: %s", exc)
            return False

        step1 = self._api(
            "files.getUploadURLExternal",
            verb="GET",
            params={"filename": file_path.name, "length": len(data)},
        )
        if step1 is None:
            return False
        upload_url = step1.get("upload_url")
        file_id = step1.get("file_id")
        if not upload_url or not file_id:
            log.warning("슬랙 files.getUploadURLExternal 응답에 upload_url/file_id 없음")
            return False

        resp = self._request(
            "upload",
            url=upload_url,
            files={"file": (file_path.name, data, mimetype)},
            verb="POST",
        )
        if resp is None:
            return False
        try:
            resp.raise_for_status()
        except Exception as exc:
            log.warning("슬랙 파일 업로드 실패: %s", exc)
            return False

        step3 = self._api(
            "files.completeUploadExternal",
            verb="POST",
            payload={
                "files": [{"id": file_id, "title": file_path.name}],
                "channel_id": chat_id,
                "initial_comment": (caption or "")[:1000],
            },
        )
        return step3 is not None

    def send_document(self, chat_id: str, path: Any, caption: str = "") -> bool:
        """파일 한 개를 채널에 올린다(HTML·MD 등 원본 그대로)."""
        return self._upload_file(chat_id, path, caption, mimetype="application/octet-stream")

    def broadcast_document(self, path: Any, caption: str = "") -> int:
        """허용된 모든 채널에 파일을 보낸다. 봇 토큰 없으면(webhook 전용) 0."""
        if not self.bot_token:
            return 0
        return sum(
            1
            for chat_id in sorted(self.allowed_channel_ids)
            if self.send_document(chat_id, path, caption)
        )

    def send_photo(self, chat_id: str, path: Any, caption: str = "") -> bool:
        """사진 한 장을 채널에 올린다."""
        return self._upload_file(chat_id, path, caption, mimetype="image/jpeg")

    def broadcast_photo(self, path: Any, caption: str = "") -> int:
        """허용된 모든 채널에 사진을 보낸다. 봇 토큰 없으면(webhook 전용) 0."""
        if not self.bot_token:
            return 0
        return sum(
            1
            for chat_id in sorted(self.allowed_channel_ids)
            if self.send_photo(chat_id, path, caption)
        )


    def check_connection(self) -> dict[str, Any]:
        """연결 점검: auth.test → 허용 채널별 conversations.info → 확인 메시지 1건.

        토큰 값은 절대 담지 않는다(안전 불변식 2).
        """
        result: dict[str, Any] = {
            "ok": False,
            "has_bot_token": bool(self.bot_token),
            "has_webhook": bool(self.webhook_url),
            "channels": [],
        }
        if not self.bot_token:
            result["note"] = "봇 토큰 없음(webhook 전용, 파일 전송·수신 불가)"
            if self.webhook_url:
                result["webhook_ok"] = self._send_webhook("연결 확인")
                result["ok"] = bool(result["webhook_ok"])
            return result

        auth = self._api("auth.test", verb="POST", payload={})
        if auth is None:
            result["note"] = "auth.test 실패(봇 토큰 확인 필요)"
            return result
        result["team"] = auth.get("team")
        result["bot_name"] = auth.get("user")

        for channel_id in sorted(self.allowed_channel_ids):
            info = self._api(
                "conversations.info", verb="GET", params={"channel": channel_id}
            )
            entry: dict[str, Any] = {"channel_id": channel_id}
            if info is None:
                entry["found"] = False
                entry["note"] = "채널 정보를 못 읽음(앱 초대 여부·채널 ID 확인)"
            else:
                chan = info.get("channel") or {}
                entry["found"] = True
                entry["name"] = chan.get("name")
                entry["is_private"] = bool(chan.get("is_private"))
                entry["is_member"] = bool(chan.get("is_member"))
                sent = self.send(channel_id, "연결 확인")
                entry["message_sent"] = sent
            result["channels"].append(entry)

        result["ok"] = bool(result["channels"]) and all(
            c.get("found") and c.get("message_sent") for c in result["channels"]
        )
        return result


def _default_data_dir() -> Path:
    """설정의 data 폴더. 설정 로드 실패 시 저장소 기준 기본값."""
    try:
        from ..config import get_settings

        return get_settings().data_dir
    except Exception:
        return Path(__file__).resolve().parent.parent.parent / "data"
