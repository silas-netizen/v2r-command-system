"""레이트 제한이면 남은 슬롯을 태우지 않고 기다렸다 이어간다 (장애 2026-09-19).

예전에는 로그인 제한 429가 나면 슬롯마다 한 번씩 실패로 찍혀 75건이 한꺼번에
실패 이벤트가 됐다. 이제는 한 번만 알리고, 제한이 풀릴 때까지 리스를 연장하며
쉰 뒤 **같은 슬롯**부터 다시 시도한다.
"""

from __future__ import annotations

import pytest

from v2r.api.errors import V2RApiError
from v2r.engine import publish as publish_mod
from v2r.engine import worker

from tests.test_engine import _one_slot, make_runtime, make_spec


# --------------------------------------------------------------------
# 대기 루프
# --------------------------------------------------------------------
def test_wait_sleeps_in_beats_and_extends_lease(tmp_path):
    rt = make_runtime(tmp_path)
    slept: list[float] = []
    beats: list[int] = []
    ok = worker._wait_for_rate_limit(
        rt, 95.0, lambda: beats.append(1), sleep=slept.append
    )
    assert ok is True
    assert slept == [30.0, 30.0, 30.0, 5.0]  # 30초 박자
    assert len(beats) == 4  # 박자마다 리스 연장
    rt.close()


def test_wait_is_capped_at_six_hours(tmp_path):
    rt = make_runtime(tmp_path)
    slept: list[float] = []
    worker._wait_for_rate_limit(rt, 99_999.0, lambda: None, sleep=slept.append)
    assert sum(slept) == worker.RATE_WAIT_CAP_S
    rt.close()


def test_wait_stops_on_stop_request(tmp_path):
    rt = make_runtime(tmp_path)
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        worker.request_stop(rt)  # 대기 중 중지 요청

    assert worker._wait_for_rate_limit(rt, 600.0, lambda: None, sleep=sleep) is False
    assert len(slept) == 1  # 더 자지 않는다
    rt.close()


@pytest.mark.parametrize(
    "retry_after,expected",
    [
        (43459, 21600.0),  # 6시간 상한
        (120, 120.0),
        (None, worker.RATE_WAIT_DEFAULT_S),
        ("나쁜값", worker.RATE_WAIT_DEFAULT_S),
    ],
)
def test_rate_wait_seconds(retry_after, expected):
    err = publish_mod.PublishError("x", kind="rate_limited_long", retry_after=retry_after)
    assert worker.rate_wait_seconds(err) == expected


# --------------------------------------------------------------------
# 슬롯 재시도
# --------------------------------------------------------------------
def test_레이트_제한이면_같은_슬롯을_기다렸다_다시_한다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    notices: list[str] = []
    rt._channels = []
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg: notices.append(msg))
    waits: list[float] = []
    monkeypatch.setattr(
        worker,
        "_wait_for_rate_limit",
        lambda rt_, seconds, beat, **k: (waits.append(seconds), True)[1],
    )

    spec = make_spec(dry_run=True)
    calls: list[str] = []

    def fake_slot(rt_, spec_, slot, **kwargs):
        calls.append(slot.manuscript.title)
        # 첫 슬롯의 첫 시도만 제한에 걸린다
        if calls.count(slot.manuscript.title) == 1 and slot.manuscript.title.endswith("0"):
            raise publish_mod.PublishError(
                "발행 보류(rate_limited_long)", kind="rate_limited_long", retry_after=1800
            )
        return {"status": "planned", "title": slot.manuscript.title}

    monkeypatch.setattr(publish_mod, "run_slot", fake_slot)
    out = worker._run_publish(rt, None, spec)

    # 같은 슬롯을 다시 하고, 나머지 두 슬롯도 정상 진행 → 실패 0건
    assert calls == ["테스트 제목 0", "테스트 제목 0", "테스트 제목 1", "테스트 제목 2"]
    assert out["ok"] is True and out["failures"] == []
    assert len(out["results"]) == 3
    assert waits == [1800.0]
    assert sum("로그인 제한" in n for n in notices) == 1  # 알림은 한 번만
    rt.close()


def test_대기중_중지요청이면_남은_슬롯을_건너뛴다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt._channels = []
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg: None)
    monkeypatch.setattr(worker, "_wait_for_rate_limit", lambda *a, **k: False)

    spec = make_spec(dry_run=True)
    calls: list[str] = []

    def fake_slot(rt_, spec_, slot, **kwargs):
        calls.append(slot.manuscript.title)
        raise publish_mod.PublishError("보류", kind="rate_limited", retry_after=60)

    monkeypatch.setattr(publish_mod, "run_slot", fake_slot)
    out = worker._run_publish(rt, None, spec)

    assert calls == ["테스트 제목 0"]  # 남은 슬롯을 줄줄이 실패시키지 않는다
    assert out.get("stopped") is True
    rt.close()


def test_계속_제한이면_정해진_횟수만_기다리고_실패로_남긴다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt._channels = []
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg: None)
    monkeypatch.setattr(worker, "_wait_for_rate_limit", lambda *a, **k: True)

    spec = make_spec(dry_run=True)
    calls: list[str] = []

    def fake_slot(rt_, spec_, slot, **kwargs):
        calls.append(slot.manuscript.title)
        raise publish_mod.PublishError("보류", kind="login_budget")

    monkeypatch.setattr(publish_mod, "run_slot", fake_slot)
    out = worker._run_publish(rt, None, spec)

    # 슬롯당 (첫 시도 + 대기 재시도 3회) = 4번, 슬롯 3개
    assert len(calls) == 4 * 3
    assert len(out["failures"]) == 3
    rt.close()


# --------------------------------------------------------------------
# 원고 소모 방지
# --------------------------------------------------------------------
def test_레이트_제한은_원고를_소모하지_않는다(tmp_path, monkeypatch):
    """429면 서버가 글을 만들지 않았다 → 다음 시도에서 건너뛰면 안 된다."""
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, count=1)
    slot = _one_slot(rt, spec)

    def limited(client, **kwargs):
        raise V2RApiError(
            "로그인 제한",
            status=429,
            code="160",
            reason="RATE_LIMIT_LOGIN",
            extra={"retry_after": 43459},
            kind="rate_limited_long",
            retry_after=43459,
        )

    monkeypatch.setattr(publish_mod.api_articles, "create_article", limited)

    with pytest.raises(publish_mod.PublishError) as exc:
        publish_mod.run_slot(rt, spec, slot)
    assert exc.value.kind == "rate_limited_long"
    assert exc.value.retry_after == 43459

    m = slot.manuscript
    # uncertain(건너뜀 대상)으로 남으면 안 되고, 다시 뽑을 수 있어야 한다
    assert rt.publications.list_uncertain() == []
    assert rt.publications.exists(m.source, m.source_row, m.content_hash) is False
    rt.close()
