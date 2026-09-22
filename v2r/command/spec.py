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
        "sync_article_index",
        "duplicate_check",
        "generate_brand",
        "bulk_generate",
        "bulk_generate_all",
        "bulk_generate_status",
        "vpc_export",
        "generate_affiliate_daily",
        "generate_daily",
        "collect_daily",
        "collect_photos",
        "collect_new_photos",
        "generate_photos",
        "approve_photos",
        "reject_photos",
        "gpt_keepalive",
        "naver_keepalive",
        "web_keepalive",
        "plan_keepalive",
        "slack_check",
        "telegram_check",
        "request_photos",
        "wash_photos",
        "learn_guides",
        "cleanup_orphans",
        "maintenance",
        "cleanup_emoji",
        "repair_comments",
        "open_login",
        "stop",
        "schedule_list",
        "schedule_run",
        "monitor_status",
        "keyword_exposure",
        "exposure_cycle_start",
        "exposure_cycle_stop",
        "exposure_cycle_status",
        "keyword_discovery",
        "keyword_discovery_all",
        "keyword_discovery_status",
        "keyword_relevance_rescan",
        "keyword_relevance_status",
        "sheet_sync_keywords",
        "pending_report",
        "daily_report",
        "progress_report",
        "dashboard",
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
    #: 원고유형 필터(시트 E열). `질문형` / `후기형` / 빈 값(전부)
    manuscript_type: str = ""
    #: 브랜드 원고 생성 방식. 빈 값 = 기본(`brand_writer.DEFAULT_MODE`),
    #: `combined` = 본문과 댓글 12개를 모델 호출 한 번으로 받는다 (`한번에`)
    generate_mode: str = ""
    #: 모델을 부를 길. 빈 값 = 설정(`config/models.yaml`)의 차례대로.
    #: `plan` = 요금제(`요금제로`), `api` = 일반 API(`api로`), `batch` = 배치(미구현)
    llm_backend: str = ""
    #: 대량 원고 구분자. `v2r`(우리 실행기 발행분) / `vpc`(가상 PC 처리분).
    #: 빈 값 = 자동 배정(`brand_queue.refill`이 하루 상한까지는 v2r, 이후 vpc).
    target: str = ""
    keyword: str = ""
    source: str = ""
    dry_run: bool = True
    #: `사진 생성 승인 …` — 사용자가 사진 생성을 명시적으로 승인했는가
    approved: bool = False
    immediate: bool = False
    #: `카페별 N건` / `카페마다 N건` — 자사 카페 전부에 각각 count건 (self-cafe-daily-rules §1)
    per_cafe: bool = False
    #: `카페별 N건`의 뜻. 빈 값 = 오늘 그 카페의 일상 글이 N건이 되게 모자란 만큼만,
    #: `추가로` = 오늘 몇 건을 올렸든 지금 N건을 더 올린다 (self-cafe-daily-rules §7)
    per_cafe_mode: str = ""
    #: `댓글 랜덤` / `댓글 0~3개` — 글마다 0~3개 랜덤 댓글 (self-cafe-daily-rules §5)
    random_comments: bool = False
    #: `예약 지금 실행 <이름>`의 예약 이름
    schedule_name: str = ""
    notes: str = ""
    manuscripts: list[dict] = Field(default_factory=list)

    @field_validator("task")
    @classmethod
    def _check_task(cls, v: str) -> str:
        if v not in ALLOWED_TASKS:
            raise ValueError(f"허용되지 않은 작업: {v}")
        return v

    @field_validator("llm_backend")
    @classmethod
    def _check_backend(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v and v not in {"plan", "batch", "api"}:
            raise ValueError(f"허용되지 않은 길: {v}")
        return v

    @field_validator("target")
    @classmethod
    def _check_target(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v and v not in {"v2r", "vpc"}:
            raise ValueError(f"허용되지 않은 구분자: {v}")
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
