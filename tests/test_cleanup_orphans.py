"""고아 글 정리(`cleanup_orphans`)와 serve 폴링 1회분 테스트."""

from __future__ import annotations

import pytest

from v2r.api.errors import V2RApiError
from v2r.command.parser import describe_spec, parse_korean_command
from v2r.command.spec import TaskSpec
from v2r.engine import cleanup as cleanup_mod
from v2r.engine import worker

from tests.test_engine import make_runtime


def _mark_failed(rt, row: int, source_id: str, stage: str = "등록 전 실패: 사진 부족") -> None:
    rt.publications.mark(
        "팥순이", row, f"h{row}", "failed", stage, source_id=source_id,
        cafe="씨씨앙", account="molitan",
    )


def _detail(source_id: str, child: str | None = None) -> dict:
    return {
        "naver_cafe_article_source": {
            "source_id": source_id,
            "parent_source_id": None,
            "child_source_id": child,
            "title": "동남아 신혼여행 견적 여쭤봐요",
        }
    }


# --- 파서 -----------------------------------------------------------
@pytest.mark.parametrize(
    "text", ["고아 글 정리해줘", "찌꺼기 정리", "고아 글 삭제", "찌꺼기 삭제해줘"]
)
def test_고아정리_명령을_알아듣는다(text):
    spec = parse_korean_command(text)
    assert spec is not None and spec.task == "cleanup_orphans"
    assert spec.dry_run is True  # 기본은 모의 실행
    assert "고아 글 정리" in describe_spec(spec)


def test_실제_발행_문구가_있어야_진짜_삭제():
    spec = parse_korean_command("고아 글 정리 실제 발행")
    assert spec.task == "cleanup_orphans" and spec.dry_run is False


# --- 본체 -----------------------------------------------------------
def test_자식이_없는_실패건은_고아로_잡힌다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    _mark_failed(rt, 2, "ORPHAN-1")
    monkeypatch.setattr(
        cleanup_mod.api_articles, "get_article", lambda c, sid: _detail(sid)
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        cleanup_mod.api_articles,
        "delete_article",
        lambda c, sid: deleted.append(sid),
    )

    out = cleanup_mod.cleanup_orphans(rt, TaskSpec(task="cleanup_orphans"))
    assert out["ok"] and out["dry_run"] is True
    assert [o["source_id"] for o in out["orphans"]] == ["ORPHAN-1"]
    assert deleted == []  # 모의 실행에서는 절대 지우지 않는다
    assert "삭제 대상" in out["report"]
    rt.close()


def test_자식이_있으면_고아가_아니다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    _mark_failed(rt, 2, "PARENT-1")
    monkeypatch.setattr(
        cleanup_mod.api_articles,
        "get_article",
        lambda c, sid: _detail(sid, child="CHILD-1"),
    )
    out = cleanup_mod.cleanup_orphans(rt, TaskSpec(task="cleanup_orphans"))
    assert out["orphans"] == []
    assert out["skipped"] and "CHILD-1" in out["skipped"][0]["reason"]
    rt.close()


def test_실제_실행이면_삭제한다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    _mark_failed(rt, 2, "ORPHAN-1")
    monkeypatch.setattr(
        cleanup_mod.api_articles, "get_article", lambda c, sid: _detail(sid)
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        cleanup_mod.api_articles,
        "delete_article",
        lambda c, sid: deleted.append(sid) or {"ok": True},
    )
    out = cleanup_mod.cleanup_orphans(
        rt, TaskSpec(task="cleanup_orphans", dry_run=False)
    )
    assert deleted == ["ORPHAN-1"] and out["deleted"] == ["ORPHAN-1"]
    assert rt.publications.by_source_id("ORPHAN-1")["stage"] == "고아 정리 완료"
    rt.close()


def test_이미_지워진_글은_건너뛴다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    _mark_failed(rt, 2, "GONE-1")

    def boom(client, sid):
        raise V2RApiError("삭제된 글", status=404, code="NOT_FOUND")

    monkeypatch.setattr(cleanup_mod.api_articles, "get_article", boom)
    monkeypatch.setattr(cleanup_mod, "classify", lambda exc: "deleted")
    out = cleanup_mod.cleanup_orphans(rt, TaskSpec(task="cleanup_orphans"))
    assert out["orphans"] == [] and out["ok"] is True
    assert out["skipped"][0]["reason"] == "이미 삭제된 글"
    rt.close()


def test_성공한_발행은_후보가_아니다(tmp_path):
    rt = make_runtime(tmp_path)
    rt.publications.mark("팥순이", 3, "h3", "done", "done", source_id="DONE-1")
    assert cleanup_mod.orphan_candidates(rt) == []
    rt.close()


