"""제휴 카페 일상 글 생성 (ChatGPT 웹 세션) 테스트 — 브라우저 없이 대역으로."""

from __future__ import annotations

from pathlib import Path

import pytest

from v2r.command.parser import describe_spec, parse_korean_command
from v2r.command.spec import ALLOWED_TASKS
from v2r.warehouse import daily_generator as dg


# --- 응답 파싱 / 규칙 검증 --------------------------------------------------
def test_parse_reply_takes_valid_pairs():
    reply = (
        "제목: 오늘 애기 낮잠 실패ㅠㅠ\n"
        "본문: 세시간째 안자고 버티는중 나도 같이 졸림ㅋㅋ\n"
        "제목: 저녁 뭐 먹을지 고민\n"
        "본문: 냉장고 열었다 닫았다만 다섯번째\n"
    )
    items = dg.parse_affiliate_reply(reply)
    assert items == [
        ("오늘 애기 낮잠 실패ㅠㅠ", "세시간째 안자고 버티는중 나도 같이 졸림ㅋㅋ"),
        ("저녁 뭐 먹을지 고민", "냉장고 열었다 닫았다만 다섯번째"),
    ]


def test_parse_reply_strips_banned_punctuation():
    items = dg.parse_affiliate_reply("제목: 비오는 날\n본문: 우산을 또 잃어버렸다.\n")
    assert items == [("비오는 날", "우산을 또 잃어버렸다")]
    assert "." not in items[0][1] and "," not in items[0][1]


def test_parse_reply_drops_too_long():
    long_title = "가" * (dg.TITLE_MAX + 1)
    long_body = "나" * (dg.BODY_MAX + 1)
    reply = f"제목: {long_title}\n본문: 짧은 본문\n제목: 짧은 제목\n본문: {long_body}\n"
    assert dg.parse_affiliate_reply(reply) == []


def test_parse_reply_ignores_noise_lines():
    reply = "물론이죠! 아래에 준비했어요\n\n제목: 장보기 실패\n본문: 세일만 쫓다가 정작 우유를 안샀네\n끝!"
    assert dg.parse_affiliate_reply(reply) == [("장보기 실패", "세일만 쫓다가 정작 우유를 안샀네")]


def test_prompt_contains_rules():
    prompt = dg.build_affiliate_prompt("씨씨앙", 5)
    assert "씨씨앙" in prompt and "5개" in prompt
    assert "제목 1줄" in prompt and "본문 1줄" in prompt
    assert str(dg.TITLE_MAX) in prompt and str(dg.BODY_MAX) in prompt
    assert "쉼표" in prompt and "ㅋㅋ" in prompt
    assert "브랜드명" in prompt


def test_prompt_lists_titles_to_avoid():
    prompt = dg.build_affiliate_prompt("양평맘", 3, ["이미 쓴 제목"])
    assert "이미 쓴 제목" in prompt


# --- 풀 생성 ---------------------------------------------------------------
def _fake_ask(replies):
    """호출 순서대로 미리 정한 응답을 주는 대역."""
    calls = {"n": 0}

    def _ask(page, prompt, timeout=0):
        i = min(calls["n"], len(replies) - 1)
        calls["n"] += 1
        return replies[i]

    _ask.calls = calls
    return _ask


def test_generate_pool_writes_jsonl(tmp_path: Path):
    reply = "".join(f"제목: 제목{i}\n본문: 본문{i} 입니다\n" for i in range(5))
    out = dg.generate_affiliate_pool_via_gpt(
        ["씨씨앙", "양평맘"], 5, page=object(), warehouse_dir=tmp_path,
        ask_fn=_fake_ask([reply]),
    )
    assert out["ok"] is True
    pool_file = dg.affiliate_pool_path(tmp_path)
    assert pool_file.is_file()
    assert pool_file.name == "affiliate_daily_pool.jsonl"

    items = dg.load_pool(tmp_path, dg.AFFILIATE_POOL_FILENAME)
    # 같은 응답을 두 카페에 쓰면 내용이 겹쳐 중복 제거된다
    assert len(items) == 5
    assert out["added"] == 5
    assert all(m.source == "affiliate_daily_pool" for m in items)
    assert all(m.images_enabled is False for m in items)


