"""원본 종류 `daily_pool`: 창고에 쌓아 둔 짧은 일상 글 풀 (결정 2, 2026-09-19).

`warehouse/manuscripts/daily_pool.jsonl`을 원고 목록으로 읽는다.
사진 없음·브랜드 언급 없음이 전제다.
"""

from __future__ import annotations

from pathlib import Path

from v2r.content.manuscript import Manuscript

KIND = "daily_pool"


def is_pool_entry(entry: dict | None) -> bool:
    """sources.yaml 항목이 일상 글 풀인가."""
    return isinstance(entry, dict) and (entry.get("kind") or "") == KIND


def load_pool(warehouse_dir: str | Path) -> list[Manuscript]:
    """풀 파일을 원고 목록으로 읽는다."""
    from v2r.warehouse.daily_generator import load_pool as _load

    return _load(warehouse_dir)


def pool_entry(sources_cfg: dict | None) -> dict | None:
    """설정에서 첫 `daily_pool` 항목을 찾는다."""
    for entry in (sources_cfg or {}).get("sources") or []:
        if is_pool_entry(entry):
            return entry
    return None


__all__ = ["KIND", "is_pool_entry", "load_pool", "pool_entry"]
