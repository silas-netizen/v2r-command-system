"""사진 세탁기. 외부 GUI 없이 Pillow + piexif로 EXIF 카메라/날짜 정보만 랜덤화한다.

원칙:
- 기존 태그를 지우지 않는다(GPS 포함). 카메라·렌즈·날짜 태그만 덮어쓴다.
- 성공 판정 = 카메라 메타 변경 또는 파일 sha256 변경.
- PNG/WebP 등은 흰 배경 합성 후 JPEG q95로 변환한다.
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

import piexif
from PIL import Image

from v2r.warehouse.store import sha256


class WashError(RuntimeError):
    """세탁 실패."""


@dataclass(frozen=True)
class CameraPreset:
    """카메라 1종의 EXIF 후보값 묶음."""

    make: str
    model: str
    lens_models: tuple[str, ...]
    iso_choices: tuple[int, ...]
    fnumber_choices: tuple[float, ...]
    exposure_choices: tuple[str, ...]  # "1/120" 형식
    focal_choices: tuple[float, ...]
    software: str = ""


_PHONE_ISO = (50, 64, 80, 100, 125, 160, 200, 320, 400, 640)
_PHONE_EXP = ("1/30", "1/50", "1/60", "1/100", "1/120", "1/250", "1/500", "1/1000")
_CAM_ISO = (100, 125, 160, 200, 250, 320, 400, 640, 800, 1250, 1600, 3200)
_CAM_EXP = ("1/40", "1/60", "1/125", "1/160", "1/250", "1/400", "1/800", "1/1600")


def _p(make, model, lenses, iso, fn, exp, focal, software=""):
    return CameraPreset(make, model, tuple(lenses), tuple(iso), tuple(fn), tuple(exp), tuple(focal), software)


CAMERA_PRESETS: list[CameraPreset] = [
    # --- 삼성 갤럭시 ---
    _p("samsung", "SM-S928N", ["Samsung Galaxy S24 Ultra Rear Camera"], _PHONE_ISO, (1.7, 2.2, 2.4, 3.4), _PHONE_EXP, (2.2, 6.3, 6.7, 15.6), "S928NKSU3AXK1"),
    _p("samsung", "SM-S921N", ["Samsung Galaxy S24 Rear Camera"], _PHONE_ISO, (1.8, 2.2, 2.4), _PHONE_EXP, (2.2, 5.4, 6.3), "S921NKSU2AXJ5"),
    _p("samsung", "SM-S911N", ["Samsung Galaxy S23 Rear Camera"], _PHONE_ISO, (1.8, 2.2, 2.4), _PHONE_EXP, (2.2, 5.4, 6.3), "S911NKSU4CXG1"),
    _p("samsung", "SM-S918N", ["Samsung Galaxy S23 Ultra Rear Camera"], _PHONE_ISO, (1.7, 2.2, 2.4, 4.9), _PHONE_EXP, (2.2, 6.3, 6.7, 17.0), "S918NKSU3BXH2"),
    _p("samsung", "SM-F956N", ["Samsung Galaxy Z Fold6 Rear Camera"], _PHONE_ISO, (1.8, 2.2, 2.4), _PHONE_EXP, (2.2, 4.7, 6.4), "F956NKSU1AXH9"),
    _p("samsung", "SM-F741N", ["Samsung Galaxy Z Flip6 Rear Camera"], _PHONE_ISO, (1.8, 2.2), _PHONE_EXP, (2.2, 6.4), "F741NKSU1AXG8"),
    _p("samsung", "SM-A546S", ["Samsung Galaxy A54 Rear Camera"], _PHONE_ISO, (1.8, 2.2, 2.4), _PHONE_EXP, (2.2, 5.4), "A546SKSU3CXE1"),
    _p("samsung", "SM-N986N", ["Samsung Galaxy Note20 Ultra Rear Camera"], _PHONE_ISO, (1.8, 2.2, 3.0), _PHONE_EXP, (2.2, 5.4, 7.0), "N986NKSU2FVI2"),
    _p("samsung", "SM-G991N", ["Samsung Galaxy S21 Rear Camera"], _PHONE_ISO, (1.8, 2.2, 2.4), _PHONE_EXP, (2.2, 5.4, 6.0), "G991NKSU5EWA1"),
    # --- 애플 아이폰 ---
    _p("Apple", "iPhone 15 Pro", ["iPhone 15 Pro back triple camera 6.765mm f/1.78"], _PHONE_ISO, (1.78, 2.2, 2.8), _PHONE_EXP, (2.22, 6.765, 9.0), "17.4.1"),
    _p("Apple", "iPhone 15", ["iPhone 15 back dual camera 5.96mm f/1.6"], _PHONE_ISO, (1.6, 2.4), _PHONE_EXP, (2.22, 5.96), "17.5.1"),
    _p("Apple", "iPhone 14 Pro", ["iPhone 14 Pro back triple camera 6.86mm f/1.78"], _PHONE_ISO, (1.78, 2.2, 2.8), _PHONE_EXP, (2.22, 6.86, 9.0), "16.6"),
    _p("Apple", "iPhone 14", ["iPhone 14 back dual camera 5.7mm f/1.5"], _PHONE_ISO, (1.5, 2.4), _PHONE_EXP, (1.54, 5.7), "16.7.2"),
    _p("Apple", "iPhone 13 mini", ["iPhone 13 mini back dual camera 5.1mm f/1.6"], _PHONE_ISO, (1.6, 2.4), _PHONE_EXP, (1.57, 5.1), "15.6.1"),
    _p("Apple", "iPhone 13 Pro", ["iPhone 13 Pro back triple camera 5.7mm f/1.5"], _PHONE_ISO, (1.5, 1.8, 2.8), _PHONE_EXP, (1.57, 5.7, 9.0), "15.7"),
    _p("Apple", "iPhone 12", ["iPhone 12 back dual camera 4.2mm f/1.6"], _PHONE_ISO, (1.6, 2.4), _PHONE_EXP, (1.55, 4.2), "14.8.1"),
    _p("Apple", "iPhone SE (3rd generation)", ["iPhone SE (3rd generation) back camera 3.99mm f/1.8"], _PHONE_ISO, (1.8,), _PHONE_EXP, (3.99,), "16.4"),
    # --- 캐논 ---
    _p("Canon", "Canon EOS R6", ["RF24-105mm F4 L IS USM", "RF50mm F1.8 STM"], _CAM_ISO, (1.8, 2.8, 4.0, 5.6, 8.0), _CAM_EXP, (24.0, 35.0, 50.0, 85.0, 105.0), "Digital Photo Professional"),
    _p("Canon", "Canon EOS R5", ["RF28-70mm F2 L USM", "RF85mm F1.2 L USM"], _CAM_ISO, (1.2, 2.0, 2.8, 4.0, 5.6), _CAM_EXP, (28.0, 50.0, 70.0, 85.0), ""),
    _p("Canon", "Canon EOS 5D Mark IV", ["EF24-70mm f/2.8L II USM"], _CAM_ISO, (2.8, 4.0, 5.6, 8.0), _CAM_EXP, (24.0, 35.0, 50.0, 70.0), ""),
    _p("Canon", "Canon EOS 200D II", ["EF-S18-55mm f/4-5.6 IS STM"], _CAM_ISO, (4.0, 5.0, 5.6, 8.0), _CAM_EXP, (18.0, 24.0, 35.0, 55.0), ""),
    # --- 소니 ---
    _p("SONY", "ILCE-7M4", ["FE 24-70mm F2.8 GM", "FE 35mm F1.8"], _CAM_ISO, (1.8, 2.8, 4.0, 5.6), _CAM_EXP, (24.0, 35.0, 50.0, 70.0), ""),
    _p("SONY", "ILCE-7C", ["FE 28-60mm F4-5.6"], _CAM_ISO, (4.0, 5.0, 5.6, 8.0), _CAM_EXP, (28.0, 35.0, 45.0, 60.0), ""),
    _p("SONY", "ILCE-6400", ["E PZ 16-50mm F3.5-5.6 OSS"], _CAM_ISO, (3.5, 4.5, 5.6, 8.0), _CAM_EXP, (16.0, 24.0, 35.0, 50.0), ""),
    # --- 니콘 / 후지 / LG / 샤오미 ---
    _p("NIKON CORPORATION", "NIKON Z 6_2", ["NIKKOR Z 24-70mm f/4 S"], _CAM_ISO, (4.0, 5.6, 8.0, 11.0), _CAM_EXP, (24.0, 35.0, 50.0, 70.0), ""),
    _p("FUJIFILM", "X-T4", ["XF18-55mmF2.8-4 R LM OIS"], _CAM_ISO, (2.8, 4.0, 5.6, 8.0), _CAM_EXP, (18.0, 27.0, 35.0, 55.0), ""),
    _p("LG Electronics", "LM-V500N", ["LG V50 ThinQ Rear Camera"], _PHONE_ISO, (1.5, 1.9, 2.4), _PHONE_EXP, (2.2, 4.6, 6.6), ""),
    _p("LG Electronics", "LM-G900N", ["LG Velvet Rear Camera"], _PHONE_ISO, (1.8, 2.2, 2.4), _PHONE_EXP, (2.2, 4.6, 6.0), ""),
    _p("Xiaomi", "2201123G", ["Xiaomi 12 Rear Camera"], _PHONE_ISO, (1.9, 2.2, 2.4), _PHONE_EXP, (2.2, 5.4, 6.0), ""),
]

_SERIAL_CHARS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"

#: 세탁본(발행에 붙는 사진)의 최대 가로 폭(px).
#: 이보다 넓으면 비율을 지켜 줄인다. **절대 확대하지 않는다.**
MAX_VARIANT_WIDTH = 400

#: 픽셀까지 손볼지의 기본값. 배포 환경에서 이 값만 바꾸면 전체 동작이 바뀐다.
#: True  = 가장자리 1px crop + 품질 92~96 재인코딩 (크기 1px 감소, 세대 손실)
#: False = EXIF 메타만 교체 (handoff §10 "변경 대상은 촬영 날짜, 카메라 정보")
DEFAULT_TWEAK_PIXELS = True


def _rand_serial(rng: random.Random, length: int = 12) -> str:
    return "".join(rng.choice(_SERIAL_CHARS) for _ in range(length))


def _rational(value: float, max_denominator: int = 10000) -> tuple[int, int]:
    frac = Fraction(value).limit_denominator(max_denominator)
    return (frac.numerator, frac.denominator)


def _exposure_rational(text: str) -> tuple[int, int]:
    if "/" in text:
        num, den = text.split("/", 1)
        return (int(float(num)), int(float(den)))
    return _rational(float(text))


def _empty_exif() -> dict:
    return {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "Interop": {}, "thumbnail": None}


def load_exif(path: str | Path) -> dict:
    """파일의 piexif 구조 EXIF. 없으면 빈 구조."""
    try:
        data = piexif.load(str(path))
    except Exception:
        return _empty_exif()
    base = _empty_exif()
    for key in ("0th", "Exif", "GPS", "1st", "Interop"):
        base[key] = dict(data.get(key) or {})
    base["thumbnail"] = data.get("thumbnail")
    return base


def random_exif(
    base_exif: dict | None,
    rng: random.Random,
    date_range: tuple[int, ...] | int = (730,),
    preset: CameraPreset | None = None,
    now: datetime | None = None,
) -> dict:
    """카메라·날짜·렌즈 태그만 랜덤화한 piexif 구조를 만든다.

    `base_exif`의 나머지 태그(GPS 포함)는 그대로 보존한다.
    `date_range`는 (days_back,) 또는 정수 days_back.
    """
    days_back = date_range if isinstance(date_range, int) else int(date_range[0])
    preset = preset or rng.choice(CAMERA_PRESETS)
    now = now or datetime.now()

    shot = now - timedelta(
        days=rng.randint(0, max(days_back, 0)),
        hours=rng.randint(0, 23),
        minutes=rng.randint(0, 59),
        seconds=rng.randint(0, 59),
    )
    stamp = shot.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
    subsec = f"{rng.randint(0, 99):02d}".encode("ascii")
    offset = b"+09:00"

    exif = _empty_exif()
    if base_exif:
        for key in ("0th", "Exif", "GPS", "1st", "Interop"):
            exif[key] = dict(base_exif.get(key) or {})
        exif["thumbnail"] = base_exif.get("thumbnail")

    zeroth = exif["0th"]
    zeroth[piexif.ImageIFD.Make] = preset.make.encode("utf-8")
    zeroth[piexif.ImageIFD.Model] = preset.model.encode("utf-8")
    zeroth[piexif.ImageIFD.DateTime] = stamp
    if preset.software:
        zeroth[piexif.ImageIFD.Software] = preset.software.encode("utf-8")

    sub = exif["Exif"]
    sub[piexif.ExifIFD.DateTimeOriginal] = stamp
    sub[piexif.ExifIFD.DateTimeDigitized] = stamp
    sub[piexif.ExifIFD.SubSecTime] = subsec
    sub[piexif.ExifIFD.SubSecTimeOriginal] = subsec
    sub[piexif.ExifIFD.SubSecTimeDigitized] = subsec
    sub[piexif.ExifIFD.OffsetTime] = offset
    sub[piexif.ExifIFD.OffsetTimeOriginal] = offset
    sub[piexif.ExifIFD.OffsetTimeDigitized] = offset
    sub[piexif.ExifIFD.ExposureTime] = _exposure_rational(rng.choice(preset.exposure_choices))
    sub[piexif.ExifIFD.FNumber] = _rational(rng.choice(preset.fnumber_choices))
    sub[piexif.ExifIFD.ISOSpeedRatings] = int(rng.choice(preset.iso_choices))
    sub[piexif.ExifIFD.FocalLength] = _rational(rng.choice(preset.focal_choices))
    sub[piexif.ExifIFD.LensMake] = preset.make.encode("utf-8")
    sub[piexif.ExifIFD.LensModel] = rng.choice(preset.lens_models).encode("utf-8")
    sub[piexif.ExifIFD.BodySerialNumber] = _rand_serial(rng).encode("ascii")
    sub[piexif.ExifIFD.LensSerialNumber] = _rand_serial(rng, 10).encode("ascii")
    return exif


def _decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").rstrip("\x00").strip()
    return "" if value is None else str(value)


def read_camera_meta(path: str | Path) -> dict:
    """카메라 판별에 쓰는 주요 EXIF 값을 문자열 dict로 읽는다."""
    exif = load_exif(path)
    zeroth, sub = exif["0th"], exif["Exif"]
    return {
        "Make": _decode(zeroth.get(piexif.ImageIFD.Make)),
        "Model": _decode(zeroth.get(piexif.ImageIFD.Model)),
        "Software": _decode(zeroth.get(piexif.ImageIFD.Software)),
        "DateTime": _decode(zeroth.get(piexif.ImageIFD.DateTime)),
        "DateTimeOriginal": _decode(sub.get(piexif.ExifIFD.DateTimeOriginal)),
        "DateTimeDigitized": _decode(sub.get(piexif.ExifIFD.DateTimeDigitized)),
        "LensMake": _decode(sub.get(piexif.ExifIFD.LensMake)),
        "LensModel": _decode(sub.get(piexif.ExifIFD.LensModel)),
        "BodySerialNumber": _decode(sub.get(piexif.ExifIFD.BodySerialNumber)),
        "LensSerialNumber": _decode(sub.get(piexif.ExifIFD.LensSerialNumber)),
        "ISOSpeedRatings": _decode(sub.get(piexif.ExifIFD.ISOSpeedRatings)),
        "FNumber": _decode(sub.get(piexif.ExifIFD.FNumber)),
        "ExposureTime": _decode(sub.get(piexif.ExifIFD.ExposureTime)),
        "FocalLength": _decode(sub.get(piexif.ExifIFD.FocalLength)),
    }


def prepare_jpeg(src: str | Path, dest: str | Path | None = None) -> Path:
    """JPEG면 그대로, 아니면 흰 배경 합성 후 JPEG q95로 변환한 경로를 준다."""
    src = Path(src)
    with Image.open(src) as img:
        fmt = (img.format or "").upper()
        if fmt in {"JPEG", "MPO"} and dest is None:
            return src
        converted = _flatten_on_white(img)
    out = Path(dest) if dest is not None else src.with_suffix(".jpg")
    if out == src:
        out = src.with_name(src.stem + "_jpeg.jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    converted.save(out, format="JPEG", quality=95, subsampling=0)
    converted.close()
    return out


def shrink_to_width(img: Image.Image, max_width: int = MAX_VARIANT_WIDTH) -> Image.Image:
    """가로가 `max_width`를 넘으면 비율을 지켜 줄인다. 작으면 **그대로** 둔다.

    확대는 하지 않는다 (작은 원본을 늘리면 화질이 티 나게 뭉개진다).
    """
    width, height = img.size
    if max_width <= 0 or width <= max_width:
        return img
    new_height = max(int(round(height * (max_width / float(width)))), 1)
    return img.resize((max_width, new_height), Image.LANCZOS)


def shrink_file_to_width(path: str | Path, max_width: int = MAX_VARIANT_WIDTH) -> bool:
    """이미 저장된 JPEG을 자리에서 줄인다. 줄였으면 True.

    EXIF는 보존한다 (세탁으로 심어 둔 카메라 메타가 날아가면 안 된다).
    """
    path = Path(path)
    with Image.open(path) as opened:
        if opened.size[0] <= max_width:
            return False
        exif_bytes = opened.info.get("exif")
        resized = shrink_to_width(opened.convert("RGB"), max_width)
    resized.save(path, format="JPEG", quality=95, subsampling=0)
    resized.close()
    if exif_bytes:
        try:
            piexif.insert(exif_bytes, str(path))
        except Exception:  # pragma: no cover - 손상된 EXIF
            pass
    return True


def resize_all_variants(washed_dir: str | Path, max_width: int = MAX_VARIANT_WIDTH) -> dict:
    """`images/washed/` 아래 모든 세탁본을 폭 `max_width` 이하로 맞춘다 (자리 수정)."""
    base = Path(washed_dir)
    stats = {"scanned": 0, "resized": 0, "skipped": 0, "errors": []}
    if not base.is_dir():
        return stats
    for path in sorted(base.rglob("*.jpg")):
        stats["scanned"] += 1
        try:
            if shrink_file_to_width(path, max_width):
                stats["resized"] += 1
            else:
                stats["skipped"] += 1
        except Exception as exc:
            stats["errors"].append(f"{path.name}: {exc}")
    return stats


def _flatten_on_white(img: Image.Image) -> Image.Image:
    """알파 채널을 흰 배경에 합성해 RGB 이미지를 만든다."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return img.convert("RGB")


