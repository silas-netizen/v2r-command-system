"""채널 묶음: 설정에서 사용 가능한 채널을 만들고 일괄 보고한다."""

from __future__ import annotations

import logging
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


def notify_all(channels: list[Channel], text: str) -> int:
    """모든 채널에 같은 보고를 보낸다. 성공 건수 반환."""
    sent = 0
    for channel in channels or []:
        try:
            sent += int(channel.broadcast(text) or 0)
        except Exception as exc:  # 알림 실패로 본 작업이 죽지 않게
            log.warning("채널 %s 보고 실패: %s", getattr(channel, "name", "?"), exc)
    return sent
