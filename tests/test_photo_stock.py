"""사진 재고·요청·수거 테스트 (실제 API/네트워크 없음)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from v2r.warehouse import photo_request, stock
from v2r.warehouse.store import NoPhotoError, Warehouse, sha256


def _img(path: Path, color=(120, 90, 60), size=(64, 48)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG", quality=95)
    return path


def _warehouse(tmp_path: Path) -> Warehouse:
    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()
    return wh


# --- 재고 -----------------------------------------------------------------
def test_inventory_counts_per_brand_folder(tmp_path: Path):
    wh = _warehouse(tmp_path)
    a = _img(wh.originals_dir / "팥순이" / "키워드" / "a.jpg", (10, 20, 30))
    _img(wh.originals_dir / "팥순이" / "BA" / "b.jpg", (40, 50, 60))
    from v2r.warehouse.photo_washer import make_variants

    made = make_variants(a, 3, wh.washed_folder(sha256(a)))
    assert len(made) == 3

    inv = stock.inventory(wh, used={str(made[0])})
    by_folder = {(r["brand"], r["folder"]): r for r in inv}
    keyword = by_folder[("팥순이", "키워드")]
    assert keyword["originals"] == 1
    assert keyword["variants"] == 3
    assert keyword["used"] == 1
    assert keyword["unused"] == 2
    assert by_folder[("팥순이", "BA")] == {
        "brand": "팥순이",
        "folder": "BA",
        "originals": 1,
        "variants": 0,
        "used": 0,
        "unused": 0,
    }


def test_inventory_counts_used_by_file_name(tmp_path: Path):
    """`photo_usage`가 파일 이름(stem)만 기록해도 사용으로 센다."""
    wh = _warehouse(tmp_path)
    a = _img(wh.originals_dir / "우아덤" / "키워드" / "a.jpg")
    from v2r.warehouse.photo_washer import make_variants

    made = make_variants(a, 2, wh.washed_folder(sha256(a)))
    inv = stock.inventory(wh, used={made[0].stem})
    assert inv[0]["used"] == 1
    assert inv[0]["unused"] == 1


def test_low_stock_filters_and_sorts(tmp_path: Path):
    inv = [
        {"brand": "A", "folder": "키워드", "originals": 1, "variants": 40, "used": 0, "unused": 40},
        {"brand": "B", "folder": "키워드", "originals": 1, "variants": 5, "used": 0, "unused": 5},
        {"brand": "C", "folder": "키워드", "originals": 1, "variants": 2, "used": 1, "unused": 1},
    ]
    low = stock.low_stock(inv, threshold=30)
    assert [r["brand"] for r in low] == ["C", "B"]
    assert stock.low_stock(inv, threshold=1) == []


def test_top_up_creates_variants_for_low_folders(tmp_path: Path):
    wh = _warehouse(tmp_path)
    _img(wh.originals_dir / "장으뜸" / "키워드" / "a.jpg", (7, 8, 9))
    out = stock.top_up(wh, threshold=30, add_per_original=2)
    assert out["ok"], out["errors"]
    assert out["created"] == 2
    inv = stock.inventory(wh)
    assert inv[0]["variants"] == 2


def test_top_up_skips_folders_above_threshold(tmp_path: Path):
    wh = _warehouse(tmp_path)
    _img(wh.originals_dir / "장으뜸" / "키워드" / "a.jpg", (7, 8, 9))
    stock.top_up(wh, threshold=3, add_per_original=3)
    out = stock.top_up(wh, threshold=3, add_per_original=3)
    assert out["created"] == 0


def test_brand_summary_aggregates(tmp_path: Path):
    inv = [
        {"brand": "팥순이", "folder": "키워드", "originals": 5, "variants": 15, "used": 1, "unused": 14},
        {"brand": "팥순이", "folder": "BA", "originals": 2, "variants": 6, "used": 0, "unused": 6},
    ]
    summary = stock.brand_summary(inv)
    assert summary == [
        {"brand": "팥순이", "folders": 2, "originals": 7, "variants": 21, "unused": 20}
    ]


# --- 규칙 0: 세탁 안 된 원본은 절대 쓰지 않는다 ---------------------------
def test_picked_photo_is_always_under_washed(tmp_path: Path):
    wh = _warehouse(tmp_path)
    original = _img(wh.originals_dir / "우아덤" / "키워드" / "a.jpg", (3, 4, 5))
    originals = wh.ensure_keyword_pool("우아덤", "키워드", min_variants=1)
    assert originals == [original]

    sha = sha256(original)
    picked = wh.pick_variant(sha, set())
    assert picked is not None
    assert wh.is_washed(picked)
    assert wh.washed_dir in picked.parents
    assert picked != original
    assert not wh.is_washed(original)


def test_no_photo_error_when_washing_fails(tmp_path: Path, monkeypatch):
    """세탁이 실패하면 원본으로 물러나지 않고 NoPhotoError."""
    wh = _warehouse(tmp_path)
    _img(wh.originals_dir / "우아덤" / "키워드" / "a.jpg", (3, 4, 5))

    def boom(*args, **kwargs):
        raise RuntimeError("세탁 실패")

    with pytest.raises(NoPhotoError):
        wh.ensure_keyword_pool("우아덤", "키워드", min_variants=1, washer=boom)


# --- GPT 프롬프트 ---------------------------------------------------------
def test_build_gpt_prompts_shape(tmp_path: Path):
    guides = tmp_path / "guides"
    guides.mkdir()
    (guides / "팥순이.txt").write_text("팥순이는 팥순추출물 건강식품입니다.", encoding="utf-8")

    prompts = photo_request.build_gpt_prompts("팥순이", "단호박 샐러드", guides, n=5)
    assert len(prompts) == 5
    assert len(set(prompts)) == 5
    for i, prompt in enumerate(prompts, start=1):
        assert isinstance(prompt, str)
        assert prompt.startswith(f"[{i}/5]")
        assert "팥순이" in prompt
        assert "단호박 샐러드" in prompt
        assert "워터마크 없음" in prompt
        assert "스마트폰" in prompt


def test_build_gpt_prompts_zero_and_default_keyword(tmp_path: Path):
    assert photo_request.build_gpt_prompts("우아덤", "키워드", tmp_path, n=0) == []
    prompts = photo_request.build_gpt_prompts("우아덤", "", tmp_path, n=1)
    assert len(prompts) == 1
    assert "키워드" in prompts[0]


def test_drop_folder_path(tmp_path: Path):
    folder = photo_request.drop_folder(tmp_path, "팥순이", "단호박")
    assert folder == tmp_path / "inbox" / "new" / "팥순이" / "단호박"


def test_request_photos_notifies_without_network(tmp_path: Path):
    wh = _warehouse(tmp_path)
    sent: list[str] = []

    class _Channel:
        name = "테스트"

        def send(self, chat_id, text):  # pragma: no cover - notify_all 경로에 따라
            sent.append(text)

        def broadcast(self, text):
            sent.append(text)

    class _RT:
        warehouse = wh
        channels = [_Channel()]

    out = photo_request.request_photos(_RT(), "팥순이", "단호박")
    assert out["ok"] is True
    assert out["brand"] == "팥순이"
    assert len(out["prompts"]) == photo_request.DEFAULT_PROMPT_COUNT
    assert Path(out["folder"]).is_dir()
    assert "inbox" in out["folder"] and "new" in out["folder"]
    assert "사진 요청" in out["message"]


# --- 새 사진 수거 ---------------------------------------------------------
def test_collect_new_imports_and_washes(tmp_path: Path):
    wh = _warehouse(tmp_path)
    drop = photo_request.drop_folder(wh.root, "팥순이", "단호박")
    _img(drop / "gpt_1.jpg", (200, 100, 50), size=(80, 60))

    out = photo_request.collect_new(wh, variants=3)
    assert out["ok"], out["errors"]
    assert out["added"] == 1
    assert out["variants"] == 3
    assert out["folders"] == [
        {"brand": "팥순이", "folder": "단호박", "added": 1, "variants": 3}
    ]

    originals = wh.list_originals("팥순이", "단호박")
    assert len(originals) == 1
    variants = wh.washed_variants(sha256(originals[0]))
    assert len(variants) == 3
    assert all(wh.is_washed(v) for v in variants)
    # 처리한 파일은 인박스에서 치운다 → 다시 실행해도 중복 적재 없음
    assert not (drop / "gpt_1.jpg").exists()
    again = photo_request.collect_new(wh, variants=3)
    assert again["added"] == 0
    assert again["variants"] == 0


# --- wash_photos 작업 -----------------------------------------------------
def _wash_rt(wh: Warehouse, max_new: int | None = None):
    class _RT:
        warehouse = None
        scratch: dict = {}

    rt = _RT()
    rt.warehouse = wh
    rt.scratch = {} if max_new is None else {"wash_max_new": max_new}
    return rt


def test_wash_photos_fills_every_folder_and_is_resumable(tmp_path: Path):
    from v2r.command.spec import TaskSpec
    from v2r.engine.worker import _wash_photos

    wh = _warehouse(tmp_path)
    _img(wh.originals_dir / "팥순이" / "키워드" / "a.jpg", (10, 20, 30))
    _img(wh.originals_dir / "팥순이" / "BA" / "b.jpg", (40, 50, 60))
    _img(wh.originals_dir / "우아덤" / "키워드" / "c.jpg", (70, 80, 90))

    out = _wash_photos(_wash_rt(wh), TaskSpec(task="wash_photos", count=3))
    assert out["ok"], out["errors"]
    assert out["per_original"] == 3
    assert out["variants"] == 9
    assert {(f["brand"], f["folder"]) for f in out["folders"]} == {
        ("팥순이", "키워드"),
        ("팥순이", "BA"),
        ("우아덤", "키워드"),
    }

    # 다시 실행하면 이미 채운 원본은 건너뛴다
    again = _wash_photos(_wash_rt(wh), TaskSpec(task="wash_photos", count=3))
    assert again["variants"] == 0
    assert again["skipped"] == 3


def test_wash_photos_brand_filter_and_max_new(tmp_path: Path):
    from v2r.command.spec import TaskSpec
    from v2r.engine.worker import _wash_photos

    wh = _warehouse(tmp_path)
    _img(wh.originals_dir / "팥순이" / "키워드" / "a.jpg", (10, 20, 30))
    _img(wh.originals_dir / "우아덤" / "키워드" / "c.jpg", (70, 80, 90))

    out = _wash_photos(_wash_rt(wh), TaskSpec(task="wash_photos", count=2, brand="팥순이"))
    assert {f["brand"] for f in out["folders"]} == {"팥순이"}
    assert out["variants"] == 2

    capped = _wash_photos(_wash_rt(wh, max_new=1), TaskSpec(task="wash_photos", count=3))
    assert capped["variants"] == 1
    assert capped.get("stopped") is True


def test_collect_new_without_inbox(tmp_path: Path):
    wh = _warehouse(tmp_path)
    out = photo_request.collect_new(wh)
    assert out["ok"] is True
    assert out["added"] == 0
