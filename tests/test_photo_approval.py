"""사진 생성 승인 흐름 테스트 (사용자 규칙 2026-09-19).

규칙: 키워드 폴더에 있는 사진을 먼저 세탁해서 쓰고, 모자랄 때만 만든다.
만들기 전에 먼저 물어보고(승인), 만든 사진도 승인/반려를 받는다.
실제 브라우저·API는 부르지 않는다(전부 대역).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from v2r.command.parser import parse_korean_command
from v2r.command.spec import TaskSpec
from v2r.config import Settings
from v2r.engine import worker
from v2r.engine.context import Runtime
from v2r.store.db import connect
from v2r.warehouse import photo_request


class FakePhotoChannel:
    """사진·글 발송을 기록만 하는 채널 대역."""

    name = "fake"
    enabled = True

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.photos: list[tuple[str, str]] = []

    def poll(self):
        return []

    def send(self, chat_id, text):
        self.sent.append(str(text))
        return True

    def broadcast(self, text):
        self.sent.append(str(text))
        return 1

    def send_photo(self, chat_id, path, caption=""):
        self.photos.append((str(path), str(caption)))
        return True

    def broadcast_photo(self, path, caption=""):
        return 1 if self.send_photo("*", path, caption) else 0


def make_runtime(tmp_path) -> tuple[Runtime, FakePhotoChannel]:
    settings = Settings(
        v2r_email="tester@example.com",
        v2r_password="",
        data_dir=tmp_path / "data",
        warehouse_dir=tmp_path / "warehouse",
        db_path=tmp_path / "data" / "v2r.sqlite",
    )
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "warehouse").mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    rt = Runtime.open(settings, conn)
    rt._client = object()
    rt._llm = None
    rt._llm_ready = True
    channel = FakePhotoChannel()
    rt._channels = [channel]
    return rt, channel


def _png(path: Path) -> Path:
    """작은 진짜 이미지 파일 1장."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 45), (120, 160, 90)).save(path)
    return path


# --- 1) 파서 ---------------------------------------------------------
@pytest.mark.parametrize(
    "text, task, approved",
    [
        ("사진 생성 승인 우아덤 키워드 2장", "generate_photos", True),
        ("사진 2장 생성 브랜드 우아덤", "generate_photos", False),
        ("사진 승인 우아덤 키워드", "approve_photos", False),
        ("사진 반려 우아덤 키워드", "reject_photos", False),
    ],
)
def test_parser_routes_photo_approval(text, task, approved):
    spec = parse_korean_command(text)
    assert spec is not None
    assert spec.task == task
    assert spec.approved is approved
    assert spec.brand == "우아덤"


def test_parser_positional_brand_and_default_keyword():
    spec = parse_korean_command("사진 승인 새브랜드")
    assert spec.task == "approve_photos"
    assert spec.brand == "새브랜드"
    assert spec.keyword == "키워드"

    spec = parse_korean_command("사진 생성 승인 우아덤 키워드 2장")
    assert spec.keyword == "키워드" and spec.count == 2


# --- 2) 승인 없는 `사진 생성` → 만들지 않고 재고 보고 ---------------
def test_generate_without_approval_reports_stock(tmp_path, monkeypatch):
    rt, channel = make_runtime(tmp_path)
    calls: list[tuple] = []

    def fake_generate_batch(*args, **kwargs):  # pragma: no cover - 불리면 실패
        calls.append((args, kwargs))
        return {"ok": True, "files": []}

    monkeypatch.setattr(
        "v2r.warehouse.gpt_images.generate_batch", fake_generate_batch, raising=False
    )
    spec = TaskSpec(task="generate_photos", brand="우아덤", keyword="국물", count=2)
    out = worker.dispatch(rt, {"id": 1, "spec_json": spec.to_json()})

    assert calls == []  # 생성은 절대 하지 않는다
    assert out["ok"] and out["generated"] == 0 and out["approved"] is False
    assert "사진 재고" in out["message"]
    assert "원본 0장" in out["message"] and "미사용 세탁본 0장" in out["message"]
    assert "사진 생성 승인 우아덤 국물 2장" in out["message"]
    assert any("사진 재고" in t for t in channel.sent)
    rt.close()


def test_generate_without_approval_says_enough_when_stocked(tmp_path, monkeypatch):
    rt, channel = make_runtime(tmp_path)
    monkeypatch.setattr(
        worker,
        "_photo_stock",
        lambda rt_, brand, folder: {
            "brand": brand,
            "folder": folder,
            "originals": 2,
            "variants": 6,
            "used": 1,
            "unused": 5,
        },
    )
    spec = TaskSpec(task="generate_photos", brand="우아덤", keyword="국물", count=2)
    out = worker.dispatch(rt, {"id": 1, "spec_json": spec.to_json()})
    assert out["enough"] is True
    assert "충분합니다" in out["message"]
    rt.close()


