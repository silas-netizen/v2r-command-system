"""스케줄러 테스트 (KST)."""

import random
from datetime import date, datetime, timedelta

import pytest

from v2r.engine.scheduler import (
    KST,
    ScheduleError,
    plan_slots,
    revision_at,
    window_bounds,
)

CAFES = {
    "affiliate": [
        {"name": "씨씨앙", "revision_delay_hours": 4, "aliases": []},
        {"name": "양평맘", "revision_delay_hours": 20, "aliases": []},
        {"name": "쌍둥이맘 모여라", "revision_delay_hours": 22, "aliases": ["쌍둥이맘"]},
    ]
}


def test_window_bounds_자정넘김():
    s, e = window_bounds(date(2026, 9, 19), "09:00", "18:00")
    assert (s.hour, e.hour) == (9, 18) and s.tzinfo == KST
    s2, e2 = window_bounds(date(2026, 9, 19), "22:00", "02:00")
    assert e2 - s2 == timedelta(hours=4) and e2.day == 20


def test_plan_slots_순차증가_창내():
    rng = random.Random(7)
    now = datetime(2026, 9, 19, 8, 0, tzinfo=KST)
    slots = plan_slots(
        5, date=date(2026, 9, 19), window_start="09:00", window_end="18:00",
        interval_min=5, interval_max=15, rng=rng, now=now,
    )
    assert len(slots) == 5
    assert slots == sorted(slots)
    start, end = window_bounds(date(2026, 9, 19), "09:00", "18:00")
    assert all(start <= s < end for s in slots)
    for a, b in zip(slots, slots[1:]):
        assert timedelta(minutes=5) <= b - a <= timedelta(minutes=15)


def test_plan_slots_now가_창보다_늦으면_now기준():
    now = datetime(2026, 9, 19, 12, 0, tzinfo=KST)
    slots = plan_slots(
        1, date=date(2026, 9, 19), window_start="09:00", window_end="18:00",
        rng=random.Random(1), now=now,
    )
    assert slots[0] >= now + timedelta(minutes=5)


def test_plan_slots_창밖이면_다음날_창시작():
    now = datetime(2026, 9, 19, 8, 0, tzinfo=KST)
    slots = plan_slots(
        4, date=date(2026, 9, 19), window_start="09:00", window_end="09:20",
        interval_min=10, interval_max=10, rng=random.Random(3), now=now,
    )
    rolled = [x for x in slots if x.day == 20]
    assert rolled, "창을 넘으면 다음 날로 이월되어야 한다"
    assert (rolled[0].hour, rolled[0].minute) == (9, 0)  # 다음 날 창 시작
    _, end = window_bounds(date(2026, 9, 19), "09:00", "09:20")
    assert all(x < end or x.day == 20 for x in slots)


def test_plan_slots_카페별_독립체인():
    now = datetime(2026, 9, 19, 8, 0, tzinfo=KST)
    per_cafe = ["씨씨앙", "양평맘", "씨씨앙", "양평맘"]
    slots = plan_slots(
        4, date=date(2026, 9, 19), window_start="09:00", window_end="18:00",
        interval_min=5, interval_max=15, per_cafe=per_cafe,
        rng=random.Random(11), now=now,
    )
    ccang = [slots[0], slots[2]]
    ypm = [slots[1], slots[3]]
    assert timedelta(minutes=5) <= ccang[1] - ccang[0] <= timedelta(minutes=15)
    assert timedelta(minutes=5) <= ypm[1] - ypm[0] <= timedelta(minutes=15)


def test_plan_slots_per_cafe_길이불일치_에러():
    with pytest.raises(ScheduleError):
        plan_slots(3, date=date(2026, 9, 19), window_start="09:00",
                   window_end="18:00", per_cafe=["a"], rng=random.Random(0),
                   now=datetime(2026, 9, 19, 8, tzinfo=KST))


def test_plan_slots_0개():
    assert plan_slots(0, date=date(2026, 9, 19), window_start="09:00",
                      window_end="18:00") == []


def test_revision_at():
    base = datetime(2026, 9, 19, 10, 0, tzinfo=KST)
    assert revision_at(base, "씨씨앙", CAFES).hour == 14
    assert revision_at(base, "양평맘", CAFES) == base + timedelta(hours=20)
    assert revision_at(base, "쌍둥이맘", CAFES) == base + timedelta(hours=22)
    with pytest.raises(ScheduleError):
        revision_at(base, "없는카페", CAFES)
