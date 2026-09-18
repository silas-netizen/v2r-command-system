"""작업 명세(TaskSpec)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

KST = ZoneInfo("Asia/Seoul")

# DESIGN §4 표의 작업 목록 (순서대로 첫 매치)
ALLOWED_TASKS: frozenset[str] = frozenset(
    {
        "inspect_failures",
        "reconcile",
        "sync_all_sources",
        "sync_sources",
        "generate_daily",
        "collect_daily",
        "collect_photos",
        "wash_photos",
        "learn_guides",
        "open_login",
        "stop",
        "status",
        "catalog",
        "publish_brand",
        "publish_info",
        "publish_batch",
        "publish_daily",
    }
)


def today_kst() -> str:
    """오늘 날짜(KST, YYYY-MM-DD)."""
    return datetime.now(KST).strftime("%Y-%m-%d")


class TaskSpec(BaseModel):
    """명령 한 줄을 구조화한 결과."""

    task: str
    count: int = 0
    account_mode: Literal["auto", "manual"] = "auto"
    account_count: int = 0
    accounts: list[str] = Field(default_factory=list)
    window_start: str = "09:00"
    window_end: str = "18:00"
    interval_min: int = 5
    interval_max: int = 15
    start_date: str = Field(default_factory=today_kst)
    cafe: str = ""
    board: str = ""
    brand: str = ""
    source: str = ""
    dry_run: bool = True
    immediate: bool = False
    notes: str = ""
    manuscripts: list[dict] = Field(default_factory=list)

    @field_validator("task")
    @classmethod
    def _check_task(cls, v: str) -> str:
        if v not in ALLOWED_TASKS:
            raise ValueError(f"허용되지 않은 작업: {v}")
        return v

    @field_validator("count", "account_count", "interval_min", "interval_max")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("음수는 허용되지 않습니다")
        return v

    @field_validator("start_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        datetime.strptime(v, "%Y-%m-%d")
        return v

    @model_validator(mode="after")
    def _check_interval(self) -> "TaskSpec":
        if self.interval_min > self.interval_max:
            raise ValueError("interval_min은 interval_max보다 클 수 없습니다")
        return self

    def to_json(self) -> str:
        """JSON 문자열로 직렬화."""
        return json.dumps(self.model_dump(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | dict[str, Any]) -> "TaskSpec":
        """JSON 문자열/딕셔너리에서 복원."""
        data = json.loads(raw) if isinstance(raw, str) else raw
        return cls.model_validate(data)