def test_generate_pool_dedupes_by_hash(tmp_path: Path):
    reply = "제목: 같은 제목\n본문: 같은 본문 입니다\n"
    first = dg.generate_affiliate_pool_via_gpt(
        ["씨씨앙"], 1, page=object(), warehouse_dir=tmp_path, ask_fn=_fake_ask([reply])
    )
    second = dg.generate_affiliate_pool_via_gpt(
        ["씨씨앙"], 1, page=object(), warehouse_dir=tmp_path, ask_fn=_fake_ask([reply])
    )
    assert first["added"] == 1
    assert second["added"] == 0
    assert len(dg.load_pool(tmp_path, dg.AFFILIATE_POOL_FILENAME)) == 1


def test_generate_pool_separate_from_self_cafe_pool(tmp_path: Path):
    """제휴 풀과 자사 xlsx 일상 글 풀은 서로 다른 파일이다."""
    dg.generate_affiliate_pool_via_gpt(
        ["씨씨앙"], 1, page=object(), warehouse_dir=tmp_path,
        ask_fn=_fake_ask(["제목: 제휴 글\n본문: 제휴 본문 이다\n"]),
    )
    assert dg.affiliate_pool_path(tmp_path).is_file()
    assert not dg.pool_path(tmp_path).exists()
    assert dg.load_pool(tmp_path) == []


def test_generate_pool_records_errors(tmp_path: Path):
    def _boom(page, prompt, timeout=0):
        raise RuntimeError("응답 없음")

    out = dg.generate_affiliate_pool_via_gpt(
        ["씨씨앙"], 2, page=object(), warehouse_dir=tmp_path, ask_fn=_boom
    )
    assert out["generated"] == 0
    assert any("응답 없음" in e for e in out["errors"])


def test_generate_pool_handles_unparsable_reply(tmp_path: Path):
    out = dg.generate_affiliate_pool_via_gpt(
        ["씨씨앙"], 2, page=object(), warehouse_dir=tmp_path,
        ask_fn=_fake_ask(["뭔가 이상한 답변만 왔다"]),
    )
    assert out["generated"] == 0
    assert any("규칙에 맞는" in e for e in out["errors"])


def test_generate_pool_no_cafes(tmp_path: Path):
    out = dg.generate_affiliate_pool_via_gpt([], 5, warehouse_dir=tmp_path, ask_fn=_fake_ask([""]))
    assert out["ok"] is True and out["generated"] == 0


# --- 발행 픽커: 제휴 풀만 읽는다 --------------------------------------------
class _FakeWarehouse:
    def __init__(self, root):
        self.root = root


class _FakeRT:
    def __init__(self, root):
        self.warehouse = _FakeWarehouse(root)
        self.scratch = {}
        self.sources_cfg = {}  # 폴백 경로에서 시트 목록을 훑는다
        self.publications = _FakePubs()


def test_daily_pool_reads_only_affiliate_pool(tmp_path: Path):
    from v2r.engine import publish

    dg.generate_affiliate_pool_via_gpt(
        ["씨씨앙"], 2, page=object(), warehouse_dir=tmp_path,
        ask_fn=_fake_ask(["제목: 하나\n본문: 첫번째 글\n제목: 둘\n본문: 두번째 글\n"]),
    )
    pool = publish._daily_pool(_FakeRT(tmp_path))
    assert len(pool) == 2
    assert {m.title for m in pool} == {"하나", "둘"}
    assert all(m.source == "affiliate_daily_pool" for m in pool)


def test_daily_pool_falls_back_with_warning(tmp_path: Path, caplog):
    from v2r.engine import publish

    with caplog.at_level("WARNING", logger="v2r.engine.publish"):
        pool = publish._daily_pool(_FakeRT(tmp_path))
    assert pool == []
    messages = [r.getMessage() for r in caplog.records]
    assert any("랜덤일상" in m and "비어" in m for m in messages), messages


