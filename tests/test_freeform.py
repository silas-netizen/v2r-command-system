"""슬랙/텔레그램 자유 대화 명령 해석기 테스트. 모델은 가짜 객체만 쓴다."""

from __future__ import annotations

import json

from tests.test_engine import make_runtime
from tests.test_schedule import RecordingChannel
from v2r.channels import freeform


class FakeRouter:
    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, purpose, system, user, max_tokens=500):
        self.calls.append((purpose, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def make_rt(tmp_path, reply):
    rt = make_runtime(tmp_path)
    rt._llm = FakeRouter(reply)
    rt._channels = []
    return rt


# --- JSON 해석 ------------------------------------------------------
def test_interpret_parses_command_json(tmp_path):
    rt = make_rt(tmp_path, '{"action":"command","text":"현황","confirm":false}')
    out = freeform.interpret(rt, "지금 뭐 하고 있어?")
    assert out == {"action": "command", "text": "현황", "confirm": False}


def test_interpret_parses_answer_json(tmp_path):
    rt = make_rt(tmp_path, '{"action":"answer","text":"오늘 12건 나갔습니다"}')
    out = freeform.interpret(rt, "오늘 몇 개 나갔어?")
    assert out["action"] == "answer"
    assert "12건" in out["text"]


def test_interpret_model_failure_falls_back_to_ask(tmp_path):
    rt = make_rt(tmp_path, RuntimeError("모델 오류"))
    out = freeform.interpret(rt, "아무 말이나")
    assert out["action"] == "ask"
    assert "못 알아들었어요" in out["text"]


def test_interpret_bad_json_falls_back_to_ask(tmp_path):
    rt = make_rt(tmp_path, "이건 JSON이 아님")
    out = freeform.interpret(rt, "아무 말이나")
    assert out["action"] == "ask"


def test_interpret_no_router_falls_back_to_ask(tmp_path):
    rt = make_runtime(tmp_path)
    rt._llm = None
    out = freeform.interpret(rt, "아무 말")
    assert out["action"] == "ask"


# --- command 경로: 문법에 맞는 명령은 바로 실행 -----------------------
def test_handle_channel_message_matches_existing_grammar_without_llm(tmp_path):
    """기존 명령 문법에 맞으면 모델을 부르지 않고 바로 handle_text로 간다."""
    rt = make_rt(tmp_path, "호출되면 안 됨")
    ch = RecordingChannel()
    out = freeform.handle_channel_message(rt, ch, "chat1", "현황 알려줘")
    assert rt.llm.calls == []  # 문법 매치라 모델 미호출
    assert out.get("ok") is True


def test_handle_channel_message_freeform_command_runs_job(tmp_path):
    rt = make_rt(tmp_path, '{"action":"command","text":"현황 알려줘","confirm":false}')
    ch = RecordingChannel()
    out = freeform.handle_channel_message(rt, ch, "chat1", "우리 지금 뭐 하고 있어")
    assert out.get("ok") is True
    assert rt.llm.calls  # 모델이 실제로 호출됐다


# --- answer / ask 경로 ----------------------------------------------
def test_handle_channel_message_answer_replies_green_icon(tmp_path):
    rt = make_rt(tmp_path, '{"action":"answer","text":"오늘 12건 나갔습니다"}')
    ch = RecordingChannel()
    freeform.handle_channel_message(rt, ch, "chat1", "오늘 몇 건 나갔어?")
    assert ch.sent == ["🟢 오늘 12건 나갔습니다"]


def test_handle_channel_message_ask_replies_without_해석하지_못했습니다(tmp_path):
    rt = make_rt(tmp_path, RuntimeError("모델 오류"))
    ch = RecordingChannel()
    freeform.handle_channel_message(rt, ch, "chat1", "asdkfjaslkdfj")
    assert len(ch.sent) == 1
    assert "해석하지 못했습니다" not in ch.sent[0]
    assert "못 알아들었어요" in ch.sent[0]


def test_unmatched_freeform_command_text_also_asks_without_해석하지_못했습니다(tmp_path):
    """모델이 command 를 냈지만 그 문장이 실제 문법에 안 맞으면 되묻기로 대신한다."""
    rt = make_rt(tmp_path, '{"action":"command","text":"이상한말이라옹","confirm":false}')
    ch = RecordingChannel()
    freeform.handle_channel_message(rt, ch, "chat1", "아무말")
    assert len(ch.sent) == 1
    assert "해석하지 못했습니다" not in ch.sent[0]
    assert "못 알아들었어요" in ch.sent[0]


# --- confirm 흐름: 네 / 아니오 / 만료 ---------------------------------
def test_confirm_flow_yes_runs_command(tmp_path):
    rt = make_rt(tmp_path, '{"action":"command","text":"중지","confirm":true}')
    ch = RecordingChannel()
    out1 = freeform.handle_channel_message(rt, ch, "chat1", "이제 그만해 줘")
    assert out1["action"] == "confirm_pending"
    assert any("실행할까요" in s for s in ch.sent)

    ch.sent.clear()
    out2 = freeform.handle_channel_message(rt, ch, "chat1", "네")
    assert out2.get("stopped") is True


def test_confirm_flow_no_cancels(tmp_path):
    rt = make_rt(tmp_path, '{"action":"command","text":"중지","confirm":true}')
    ch = RecordingChannel()
    freeform.handle_channel_message(rt, ch, "chat1", "이제 그만해 줘")
    ch.sent.clear()
    out = freeform.handle_channel_message(rt, ch, "chat1", "아니 됐어")
    assert out == {"action": "cancelled"}
    assert ch.sent == ["취소했습니다."]


def test_confirm_flow_expires_after_ttl(tmp_path, monkeypatch):
    rt = make_rt(tmp_path, '{"action":"command","text":"중지","confirm":true}')
    ch = RecordingChannel()
    freeform.handle_channel_message(rt, ch, "chat1", "이제 그만해 줘")

    import time as _time

    real_time = _time.time
    monkeypatch.setattr(_time, "time", lambda: real_time() + freeform.STATE_TTL_SECONDS + 5)
    assert freeform.get_pending(ch.name, "chat1", rt=rt) is None


# --- 긴 작업: "접수 — 약 N분 예상" 1줄 ---------------------------------
def test_long_job_gets_accept_line(tmp_path, monkeypatch):
    monkeypatch.setattr(freeform, "_long_job_eta", lambda task: 8 if task == "publish_daily" else 0)

    rt = make_rt(tmp_path, "호출 안 됨")
    ch = RecordingChannel()
    out = freeform.handle_channel_message(rt, ch, "chat1", "일상 글 5개 올려줘")
    assert out.get("ok") is True
    assert any("접수" in s and "분 예상" in s for s in ch.sent)


def test_short_job_gets_no_accept_line(tmp_path, monkeypatch):
    monkeypatch.setattr(freeform, "_long_job_eta", lambda task: 0)
    rt = make_rt(tmp_path, "호출 안 됨")
    ch = RecordingChannel()
    out = freeform.handle_channel_message(rt, ch, "chat1", "현황 알려줘")
    assert out.get("ok") is True
    assert ch.sent == []  # 짧은 작업은 접수 답장이 없다(결과는 reply_to_origin 몫)
