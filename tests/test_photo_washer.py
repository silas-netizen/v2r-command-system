"""사진 세탁기 테스트."""

from __future__ import annotations

import random
from pathlib import Path

import piexif
import pytest
from PIL import Image

from v2r.warehouse import photo_washer
from v2r.warehouse.photo_washer import (
    CAMERA_PRESETS,
    WashError,
    load_exif,
    make_variants,
    prepare_jpeg,
    read_camera_meta,
    wash,
)


def _make_jpeg(path: Path, size=(160, 120), with_exif: bool = True) -> Path:
    img = Image.new("RGB", size, (30, 90, 160))
    for x in range(size[0]):  # 단색 압축 이슈 방지용 그라데이션
        for y in range(0, size[1], 4):
            img.putpixel((x, y), (x % 256, (x * y) % 256, y % 256))
    img.save(path, format="JPEG", quality=95)
    if with_exif:
        exif = {
            "0th": {
                piexif.ImageIFD.Make: b"OLDMAKE",
                piexif.ImageIFD.Model: b"OLDMODEL",
                piexif.ImageIFD.DateTime: b"2001:01:01 00:00:00",
                piexif.ImageIFD.Artist: b"keep-artist",
                piexif.ImageIFD.ImageDescription: b"keep-me",
            },
            "Exif": {
                piexif.ExifIFD.DateTimeOriginal: b"2001:01:01 00:00:00",
                piexif.ExifIFD.UserComment: b"\x00\x00\x00\x00\x00\x00\x00\x00keep",
            },
            "GPS": {
                piexif.GPSIFD.GPSLatitudeRef: b"N",
                piexif.GPSIFD.GPSLatitude: ((37, 1), (30, 1), (0, 1)),
            },
            "1st": {},
            "Interop": {},
            "thumbnail": None,
        }
        piexif.insert(piexif.dump(exif), str(path))
    return path


def test_presets_are_realistic():
    assert len(CAMERA_PRESETS) >= 20
    models = {(p.make, p.model) for p in CAMERA_PRESETS}
    assert len(models) == len(CAMERA_PRESETS)
    assert ("samsung", "SM-S928N") in models
    assert ("Apple", "iPhone 15 Pro") in models
    assert ("Canon", "Canon EOS R6") in models
    assert ("SONY", "ILCE-7M4") in models
    for preset in CAMERA_PRESETS:
        assert preset.lens_models and preset.iso_choices
        assert preset.fnumber_choices and preset.exposure_choices and preset.focal_choices


def test_wash_changes_camera_and_date_and_hash(tmp_path: Path):
    from v2r.warehouse.store import sha256

    src = _make_jpeg(tmp_path / "src.jpg")
    out = tmp_path / "out.jpg"
    wash(src, out, random.Random(7))

    before, after = read_camera_meta(src), read_camera_meta(out)
    assert after["Make"] != before["Make"]
    assert after["Model"] != before["Model"]
    assert after["DateTimeOriginal"] != before["DateTimeOriginal"]
    assert after["DateTime"] != before["DateTime"]
    assert after["LensModel"] and after["BodySerialNumber"]
    assert sha256(out) != sha256(src)


def test_wash_preserves_non_camera_tags(tmp_path: Path):
    src = _make_jpeg(tmp_path / "src.jpg")
    out = tmp_path / "out.jpg"
    wash(src, out, random.Random(3))

    exif = load_exif(out)
    assert exif["0th"][piexif.ImageIFD.ImageDescription] == b"keep-me"
    assert exif["Exif"][piexif.ExifIFD.UserComment].endswith(b"keep")
    # GPS는 절대 지우지 않는다.
    assert exif["GPS"][piexif.GPSIFD.GPSLatitudeRef] == b"N"
    assert exif["GPS"][piexif.GPSIFD.GPSLatitude] == ((37, 1), (30, 1), (0, 1))


