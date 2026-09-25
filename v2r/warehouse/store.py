"""창고 저장소. 원본 이미지·세탁본·원고·지침 파일 배치를 담당한다.

배치:
    <root>/images/originals/<브랜드>/<폴더>/<파일>
    <root>/images/washed/<원본 sha256>/<변형번호>.jpg
    <root>/manuscripts/<이름>.txt
    <root>/guides/<이름>.txt
"""

from __future__ import annotations

import hashlib
import random
import re
import shutil
from pathlib import Path
from typing import Any

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

_SAFE_RE = re.compile(r"[^0-9A-Za-z가-힣._\- ]+")

#: 키워드 공용 폴더 이름 (`{키워드}` 토큰이 가리키는 폴더)
KEYWORD_FOLDER = "키워드"


def _topic_folder_names(entry: dict, keyword: str) -> list[str] | None:
    """`keyword_topic_folders` 규칙에서 keyword가 걸리는 폴더 목록(config 표기 그대로).

    사용자 규칙(2026-09-25): 브랜드 항목에 `keyword_topic_folders`가 있으면
    키워드(공백 제거·casefold)에 `match` 낱말이 하나라도 포함될 때 그 `folders`를 쓴다.
    걸리는 주제가 없으면 `None`(기존 규칙으로 넘어간다).
    """
    topics = entry.get("keyword_topic_folders") or []
    key = _squash(keyword)
    if not key or not isinstance(topics, list):
        return None
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        words = topic.get("match") or []
        if any(_squash(w) and _squash(w) in key for w in words):
            folders = topic.get("folders") or []
            return [str(f) for f in folders if f]
    return None


class NoPhotoError(RuntimeError):
    """키워드 폴더를 채울 원본 사진이 하나도 없다(텔레그램 알림 대상)."""


def _squash(text: str) -> str:
    """공백 제거 + casefold (레거시 `images.py` 비교 규칙)."""
    return re.sub(r"\s+", "", str(text or "")).casefold()


def load_brands_config() -> dict:
    """`config/brands.yaml`을 읽는다. 없으면 빈 dict."""
    try:
        from v2r.config import load_yaml

        return load_yaml("brands") or {}
    except Exception:
        return {}


def brand_entry(brand: str, cfg: dict | None = None) -> dict:
    """브랜드 설정 1건. 별칭(`aliases`)으로도 찾는다."""
    cfg = cfg if cfg is not None else load_brands_config()
    brands = cfg.get("brands") or {}
    key = _squash(brand)
    for name, entry in brands.items():
        if not isinstance(entry, dict):
            continue
        if _squash(name) == key:
            return entry
        if any(_squash(a) == key for a in (entry.get("aliases") or [])):
            return entry
    return {}


def brand_folder_name(brand: str, cfg: dict | None = None) -> str:
    """브랜드 → 창고/인박스 폴더 이름 (별칭을 정식 폴더명으로 바꾼다)."""
    entry = brand_entry(brand, cfg)
    return str(entry.get("folder") or entry.get("drive_folder") or brand or "")


def token_folder_name(
    brand: str, token: str, keyword: str = "", cfg: dict | None = None
) -> str:
    """플레이스홀더 토큰 → 브랜드 하위 폴더 이름.

    - `{키워드}` 또는 A열 키워드와 같은 문자열 → 브랜드에 `keyword` 규칙이 있으면 `키워드`
    - 브랜드별 별칭(팥순이 `B/A` → `BA`) → 지정 폴더
    - 그 외 → 토큰 문자열 그대로
    """
    cfg = cfg if cfg is not None else load_brands_config()
    entry = brand_entry(brand, cfg)
    rules = entry.get("placeholder_rules") or {}
    token = str(token or "").strip()

    aliases = rules.get("aliases") or {}
    for alias, rule in aliases.items():
        if _squash(alias) == _squash(token) and isinstance(rule, dict):
            return str(rule.get("folder") or token)

    is_keyword = _squash(token) == _squash(KEYWORD_FOLDER) or (
        bool(keyword) and _squash(token) == _squash(keyword)
    )
    keyword_rule = rules.get("keyword")
    if is_keyword and isinstance(keyword_rule, dict):
        return str(keyword_rule.get("folder") or KEYWORD_FOLDER)
    if _squash(token) == _squash(KEYWORD_FOLDER):
        return KEYWORD_FOLDER
    return token or KEYWORD_FOLDER