# --- 3) 승인된 생성 → collect=False + 사진 전송 ----------------------
def test_approved_generation_keeps_files_in_inbox_and_sends_photos(tmp_path, monkeypatch):
    rt, channel = make_runtime(tmp_path)
    made = [
        _png(tmp_path / "inbox" / "a.jpg"),
        _png(tmp_path / "inbox" / "b.jpg"),
    ]
    seen: dict = {}

    def fake_generate_batch(brand, keyword, n=3, **kwargs):
        seen.update({"brand": brand, "keyword": keyword, "n": n, **kwargs})
        return {
            "ok": True,
            "brand": brand,
            "keyword": keyword,
            "files": [str(p) for p in made],
            "generated": len(made),
            "errors": [],
        }

    monkeypatch.setattr("v2r.warehouse.gpt_images.generate_batch", fake_generate_batch)
    spec = TaskSpec(
        task="generate_photos", brand="우아덤", keyword="키워드", count=2, approved=True
    )
    out = worker.dispatch(rt, {"id": 1, "spec_json": spec.to_json()})

    assert seen["collect"] is False  # 세탁·적재하지 않는다
    assert seen["n"] == 2
    assert out["pending_approval"] is True and out["photo_sent"] == 2
    assert [p for p, _ in channel.photos] == [str(p) for p in made]
    assert "우아덤/키워드 1/2" in channel.photos[0][1]
    assert "사진 승인 우아덤 키워드" in channel.photos[0][1]
    assert "사진 반려 우아덤 키워드" in channel.photos[1][1]
    # 파일은 그대로 남아 있어야 한다 (세탁 전)
    assert all(p.is_file() for p in made)
    rt.close()


def test_approved_generation_falls_back_to_text(tmp_path, monkeypatch):
    """사진을 못 보내는 채널뿐이면 경로를 글로 알린다."""
    rt, channel = make_runtime(tmp_path)
    rt._channels = [_TextOnlyChannel()]
    photo = _png(tmp_path / "inbox" / "a.jpg")
    monkeypatch.setattr(
        "v2r.warehouse.gpt_images.generate_batch",
        lambda brand, keyword, n=3, **kw: {
            "ok": True,
            "brand": brand,
            "keyword": keyword,
            "files": [str(photo)],
            "generated": 1,
            "errors": [],
        },
    )
    spec = TaskSpec(task="generate_photos", brand="우아덤", approved=True, count=1)
    out = worker.dispatch(rt, {"id": 1, "spec_json": spec.to_json()})
    assert out["photo_sent"] == 0
    assert any(str(photo) in t for t in rt.channels[0].sent)
    rt.close()


class _TextOnlyChannel(FakePhotoChannel):
    """사진 전송을 지원하지 않는 채널."""

    broadcast_photo = None  # type: ignore[assignment]


# --- 4) 사진 승인 / 반려 --------------------------------------------
def test_approve_collects_only_that_folder(tmp_path):
    rt, channel = make_runtime(tmp_path)
    root = rt.warehouse.root
    mine = _png(root / "inbox" / "new" / "우아덤" / "국물" / "one.jpg")
    other = _png(root / "inbox" / "new" / "우아덤" / "다른키워드" / "two.jpg")

    spec = TaskSpec(task="approve_photos", brand="우아덤", keyword="국물")
    out = worker.dispatch(rt, {"id": 1, "spec_json": spec.to_json()})

    assert out["added"] == 1 and out["variants"] > 0
    assert not mine.is_file()  # 적재 후 _done으로 옮겨진다
    assert other.is_file()  # 다른 폴더는 건드리지 않는다
    assert "사진 승인" in out["message"]
    assert any("사진 승인" in t for t in channel.sent)
    rt.close()


def test_reject_deletes_only_inbox_files(tmp_path):
    rt, channel = make_runtime(tmp_path)
    root = rt.warehouse.root
    doomed = _png(root / "inbox" / "new" / "우아덤" / "국물" / "one.jpg")
    kept_inbox = _png(root / "inbox" / "new" / "우아덤" / "다른키워드" / "two.jpg")
    kept_original = _png(root / "images" / "originals" / "우아덤" / "국물" / "keep.jpg")

    spec = TaskSpec(task="reject_photos", brand="우아덤", keyword="국물")
    out = worker.dispatch(rt, {"id": 1, "spec_json": spec.to_json()})

    assert out["removed"] == 1 and out["ok"]
    assert not doomed.is_file()
    assert kept_inbox.is_file() and kept_original.is_file()
    assert "사진 반려" in out["message"]
    rt.close()


def test_reject_refuses_outside_inbox(tmp_path):
    rt, _ = make_runtime(tmp_path)
    out = photo_request.reject_new(rt.warehouse, brand="", keyword="국물")
    assert out["ok"] is False
    rt.close()


# --- 5) 부족 알림 ----------------------------------------------------
def test_shortage_notice_sent_once(tmp_path):
    rt, channel = make_runtime(tmp_path)
    from v2r.engine.publish import notify_photo_shortage

    spec = TaskSpec(task="publish_daily", brand="우아덤", dry_run=False)
    assert notify_photo_shortage(rt, spec, "우아덤", "키워드", 2, 0) is True
    assert notify_photo_shortage(rt, spec, "우아덤", "키워드", 2, 0) is False
    assert len(channel.sent) == 1
    text = channel.sent[0]
    assert text.startswith("사진 부족: 브랜드 우아덤 / 키워드 폴더")
    assert "필요 2장, 사용 가능 0장" in text
    assert "'사진 생성 승인 우아덤 키워드 2장' 이라고 보내세요" in text
    # 다른 폴더는 따로 한 번 더 알린다
    assert notify_photo_shortage(rt, spec, "우아덤", "국물", 1, 0) is True
    assert len(channel.sent) == 2
    rt.close()


def test_shortage_notice_silent_on_dry_run(tmp_path):
    rt, channel = make_runtime(tmp_path)
    from v2r.engine.publish import notify_photo_shortage

    spec = TaskSpec(task="publish_daily", brand="우아덤", dry_run=True)
    assert notify_photo_shortage(rt, spec, "우아덤", "키워드", 2, 0) is False
    assert channel.sent == []
    rt.close()
