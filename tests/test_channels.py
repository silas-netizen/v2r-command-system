"""채널 테스트: 허용 목록 강제와 보고 문장."""

from __future__ import annotations

import json



from v2r.channels import build_channels, notify_all
from v2r.channels.base import format_report
from v2r.channels.slack import SlackChannel
from v2r.channels.telegram import TelegramChannel

TOKEN = "test-token"
UPDATES_URL = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
SEND_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def _update(update_id: int, chat_id: str, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "text": text,
            "chat": {"id": int(chat_id)},
            "from": {"id": 777},
        },
    }


# --- 보고 문장 ----------------------------------------------------
def test_format_report_strings():
    assert format_report(1, "done", "일상 글 발행", url="https://v2r/1") == (
        "작업 1 완료: 일상 글 발행 https://v2r/1"
    )
    assert format_report(2, "done", "일상 글 발행") == "작업 2 완료: 일상 글 발행"
    assert format_report(3, "failed", "발행", error="429 제한") == "작업 3 실패: 429 제한"
    assert format_report(4, "uncertain", "발행 확인 중") == (
        "작업 4 불확실: 발행 확인 중 (수동 확인 필요)"
    )
    assert format_report(5, "running", "원고 수집") == "작업 5 진행: 원고 수집"


# --- 텔레그램 ------------------------------------------------------
def test_telegram_disabled_without_allowed_ids(tmp_path):
    channel = TelegramChannel(TOKEN, set(), data_dir=tmp_path)
    assert channel.enabled is False
    assert channel.poll() == []


def test_telegram_ignores_non_allowed_chat(tmp_path, httpx_mock):
    httpx_mock.add_response(
        url=UPDATES_URL,
        json={
            "ok": True,
            "result": [
                _update(11, "999", "일상 글 20개 올려줘"),  # 허용되지 않은 방
                _update(12, "1234", "상태 알려줘"),
            ],
        },
    )
    channel = TelegramChannel(TOKEN, {"1234"}, data_dir=tmp_path)
    commands = channel.poll()

    assert [c.chat_id for c in commands] == ["1234"]
    assert commands[0].text == "상태 알려줘"
    assert commands[0].channel == "telegram"
    # offset 저장 확인
    saved = json.loads((tmp_path / "telegram_offset.json").read_text(encoding="utf-8"))
    assert saved["offset"] == 13


def test_telegram_send_blocks_non_allowed(tmp_path, httpx_mock):
    channel = TelegramChannel(TOKEN, {"1234"}, data_dir=tmp_path)
    assert channel.send("999", "작업 1 완료: 테스트") is False
    assert httpx_mock.get_requests() == []


def test_telegram_broadcast(tmp_path, httpx_mock):
    httpx_mock.add_response(url=SEND_URL, json={"ok": True, "result": {}}, is_reusable=True)
    channel = TelegramChannel(TOKEN, {"1234", "5678"}, data_dir=tmp_path)
    assert channel.broadcast("작업 1 진행: 발행 시작") == 2


# --- 슬랙 ---------------------------------------------------------
def test_slack_requires_allowed_channels(tmp_path):
    channel = SlackChannel("xoxb-abc", set(), data_dir=tmp_path)
    assert channel.can_receive is False
    assert channel.poll() == []


def test_slack_poll_skips_bot_messages(tmp_path, httpx_mock):
    httpx_mock.add_response(
        url="https://slack.com/api/conversations.history?channel=C1&limit=50",
        json={
            "ok": True,
            "messages": [
                {"ts": "200.0", "text": "봇 보고", "bot_id": "B1"},
                {"ts": "100.0", "text": "상태 알려줘", "user": "U1"},
            ],
        },
    )
    channel = SlackChannel("xoxb-abc", {"C1"}, data_dir=tmp_path)
    commands = channel.poll()

    assert [c.text for c in commands] == ["상태 알려줘"]
    cursors = json.loads((tmp_path / "slack_cursor.json").read_text(encoding="utf-8"))
    assert cursors["C1"] == "200.0"


def test_slack_webhook_only_mode(tmp_path, httpx_mock):
    httpx_mock.add_response(url="https://hooks.slack.test/abc", text="ok")
    channel = SlackChannel("", set(), webhook_url="https://hooks.slack.test/abc", data_dir=tmp_path)
    assert channel.webhook_only is True
    assert channel.enabled is True
    assert channel.broadcast("작업 7 실패: 토큰 만료") == 1


# --- 묶음 ---------------------------------------------------------
class _Settings:
    telegram_bot_token = TOKEN
    telegram_allowed_chat_ids = ["1234"]
    slack_bot_token = ""
    slack_allowed_channel_ids: list[str] = []
    slack_webhook_url = ""
    anthropic_api_key = ""

    def __init__(self, data_dir):
        self.data_dir = data_dir


def test_build_channels_only_enabled(tmp_path):
    channels = build_channels(_Settings(tmp_path))
    assert [c.name for c in channels] == ["telegram"]


def test_notify_all_counts_and_survives_errors(tmp_path):
    class Boom:
        name = "boom"

        def broadcast(self, text):
            raise RuntimeError("실패")

    class Ok:
        name = "ok"

        def broadcast(self, text):
            return 2

    assert notify_all([Boom(), Ok()], "작업 1 진행: 테스트") == 2