def test_child_source_id_추출():
    assert cleanup_mod.child_source_id(_detail("A", child="B")) == "B"
    assert cleanup_mod.child_source_id(_detail("A")) is None
    assert cleanup_mod.child_source_id({"a": [{"childSourceId": "C"}]}) == "C"


def test_worker_dispatch로_연결된다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    _mark_failed(rt, 2, "ORPHAN-1")
    monkeypatch.setattr(
        cleanup_mod.api_articles, "get_article", lambda c, sid: _detail(sid)
    )
    spec = TaskSpec(task="cleanup_orphans")
    out = worker.dispatch(rt, {"id": 1, "spec_json": spec.to_json()})
    assert out["ok"] and len(out["orphans"]) == 1
    rt.close()


# --- serve 폴링 -----------------------------------------------------
class FakeChannel:
    """텔레그램 대역: 정해진 명령을 한 번만 돌려준다."""

    name = "fake"
    enabled = True

    def __init__(self, texts: list[str], chat_id: str = "111", fail_poll: bool = False):
        self.texts = list(texts)
        self.chat_id = chat_id
        self.fail_poll = fail_poll
        self.sent: list[tuple[str, str]] = []

    def poll(self):
        if self.fail_poll:
            raise RuntimeError("수신 실패")
        from v2r.channels.base import IncomingCommand

        out = [
            IncomingCommand(self.name, "u1", self.chat_id, t) for t in self.texts
        ]
        self.texts = []
        return out

    def send(self, chat_id, text):
        self.sent.append((str(chat_id), text))
        return True

    def broadcast(self, text):
        self.sent.append(("*", text))
        return 1


def test_serve는_명령을_보낸_방에_답한다(tmp_path):
    rt = make_runtime(tmp_path)
    channel = FakeChannel(["상태 알려줘"], chat_id="777")
    rt._channels = [channel]

    out = worker.serve_poll(rt)
    assert out["received"] == 1
    replies = [t for cid, t in channel.sent if cid == "777"]
    # 접수 답장은 없앴다(정책 2026-09-23) — 짧은 작업은 완료 보고 1건만.
    assert len(replies) == 1
    assert any("완료" in t or "상태 조회" in t for t in replies)
    rt.close()


def test_serve는_채널_예외에도_계속_돈다(tmp_path):
    rt = make_runtime(tmp_path)
    bad = FakeChannel([], fail_poll=True)
    good = FakeChannel(["상태 알려줘"], chat_id="888")
    rt._channels = [bad, good]

    out = worker.serve_poll(rt)  # 예외가 새어 나오면 안 된다
    assert out["received"] == 1
    assert any(cid == "888" for cid, _ in good.sent)
    rt.close()


def test_serve는_해석_실패를_그_방에_알린다(tmp_path):
    rt = make_runtime(tmp_path)
    channel = FakeChannel(["asdf 알수없는 말"], chat_id="999")
    rt._channels = [channel]

    worker.serve_poll(rt)
    # 문법에 안 맞으면 자유 대화 해석기로 넘어간다(모델 없으면 되묻기 1건,
    # "명령을 해석하지 못했습니다" 문구는 나가지 않는다 — 정책 2026-09-23).
    assert channel.sent
    text = channel.sent[0][1]
    assert "못 알아들었어요" in text
    assert "해석하지 못했습니다" not in text
    rt.close()


def test_중지_플래그가_있으면_큐를_돌리지_않는다(tmp_path):
    rt = make_runtime(tmp_path)
    rt._channels = []
    worker.request_stop(rt)
    rt.jobs.enqueue(TaskSpec(task="status"), "k1")

    out = worker.serve_poll(rt)
    assert out["stopped"] is True and out["done"] == []
    assert rt.jobs.get(1)["status"] == "queued"
    rt.close()


def test_중지_뒤_새_명령이_오면_다시_돈다(tmp_path):
    rt = make_runtime(tmp_path)
    channel = FakeChannel(["상태 알려줘"], chat_id="777")
    rt._channels = [channel]
    worker.request_stop(rt)

    out = worker.serve_poll(rt)
    assert out["stopped"] is False
    assert worker.stop_requested(rt) is False
    assert len(out["done"]) == 1
    rt.close()


def test_serve는_리스가_끊긴_작업을_정리한다(tmp_path):
    rt = make_runtime(tmp_path)
    rt._channels = []
    job_id = rt.jobs.enqueue(TaskSpec(task="status"), "k2")
    rt.conn.execute(
        "UPDATE jobs SET status = 'running', lease_until = '2000-01-01T00:00:00+09:00'"
        " WHERE id = ?",
        (job_id,),
    )
    rt.conn.commit()

    out = worker.serve_poll(rt)
    assert out["reaped"] >= 1
    rt.close()
