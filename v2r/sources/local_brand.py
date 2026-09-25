"""원본 종류 `local_brand`: 로컬 폴더의 완성 브랜드 원고 JSON 묶음.

`원고폴더 <이름>` 명령 토큰(`TaskSpec.source_folder`)으로 지정한
`warehouse/manuscripts/<이름>/*.json` 을 그대로 Manuscript 목록으로 읽는다.
파일 하나 = 원고 한 건. `_`로 시작하는 키(예: `_note`)는 무시한다.

`(source, source_row)` 는 `publications` 테이블에서 "이미 발행됨"을 판단하는
열쇠라, 파일 순서가 아니라 **파일 이름**에서 안정적으로 뽑는다(파일이
추가·삭제돼도 기존 파일의 키가 흔들리지 않게).
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path

from v2r.content.manuscript import Manuscript, content_hash

#: 원본 종류 이름
KIND = "local_brand"
#: 기본 폴더 (settings.warehouse_dir 기준 상대 경로)
DEFAULT_BASE_DIR = "warehouse/manuscripts"
#: 이 원천의 기본 카페·게시판 (사용자 지시 2026-09-25)
DEFAULT_CAFE = "쌍둥이맘 모여라"
DEFAULT_BOARD = "가족업체 자유게시판"


def folder_entry(base_dir: str | Path, name: str) -> dict:
    """`원고폴더 <name>` 하나를 `_sheet_entries` 형식의 원본 항목으로."""
    base = Path(base_dir)
    path = base / name if not Path(name).is_absolute() else Path(name)
    return {"name": str(name), "kind": KIND, "path": str(path)}


def _row_key(filename: str) -> int:
    """파일 이름에서 안정적인 행 번호를 뽑는다(순서 무관)."""
    return zlib.crc32(filename.encode("utf-8")) & 0x7FFFFFFF


def _clean(data: dict) -> dict:
    """`_`로 시작하는 키(주석용)를 뺀다."""
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def load_folder(path: str | Path, source: str = "") -> list[Manuscript]:
    """폴더의 `*.json` 파일들을 Manuscript 목록으로 읽는다.

    각 파일은 Manuscript 규격 dict(title, body, cafe, board, keyword, tags,
    comments, manuscript_type 등)를 담는다. cafe/board/account가 비어 있으면
    이 원천의 기본값(쌍둥이맘 모여라 / 가족업체 자유게시판 / 자동 계정)을 채운다.
    """
    base = Path(path)
    if not base.is_dir():
        return []
    out: list[Manuscript] = []
    for file in sorted(base.glob("*.json")):
        if not file.is_file():
            continue
        if file.name.startswith("_"):
            continue  # `_results.json` 같은 요약/메타 파일은 원고가 아니다
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        data = _clean(raw)
        # 값이 빈 문자열("")이어도 채워야 한다 — 파일이 키만 두고 비워 둔 경우가
        # 흔하다(예: `board: ""`). setdefault는 이런 경우 안 채운다.
        data["cafe"] = data.get("cafe") or DEFAULT_CAFE
        data["board"] = data.get("board") or DEFAULT_BOARD
        data["head"] = data.get("head") or ""
        data["account"] = data.get("account") or ""  # 빈 값 = 자동 계정
        # 사진 폴더 브랜드: 파일 이름이 `브랜드-키워드.json`이면 그 브랜드를 쓴다
        # (사진 창고가 브랜드별로 나뉘어 있어, 발행 중복 방지 열쇠인 `source`를
        # 폴더 이름으로 고정한 채로도 올바른 브랜드 사진을 찾게 해준다).
        if not data.get("brand"):
            stem = file.stem
            data["brand"] = stem.split("-", 1)[0].strip() if "-" in stem else ""
        # `(source, source_row)`는 publications 표의 발행 열쇠라, 폴더(원본) 이름으로
        # 고정해야 이미 발행됨 판정(prepare_manuscripts)과 일치한다.
        data["source"] = source or str(base.name)
        data["source_row"] = _row_key(file.name)
        title = str(data.get("title") or "")
        body = str(data.get("body") or "")
        data["content_hash"] = data.get("content_hash") or content_hash(title, body)
        try:
            m = Manuscript.model_validate(data)
        except Exception:
            continue
        out.append(m)
    return out


__all__ = [
    "DEFAULT_BASE_DIR",
    "DEFAULT_CAFE",
    "DEFAULT_BOARD",
    "KIND",
    "folder_entry",
    "load_folder",
]
