"""창고 저장소. 원본 이미지·세탁본·원고·지침 파일 배치를 담당한다.

배치:
    <root>/images/originals/<브랜드>/<폴더>/<파일>
    <root>/images/washed/<원본 sha256>/<변형번호>.jpg
    <root>/manuscripts/<이름>.txt
    <root>/guides/<이름>.txt
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

_SAFE_RE = re.compile(r"[^0-9A-Za-z가-힣._\- ]+")


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

    def pick_variant(self, sha: str, used: set[str]) -> Path | None:
        """`used`(변형 이름 집합)에 없는 첫 세탁본. 남은 게 없으면 None."""
        for variant in self.washed_variants(sha):
            if variant.stem not in used and str(variant) not in used:
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