def test_wash_works_without_source_exif(tmp_path: Path):
    src = _make_jpeg(tmp_path / "plain.jpg", with_exif=False)
    out = tmp_path / "plain_out.jpg"
    wash(src, out, random.Random(11))
    meta = read_camera_meta(out)
    assert meta["Make"] and meta["Model"] and meta["DateTimeOriginal"]


def test_offset_time_is_seoul(tmp_path: Path):
    src = _make_jpeg(tmp_path / "s.jpg")
    out = tmp_path / "o.jpg"
    wash(src, out, random.Random(5))
    exif = load_exif(out)
    assert exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal] == b"+09:00"


def test_make_variants_distinct_combos(tmp_path: Path):
    src = _make_jpeg(tmp_path / "src.jpg")
    out_dir = tmp_path / "washed"
    paths = make_variants(src, 6, out_dir, seed=42)

    assert len(paths) == 6
    assert [p.name for p in paths] == [f"{i}.jpg" for i in range(6)]
    combos = set()
    for path in paths:
        meta = read_camera_meta(path)
        combos.add((meta["Make"], meta["Model"], meta["DateTimeOriginal"]))
    assert len(combos) == 6


def test_make_variants_does_not_overwrite_existing(tmp_path: Path):
    from v2r.warehouse.store import sha256

    src = _make_jpeg(tmp_path / "src.jpg")
    out_dir = tmp_path / "washed"
    first = make_variants(src, 3, out_dir, seed=1)
    digests = {p.name: sha256(p) for p in first}

    more = make_variants(src, 2, out_dir, seed=2)
    assert [p.name for p in more] == ["3.jpg", "4.jpg"]  # 번호를 이어서
    for path in first:
        assert sha256(path) == digests[path.name]  # 기존 파일 그대로

    combos = set()
    for path in sorted(out_dir.glob("*.jpg")):
        meta = read_camera_meta(path)
        combos.add((meta["Make"], meta["Model"], meta["DateTimeOriginal"]))
    assert len(combos) == 5  # 기존 변형과도 조합이 겹치지 않는다


def test_make_variants_zero(tmp_path: Path):
    src = _make_jpeg(tmp_path / "src.jpg")
    assert make_variants(src, 0, tmp_path / "w") == []


def test_prepare_jpeg_converts_png_with_alpha(tmp_path: Path):
    png = tmp_path / "a.png"
    Image.new("RGBA", (40, 40), (255, 0, 0, 0)).save(png)
    out = prepare_jpeg(png)
    assert out.suffix == ".jpg"
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert img.mode == "RGB"
        assert img.getpixel((5, 5)) == (255, 255, 255)  # 투명 → 흰 배경


def test_prepare_jpeg_keeps_jpeg(tmp_path: Path):
    src = _make_jpeg(tmp_path / "keep.jpg")
    assert prepare_jpeg(src) == src


def test_wash_png_source(tmp_path: Path):
    png = tmp_path / "b.png"
    Image.new("RGBA", (60, 60), (10, 200, 10, 128)).save(png)
    out = tmp_path / "b_out.jpg"
    wash(png, out, random.Random(1))
    with Image.open(out) as img:
        assert img.format == "JPEG"
    assert read_camera_meta(out)["Model"]


def test_wash_missing_file_raises(tmp_path: Path):
    with pytest.raises((FileNotFoundError, WashError, OSError)):
        wash(tmp_path / "nope.jpg", tmp_path / "x.jpg", random.Random(0))


# --- 폭 400px 규칙 --------------------------------------------------------
def test_wash_output_is_at_most_400_wide(tmp_path: Path):
    """세탁본은 언제나 폭 400px 이하 (비율 유지)."""
    src = _make_jpeg(tmp_path / "big.jpg", size=(1600, 1200))
    out = photo_washer.wash(src, tmp_path / "out.jpg")
    with Image.open(out) as img:
        assert img.size[0] <= photo_washer.MAX_VARIANT_WIDTH
        # 비율 유지 (4:3 → 400x300 근처)
        assert abs(img.size[0] / img.size[1] - 1600 / 1200) < 0.02


