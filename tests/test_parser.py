"""파서 테스트."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from v2r.command.parser import describe_spec, parse_korean_command
from v2r.command.spec import ALLOWED_TASKS, TaskSpec

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def test_legacy_example_sentence():
    text = (
        "내일 오전 9시부터 오후 6시까지만 일상 글 20개 올려줘. "
        "아이디 5개 자동으로 쓰고 10분 간격으로 해줘."
    )
    spec = parse_korean_command(text, now=NOW)
    assert spec is not None
    assert spec.task == "publish_daily"
    assert spec.count == 20
    assert spec.account_count == 5
    assert spec.account_mode == "auto"
    assert (spec.window_start, spec.window_end) == ("09:00", "18:00")
    assert (spec.interval_min, spec.interval_max) == (10, 10)
    assert spec.start_date == "2026-09-20"
    assert spec.dry_run is True
    assert spec.notes == text
    assert "일상 글 발행" in describe_spec(spec)


@pytest.mark.parametrize(
    "text,task",
    [
        ("최근 7일 실패 글 점검해줘", "inspect_failures"),
        ("끊긴 작업 이어가", "reconcile"),
        ("전체 원본 지금 동기화", "sync_all_sources"),
        ("시트 갱신해줘", "sync_sources"),
        ("일상 글 수집해줘", "collect_daily"),
        ("사진 가져와", "collect_photos"),
        ("사진 세탁 30장", "wash_photos"),
        ("메이크 지침 학습해줘", "learn_guides"),
        ("로그인 창 열어줘", "open_login"),
        ("작업 중지", "stop"),
        ("상태 알려줘", "status"),
        ("카페 목록 보여줘", "catalog"),
        ("브랜드 글 올려줘", "publish_brand"),
        ("정보성 글 발행", "publish_info"),
        ("일괄 발행 해줘", "publish_batch"),
        ("일상 글 5개 올려줘", "publish_daily"),
    ],
)
def test_each_task_pattern(text, task):
    spec = parse_korean_command(text, now=NOW)
    assert spec is not None, text
    assert spec.task == task
    assert task in ALLOWED_TASKS
    assert describe_spec(spec)


def test_all_tasks_covered_by_tests():
    assert len(ALLOWED_TASKS) == 16


def test_wash_photos_count():
    spec = parse_korean_command("사진 세탁 30장", now=NOW)
    assert spec.count == 30


def test_real_publish_turns_off_dry_run():
    spec = parse_korean_command("일상 글 3개 실제 발행", now=NOW)
    assert spec.dry_run is False


def test_explicit_accounts_manual_mode():
    spec = parse_korean_command("아이디 abc01, abc02 로 일상 글 2개 올려줘", now=NOW)
    assert spec.account_mode == "manual"
    assert spec.accounts == ["abc01", "abc02"]


def test_slots_cafe_brand_board_source_interval_range():
    spec = parse_korean_command(
        "씨씨앙 카페에 우아덤 브랜드 글 올려줘. 게시판 자유수다방 시트 각색전체 5~15분",
        now=NOW,
    )
    assert spec.task == "publish_brand"
    assert spec.cafe == "씨씨앙"
    assert spec.brand == "우아덤"
    assert spec.board == "자유수다방"
    assert spec.source == "각색전체"
    assert (spec.interval_min, spec.interval_max) == (5, 15)


def test_cafe_name_with_spaces_and_date_words():
    spec = parse_korean_command("모레 쌍둥이맘 모여라 에 일상 글 1개 올려줘", now=NOW)
    assert spec.cafe == "쌍둥이맘 모여라"
    assert spec.start_date == "2026-09-21"


def test_korean_date_and_immediate():
    spec = parse_korean_command("10월 3일 일상 글 1개 즉시 올려줘", now=NOW)
    assert spec.start_date == "2026-10-03"
    assert spec.immediate is True


def test_iso_date():
    spec = parse_korean_command("2026-12-01 일상 글 1개 올려줘", now=NOW)
    assert spec.start_date == "2026-12-01"


def test_midnight_meridiem_conversion():
    spec = parse_korean_command("오전 12시부터 오후 1시 30분까지 일상 글 1개 올려줘", now=NOW)
    assert (spec.window_start, spec.window_end) == ("00:00", "13:30")


def test_json_input():
    payload = '{"task": "publish_daily", "count": 3, "dry_run": false}'
    spec = parse_korean_command(payload, now=NOW)
    assert spec is not None
    assert spec.task == "publish_daily"
    assert spec.count == 3
    assert spec.dry_run is False


def test_unparsable_returns_none():
    assert parse_korean_command("오늘 날씨 어때", now=NOW) is None
    assert parse_korean_command("", now=NOW) is None


def test_spec_roundtrip_and_validation():
    spec = TaskSpec(task="status")
    assert TaskSpec.from_json(spec.to_json()) == spec
    with pytest.raises(ValueError):
        TaskSpec(task="없는작업")
    with pytest.raises(ValueError):
        TaskSpec(task="status", count=-1)
    with pytest.raises(ValueError):
        TaskSpec(task="status", interval_min=20, interval_max=5)
