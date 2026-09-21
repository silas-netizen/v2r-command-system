"""사용량 장부 — 모델 호출마다 토큰을 한 줄씩 적어 둔다 (2026-09-22).

요금제 길은 돈이 0원이지만 **요금제 한도**를 그대로 갉아먹는다. 그래서 호출마다
`cache_read` / `cache_creation` / `input` / `output` 토큰을 남겨 두고, 캐시가 실제로
걸리고 있는지(`cache_hit_ratio`)를 눈으로 확인한다.

파일은 **월별로 쪼갠다** (`data/llm_usage-2026-09.jsonl`). 한 파일이 끝없이 커지면
읽을 때마다 느려지기 때문이다.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "LEDGER_BASENAME",
    "LEDGER_DIR_ENV",
    "append_call",
    "cache_hit_ratio",
    "ledger_path",
    "read_month",
]

#: 장부 파일 이름 뼈대 (`llm_usage-YYYY-MM.jsonl` 로 쪼개 쓴다)
LEDGER_BASENAME = "llm_usage"

#: 장부를 **다른 폴더로 돌리는** 환경변수 (2026-09-22).
#:
#: 시험(pytest)에서 라우터를 `data_dir` 없이 만들면 진짜 `data/` 장부에 가짜
#: 호출이 그대로 적혀 버렸다. 그래서 이 변수 하나로 장부 폴더를 통째 갈아끼울
#: 수 있게 했다. `tests/conftest.py`가 임시 폴더를 넣어 준다.
LEDGER_DIR_ENV = "V2R_USAGE_LEDGER_DIR"

#: 장부에 적는 토큰 칸
TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def ledger_path(data_dir: str | Path, when: datetime | None = None) -> Path:
    """그 달의 장부 파일 경로 (`data/llm_usage-2026-09.jsonl`).

    환경변수 `V2R_USAGE_LEDGER_DIR`가 있으면 **그 폴더가 이긴다** (시험용).
    """
    stamp = (when or datetime.now()).strftime("%Y-%m")
    override = (os.environ.get(LEDGER_DIR_ENV, "") or "").strip()
    base = Path(override) if override else Path(data_dir)
    return base / f"{LEDGER_BASENAME}-{stamp}.jsonl"


def cache_hit_ratio(counts: dict[str, Any] | None) -> float:
    """읽어들인 입력 토큰 가운데 **캐시로 지나간 몫**의 비율 (0~1).

    분모는 `cache_read + cache_creation + input` 이다 — 이번에 모델이 읽은 입력
    전부. 1에 가까울수록 지침을 다시 읽지 않았다는 뜻이다.
    """
    data = counts or {}
    read = int(data.get("cache_read_input_tokens", 0) or 0)
    total = (
        read
        + int(data.get("cache_creation_input_tokens", 0) or 0)
        + int(data.get("input_tokens", 0) or 0)
    )
    return (read / total) if total else 0.0


def append_call(
    data_dir: str | Path,
    backend: str,
    purpose: str,
    model: str,
    counts: dict[str, Any] | None,
    prompt_sha256: str = "",
    when: datetime | None = None,
) -> Path | None:
    """호출 한 건을 장부에 적는다. 실패해도 절대 예외를 올리지 않는다."""
    row = {
        "at": (when or datetime.now()).astimezone().isoformat(timespec="seconds"),
        "backend": backend,
        "purpose": purpose,
        "model": model,
        "prompt_sha256": prompt_sha256,
    }
    for field in TOKEN_FIELDS:
        row[field] = int((counts or {}).get(field, 0) or 0)
    row["cache_hit_ratio"] = round(cache_hit_ratio(counts), 4)
    path = ledger_path(data_dir, when)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:  # 장부는 덤이다 — 못 적어도 원고 생성을 막지 않는다
        log.warning("사용량 장부 기록 실패: %s", exc)
        return None
    return path


def read_month(data_dir: str | Path, when: datetime | None = None) -> list[dict]:
    """그 달 장부를 줄 단위로 읽는다 (깨진 줄은 건너뛴다)."""
    path = ledger_path(data_dir, when)
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out