def test_daily_pool_is_cached_in_scratch(tmp_path: Path):
    from v2r.engine import publish

    rt = _FakeRT(tmp_path)
    publish._daily_pool(rt)
    rt.scratch["daily_pool"] = ["표시"]
    assert publish._daily_pool(rt) == ["표시"]


# --- 명령 해석 -------------------------------------------------------------
def test_task_registered():
    assert "generate_affiliate_daily" in ALLOWED_TASKS


@pytest.mark.parametrize(
    "text",
    ["제휴 일상 글 생성해줘", "제휴 카페 일상 글 20개 만들어줘", "제휴 일상글 만들어"],
)
def test_affiliate_daily_patterns(text):
    spec = parse_korean_command(text)
    assert spec is not None and spec.task == "generate_affiliate_daily", text


def test_plain_daily_still_goes_to_generate_daily():
    """자사 카페 일상 글은 기존 경로 그대로."""
    spec = parse_korean_command("일상 글 30개 만들어줘")
    assert spec is not None and spec.task == "generate_daily"


def test_affiliate_daily_count_and_label():
    spec = parse_korean_command("제휴 일상 글 20개 만들어줘")
    assert spec is not None and spec.count == 20
    assert "제휴 일상 글 생성(GPT)" in describe_spec(spec)


def test_worker_reports_missing_affiliate_cafes():
    from v2r.engine import worker

    class _RT:
        cafes_cfg = {}
        channels = []

    spec = parse_korean_command("제휴 일상 글 만들어줘")
    out = worker._generate_affiliate_daily(_RT(), spec)
    assert out["ok"] is False and "제휴 카페" in out["error"]


def test_affiliate_cafes_from_config():
    from v2r.engine import worker

    class _RT:
        cafes_cfg = {
            "affiliate": [{"name": "씨씨앙"}, {"name": "양평맘"}, {"name": "쌍둥이맘"}],
            "self_owned": [{"name": "고요한 아침"}],
        }

    assert worker._affiliate_cafes(_RT()) == ["씨씨앙", "양평맘", "쌍둥이맘"]


# --- 실발행은 ChatGPT 창에서 즉석 생성(사용자 규칙 2026-09-19) ----------------
from v2r.engine import publish
from v2r.content.manuscript import Manuscript


class _FakePubs:
    def exists(self, *a, **k):
        return False


def _write_pool(root):
    dg.generate_affiliate_pool_via_gpt(
        ["씨씨앙"], 1, page=object(), warehouse_dir=root,
        ask_fn=_fake_ask(["제목: 하나\n본문: 첫번째 글\n"]),
    )


def test_take_daily_live_generates_via_gpt(tmp_path: Path, monkeypatch):

    made = Manuscript(title="즉석", body="생성", cafe="씨씨앙", source="affiliate_daily_pool",
                      source_row=1, content_hash="h1")
    calls = []

    def fake_gen(cafes, per_cafe, **kw):
        calls.append((cafes, per_cafe))
        return {"ok": True, "items": [made], "errors": []}

    monkeypatch.setattr(dg, "generate_affiliate_pool_via_gpt", fake_gen)
    rt = _FakeRT(tmp_path)
    got = publish._take_daily(rt, "씨씨앙", dry_run=False)
    assert got is made
    assert calls == [(["씨씨앙"], 1)]


def test_take_daily_live_fails_when_gpt_returns_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dg, "generate_affiliate_pool_via_gpt",
                        lambda *a, **k: {"ok": False, "items": [], "errors": ["x"]})
    with pytest.raises(publish.PublishError):
        publish._take_daily(_FakeRT(tmp_path), "양평맘", dry_run=False)


def test_take_daily_dry_run_does_not_open_gpt(tmp_path: Path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry_run에서 GPT를 열면 안 된다")

    _write_pool(tmp_path)  # 풀은 먼저 채우고(진짜 함수) 그 다음 GPT 호출을 막는다
    monkeypatch.setattr(dg, "generate_affiliate_pool_via_gpt", boom)
    rt = _FakeRT(tmp_path)
    got = publish._take_daily(rt, "씨씨앙", dry_run=True)
    assert got.source == "affiliate_daily_pool"
