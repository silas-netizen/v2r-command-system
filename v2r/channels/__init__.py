"""채널 묶음: 설정에서 사용 가능한 채널을 만들고 일괄 보고한다."""

from __future__ import annotations

import logging
import re
from typing import Any

from .base import Channel, IncomingCommand, format_report
from .slack import SlackChannel
from .telegram import TelegramChannel

log = logging.getLogger(__name__)

__all__ = [
    "Channel",
    "IncomingCommand",
    "format_report",
    "SlackChannel",
    "TelegramChannel",
    "build_channels",
    "notify_all",
]


def build_channels(settings: Any | None = None) -> list[Channel]:
    """설정에서 활성 채널 목록을 만든다. 비활성 채널은 제외."""
    if settings is None:
        from ..config import get_settings

        settings = get_settings()

    data_dir = getattr(settings, "data_dir", None)
    channels: list[Channel] = []

    telegram = TelegramChannel(
        token=getattr(settings, "telegram_bot_token", "") or "",
        allowed_chat_ids=set(getattr(settings, "telegram_allowed_chat_ids", []) or []),
        data_dir=data_dir,
    )
    if telegram.enabled:
        channels.append(telegram)
    else:
        log.info("텔레그램 채널 비활성(토큰 또는 허용 chat_id 없음)")

    slack = SlackChannel(
        bot_token=getattr(settings, "slack_bot_token", "") or "",
        allowed_channel_ids=set(getattr(settings, "slack_allowed_channel_ids", []) or []),
        webhook_url=getattr(settings, "slack_webhook_url", "") or "",
        data_dir=data_dir,
    )
    if slack.enabled:
        channels.append(slack)
    else:
        log.info("슬랙 채널 비활성(봇 토큰+허용 채널 또는 webhook 없음)")

    return channels



_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)(token=)[^&\s]+"),
    re.compile(r"(?i)(password[=:]\s*)\S+"),
    re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
]


def sanitize(text: str, limit: int = 500) -> str:
    """보고 문구에서 토큰·비밀번호·이메일을 가리고 길이를 제한한다."""
    out = str(text or "")
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "***", out)
    return out[:limit]

def notify_all(channels: list[Channel], text: str) -> int:
    """모든 채널에 같은 보고를 보낸다. 성공 건수 반환."""
    sent = 0
    for channel in channels or []:
        try:
            sent += int(channel.broadcast(sanitize(text)) or 0)
        except Exception as exc:  # 알림 실패로 본 작업이 죽지 않게
            log.warning("채널 %s 보고 실패: %s", getattr(channel, "name", "?"), exc)
    return sent
