"""규칙 해석이 실패한 문장만 모델에 맡긴다 (DESIGN §1 A2)."""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# "실제 발행", "바로 등록"처럼 명시했을 때만 실제 실행
REAL_RUN_RE = re.compile(r"(실제|바로)\s*(발행|등록)")


def interpret_with_llm(text: str, router: Any) -> Any:
    """모호한 한국어 문장을 TaskSpec으로 바꾼다. 실패하면 ValueError."""
    from ..llm.prompts import AMBIGUOUS_COMMAND_SYSTEM
    from .spec import ALLOWED_TASKS, TaskSpec

    if router is None or not getattr(router, "enabled", False):
        raise ValueError("명령을 해석하지 못했습니다")

    tasks = ", ".join(sorted(ALLOWED_TASKS))
    system = AMBIGUOUS_COMMAND_SYSTEM.format(tasks=tasks)
    try:
        data = router.complete_json("ambiguous_command", system, text)
    except Exception as exc:
        log.warning("모델 명령 해석 실패: %s", exc)
        raise ValueError("명령을 해석하지 못했습니다") from exc

    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else None
    if not isinstance(data, dict):
        raise ValueError("명령을 해석하지 못했습니다")

    task = str(data.get("task", "")).strip()
    if task not in ALLOWED_TASKS:
        raise ValueError("명령을 해석하지 못했습니다")

    # 원문에 명시가 없으면 무조건 드라이런
    data["dry_run"] = not REAL_RUN_RE.search(text or "")
    if not data.get("notes"):
        data["notes"] = text or ""
    try:
        return TaskSpec.model_validate(data)
    except Exception as exc:
        log.warning("모델 응답이 TaskSpec 형식이 아님: %s", exc)
        raise ValueError("명령을 해석하지 못했습니다") from exc
