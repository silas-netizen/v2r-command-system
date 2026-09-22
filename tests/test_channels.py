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


def test_telegram_error_log_hides_token(tmp_path, httpx_mock, caplog):
    """오류 로그에 봇 토큰이 남으면 안 된다 (안전 불변식 2)."""
    import logging

    httpx_mock.add_response(url=UPDATES_URL, status_code=404, json={})
    channel = TelegramChannel(TOKEN, {"1234"}, data_dir=tmp_path)
    with caplog.at_level(logging.WARNING):
        assert channel.poll() == []
    text = " ".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in text
    assert "404" in text


def test_telegram_check_connection(tmp_path, httpx_mock):
    httpx_mock.add_response(
        url=f"https://api.telegram.org/bot{TOKEN}/getMe",
        json={"ok": True, "result": {"username": "v2r_bot"}},
    )
    httpx_mock.add_response(url=SEND_URL, json={"ok": True, "result": {}}, is_reusable=True)

    channel = TelegramChannel(TOKEN, {"1234", "5678"}, data_dir=tmp_path)
    out = channel.check_connection()

    assert out["ok"] is True
    assert out["bot_name"] == "v2r_bot"
    assert {c["chat_id"] for c in out["chats"]} == {"1234", "5678"}
    assert TOKEN not in json.dumps(out, ensure_ascii=False)


def test_telegram_check_connection_no_token(tmp_path):
    channel = TelegramChannel("", set(), data_dir=tmp_path)
    out = channel.check_connection()
    assert out["has_token"] is False
    assert out["note"]


def test_telegram_allowed_user_ids(tmp_path, httpx_mock):
    httpx_mock.add_response(
        url=UPDATES_URL,
        json={"ok": True, "result": [_update(21, "1234", "상태 알려줘")]},
    )
    channel = TelegramChannel(
        TOKEN, {"1234"}, data_dir=tmp_path, allowed_user_ids={"999"}
    )
    assert channel.poll() == []  # 발신자 777은 허용 목록 밖


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


def test_slack_broadcast_document_three_steps(tmp_path, httpx_mock):
    """새 업로드 방식: getUploadURLExternal → 업로드 URL POST → completeUploadExternal."""
    upload_url = "https://files.slack.com/upload/v1/abc123"
    httpx_mock.add_response(
        url="https://slack.com/api/files.getUploadURLExternal?filename=report.md&length=5",
        json={"ok": True, "upload_url": upload_url, "file_id": "F123"},
    )
    httpx_mock.add_response(url=upload_url, text="ok")
    httpx_mock.add_response(
        url="https://slack.com/api/files.completeUploadExternal",
        json={"ok": True, "files": [{"id": "F123"}]},
    )

    report = tmp_path / "report.md"
    report.write_text("hello", encoding="utf-8")

    channel = SlackChannel("xoxb-abc", {"C1"}, data_dir=tmp_path)
    assert channel.broadcast_document(report, "테스트 보고서") == 1

    complete_req = [
        r for r in httpx_mock.get_requests() if "completeUploadExternal" in str(r.url)
    ][0]
    body = json.loads(complete_req.content)
    assert body["channel_id"] == "C1"
    assert body["files"] == [{"id": "F123", "title": "report.md"}]


def test_slack_broadcast_document_no_bot_token_returns_zero(tmp_path):
    """webhook 전용(봇 토큰 없음)이면 파일 전송이 불가하므로 0."""
    channel = SlackChannel(
        "", set(), webhook_url="https://hooks.slack.test/abc", data_dir=tmp_path
    )
    report = tmp_path / "report.md"
    report.write_text("hi", encoding="utf-8")
    assert channel.broadcast_document(report, "캡션") == 0
    assert channel.broadcast_photo(report, "캡션") == 0


def test_slack_check_connection(tmp_path, httpx_mock):
    httpx_mock.add_response(
        url="https://slack.com/api/auth.test",
        json={"ok": True, "team": "V2R팀", "user": "v2r-bot"},
    )
    httpx_mock.add_response(
        url="https://slack.com/api/conversations.info?channel=C1",
        json={"ok": True, "channel": {"name": "general", "is_private": False, "is_member": True}},
    )
    httpx_mock.add_response(
        url="https://slack.com/api/chat.postMessage", json={"ok": True, "result": {}}
    )

    channel = SlackChannel("xoxb-abc", {"C1"}, data_dir=tmp_path)
    out = channel.check_connection()

    assert out["ok"] is True
    assert out["team"] == "V2R팀"
    assert out["channels"][0]["found"] is True
    assert out["channels"][0]["message_sent"] is True
    # 토큰 값이 결과에 담기지 않는다
    assert "xoxb-abc" not in json.dumps(out, ensure_ascii=False)


def test_slack_check_connection_no_token(tmp_path):
    channel = SlackChannel(
        "", set(), webhook_url="https://hooks.slack.test/abc", data_dir=tmp_path
    )
    out = channel.check_connection()
    assert out["has_bot_token"] is False
    assert out["note"]


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
