"""예약 시각 계산기(KST). DESIGN §6, legacy §4 기준."""

from __future__ import annotations

import random
from datetime import date as _date
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from v2r.accounts.rules import find_affiliate, normalize_name

KST = ZoneInfo("Asia/Seoul")


class ScheduleError(RuntimeError):
    """스케줄 계산 실패."""


def _hhmm(value: str) -> time:
    """"09:00" / "0900" / "9" → time."""
    raw = str(value).strip()
    if ":" in raw:
        h, m = raw.split(":", 1)
    elif len(raw) == 4 and raw.isdigit():
        h, m = raw[:2], raw[2:]
    else:
        h, m = raw, "0"
    return time(int(h), int(m), tzinfo=None)


def window_bounds(
    date: _date, start_hhmm: str, end_hhmm: str
) -> tuple[datetime, datetime]:
    """시간창 시작/종료(KST). 종료 <= 시작이면 다음 날로 넘긴다."""
    s, e = _hhmm(start_hhmm), _hhmm(end_hhmm)
    start = datetime.combine(date, s, tzinfo=KST)
    end = datetime.combine(date, e, tzinfo=KST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def plan_slots(
    n: int,
    *,
    date: _date,
    window_start: str,
    window_end: str,
    interval_min: int = 5,
    interval_max: int = 15,
    first_offset_min: tuple[int, int] = (5, 15),
    per_cafe: list[str] | None = None,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[datetime]:
    """n개 슬롯을 계산한다. per_cafe가 있으면 카페별 독립 체인."""
    if n <= 0:
        return []
    rng = rng or random.Random()
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    if per_cafe is not None and len(per_cafe) != n:
        raise ScheduleError("per_cafe 길이가 슬롯 개수와 다릅니다")

    start, end = window_bounds(date, window_start, window_end)
    day = 0  # 카페 체인이 이월될 때 쓰는 보조 카운터

    def window_for(offset_days: int) -> tuple[datetime, datetime]:
        return start + timedelta(days=offset_days), end + timedelta(days=offset_days)

    def first_for(chain_start: datetime) -> datetime:
        base = max(now, chain_start)
        return base + timedelta(minutes=rng.randint(*first_offset_min))

    chains: dict[str, datetime] = {}
    chain_days: dict[str, int] = {}
    slots: list[datetime] = []

    for i in range(n):
        key = per_cafe[i] if per_cafe is not None else "_"
        if key not in chains:
            chain_days[key] = day
            w_start, w_end = window_for(chain_days[key])
            nxt = first_for(w_start)
            while nxt >= w_end:
                chain_days[key] += 1
                w_start, w_end = window_for(chain_days[key])
                nxt = first_for(w_start)
        else:
            _, w_end = window_for(chain_days[key])
            nxt = chains[key] + timedelta(minutes=rng.randint(interval_min, interval_max))
            if nxt >= w_end:
                chain_days[key] += 1
                w_start, w_end = window_for(chain_days[key])
                nxt = w_start
        chains[key] = nxt
        slots.append(nxt)

    return slots


def revision_at(daily_at: datetime, cafe_name: str, cafes_cfg: dict) -> datetime:
    """제휴 수정글 예약 시각 = 원본 + 카페별 지연시간."""
    entry = find_affiliate(cafe_name, cafes_cfg)
    if entry is None:
        raise ScheduleError(f"제휴 카페를 찾을 수 없습니다: {cafe_name}")
    hours = entry.get("revision_delay_hours")
    if hours is None:
        raise ScheduleError(f"수정글 지연시간 미설정: {cafe_name}")
    return daily_at + timedelta(hours=int(hours))


__all__ = [
    "KST",
    "ScheduleError",
    "window_bounds",
    "plan_slots",
    "revision_at",
    "normalize_name",
]