def wash(
    src: str | Path,
    out: str | Path,
    rng: random.Random | None = None,
    tweak_pixels: bool | None = None,
    preset: CameraPreset | None = None,
) -> Path:
    """`src`를 세탁해 `out`(JPEG)으로 저장한다. 실패하면 WashError.

    `tweak_pixels`가 참이면 가장자리 1px을 잘라내고 품질 92~96으로 재인코딩한다
    (픽셀 해시까지 확실히 달라지지만 크기가 1px 줄고 세대 손실이 생긴다).
    거짓이면 EXIF만 바꾼다(해시는 그래도 달라진다).
    `None`이면 모듈 기본값 `DEFAULT_TWEAK_PIXELS`를 따른다.
    """
    if tweak_pixels is None:
        tweak_pixels = DEFAULT_TWEAK_PIXELS
    rng = rng or random.Random()
    src = Path(src)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    src_sha = sha256(src)
    before = read_camera_meta(src)
    base_exif = load_exif(src)

    with Image.open(src) as img:
        image = _flatten_on_white(img)
        width, height = image.size
        if tweak_pixels and width > 8 and height > 8:
            edge = rng.choice(("left", "right", "top", "bottom"))
            box = {
                "left": (1, 0, width, height),
                "right": (0, 0, width - 1, height),
                "top": (0, 1, width, height),
                "bottom": (0, 0, width, height - 1),
            }[edge]
            image = image.crop(box)
        # 세탁본은 언제나 폭 400px 이하 (비율 유지, 확대 없음)
        image = shrink_to_width(image, MAX_VARIANT_WIDTH)
        quality = rng.randint(92, 96) if tweak_pixels else 95
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, subsampling=0)
        image.close()

    out.write_bytes(buffer.getvalue())

    exif = random_exif(base_exif, rng, preset=preset)
    try:
        piexif.insert(piexif.dump(exif), str(out))
    except Exception as exc:  # pragma: no cover - piexif 내부 오류
        raise WashError(f"EXIF 삽입 실패: {exc}") from exc

    after = read_camera_meta(out)
    meta_changed = any(
        before.get(k) != after.get(k)
        for k in ("Make", "Model", "DateTimeOriginal", "DateTimeDigitized", "DateTime")
    )
    hash_changed = sha256(out) != src_sha
    if not (meta_changed or hash_changed):
        raise WashError("세탁 실패: 카메라 메타도 해시도 바뀌지 않았습니다.")
    return out