def token_select_mode(
    brand: str, token: str, keyword: str = "", cfg: dict | None = None
) -> str:
    """토큰의 사진 선택 방식: `random` 또는 `filename_match`."""
    cfg = cfg if cfg is not None else load_brands_config()
    entry = brand_entry(brand, cfg)
    rules = entry.get("placeholder_rules") or {}
    folder = token_folder_name(brand, token, keyword, cfg)
    keyword_rule = rules.get("keyword")
    if isinstance(keyword_rule, dict) and _squash(keyword_rule.get("folder") or "") == _squash(folder):
        return str(keyword_rule.get("select") or "random")
    for alias, rule in (rules.get("aliases") or {}).items():
        del alias
        if isinstance(rule, dict) and _squash(rule.get("folder") or "") == _squash(folder):
            return str(rule.get("select") or "random")
    default = rules.get("default") or {}
    return str(default.get("select") or "random")


def inbox_brand_dirs(root: str | Path, brand_folder: str) -> list[Path]:
    """`inbox/image/**/<브랜드>` 후보 폴더 목록 (`image/image` 이중 경로 포함)."""
    base = Path(root) / "inbox"
    if not base.exists():
        return []
    key = _squash(brand_folder)
    out: list[Path] = []
    for path in base.rglob("*"):
        if path.is_dir() and _squash(path.name) == key:
            out.append(path)
    return sorted(out)


def sha256(path: str | Path) -> str:
    """파일 내용의 sha256 16진 문자열."""
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(name: str) -> str:
    """경로 구분자·특수문자를 제거한 안전한 폴더/파일 이름."""
    cleaned = _SAFE_RE.sub("_", str(name).strip()).strip(". ")
    return cleaned or "_"


def default_root() -> Path:
    """설정의 warehouse_dir. 설정 모듈이 없으면 `warehouse`."""
    try:
        from v2r.config import get_settings  # 지연 임포트 (묶음 1 병렬 작성 중)

        return Path(get_settings().warehouse_dir)
    except Exception:
        return Path("warehouse")


