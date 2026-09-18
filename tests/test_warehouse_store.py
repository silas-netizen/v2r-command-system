"""창고 저장소 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from v2r.warehouse.photo_collector import collect_from_drive_export, collect_from_folder
from v2r.warehouse.store import Warehouse, sha256


def _img(path: Path, color=(1, 2, 3), size=(20, 20)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def test_layout(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()
    root = tmp_path / "wh"
    assert (root / "images" / "originals").is_dir()
    assert (root / "images" / "washed").is_dir()
    assert (root / "manuscripts").is_dir()
    assert (root / "guides").is_dir()
    assert wh.brand_folder("팥순이", "키워드") == root / "images" / "originals" / "팥순이" / "키워드"
    assert wh.washed_folder("abc123") == root / "images" / "washed" / "abc123"


def test_add_original_dedupes_by_sha(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()
    a = _img(tmp_path / "src" / "a.jpg", (10, 20, 30))
    b = tmp_path / "src" / "copy_of_a.jpg"
    b.write_bytes(a.read_bytes())
    c = _img(tmp_path / "src" / "c.jpg", (90, 80, 70))

    first = wh.add_original(a, "팥순이", "BA")
    second = wh.add_original(b, "팥순이", "BA")
    third = wh.add_original(c, "팥순이", "BA")

    assert first == second  # 같은 내용 → 같은 파일
    assert third != first
    assert len(wh.list_originals("팥순이", "BA")) == 2
    assert sha256(first) == sha256(a)


def test_list_originals_filters(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()
    wh.add_original(_img(tmp_path / "s1.jpg", (1, 1, 1)), "브랜드A", "폴더1")
    wh.add_original(_img(tmp_path / "s2.jpg", (2, 2, 2)), "브랜드A", "폴더2")
    wh.add_original(_img(tmp_path / "s3.jpg", (3, 3, 3)), "브랜드B", "폴더1")

    assert len(wh.list_originals()) == 3
    assert len(wh.list_originals("브랜드A")) == 2
    assert len(wh.list_originals("브랜드A", "폴더1")) == 1
    assert wh.list_originals("없는브랜드") == []


def test_add_original_missing(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    with pytest.raises(FileNotFoundError):
        wh.add_original(tmp_path / "nope.jpg", "b", "f")


def test_washed_variants_and_pick(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()
    sha = "a" * 64
    folder = wh.washed_folder(sha)
    folder.mkdir(parents=True)
    for i in (0, 1, 2, 10):
        _img(folder / f"{i}.jpg", (i, i, i))
    (folder / "notes.txt").write_text("무시", encoding="utf-8")

    variants = wh.washed_variants(sha)
    assert [p.stem for p in variants] == ["0", "1", "2", "10"]  # 숫자 순

    assert wh.pick_variant(sha, set()).stem == "0"
    assert wh.pick_variant(sha, {"0"}).stem == "1"
    assert wh.pick_variant(sha, {"0", "1", "2"}).stem == "10"
    assert wh.pick_variant(sha, {"0", "1", "2", "10"}) is None
    assert wh.washed_variants("없는해시") == []
    assert wh.pick_variant("없는해시", set()) is None


def test_pick_variant_accepts_full_paths(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()
    sha = "b" * 64
    folder = wh.washed_folder(sha)
    folder.mkdir(parents=True)
    _img(folder / "0.jpg")
    _img(folder / "1.jpg", (9, 9, 9))
    used = {str(folder / "0.jpg")}
    assert wh.pick_variant(sha, used).stem == "1"


def test_save_manuscript_and_guide(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()
    m = wh.save_manuscript("오늘의 일상", "제목 : 안녕\n본문 :\n반가워요")
    g = wh.save_guide("메이크/지침", "시나리오 설명")
    assert m.parent == wh.manuscripts_dir and m.suffix == ".txt"
    assert m.read_text(encoding="utf-8").startswith("제목 : 안녕")
    assert g.parent == wh.guides_dir
    assert "/" not in g.name  # 경로 구분자 제거


def test_collect_from_folder(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    src = tmp_path / "drive" / "여름"
    _img(src / "1.jpg", (1, 2, 3))
    _img(src / "sub" / "2.png", (4, 5, 6))
    (src / "readme.txt").write_text("무시", encoding="utf-8")

    stats = collect_from_folder(src, "팥순이", "여름", wh)
    assert stats["added"] == 2
    assert stats["skipped"] == 1
    assert stats["errors"] == []

    again = collect_from_folder(src, "팥순이", "여름", wh)
    assert again["added"] == 0
    assert again["duplicated"] == 2

    missing = collect_from_folder(tmp_path / "nope", "팥순이", "여름", wh)
    assert missing["errors"]


def test_collect_from_drive_export(tmp_path: Path):
    import zipfile

    wh = Warehouse(tmp_path / "wh")
    stage = tmp_path / "stage"
    _img(stage / "여름" / "a.jpg", (7, 7, 7))
    _img(stage / "겨울" / "b.jpg", (8, 8, 8))
    _img(stage / "loose.jpg", (9, 9, 9))

    zip_path = tmp_path / "브랜드폴더.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in stage.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(stage).as_posix())

    stats = collect_from_drive_export(zip_path, "팥순이", wh)
    assert stats["added"] == 3
    assert len(wh.list_originals("팥순이", "여름")) == 1
    assert len(wh.list_originals("팥순이", "겨울")) == 1
    assert len(wh.list_originals("팥순이", "브랜드폴더")) == 1

    bad = collect_from_drive_export(tmp_path / "none.zip", "팥순이", wh)
    assert bad["errors"]
