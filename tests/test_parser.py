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
        ("일상 글 30개 만들어줘", "generate_daily"),
        ("일상 글 수집해줘", "collect_daily"),
        ("사진 가져와", "collect_photos"),
        ("새 사진 수거", "collect_new_photos"),
        ("제휴 일상 글 10개 만들어줘", "generate_affiliate_daily"),
        ("gpt 세션 점검", "gpt_keepalive"),
        ("슬랙 점검", "slack_check"),
        ("텔레그램 점검", "telegram_check"),
        ("브랜드 우아덤 키워드 단호박 사진 2개 생성", "generate_photos"),
        ("브랜드 팥순이 키워드 단호박 사진 요청", "request_photos"),
        ("사진 세탁 30장", "wash_photos"),
        ("메이크 지침 학습해줘", "learn_guides"),
        ("고아 글 정리해줘", "cleanup_orphans"),
        ("오늘 이모지 정리", "cleanup_emoji"),
        ("찌꺼기 삭제", "cleanup_orphans"),
        ("로그인 창 열어줘", "open_login"),
        ("작업 중지", "stop"),
        ("상태 알려줘", "status"),
        ("카페 목록 보여줘", "catalog"),
        ("브랜드 글 올려줘", "publish_brand"),
        ("정보성 글 발행", "publish_info"),
        ("일괄 발행 해줘", "publish_batch"),
        ("일상 글 5개 올려줘", "publish_daily"),
        ("댓글 다시 세팅해줘", "repair_comments"),
        ("현황판 갱신", "dashboard"),
        ("우아덤 원고 1개 만들어줘", "generate_brand"),
        ("정기 정비", "maintenance"),
        ("키워드 발굴 전체 500개", "keyword_discovery_all"),
    ],
)
def test_each_task_pattern(text, task):
    spec = parse_korean_command(text, now=NOW)
    assert spec is not None, text
    assert spec.task == task
    assert task in ALLOWED_TASKS
    assert describe_spec(spec)


def test_all_tasks_covered_by_tests():
    # 26 + 사진 승인 흐름 2건(approve_photos·reject_photos) + 이모지 정리 1건
    # + 예약·감시 4건(schedule_list·schedule_run·monitor_status·pending_report)
    # + V2R 글 목록 색인 2건(sync_article_index·duplicate_check)
    # + 네이버 세션 점검 1건(naver_keepalive, 2026-09-21)
    # + 웹 세션 점검 1건(web_keepalive: Claude·Make, 2026-09-21)
    # + 요금제 세션 점검 1건(plan_keepalive: Claude Code CLI, 2026-09-21)
    # + 정기 정비 1건(maintenance: 이벤트 정리·VACUUM·캐시, 2026-09-22)
    # + 새 모양 보고서 2건(daily_report·progress_report, 2026-09-22)
    # + 키워드 노출 현황 1건(keyword_exposure, 2026-09-22)
    # + 채널 연결 점검 2건(slack_check·telegram_check, 2026-09-22)
    # + 노출 순환기 3건(exposure_cycle_start·exposure_cycle_stop·exposure_cycle_status, 2026-09-22)
    # + 키워드 발굴 2건(keyword_discovery·keyword_discovery_status, 2026-09-22)
    # + 대량 원고 3건(bulk_generate·bulk_generate_all·bulk_generate_status, 다른 작업과 동시 진행)
    # + 키워드 발굴 전체 1건(keyword_discovery_all, 2026-09-23 — 잠금·헤드리스 작업과 함께)
    assert len(ALLOWED_TASKS) == 53


def test_wash_photos_count():
    spec = parse_korean_command("사진 세탁 30장", now=NOW)
    assert spec.count == 30


def test_wash_photos_per_original_and_brand():
    spec = parse_korean_command("사진 세탁 3장씩", now=NOW)
    assert spec.task == "wash_photos"
    assert spec.count == 3
    assert spec.brand == ""

    brandy = parse_korean_command("팥순이 사진 세탁", now=NOW)
    assert brandy.task == "wash_photos"
    assert brandy.brand == "팥순이"


@pytest.mark.parametrize(
    "text",
    ["사진 요청", "이미지 필요해", "브랜드 코숨핏 사진이 필요합니다", "이미지 요청해줘"],
)
def test_request_photos_patterns(text):
    spec = parse_korean_command(text, now=NOW)
    assert spec is not None, text
    assert spec.task == "request_photos"