def test_wash_does_not_upscale_small_images(tmp_path: Path):
    src = _make_jpeg(tmp_path / "small.jpg", size=(160, 120))
    out = photo_washer.wash(src, tmp_path / "out.jpg")
    with Image.open(out) as img:
        assert img.size[0] <= 160


def test_all_variants_are_at_most_400_wide(tmp_path: Path):
    src = _make_jpeg(tmp_path / "big.jpg", size=(2000, 1500))
    made = photo_washer.make_variants(src, 3, tmp_path / "washed")
    assert len(made) == 3
    for path in made:
        with Image.open(path) as img:
            assert img.size[0] <= photo_washer.MAX_VARIANT_WIDTH, path


def test_shrink_to_width_keeps_aspect_and_never_upscales():
    big = Image.new("RGB", (1000, 500))
    assert photo_washer.shrink_to_width(big, 400).size == (400, 200)
    small = Image.new("RGB", (200, 100))
    assert photo_washer.shrink_to_width(small, 400).size == (200, 100)


def test_shrink_file_keeps_exif(tmp_path: Path):
    src = _make_jpeg(tmp_path / "big.jpg", size=(1200, 900))
    washed = photo_washer.wash(src, tmp_path / "w.jpg")
    # 일부러 다시 키운 옛 세탁본을 흉내낸다
    with Image.open(washed) as img:
        exif = img.info.get("exif")
        img.resize((1200, 900)).save(washed, format="JPEG", quality=95, exif=exif or b"")
    before = photo_washer.read_camera_meta(washed)

    assert photo_washer.shrink_file_to_width(washed) is True
    with Image.open(washed) as img:
        assert img.size[0] == 400
    after = photo_washer.read_camera_meta(washed)
    assert after["Make"] == before["Make"] and after["Model"] == before["Model"]
    # 이미 작으면 다시 줄이지 않는다
    assert photo_washer.shrink_file_to_width(washed) is False


def test_resize_all_variants_walks_tree(tmp_path: Path):
    washed_dir = tmp_path / "washed"
    src = _make_jpeg(tmp_path / "s.jpg", size=(800, 600))
    for sha in ("aa", "bb"):
        folder = washed_dir / sha
        folder.mkdir(parents=True)
        for i in range(2):
            with Image.open(src) as img:
                img.save(folder / f"{i}.jpg", format="JPEG", quality=95)

    stats = photo_washer.resize_all_variants(washed_dir)
    assert stats["scanned"] == 4
    assert stats["resized"] == 4
    assert not stats["errors"]
    for path in washed_dir.rglob("*.jpg"):
        with Image.open(path) as img:
            assert img.size[0] == 400
    # 두 번째 실행은 손댈 게 없다
    assert photo_washer.resize_all_variants(washed_dir)["resized"] == 0


def test_pick_variant_shrinks_legacy_wide_variant(tmp_path: Path):
    from v2r.warehouse.store import Warehouse, sha256

    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()
    (wh.originals_dir / "브랜드" / "키워드").mkdir(parents=True, exist_ok=True)
    original = _make_jpeg(wh.originals_dir / "브랜드" / "키워드" / "a.jpg", size=(900, 600))
    sha = sha256(original)
    folder = wh.washed_folder(sha)
    folder.mkdir(parents=True, exist_ok=True)
    photo_washer.wash(original, folder / "0.jpg")
    # 옛 규칙으로 저장된 넓은 세탁본을 흉내낸다
    with Image.open(folder / "0.jpg") as img:
        exif = img.info.get("exif")
        img.resize((900, 600)).save(folder / "0.jpg", format="JPEG", quality=95, exif=exif or b"")

    picked = wh.pick_variant(sha, set())
    assert picked is not None
    with Image.open(picked) as img:
        assert img.size[0] <= photo_washer.MAX_VARIANT_WIDTH