def make_variants(
    src: str | Path,
    count: int,
    out_dir: str | Path,
    seed: int | None = None,
    tweak_pixels: bool | None = None,
) -> list[Path]:
    """서로 다른 (Make, Model, DateTimeOriginal) 조합의 세탁본 `count`개를 만든다.

    **기존 세탁본은 절대 덮어쓰지 않는다.** 이미 있는 `N.jpg` 중 가장 큰 번호
    다음부터 번호를 이어 붙이고, 기존 변형의 조합도 중복 검사에 포함한다
    (`photo_usage`가 파일 이름(stem)으로 사용 여부를 기록하기 때문).
    `tweak_pixels=None`이면 `DEFAULT_TWEAK_PIXELS`를 따른다.
    """
    if count <= 0:
        return []
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    base_exif = load_exif(src)
    combos: set[tuple[str, str, str]] = set()
    results: list[Path] = []

    # 기존 변형 파악: 다음 번호와 이미 쓰인 조합
    next_index = 0
    for existing in out_dir.glob("*.jpg"):
        if existing.stem.isdigit():
            next_index = max(next_index, int(existing.stem) + 1)
        meta = read_camera_meta(existing)
        combos.add((meta["Make"], meta["Model"], meta["DateTimeOriginal"]))

    presets = list(CAMERA_PRESETS)
    attempts = 0
    max_attempts = count * 40 + 60
    while len(results) < count and attempts < max_attempts:
        attempts += 1
        preset = presets[(len(results) + attempts) % len(presets)] if attempts > count * 4 else rng.choice(presets)
        dest = out_dir / f"{next_index + len(results)}.jpg"
        try:
            wash(src, dest, rng, tweak_pixels=tweak_pixels, preset=preset)
        except WashError:
            continue
        meta = read_camera_meta(dest)
        combo = (meta["Make"], meta["Model"], meta["DateTimeOriginal"])
        if combo in combos:
            dest.unlink(missing_ok=True)
            continue
        combos.add(combo)
        results.append(dest)

    if len(results) < count:
        raise WashError(f"서로 다른 조합 {count}개를 만들지 못했습니다 (생성 {len(results)}개).")
    return results


__all__ = [
    "CAMERA_PRESETS",
    "DEFAULT_TWEAK_PIXELS",
    "MAX_VARIANT_WIDTH",
    "resize_all_variants",
    "shrink_file_to_width",
    "shrink_to_width",
    "CameraPreset",
    "WashError",
    "load_exif",
    "make_variants",
    "prepare_jpeg",
    "random_exif",
    "read_camera_meta",
    "wash",
]