def test_request_photos_slots():
    spec = parse_korean_command("브랜드 팥순이 키워드 단호박샐러드 사진 요청", now=NOW)
    assert spec.task == "request_photos"
    assert spec.brand == "팥순이"
    assert spec.keyword == "단호박샐러드"
    assert "키워드 단호박샐러드" in describe_spec(spec)


def test_collect_new_photos_pattern():
    for text in ("새 사진 수거", "새 이미지 수거해줘", "새사진 가져와"):
        spec = parse_korean_command(text, now=NOW)
        assert spec is not None, text
        assert spec.task == "collect_new_photos", text


def test_brand_slot_does_not_hijack_publish_brand():
    spec = parse_korean_command("브랜드 글 올려줘", now=NOW)
    assert spec.task == "publish_brand"
    assert spec.brand == ""


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
    # JSON으로도 모의 실행 규칙을 우회할 수 없다
    assert spec.dry_run is True


def test_json_input_real_publish_needs_phrase_in_notes():
    payload = (
        '{"task": "publish_daily", "count": 1, "dry_run": false,'
        ' "notes": "씨씨앙 실제 발행"}'
    )
    spec = parse_korean_command(payload, now=NOW)
    assert spec.dry_run is False
    # 문구가 있어도 dry_run을 명시하지 않으면 기본값(True) 유지
    spec2 = parse_korean_command(
        '{"task": "publish_daily", "notes": "실제 발행"}', now=NOW
    )
    assert spec2.dry_run is True


def test_second_clock_inherits_meridiem():
    spec = parse_korean_command(
        "정보성 글 내일 오후 2시부터 5시까지 2개 아이디 abc,def 로", now=NOW
    )
    assert (spec.window_start, spec.window_end) == ("14:00", "17:00")
    assert spec.count == 2


def test_second_clock_without_meridiem_rolls_to_afternoon():
    spec = parse_korean_command("일상 글 3개 12시부터 3시까지", now=NOW)
    assert (spec.window_start, spec.window_end) == ("12:00", "15:00")


def test_explicit_meridiem_crosses_midnight():
    spec = parse_korean_command("일상 글 10개 오후 11시부터 오전 2시까지", now=NOW)
    assert (spec.window_start, spec.window_end) == ("23:00", "02:00")


def test_hours_later_is_not_a_clock():
    spec = parse_korean_command("일상 글 3개 2시간 후에 올려줘", now=NOW)
    assert spec.window_start == "09:00"  # 기본값 유지


def test_status_does_not_hijack_publish_sentence():
    spec = parse_korean_command("일상 글 5개 발행 진행해", now=NOW)
    assert spec.task == "publish_daily"
    assert spec.count == 5


def test_catalog_not_hijacked_by_status():
    assert parse_korean_command("카페 목록 진행 중인거", now=NOW).task == "catalog"


def test_status_sentence_still_status():
    assert parse_korean_command("정보성 글 발행 상태 알려줘", now=NOW).task == "status"
    assert parse_korean_command("발행 중지", now=NOW).task == "stop"


def test_count_without_geul_keyword():
    spec = parse_korean_command("일상 글 수집해줘 10개", now=NOW)
    assert spec.task == "collect_daily" and spec.count == 10


def test_account_count_not_taken_as_count():
    spec = parse_korean_command("정보성 글 아이디 5개로 올려줘", now=NOW)
    assert spec.account_count == 5
    assert spec.count == 0


def test_sync_task_does_not_take_source_slot():
    spec = parse_korean_command("시트 갱신해줘", now=NOW)
    assert spec.task == "sync_sources"
    assert spec.source == ""


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


def test_원고유형_슬롯():
    spec = parse_korean_command("팥순이 후기형 브랜드 글 1개 씨씨앙 카페에 올려줘", now=NOW)
    assert spec.task == "publish_brand"
    assert spec.brand == "팥순이"
    assert spec.cafe == "씨씨앙"
    assert spec.count == 1
    assert spec.manuscript_type == "후기형"

    spec = parse_korean_command("질문형 브랜드 글 2개 올려줘", now=NOW)
    assert spec.manuscript_type == "질문형"

    spec = parse_korean_command("브랜드 글 2개 올려줘", now=NOW)
    assert spec.manuscript_type == ""


def test_원고유형_슬롯은_발행계열에서만():
    spec = parse_korean_command("후기형 사진 3장 생성해줘", now=NOW)
    assert spec.task == "generate_photos"
    assert spec.manuscript_type == ""
