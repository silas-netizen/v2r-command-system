"""부족한 사진 확보 흐름 (계획 §"부족한 사진 새로 확보하는 방법" 2·3단계).

1. `build_gpt_prompts` — 브랜드·키워드·지침을 바탕으로 GPT 이미지 생성용
   한국어 프롬프트 묶음을 만든다. (API 호출 없음, 순수 문자열 생성)
2. `request_photos` — 프롬프트 묶음과 저장 위치(`inbox/new/<브랜드>/<키워드>/`)를
   텔레그램 등 채널로 보낸다. 사용자는 GPT 채팅에 붙여넣고 결과를 그 폴더에 저장한다.
3. `collect_new` — `inbox/new/**`를 창고 원본으로 옮기고 **즉시 세탁**한다.
   (규칙 0: 세탁 안 된 원본은 절대 발행에 쓰지 않는다)
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from v2r.warehouse.store import (
    IMAGE_SUFFIXES,
    KEYWORD_FOLDER,
    Warehouse,
    brand_entry,
    brand_folder_name,
    load_brands_config,
    safe_name,
    sha256,
)

log = logging.getLogger(__name__)

#: 새 사진을 받아 둘 인박스 하위 경로
NEW_INBOX = ("inbox", "new")
#: 요청 1회당 기본 프롬프트 수
DEFAULT_PROMPT_COUNT = 5
#: 새로 들어온 원본 1장당 즉시 만들 세탁본 수
NEW_PHOTO_VARIANTS = 3

#: 장면 변주 (프롬프트마다 다른 상황을 준다)
SCENES: tuple[str, ...] = (
    "밝은 자연광이 드는 집 안 식탁 위, 흰 접시와 나무 도마를 곁들인 구도",
    "창가 책상 위에 올려둔 모습, 오후 햇살과 부드러운 그림자",
    "주방 조리대 위 사용 직전의 모습, 생활감 있는 배경 소품 약간",
    "손에 들고 있는 1인칭 시점, 배경은 흐릿한 실내",
    "거실 소파 옆 작은 탁자 위, 따뜻한 조명",
    "야외 테이블 위, 흐린 날의 부드러운 빛",
    "정리된 선반 위 정면 구도, 단순한 배경",
    "아침 식사 상차림 한쪽에 자연스럽게 놓인 모습",
)

#: 모든 프롬프트에 공통으로 붙는 스타일 지시
STYLE_RULES = (
    "스마트폰으로 찍은 듯한 현실적인 생활 사진, 과하지 않은 자연광, "
    "광고 스튜디오 느낌 금지, 글자·문구·로고·워터마크 없음, "
    "사람 얼굴이 크게 나오지 않게, 4:3 또는 3:4 비율, 고해상도"
)


def _read_guides(guides_dir: str | Path, brand: str, limit: int = 200) -> str:
    """지침 폴더에서 브랜드 관련 텍스트를 짧게 모은다."""
    base = Path(guides_dir)
    if not base.is_dir():
        return ""
    key = re.sub(r"\s+", "", brand or "").casefold()
    chunks: list[str] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".txt", ".md", ".json"):
            continue
        name = re.sub(r"\s+", "", path.stem).casefold()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if key and key not in name and key not in re.sub(r"\s+", "", text[:4000]).casefold():
            continue
        cleaned = re.sub(r"\s+", " ", text).strip()
        if cleaned:
            chunks.append(cleaned)
        if sum(len(c) for c in chunks) >= limit:
            break
    return " ".join(chunks)[:limit]


def brand_context(brand: str, cfg: dict | None = None, guides_dir: str | Path | None = None) -> str:
    """브랜드 한 줄 설명 (설정 + 지침에서 끌어온다)."""
    cfg = cfg if cfg is not None else load_brands_config()
    entry = brand_entry(brand, cfg)
    parts: list[str] = [f"브랜드 {brand}"]
    aliases = [str(a) for a in (entry.get("aliases") or []) if str(a).strip()]
    if aliases:
        parts.append("다른 표기: " + ", ".join(aliases))
    folders = [str(f) for f in (entry.get("known_folders") or []) if str(f).strip()]
    if folders:
        parts.append("사진 폴더: " + ", ".join(folders[:8]))
    if guides_dir is not None:
        guide = _read_guides(guides_dir, brand)
        if guide:
            parts.append("제품 설명: " + guide)
    return " / ".join(parts)


def build_gpt_prompts(
    brand: str,
    keyword: str,
    guides_dir: str | Path,
    n: int = DEFAULT_PROMPT_COUNT,
    cfg: dict | None = None,
) -> list[str]:
    """GPT 이미지 생성용 한국어 프롬프트 `n`개. 장면이 서로 겹치지 않게 만든다."""
    count = max(int(n or 0), 0)
    if count <= 0:
        return []
    subject = (keyword or "").strip() or KEYWORD_FOLDER
    context = brand_context(brand, cfg, guides_dir)
    out: list[str] = []
    for i in range(count):
        scene = SCENES[i % len(SCENES)]
        out.append(
            f"[{i + 1}/{count}] {context}.\n"
            f"'{subject}' 주제에 어울리는 사진을 만들어 줘. 장면: {scene}.\n"
            f"스타일: {STYLE_RULES}."
        )
    return out


def drop_folder(root: str | Path, brand: str, keyword: str) -> Path:
    """새 사진을 저장할 인박스 폴더 경로."""
    base = Path(root)
    for part in NEW_INBOX:
        base = base / part
    return base / safe_name(brand) / safe_name((keyword or "").strip() or KEYWORD_FOLDER)


def request_photos(rt: Any, brand: str, keyword: str, n: int = DEFAULT_PROMPT_COUNT) -> dict:
    """프롬프트 묶음 + 저장 폴더 경로를 채널로 보낸다 (이미지 생성은 GPT 채팅에서)."""
    from v2r.channels import notify_all

    wh: Warehouse = rt.warehouse
    cfg = load_brands_config()
    brand_name = brand_folder_name(brand, cfg) or brand
    prompts = build_gpt_prompts(brand_name, keyword, wh.guides_dir, n, cfg)
    folder = drop_folder(wh.root, brand_name, keyword)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - 권한 문제
        log.warning("새 사진 폴더 생성 실패 %s: %s", folder, exc)

    message = "\n\n".join(
        [
            f"[사진 요청] 브랜드 {brand_name} / 키워드 {keyword or KEYWORD_FOLDER}",
            "아래 프롬프트를 GPT 채팅에 붙여넣고 '만들어줘'라고 하세요.",
            *prompts,
            f"완성된 이미지는 이 폴더에 저장해 주세요:\n{folder}",
            "저장 후 '새 사진 수거' 명령을 보내면 원본 적재 + 세탁까지 자동으로 합니다.",
        ]
    )
    # 채널 보고는 500자에서 잘리므로(`channels.sanitize`) 프롬프트를 한 건씩 나눠 보낸다
    chunks = [
        f"[사진 요청] 브랜드 {brand_name} / 키워드 {keyword or KEYWORD_FOLDER}"
        " — 아래 프롬프트를 GPT 채팅에 붙여넣고 '만들어줘'라고 하세요.",
        *prompts,
        f"완성된 이미지는 이 폴더에 저장하세요: {folder}"
        " — 저장 후 '새 사진 수거' 명령을 보내면 원본 적재 + 세탁까지 자동으로 합니다.",
    ]
    sent = 0
    for chunk in chunks:
        try:
            sent += notify_all(rt.channels, chunk)
        except Exception as exc:
            log.warning("사진 요청 알림 실패: %s", exc)
    return {
        "ok": True,
        "brand": brand_name,
        "keyword": keyword or KEYWORD_FOLDER,
        "prompts": prompts,
        "folder": str(folder),
        "channels": sent,
        "message": message,
    }


def collect_new(wh: Warehouse | None = None, variants: int = NEW_PHOTO_VARIANTS) -> dict:
    """`inbox/new/<브랜드>/<키워드>/**`를 원본으로 적재하고 즉시 세탁한다.

    적재한 파일은 `inbox/new/_done/` 아래로 옮겨 다음 실행에서 다시 읽지 않는다.
    """
    from v2r.warehouse.photo_washer import make_variants

    wh = wh or Warehouse()
    wh.ensure_dirs()
    cfg = load_brands_config()
    base = wh.root
    for part in NEW_INBOX:
        base = base / part
    stats: dict[str, Any] = {
        "ok": True,
        "scanned": 0,
        "added": 0,
        "duplicated": 0,
        "variants": 0,
        "folders": [],
        "errors": [],
        "inbox": str(base),
    }
    if not base.is_dir():
        return stats

    done_root = base / "_done"
    for brand_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        if brand_dir.name.startswith("_"):
            continue
        brand = brand_folder_name(brand_dir.name, cfg) or brand_dir.name
        targets = [(p, p.name) for p in sorted(brand_dir.iterdir()) if p.is_dir()]
        if any(
            p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES for p in brand_dir.iterdir()
        ):
            # 브랜드 폴더 바로 아래 놓인 파일은 키워드 폴더로 본다
            targets.append((brand_dir, KEYWORD_FOLDER))
        for keyword_dir, folder in targets:
            added = made = 0
            scan = keyword_dir.iterdir() if keyword_dir == brand_dir else keyword_dir.rglob("*")
            for path in sorted(p for p in scan if p.is_file()):
                if path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                stats["scanned"] += 1
                try:
                    before = {str(p) for p in wh.list_originals(brand, folder)}
                    dest = wh.add_original(path, brand, folder)
                except Exception as exc:
                    stats["errors"].append(f"{path.name}: {exc}")
                    continue
                if str(dest) in before:
                    stats["duplicated"] += 1
                else:
                    stats["added"] += 1
                    added += 1
                try:
                    sha = sha256(dest)
                    have = len(wh.washed_variants(sha))
                    if have < variants:
                        new = make_variants(dest, variants - have, wh.washed_folder(sha))
                        made += len(new)
                        stats["variants"] += len(new)
                except Exception as exc:
                    stats["errors"].append(f"세탁 실패 {path.name}: {exc}")
                _archive(path, keyword_dir, done_root / brand_dir.name / folder)
            if added or made:
                stats["folders"].append(
                    {"brand": brand, "folder": folder, "added": added, "variants": made}
                )
    stats["ok"] = not stats["errors"]
    return stats


def _archive(path: Path, source_root: Path, done_dir: Path) -> None:
    """처리한 인박스 파일을 `_done` 폴더로 옮긴다 (실패해도 무시)."""
    try:
        rel = path.relative_to(source_root)
    except ValueError:
        rel = Path(path.name)
    dest = done_dir / rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        shutil.move(str(path), str(dest))
    except OSError as exc:
        log.info("인박스 파일 정리 실패 %s: %s", path, exc)


__all__ = [
    "DEFAULT_PROMPT_COUNT",
    "NEW_INBOX",
    "brand_context",
    "build_gpt_prompts",
    "collect_new",
    "drop_folder",
    "request_photos",
]
