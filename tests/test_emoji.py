"""이모지 제거 규칙 테스트 (사용자 절대 규칙: 이모지는 모든 원고에서 제외).

2026-09-19 자사 카페 일상 글 18건이 제목 이모지를 달고 나간 사고의 재발 방지막:
생성 단계 · 적재 단계 · 발행 단계 세 겹을 모두 확인한다.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from v2r.command.parser import describe_spec, parse_korean_command
from v2r.command.spec import TaskSpec
from v2r.content import sanitize
from v2r.content.manuscript import CommentNode, Manuscript
from v2r.engine import emoji_cleanup, publish as publish_mod

from tests.test_engine import make_runtime


# --- sanitize 단위 --------------------------------------------------
@pytest.mark.parametrize("text", ["ㅋㅋㅋ 진짜 웃겨", "ㅠㅠ 속상해요", "ㅎㅎ 좋네요", "아 진짜?! 대박..."])
def test_한글_이모티콘과_문장부호는_지우지_않는다(text):
    assert sanitize.has_emoji(text) is False
    assert sanitize.strip_emoji(text) == text


@pytest.mark.parametrize(
    "text", ["오늘 좋아요 😊", "선물 🎁 도착", "완료 ✅", "별점 ⭐", "사랑 ❤️", "1️⃣ 첫째"]
)
def test_이모지는_찾아내고_지운다(text):
    assert sanitize.has_emoji(text) is True
    cleaned = sanitize.strip_emoji(text)
    assert sanitize.has_emoji(cleaned) is False


def test_변이선택자_FE0F도_지운다():
    assert sanitize.has_emoji("경고 ❗️") is True
    assert sanitize.strip_emoji("❤️") == ""


def test_지우면서_생긴_겹공백과_끝공백을_정리한다():
    assert sanitize.strip_emoji("오늘 😊 날씨 좋네요 🎁") == "오늘 날씨 좋네요"
    assert sanitize.strip_emoji("제목 😊  ") == "제목"


def test_이모지가_없으면_원문_그대로():
    raw = "  들여쓰기는  그대로  "
    assert sanitize.strip_emoji(raw) == raw


def _dirty_manuscript() -> Manuscript:
    return Manuscript(
        title="오늘의 간식 🎁",
        body="첫 줄 😊\n둘째 줄",
        head="공지 ✅",
        tags=["단호박🎁"],
        comments=[
            CommentNode(label="댓글1", text="좋아요 ❤️"),
            CommentNode(label="댓글2", text="ㅋㅋㅋ 공감돼요"),
        ],
        content_hash="HASH-1",
    )


def test_원고_전체를_깨끗하게_만든다():
    m = _dirty_manuscript()
    assert sanitize.emoji_spots(m) == ["제목", "본문", "말머리", "태그", "댓글1"]
    out = sanitize.sanitize_manuscript(m)
    assert out.title == "오늘의 간식"
    assert out.body == "첫 줄\n둘째 줄"
    assert out.head == "공지"
    assert out.tags == ["단호박"]
    assert out.comments[0].text == "좋아요"
    assert out.comments[1].text == "ㅋㅋㅋ 공감돼요"  # 한글 이모티콘은 그대로
    assert out.content_hash == "HASH-1"  # 해시는 건드리지 않는다(중복 판정 유지)


# --- 발행 거름망 ----------------------------------------------------
def _slot(m: Manuscript) -> publish_mod.Slot:
    return publish_mod.Slot(manuscript=m, account="user1", cafe="고요한 아침", board="자유게시판")


def test_발행_직전_거름망이_지우고_경고를_남긴다(tmp_path):
    rt = make_runtime(tmp_path)
    slot = _slot(_dirty_manuscript())
    cleaned = publish_mod.sanitize_slot(rt, slot, job_id=None)
    assert "🎁" not in cleaned.title
    assert slot.manuscript is cleaned  # build_comments가 깨끗한 댓글을 쓰도록 슬롯도 바뀐다
    messages = [e["message"] for e in rt.events.recent(limit=10)]
    assert any("이모지 제거: 제목/본문/댓글" in msg for msg in messages)


def test_깨끗한_원고는_경고도_사본도_없다(tmp_path):
    rt = make_runtime(tmp_path)
    m = Manuscript(title="오늘의 간식", body="첫 줄", content_hash="H")
    slot = _slot(m)
    assert publish_mod.sanitize_slot(rt, slot) is m
    assert rt.events.recent(limit=10) == []


def test_마지막_확인에서_이모지가_남아_있으면_발행을_멈춘다():
    with pytest.raises(publish_mod.PublishError, match="이모지"):
        publish_mod.assert_no_emoji("제목 😊", "본문")
    with pytest.raises(publish_mod.PublishError, match="이모지"):
        publish_mod.assert_no_emoji("제목", "본문 ⭐")
    with pytest.raises(publish_mod.PublishError, match="이모지"):
        publish_mod.assert_no_emoji("제목", "본문", [{"contents": "좋아요 ❤️"}])
    publish_mod.assert_no_emoji("제목", "본문", [{"contents": "ㅋㅋ 좋아요"}])


# --- 생성 단계 ------------------------------------------------------
def test_일상글_댓글은_이모지가_있으면_지워서_받는다():
    from v2r.content import daily_comments as dc

    assert dc.clean_text("오늘 날씨 진짜 좋네요 😊") == "오늘 날씨 진짜 좋네요"
    assert dc.clean_text("😊🎁⭐") == ""  # 이모지만 남으면 버린다
    assert dc.clean_text("ㅋㅋㅋ 이거 진짜 공감돼요") == "ㅋㅋㅋ 이거 진짜 공감돼요"


def test_짧은_일상글_생성기는_이모지를_거른다():
    from v2r.warehouse import daily_generator as dg

    assert dg.clean_line("오늘 간식 🎁 최고") == "오늘 간식 최고"
    assert dg.is_valid("오늘 간식 🎁", "맛있었어요") is False
    assert dg.is_valid("오늘 간식", "맛있었어요") is True


def test_브랜드_원고_검증이_이모지를_잡는다():
    from v2r.content import brand_writer

    m = Manuscript(
        title="요즘 고민 😊",
        body="{키워드}\n요즘 고민이 많아요",
        keyword="단호박",
        source="generated:우아덤",
        comments=[CommentNode(label="댓글1", text="공감돼요 🎁")],
    )
    checks = brand_writer.validate(m)
    items = {c["항목"]: c for c in checks}
    assert items["이모지 포함"]["통과"] is False
    assert items["이모지 포함"]["필수"] is True
    assert items["댓글 이모지 포함"]["통과"] is False
    assert any("이모지" in f for f in brand_writer.failures(checks))


# --- 적재 단계 (xlsx) -----------------------------------------------
def test_xlsx_적재가_이모지를_지우되_해시는_원문_기준():
    from v2r.content.manuscript import content_hash
    from v2r.sources.local_files import parse_daily_xlsx_rows

    rows = [
        {
            "카페명": "고요한 아침",
            "게시판명": "자유게시판",
            "각색제목": "오늘의 간식 🎁",
            "각색본문": "맛있었어요 😊",
            "등록시간": "",
            "상태": "",
        }
    ]
    out = parse_daily_xlsx_rows(rows, source="각색_고요한아침")
    assert len(out) == 1
    assert out[0].title == "오늘의 간식"
    assert out[0].body == "맛있었어요"
    # 해시는 지우기 **전** 원문으로 — 이미 발행된 글이 새 글로 보이지 않게 한다
    assert out[0].content_hash == content_hash("오늘의 간식 🎁", "맛있었어요 😊")


# --- 명령 ------------------------------------------------------------
@pytest.mark.parametrize(
    "text", ["이모지 정리", "오늘 이모지 정리", "이모지 정리 전체", "이모지 제거해줘"]
)
def test_이모지_정리_명령을_알아듣는다(text):
    spec = parse_korean_command(text, now=datetime(2026, 9, 19, 10, 0))
    assert spec is not None and spec.task == "cleanup_emoji"
    assert spec.dry_run is True
    assert "이모지 정리" in describe_spec(spec)


def test_실제라고_해야_진짜_고친다():
    spec = parse_korean_command("오늘 이모지 정리 실제")
    assert spec.task == "cleanup_emoji" and spec.dry_run is False


# --- 처리기 ----------------------------------------------------------
class FakeArticles:
    """이모지가 든 글 1건 + 깨끗한 글 1건을 들고 있는 가짜 V2R."""

    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.store = {
            "SID-DIRTY": {"title": "오늘의 간식 🎁", "body": "맛있었어요 😊", "comment": "좋아요 ❤️"},
            "SID-CLEAN": {"title": "오늘의 간식", "body": "맛있었어요", "comment": "ㅋㅋ 좋아요"},
        }

    def get_article(self, client, source_id):
        row = self.store[source_id]
        return {
            "naver_cafe_article_source": {"source_id": source_id, "title": row["title"]},
            "naver_cafe_article_source_detail": {"body": ""},
            "naver_cafe_article_source_comments": [{"contents": row["comment"]}],
            "_body_lines": [row["body"]],
        }

    def update_article(self, client, source_id, *, title, content_json, tags=None):
        self.updates.append({"source_id": source_id, "title": title})
        self.store[source_id]["title"] = title
        self.store[source_id]["body"] = "맛있었어요"
        return {"is_success": True}


@pytest.fixture()
def fake_v2r(monkeypatch):
    fake = FakeArticles()
    monkeypatch.setattr(emoji_cleanup.api_articles, "get_article", fake.get_article)
    monkeypatch.setattr(emoji_cleanup.api_articles, "update_article", fake.update_article)
    monkeypatch.setattr(
        emoji_cleanup.api_articles, "body_lines_of", lambda d: list(d.get("_body_lines") or [])
    )
    monkeypatch.setattr(emoji_cleanup.time, "sleep", lambda s: None)
    return fake


def _seed(rt) -> None:
    rt.publications.mark(
        "각색_고요한아침", 2, "h2", "done", "done", source_id="SID-DIRTY", cafe="고요한 아침"
    )
    rt.publications.mark(
        "각색_고요한아침", 3, "h3", "done", "done", source_id="SID-CLEAN", cafe="고요한 아침"
    )


def test_모의_실행은_고치지_않고_목록만(tmp_path, fake_v2r):
    rt = make_runtime(tmp_path)
    _seed(rt)
    spec = TaskSpec(task="cleanup_emoji", notes="이모지 정리 전체")
    out = emoji_cleanup.cleanup_emoji(rt, spec)
    assert out["dry_run"] is True
    assert fake_v2r.updates == []
    assert len(out["fixed"]) == 1 and out["fixed"][0]["source_id"] == "SID-DIRTY"
    assert out["clean"] == 1


def test_실제_실행은_이모지_글만_고치고_다시_확인한다(tmp_path, fake_v2r):
    rt = make_runtime(tmp_path)
    _seed(rt)
    spec = TaskSpec(task="cleanup_emoji", notes="이모지 정리 전체 실제", dry_run=False)
    out = emoji_cleanup.cleanup_emoji(rt, spec)
    assert out["ok"] is True
    assert [u["source_id"] for u in fake_v2r.updates] == ["SID-DIRTY"]  # 깨끗한 글은 건드리지 않는다
    assert fake_v2r.store["SID-DIRTY"]["title"] == "오늘의 간식"
    assert out["fixed"][0]["before_title"] == "오늘의 간식 🎁"
    assert out["fixed"][0]["after_title"] == "오늘의 간식"
    assert out["fixed"][0]["fixed"] is True


def test_댓글_이모지는_수동_확인으로_보고한다(tmp_path, fake_v2r):
    rt = make_runtime(tmp_path)
    _seed(rt)
    spec = TaskSpec(task="cleanup_emoji", notes="이모지 정리 전체 실제", dry_run=False)
    out = emoji_cleanup.cleanup_emoji(rt, spec)
    assert [i["source_id"] for i in out["manual"]] == ["SID-DIRTY"]
    assert "댓글 이모지 — 수동 확인" in out["report"]


def test_보고서_파일을_남긴다(tmp_path, fake_v2r):
    from pathlib import Path

    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    _seed(rt)
    spec = TaskSpec(task="cleanup_emoji", notes="이모지 정리 전체", start_date="2026-09-19")
    out = emoji_cleanup.cleanup_emoji(rt, spec)
    path = Path(out["report_path"])
    assert path.name == "emoji-cleanup-2026-09-19.md"
    text = path.read_text(encoding="utf-8")
    assert "SID-DIRTY" in text and "오늘의 간식 🎁 → 오늘의 간식" in text


def test_오늘만_보는_것이_기본이다(tmp_path, fake_v2r):
    rt = make_runtime(tmp_path)
    _seed(rt)
    # 오늘이 아닌 날짜를 지정하면 대상이 없다
    spec = TaskSpec(task="cleanup_emoji", notes="이모지 정리", start_date="2000-01-01")
    out = emoji_cleanup.cleanup_emoji(rt, spec)
    assert out["checked"] == 0 and out["fixed"] == []


def test_worker_가_이모지_정리를_배분한다(tmp_path, fake_v2r, monkeypatch):
    from v2r.engine import worker

    rt = make_runtime(tmp_path)
    _seed(rt)
    job_id = rt.jobs.enqueue(TaskSpec(task="cleanup_emoji", notes="이모지 정리 전체"), "테스트")
    job = rt.jobs.get(job_id)
    out = worker.dispatch(rt, job)
    assert out["ok"] is True and len(out["fixed"]) == 1