class Warehouse:
    """창고 루트 한 개를 감싸는 파일 저장소."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()

    # --- 경로 ---------------------------------------------------------
    @property
    def images_dir(self) -> Path:
        return self.root / "images"

    @property
    def originals_dir(self) -> Path:
        return self.images_dir / "originals"

    @property
    def washed_dir(self) -> Path:
        return self.images_dir / "washed"

    @property
    def manuscripts_dir(self) -> Path:
        return self.root / "manuscripts"

    @property
    def guides_dir(self) -> Path:
        return self.root / "guides"

    def ensure_dirs(self) -> None:
        """필요한 하위 폴더를 모두 만든다."""
        for path in (
            self.originals_dir,
            self.washed_dir,
            self.manuscripts_dir,
            self.guides_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def brand_folder(self, brand: str, folder: str) -> Path:
        """`originals/<브랜드>/<폴더>` 경로."""
        return self.originals_dir / safe_name(brand) / safe_name(folder)

    def washed_folder(self, sha: str) -> Path:
        """원본 해시별 세탁본 폴더."""
        return self.washed_dir / sha

    def brand_root(self, brand: str, cfg: dict | None = None) -> Path:
        """`originals/<브랜드>` 경로 (별칭은 정식 폴더명으로)."""
        return self.originals_dir / safe_name(brand_folder_name(brand, cfg) or brand)

    def keyword_folder(
        self, brand: str, keyword: str, *, cfg: dict | None = None, token: str = ""
    ) -> Path:
        """`originals/<브랜드>/키워드`(또는 토큰 폴더) 경로.

        `token`을 주면 그 토큰의 폴더 규칙을 따른다. 안 주면 `keyword`를 토큰으로 본다.
        """
        cfg = cfg if cfg is not None else load_brands_config()
        folder = token_folder_name(brand, token or keyword, keyword, cfg)
        return self.brand_root(brand, cfg) / safe_name(folder)

    def topic_pool(self, brand: str, keyword: str, cfg: dict | None = None) -> list[Path]:
        """`keyword_topic_folders` 주제 라우팅: 사진이 있는 폴더를 합친 원본 목록(랜덤 순서).

        주제에 안 걸리거나(브랜드에 규칙이 없거나) 걸린 폴더에 사진이 하나도 없으면
        빈 리스트(호출자는 기존 규칙으로 넘어간다).
        """
        cfg = cfg if cfg is not None else load_brands_config()
        entry = brand_entry(brand, cfg)
        names = _topic_folder_names(entry, keyword)
        if not names:
            return []
        base = self.brand_root(brand, cfg)
        pool: list[Path] = []
        for name in names:
            folder = base / safe_name(name)
            if not folder.is_dir():
                continue
            pool.extend(
                p
                for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
            )
        if pool:
            random.shuffle(pool)
        return pool

    def root_originals(self, brand: str, cfg: dict | None = None) -> list[Path]:
        """브랜드 폴더 **바로 아래** 원본 목록 (키워드 폴더를 채울 후보)."""
        base = self.brand_root(brand, cfg)
        if not base.is_dir():
            return []
        return sorted(
            p
            for p in base.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        )

    def seed_candidates(self, brand: str, cfg: dict | None = None) -> list[Path]:
        """키워드 폴더 씨앗이 될 원본 후보: 창고 브랜드 루트 + 인박스 브랜드 폴더."""
        cfg = cfg if cfg is not None else load_brands_config()
        out: list[Path] = list(self.root_originals(brand, cfg))
        folder = brand_folder_name(brand, cfg) or brand
        for directory in inbox_brand_dirs(self.root, folder):
            out.extend(
                sorted(
                    p
                    for p in directory.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
                )
            )
        return out

    def ensure_keyword_pool(
        self,
        brand: str,
        keyword: str,
        min_variants: int = 1,
        washer: Any = None,
        *,
        token: str = "",
        cfg: dict | None = None,
    ) -> list[Path]:
        """키워드(토큰) 폴더에 원본과 세탁본을 확보한다. 원본 목록을 돌려준다.

        1. 폴더에 원본이 하나도 없으면 브랜드 루트/인박스 원본을 복사해 채운다.
        2. 세탁본이 `min_variants`개에 못 미치면 `photo_washer.make_variants`로 만든다.
        3. 쓸 원본이 하나도 없으면 `NoPhotoError`(한국어 메시지, 브랜드·키워드 명시).
        """
        cfg = cfg if cfg is not None else load_brands_config()
        label = token or keyword or KEYWORD_FOLDER
        folder = self.keyword_folder(brand, keyword, cfg=cfg, token=token)
        folder_name = folder.name

        topic_pool = self.topic_pool(brand, keyword, cfg)
        if topic_pool:
            originals = topic_pool
            folder_name = "/".join(sorted({p.parent.name for p in topic_pool}))
        else:
            originals = [
                p
                for p in (sorted(folder.iterdir()) if folder.is_dir() else [])
                if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
            ]

        if not originals:
            seeds = self.seed_candidates(brand, cfg)
            if not seeds:
                raise NoPhotoError(
                    f"사진이 필요합니다: 브랜드 {brand}의 '{label}' 폴더"
                    f"({folder_name})에 쓸 원본이 하나도 없습니다."
                    " 창고 브랜드 폴더나 인박스에 사진을 넣어 주세요."
                )
            for seed in seeds:
                try:
                    self.add_original(seed, brand_folder_name(brand, cfg) or brand, folder_name)
                except Exception:
                    continue
            originals = [
                p
                for p in (sorted(folder.iterdir()) if folder.is_dir() else [])
                if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
            ]

        if not originals:
            raise NoPhotoError(
                f"사진이 필요합니다: 브랜드 {brand}의 '{label}' 폴더"
                f"({folder_name})를 채우지 못했습니다."
            )

        # 규칙 0: 세탁본 없이는 절대 원본을 쓰지 않는다. 최소 1장은 즉석에서 세탁한다.
        need_min = max(int(min_variants or 0), 1)
        if washer is None:
            from v2r.warehouse import photo_washer

            washer = photo_washer.make_variants
        made = sum(len(self.washed_variants(sha256(p))) for p in originals)
        wash_errors: list[str] = []
        for original in originals:
            if made >= need_min:
                break
            sha = sha256(original)
            need = need_min - made
            try:
                washer(original, need, self.washed_folder(sha))
            except Exception as exc:
                wash_errors.append(f"{original.name}: {exc}")
                continue
            made = sum(len(self.washed_variants(sha256(p))) for p in originals)
        if made <= 0:
            detail = f" ({'; '.join(wash_errors[:3])})" if wash_errors else ""
            raise NoPhotoError(
                f"사진이 필요합니다: 브랜드 {brand}의 '{label}' 폴더"
                f"({folder_name}) 세탁본을 만들지 못했습니다{detail}."
            )
        return originals

    # --- 원본 ---------------------------------------------------------
    def add_original(self, path: str | Path, brand: str, folder: str) -> Path:
        """원본 이미지를 창고로 복사한다. 같은 sha256이 이미 있으면 그 파일을 돌려준다."""
        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(f"원본 파일이 없습니다: {src}")
        sha = sha256(src)
        dest_dir = self.brand_folder(brand, folder)
        dest_dir.mkdir(parents=True, exist_ok=True)

        for existing in sorted(dest_dir.iterdir()):
            if existing.is_file() and sha256(existing) == sha:
                return existing

        suffix = src.suffix.lower() or ".jpg"
        dest = dest_dir / f"{sha[:16]}{suffix}"
        n = 1
        while dest.exists():
            dest = dest_dir / f"{sha[:16]}_{n}{suffix}"
            n += 1
        shutil.copy2(src, dest)
        return dest

    def list_originals(
        self, brand: str | None = None, folder: str | None = None
    ) -> list[Path]:
        """원본 이미지 목록. 브랜드/폴더로 좁힐 수 있다."""
        base = self.originals_dir
        if brand is not None:
            base = base / safe_name(brand)
            if folder is not None:
                base = base / safe_name(folder)
        if not base.exists():
            return []
        out = [
            p
            for p in base.rglob("*")
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        ]
        return sorted(out)

    # --- 세탁본 -------------------------------------------------------
    def washed_variants(self, sha: str) -> list[Path]:
        """원본 해시에 대한 세탁본 변형 목록(번호순)."""
        folder = self.washed_folder(sha)
        if not folder.exists():
            return []
        items: list[tuple[int, Path]] = []
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() == ".jpg":
                try:
                    items.append((int(p.stem), p))
                except ValueError:
                    continue
        return [p for _, p in sorted(items)]

    def is_washed(self, path: str | Path) -> bool:
        """규칙 0 검사: 경로가 `images/washed/` 아래인가."""
        try:
            Path(path).resolve().relative_to(self.washed_dir.resolve())
        except (ValueError, OSError):
            return False
        return True

    def pick_variant(self, sha: str, used: set[str]) -> Path | None:
        """`used`(변형 이름 집합)에 없는 첫 세탁본. 남은 게 없으면 None.

        **규칙 0**: 여기서 나오는 경로는 반드시 `images/washed/` 아래다.
        세탁 안 된 원본은 어떤 경우에도 돌려주지 않는다.

        **폭 400px 규칙**: 옛 세탁본이 더 넓으면 돌려주기 전에 자리에서 줄인다
        (비율 유지, 확대 없음). 붙는 사진은 언제나 400px 이하다.
        """
        for variant in self.washed_variants(sha):
            if variant.stem in used or str(variant) in used:
                continue
            if not self.is_washed(variant):  # pragma: no cover - 방어적 검사
                continue
            try:
                from v2r.warehouse.photo_washer import shrink_file_to_width

                shrink_file_to_width(variant)
            except Exception:  # pragma: no cover - 축소 실패해도 발행은 막지 않는다
                pass
            return variant
        return None

    # --- 텍스트 -------------------------------------------------------
    def save_manuscript(self, name: str, text: str) -> Path:
        """원고 텍스트 저장."""
        return self._save_text(self.manuscripts_dir, name, text)

    def save_guide(self, name: str, text: str) -> Path:
        """지침 텍스트 저장."""
        return self._save_text(self.guides_dir, name, text)

    @staticmethod
    def _save_text(directory: Path, name: str, text: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        safe = safe_name(name)
        if not safe.lower().endswith((".txt", ".md", ".json")):
            safe += ".txt"
        dest = directory / safe
        dest.write_text(text, encoding="utf-8")
        return dest

    @staticmethod
    def sha256(path: str | Path) -> str:
        """파일 sha256 (모듈 함수와 동일)."""
        return sha256(path)
