"""한국어 명령 정규식 해석기 (토큰 0). DESIGN §4 표 순서대로 첫 매치."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from v2r.command.spec import KST, TaskSpec

# --- 작업 패턴 (표 순서 고정) ---
TASK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 예약/감시 명령은 `현황`·`목록`·`점검` 같은 낱말에 가로채이지 않게 맨 앞에 둔다
    ("schedule_run", re.compile(r"예약\s*(?:지금\s*)?(?:실행|강제\s*실행|돌려)")),
    ("schedule_list", re.compile(r"예약\s*(목록|리스트|현황|확인|상태|표)")),
    ("monitor_status", re.compile(r"감시\s*(상태|현황|목록|확인)")),
    # 노출 순환기(B2, 2026-09-22) — 무한 순환 시작/중지/상태.
    # "노출 현황"보다 먼저 봐야 "순환"이 일반 노출 현황 조회로 가로채이지 않는다
    ("exposure_cycle_start", re.compile(r"노출\s*순환\s*시작")),
    ("exposure_cycle_stop", re.compile(r"노출\s*순환\s*(중지|정지|멈춰)")),
    ("exposure_cycle_status", re.compile(r"노출\s*순환\s*(상태|현황)")),
    # 키워드 노출 현황 (`키워드 노출 현황` / `우아덤 노출 현황`) — `상태|현황` 조회
    # 패턴보다 앞에 둬야 "노출 현황"이 일반 status로 가로채이지 않는다
    ("keyword_exposure", re.compile(r"(키워드\s*노출|노출\s*현황)")),
    # 키워드 발굴 (`우아덤 키워드 발굴 1000개` / `키워드 발굴 현황`) — 노출 패턴 뒤,
    # 일반 status 패턴보다는 앞에 둔다
    ("keyword_discovery_status", re.compile(r"키워드\s*발굴\s*현황")),
    # 예약 "키워드 발굴 전체 500개" — 브랜드 5개를 순차로 도는 일괄 발굴.
    # 특정 브랜드용 `keyword_discovery`(예: "우아덤 키워드 발굴 1000개")보다 먼저 본다.
    ("keyword_discovery_all", re.compile(r"키워드\s*발굴\s*전체")),
    ("keyword_discovery", re.compile(r"키워드\s*발굴")),
    ("pending_report", re.compile(r"미처리\s*(알림|목록|보고|리스트)")),
    # 새 모양 보고서(2026-09-22): 어제 기준 일일 보고 / 오늘 기준 중간 보고
    ("daily_report", re.compile(r"(일일|하루|어제)\s*(보고|보고서|현황판)")),
    ("progress_report", re.compile(r"(중간|진행)\s*(보고|보고서|현황판)")),
    ("inspect_failures", re.compile(r"(실패|미완성|불확실).*(점검|재시도|모아|확인)")),
    ("reconcile", re.compile(r"(끊긴|미완료).*(이어|재개|점검)")),
    ("sync_all_sources", re.compile(r"전체\s*(원본|시트).*(동기화|갱신)")),
    ("sync_sources", re.compile(r"(원본|시트).*(동기화|갱신)")),
    # 대량 원고 생성(밀려남 키워드 대기열, 2026-09-23) — `원고`라는 낱말이 있어
    # `generate_brand`보다 먼저 봐야 한다. `현황`이 먼저다(조회가 생성으로 안 잡히게).
    ("bulk_generate_status", re.compile(r"대량\s*원고\s*현황")),
    ("bulk_generate_all", re.compile(r"대량\s*원고\s*전체")),
    ("bulk_generate", re.compile(r"대량\s*원고")),
    # 브랜드 원고 생성 (`브랜드 원고 생성 우아덤 1건` / `우아덤 원고 1개 만들어줘`).
    # `일상 글 생성`보다 앞서지만 `원고`라는 낱말이 있어야만 잡힌다.
    ("generate_brand", re.compile(r"원고.*(생성|만들어|작성)|(생성|만들어|작성).*원고")),
    # 제휴 카페 일상 글은 ChatGPT 웹 세션으로 만든다 (자사 xlsx 일상 글과 별개)
    ("generate_affiliate_daily", re.compile(r"제휴.*일상\s*글.*(생성|만들어)")),
    ("generate_daily", re.compile(r"일상\s*글.*(생성|만들어)")),
    ("collect_daily", re.compile(r"일상\s*글.*(수집|가져와)")),
    ("collect_new_photos", re.compile(r"새\s*(사진|이미지)\s*(수거|회수|가져오기|가져와)")),
    # 채널 연결 점검(2026-09-22) — 다른 "점검" 패턴보다 앞에 둬야
    # `web_keepalive` 등이 "슬랙"/"텔레그램" 낱말을 가로채지 않는다
    ("slack_check", re.compile(r"슬랙\s*(점검|연결\s*확인|확인)")),
    ("telegram_check", re.compile(r"텔레그램\s*(점검|연결\s*확인|확인)")),
    ("gpt_keepalive", re.compile(r"(gpt|지피티).*(유지|점검)", re.I)),
    ("naver_keepalive", re.compile(r"네이버.*(세션|로그인).*(유지|점검)")),
    # 요금제(Claude Code CLI) 로그인 점검. `클로드`가 든 문장을 web_keepalive가
    # 가로채지 않게 **반드시 그보다 앞**에 둔다.
    (
        "plan_keepalive",
        re.compile(r"(요금제|claude\s*code|클로드\s*코드).*(세션|로그인).*(유지|점검)", re.I),
    ),
    ("web_keepalive", re.compile(r"(웹|make|메이크|claude|클로드|플랫폼).*(세션|로그인).*(유지|점검)", re.I)),
    # 사진 승인 흐름 (사용자 규칙 2026-09-19): 생성은 반드시 승인을 받고,
    # 만든 사진도 승인/반려를 받는다. 일반 `사진 생성` 패턴보다 먼저 본다.
    ("generate_photos", re.compile(r"사진\s*생성\s*승인")),
    ("approve_photos", re.compile(r"사진\s*승인")),
    ("reject_photos", re.compile(r"사진\s*반려")),
    # GPT 웹앱으로 직접 생성 (요청서 발송인 `request_photos`보다 앞선다)
    ("generate_photos", re.compile(r"(사진|이미지).*(생성|만들어)")),
    ("request_photos", re.compile(r"(사진|이미지).*(요청|필요)")),
    ("collect_photos", re.compile(r"사진.*(수집|가져와)")),
    ("wash_photos", re.compile(r"사진.*(세탁|변형)\s*(\d+)?")),
    ("learn_guides", re.compile(r"(메이크|make|지침).*(학습|읽어|가져와)", re.I)),
    # V2R 글 목록 색인 (중복 방지) — `글 목록 동기화` / `자사 카페 글 목록 동기화`
    ("sync_article_index", re.compile(r"글\s*목록.*(동기화|색인|갱신)")),
    ("duplicate_check", re.compile(r"중복\s*(검사|점검|확인)")),
    ("cleanup_orphans", re.compile(r"(고아|찌꺼기).*(정리|삭제)")),
    # 정기 정비(이벤트 정리·VACUUM·임시 파일·브라우저 캐시) — 다른 "정리"보다 뒤에 둔다
    ("maintenance", re.compile(r"(정기\s*정비|db\s*정비|디비\s*정비|정비\s*작업)", re.I)),
    # 이미 올라간 글의 이모지 뒷정리 (`오늘 이모지 정리`, `이모지 정리 전체`).
    # `발행`이 든 문장도 가로채이지 않게 publish_* 패턴보다 앞에 둔다.
    ("cleanup_emoji", re.compile(r"이모지.*(정리|제거|삭제)")),
    # 예약 수정글의 댓글 역할 복구 (`댓글 … 다시 세팅` / `댓글 재설정` / `댓글 복구`)
    ("repair_comments", re.compile(r"댓글.*(복구|재설정|다시)")),
    ("open_login", re.compile(r"로그인\s*(창|세션|준비)")),
    ("stop", re.compile(r"(중지|멈춰|중단|취소)")),
    # 현황판(HTML)은 `현황`/`상태` 조회(status)보다 먼저 봐야 가로채이지 않는다
    ("dashboard", re.compile(r"(현황판|대시\s*보드|dashboard)", re.I)),
    # `진행`은 "진행 상황/중/률"처럼 명사형일 때만 상태 조회로 본다(발행 문장 가로채기 방지)
    ("status", re.compile(r"(상태|현황|진행\s*(?:상황|중|률))")),
    ("catalog", re.compile(r"(카페|게시판|계정)\s*(목록|카탈로그)")),
    ("publish_brand", re.compile(r"(브랜드|수정)\s*글")),
    ("publish_info", re.compile(r"정보성\s*글")),
    ("publish_batch", re.compile(r"(일괄|배치)\s*(발행|등록)")),
    ("publish_daily", re.compile(r"(일상\s*글|올려|발행|등록)")),
]

# --- 리터럴 목록 ---
CAFE_NAMES = [
    "씨씨앙",
    "양평맘",
    "쌍둥이맘 모여라",
    "쌍둥이맘",
    "고요한 아침",
    "글로시 마이",
    "웨딩 노트",
    "송도포털",
    "헬씨 트리",
    "러브 인썸",
    "마이 웨딩 드림",
    "태극",
    "소나무",
]
BRAND_NAMES = ["우아덤", "코숨핏", "뉴더미스", "장으뜸", "팥순이"]


def _loose(name: str) -> re.Pattern[str]:
    """이름 사이 공백을 허용하는 패턴 생성."""
    chars = [re.escape(c) for c in name.replace(" ", "")]
    return re.compile(r"\s*".join(chars))


CAFE_PATTERNS = [(n, _loose(n)) for n in CAFE_NAMES]
BRAND_PATTERNS = [(n, _loose(n)) for n in BRAND_NAMES]

# `2시간`의 `2시`를 시각으로 오인하지 않도록 `간`을 부정 전방탐색으로 제외
RE_CLOCK = re.compile(r"(오전|오후)?\s*(\d{1,2})\s*시(?!간)(?:\s*(\d{1,2})\s*분)?")
RE_COUNT = re.compile(r"(?:일상\s*글|글)\s*(\d+)\s*(?:개|건)")
#: `50개` / `50건` 둘 다 개수로 본다
RE_ANY_COUNT = re.compile(r"(\d+)\s*(?:개|건)")
#: `카페별` / `카페마다` — 자사 카페마다 count건
RE_PER_CAFE = re.compile(r"카페\s*(?:별|마다)")
#: `추가로` / `더` — 오늘 올린 수와 무관하게 N건을 더 올린다 (self-cafe-daily-rules §7)
RE_PER_CAFE_ADD = re.compile(r"추가로|추가\s*발행|더\s*(?:올려|발행)")
#: `댓글 랜덤` / `댓글 무작위` / `댓글 0~3개`
RE_RANDOM_COMMENTS = re.compile(r"댓글\s*(?:랜덤|무작위|\d+\s*~\s*\d+\s*개)")
#: 개수 인식 전에 지우는 댓글 개수 표현 (`댓글 0~3개`가 글 수로 잡히지 않게)
RE_COMMENT_COUNT = re.compile(r"댓글\s*\d+\s*(?:~\s*\d+\s*)?개")
#: 자사 카페 일상 글의 **카페별** 글 사이 대기(분) — self-cafe-daily-rules §4.
#: 2026-09-21 결정: 간격은 카페마다 따로 센다. 같은 카페의 다음 글만 2~5분 기다리고,
#: 다른 카페 글은 곧바로 올린다.
SELF_DAILY_INTERVAL = (2, 5)
RE_SHEETS = re.compile(r"(\d+)\s*장")
RE_ACCOUNT_COUNT = re.compile(r"(?:아이디|계정)\s*(\d+)\s*개")
RE_INTERVAL_RANGE = re.compile(r"(\d+)\s*~\s*(\d+)\s*분")
RE_INTERVAL_FIXED = re.compile(r"(\d+)\s*분\s*간격")
RE_BOARD = re.compile(r"게시판\s*(\S+)")
RE_SOURCE = re.compile(r"시트\s*(\S+)")
RE_BRAND_SLOT = re.compile(r"브랜드\s+(\S+)")
#: 원고유형 슬롯 (시트 E열). 발행 계열 명령에서만 읽는다.
RE_MANUSCRIPT_TYPE = re.compile(r"(질문형|후기형)")
#: `한번에` / `한 번에` — 본문과 댓글을 모델 호출 한 번으로 받는다 (비용 절감)
RE_COMBINED_MODE = re.compile(r"한\s*번에")
#: 모델을 부를 길 지정 (`요금제로 만들어줘` / `api로 만들어줘`).
#: 지침·품질은 어느 쪽이든 완전히 같고 돈이 나가는지 여부만 다르다.
RE_BACKEND_PLAN = re.compile(r"요금제(?:\s*길)?\s*(?:로|으로)|구독\s*(?:로|으로)")
RE_BACKEND_API = re.compile(r"\bapi(?:\s*길)?\s*(?:로|으로)\b", re.I)
RE_BACKEND_BATCH = re.compile(r"배치(?:\s*api)?(?:\s*길)?\s*(?:로|으로)", re.I)
RE_KEYWORD_SLOT = re.compile(r"키워드\s+(\S+)")
#: 사진 생성 승인 문구 (`사진 생성 승인 우아덤 키워드 2장`)
RE_PHOTO_APPROVE_GEN = re.compile(r"사진\s*생성\s*승인")
#: 승인/반려 명령의 자리값 형태 (`사진 승인 우아덤 키워드`)
RE_PHOTO_POSITIONAL = re.compile(
    r"사진\s*(?:생성\s*승인|승인|반려)\s+(\S+)(?:\s+(\S+))?"
)
#: `2장` / `3개`처럼 개수로 읽어야 할 토큰 (자리값 브랜드·키워드에서 제외)
RE_COUNT_TOKEN = re.compile(r"^\d+\s*(?:장|개|건)?$")
#: `예약 지금 실행 <이름>` 에서 예약 이름
RE_SCHEDULE_NAME = re.compile(r"예약\s*(?:지금\s*)?(?:실행|강제\s*실행|돌려(?:줘|라)?)\s*(.*)$")
#: 키워드를 안 적었을 때 쓰는 기본 폴더 이름 (`warehouse.store.KEYWORD_FOLDER`와 같다)
KEYWORD_FOLDER = "키워드"
RE_ACCOUNTS = re.compile(
    r"아이디\s+([A-Za-z0-9_,\s]+?)(?=\s*(?:로|으로|써|사용|$))"
)
RE_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
RE_KO_DATE = re.compile(r"(\d{1,2})월\s*(\d{1,2})일")
RE_REAL = re.compile(r"(실제|바로)\s*(발행|등록)")
RE_IMMEDIATE = re.compile(r"즉시|바로\s*올려")

_PARTICLES = ("에다가", "에다", "으로", "에서", "에", "로", "는", "은", "을", "를")


def _strip_particle(word: str) -> str:
    """조사 꼬리 제거."""
    for p in _PARTICLES:
        if len(word) > len(p) and word.endswith(p):
            return word[: -len(p)]
    return word


def _to_hhmm(meridiem: str | None, hour: int, minute: int | None) -> str:
    """오전/오후 표기를 24시간 HH:MM으로."""
    h = hour
    if meridiem == "오전":
        if h == 12:
            h = 0
    elif meridiem == "오후":
        if h < 12:
            h += 12
    return f"{h % 24:02d}:{(minute or 0) % 60:02d}"


#: 발행 계열 작업 (개수 표현이 붙는 작업)
PUBLISH_TASKS = frozenset(
    {"publish_brand", "publish_info", "publish_batch", "publish_daily"}
)
#: 개수가 함께 나오면 `stop`/`status`보다 우선하는 작업
_PRIORITY_TASKS = PUBLISH_TASKS | {"catalog"}
_HIJACKABLE = {"stop", "status"}
#: `브랜드 X` / `키워드 Y` 슬롯을 읽는 작업 (사진 계열)
_PHOTO_SLOT_TASKS = frozenset(
    {
        "request_photos",
        "generate_photos",
        "approve_photos",
        "reject_photos",
        "collect_new_photos",
        "collect_photos",
        "wash_photos",
    }
)
#: 시트/게시판 같은 슬롯 추출을 하지 않는 작업
_NO_SLOT_TASKS = frozenset(
    {
        "sync_sources",
        "sync_all_sources",
        "status",
        "dashboard",
        "stop",
        "reconcile",
        "inspect_failures",
        "catalog",
        "sync_article_index",
        "duplicate_check",
        "cleanup_orphans",
        "maintenance",
        "cleanup_emoji",
        "repair_comments",
        "gpt_keepalive",
        "naver_keepalive",
        "web_keepalive",
        "plan_keepalive",
        "schedule_list",
        "schedule_run",
        "monitor_status",
        "pending_report",
        "daily_report",
        "progress_report",
        "keyword_exposure",
        "keyword_discovery_status",
        "keyword_discovery_all",
    }
)


def _match_task(text: str) -> str | None:
    """표 순서대로 첫 매치. 단, 개수 표현이 있으면 발행/카탈로그를 우선한다.

    `일상 글 5개 발행 진행해`처럼 발행 의도가 분명한 문장을
    `status`/`stop` 패턴이 가로채지 않게 한다.
    """
    preferred: str | None = None
    if RE_ANY_COUNT.search(text) or "목록" in text or "카탈로그" in text:
        for task, pattern in TASK_PATTERNS:
            if task in _PRIORITY_TASKS and pattern.search(text):
                preferred = task
                break

    for task, pattern in TASK_PATTERNS:
        if pattern.search(text):
            if task in _HIJACKABLE and preferred:
                return preferred
            return task
    return None


def _parse_date(text: str, now: datetime) -> str:
    m = RE_ISO_DATE.search(text)
    if m:
        return m.group(1)
    m = RE_KO_DATE.search(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        return f"{now.year:04d}-{month:02d}-{day:02d}"
    if "모레" in text:
        return (now + timedelta(days=2)).strftime("%Y-%m-%d")
    if "내일" in text:
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


def parse_korean_command(text: str, now: datetime | None = None) -> TaskSpec | None:
    """한국어 명령 → TaskSpec. 해석 실패 시 None."""
    if not text or not text.strip():
        return None
    raw = text.strip()
    now = now or datetime.now(KST)

    # 1) JSON 명령 우선
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            # 안전 불변식: 실제 발행은 `(실제|바로)(발행|등록)` 문구가 있을 때만.
            # JSON의 `dry_run: false`는 `notes`에 그 문구가 있을 때만 존중한다.
            notes = data.get("notes")
            allowed = RE_REAL.search(notes) is not None if isinstance(notes, str) else False
            if not allowed:
                data = {**data, "dry_run": True}
            try:
                return TaskSpec.from_json(data)
            except Exception:
                return None

    task = _match_task(raw)
    if task is None:
        return None

    spec: dict = {"task": task, "notes": raw}

    # 예약 이름 (`예약 지금 실행 아침 일상 글`)
    if task == "schedule_run":
        m = RE_SCHEDULE_NAME.search(raw)
        if m:
            spec["schedule_name"] = m.group(1).strip()

    # 시간창: 시각 표기 2개 → 시작/종료
    clocks = RE_CLOCK.findall(raw)
    if len(clocks) >= 2:
        start = _to_hhmm(clocks[0][0] or None, int(clocks[0][1]), int(clocks[0][2] or 0))
        end_meridiem = clocks[1][0] or None
        end_hour, end_min = int(clocks[1][1]), int(clocks[1][2] or 0)
        end = _to_hhmm(end_meridiem, end_hour, end_min)
        # 둘째 시각에 오전/오후가 없고 그대로 두면 종료 <= 시작이 되는 경우
        # 첫 시각의 오전/오후를 상속한다. ("오후 2시부터 5시까지" → 14:00~17:00)
        if end_meridiem is None and end <= start and end_hour < 12:
            end = _to_hhmm("오후", end_hour, end_min)
        spec["window_start"] = start
        spec["window_end"] = end
    elif len(clocks) == 1:
        spec["window_start"] = _to_hhmm(
            clocks[0][0] or None, int(clocks[0][1]), int(clocks[0][2] or 0)
        )

    # 개수 (`댓글 0~3개`는 글 수가 아니므로 미리 지운다)
    counting = RE_COMMENT_COUNT.sub(" ", raw)
    m = RE_COUNT.search(counting)
    if m:
        spec["count"] = int(m.group(1))
    elif task in PUBLISH_TASKS | {
        "generate_brand",
        "bulk_generate",
        "bulk_generate_all",
        "generate_affiliate_daily",
        "generate_daily",
        "collect_daily",
        "collect_photos",
        "generate_photos",
        "keyword_exposure",
        "keyword_discovery",
        "keyword_discovery_all",
    }:
        # `글` 없이 `N개`만 있어도 개수로 인정. 단 계정 수 표현은 먼저 제거한다.
        m = RE_ANY_COUNT.search(RE_ACCOUNT_COUNT.sub(" ", counting))
        if m:
            spec["count"] = int(m.group(1))
    if task == "generate_photos" and not spec.get("count"):
        # `사진 3장 생성`처럼 `장` 단위로 세는 표현도 받는다
        m = RE_SHEETS.search(raw)
        if m:
            spec["count"] = int(m.group(1))
    if task == "wash_photos":
        m = RE_SHEETS.search(raw)
        if m:
            spec["count"] = int(m.group(1))
        elif not spec.get("count"):
            m = re.search(r"(?:세탁|변형)\s*(\d+)", raw)
            if m:
                spec["count"] = int(m.group(1))

    # 계정 수
    m_acc_count = RE_ACCOUNT_COUNT.search(raw)
    if m_acc_count:
        spec["account_count"] = int(m_acc_count.group(1))

    # 간격
    explicit_interval = False
    m = RE_INTERVAL_RANGE.search(raw)
    if m:
        spec["interval_min"], spec["interval_max"] = int(m.group(1)), int(m.group(2))
        explicit_interval = True
    else:
        m = RE_INTERVAL_FIXED.search(raw)
        if m:
            spec["interval_min"] = spec["interval_max"] = int(m.group(1))
            explicit_interval = True

    # 카페 / 브랜드
    for name, pattern in CAFE_PATTERNS:
        if pattern.search(raw):
            spec["cafe"] = name
            break
    for name, pattern in BRAND_PATTERNS:
        if pattern.search(raw):
            spec["brand"] = name
            break

    # `브랜드 X` / `키워드 Y` 슬롯 (사진 작업에서만. `브랜드 글`처럼 작업 이름이
    # 뒤에 오는 발행 문장에서는 쓰지 않는다)
    if task in _PHOTO_SLOT_TASKS:
        if "brand" not in spec:
            m = RE_BRAND_SLOT.search(raw)
            if m:
                spec["brand"] = _strip_particle(m.group(1))
        m = RE_KEYWORD_SLOT.search(raw)
        if m:
            value = _strip_particle(m.group(1))
            # `키워드 2장`처럼 뒤에 개수가 오면 폴더 이름이 아니라 개수다
            if not RE_COUNT_TOKEN.match(value):
                spec["keyword"] = value

    # 사진 승인 흐름의 자리값 형태 (`사진 승인 우아덤 키워드`)
    if task in {"approve_photos", "reject_photos"} or (
        task == "generate_photos" and RE_PHOTO_APPROVE_GEN.search(raw)
    ):
        if task == "generate_photos":
            spec["approved"] = True
        m = RE_PHOTO_POSITIONAL.search(raw)
        if m:
            first = _strip_particle(m.group(1))
            second = _strip_particle(m.group(2) or "")
            if not spec.get("brand") and first and not RE_COUNT_TOKEN.match(first):
                spec["brand"] = first
            if not spec.get("keyword") and second and not RE_COUNT_TOKEN.match(second):
                spec["keyword"] = second
        if not spec.get("keyword"):
            spec["keyword"] = KEYWORD_FOLDER

    # 원고유형 (발행 계열과 브랜드 원고 생성에서. `후기형 …` → 그 유형만)
    if task in PUBLISH_TASKS | {"generate_brand"}:
        m = RE_MANUSCRIPT_TYPE.search(raw)
        if m:
            spec["manuscript_type"] = m.group(1)

    # 생성 방식 (`한번에` → 본문+댓글을 한 번의 호출로 받는다)
    if task == "generate_brand" and RE_COMBINED_MODE.search(raw):
        spec["generate_mode"] = "combined"

    # 모델을 부를 길 (`요금제로` / `api로` / `배치로`). 적어 두면 그 길만 쓴다.
    if RE_BACKEND_PLAN.search(raw):
        spec["llm_backend"] = "plan"
    elif RE_BACKEND_BATCH.search(raw):
        spec["llm_backend"] = "batch"
    elif RE_BACKEND_API.search(raw):
        spec["llm_backend"] = "api"

    # 게시판 / 시트
    m = RE_BOARD.search(raw)
    if m:
        spec["board"] = _strip_particle(m.group(1))
    # 동기화·상태류 문장에서는 `시트 갱신`의 "갱신"이 시트 이름으로 잡히므로 건너뛴다
    if task not in _NO_SLOT_TASKS:
        m = RE_SOURCE.search(raw)
        if m:
            spec["source"] = _strip_particle(m.group(1))

    # 지정 계정 (계정 수 표현이면 무시)
    if not m_acc_count:
        m = RE_ACCOUNTS.search(raw)
        if m:
            ids = [i.strip() for i in re.split(r"[,\s]+", m.group(1)) if i.strip()]
            ids = [i for i in ids if not i.isdigit()]
            if ids:
                spec["accounts"] = ids
                spec["account_mode"] = "manual"
    if "자동" in raw and "accounts" not in spec:
        spec["account_mode"] = "auto"

    # 날짜 / 실제 발행 / 즉시
    spec["start_date"] = _parse_date(raw, now)
    spec["dry_run"] = RE_REAL.search(raw) is None
    if task == "cleanup_emoji" and "실제" in raw:
        # 이모지 정리는 새 글을 올리지 않고 이미 나간 글의 이모지만 지운다 →
        # `실제 발행`까지 요구하지 않고 `실제` 한 낱말이면 진짜 고친다.
        spec["dry_run"] = False
    spec["immediate"] = RE_IMMEDIATE.search(raw) is not None

    # 자사 카페 일상 글: `카페별 N건` / `댓글 랜덤` (self-cafe-daily-rules §1·§5)
    if task in PUBLISH_TASKS:
        spec["per_cafe"] = RE_PER_CAFE.search(raw) is not None
        if spec["per_cafe"] and RE_PER_CAFE_ADD.search(raw):
            spec["per_cafe_mode"] = "추가로"
        spec["random_comments"] = RE_RANDOM_COMMENTS.search(raw) is not None
        if spec["per_cafe"] or task == "publish_daily":
            # 규칙 §4: 자사 카페 일상 글은 (카페별이든 카페 하나든) 예약하지 않고 항상 즉시 발행하며,
            # **같은 카페**의 다음 글까지 기본 2~5분 쉰다(`N~M분 간격`을 적으면 그 값).
            spec["immediate"] = True
            if not explicit_interval:
                spec["interval_min"], spec["interval_max"] = SELF_DAILY_INTERVAL

    try:
        return TaskSpec(**spec)
    except Exception:
        return None


# --- 요약 문구 ---
TASK_LABELS: dict[str, str] = {
    "inspect_failures": "실패 글 점검",
    "reconcile": "끊긴 작업 이어가기",
    "sync_all_sources": "전체 원본 동기화",
    "sync_sources": "원본 동기화",
    "generate_brand": "브랜드 원고 생성",
    "bulk_generate": "대량 원고 생성",
    "bulk_generate_all": "대량 원고 전체 생성",
    "bulk_generate_status": "대량 원고 현황",
    "generate_affiliate_daily": "제휴 일상 글 생성(GPT)",
    "generate_daily": "일상 글 생성",
    "collect_daily": "일상 글 수집",
    "collect_photos": "사진 수집",
    "collect_new_photos": "새 사진 수거",
    "generate_photos": "사진 생성(GPT)",
    "approve_photos": "사진 승인",
    "reject_photos": "사진 반려",
    "slack_check": "슬랙 연결 점검",
    "telegram_check": "텔레그램 연결 점검",
    "gpt_keepalive": "GPT 세션 점검",
    "naver_keepalive": "네이버 세션 점검",
    "web_keepalive": "웹 세션 점검(Claude·Make)",
    "plan_keepalive": "요금제 세션 점검(Claude Code)",
    "request_photos": "사진 요청",
    "wash_photos": "사진 세탁",
    "learn_guides": "지침 학습",
    "sync_article_index": "V2R 글 목록 동기화",
    "duplicate_check": "중복 검사",
    "cleanup_orphans": "고아 글 정리",
    "maintenance": "정기 정비",
    "cleanup_emoji": "이모지 정리",
    "repair_comments": "댓글 복구",
    "open_login": "로그인 창 열기",
    "stop": "작업 중지",
    "schedule_list": "예약 목록",
    "schedule_run": "예약 지금 실행",
    "monitor_status": "감시 상태",
    "keyword_exposure": "키워드 노출 현황",
    "exposure_cycle_start": "노출 순환 시작",
    "exposure_cycle_stop": "노출 순환 중지",
    "exposure_cycle_status": "노출 순환 상태",
    "pending_report": "미처리 목록 보내기",
    "daily_report": "일일 보고(어제 기준)",
    "progress_report": "중간 보고(오늘 기준)",
    "status": "상태 조회",
    "dashboard": "현황판 갱신",
    "catalog": "카탈로그 조회",
    "publish_brand": "브랜드(수정) 글 발행",
    "publish_info": "정보성 글 발행",
    "publish_batch": "일괄 발행",
    "publish_daily": "일상 글 발행",
}

_PUBLISH_TASKS = PUBLISH_TASKS


def describe_spec(spec: TaskSpec) -> str:
    """명세 한 줄 한국어 요약."""
    parts: list[str] = [TASK_LABELS.get(spec.task, spec.task)]
    if getattr(spec, "per_cafe", False):
        parts.append("카페별")
    if spec.cafe:
        parts.append(f"카페 {spec.cafe}")
    if spec.brand:
        parts.append(f"브랜드 {spec.brand}")
    if getattr(spec, "schedule_name", ""):
        parts.append(f"예약 {spec.schedule_name}")
    if getattr(spec, "keyword", ""):
        parts.append(f"키워드 {spec.keyword}")
    if spec.board:
        parts.append(f"게시판 {spec.board}")
    if spec.source:
        parts.append(f"시트 {spec.source}")
    if spec.count:
        unit = "장" if spec.task == "wash_photos" else "개"
        parts.append(f"{spec.count}{unit}")
    if spec.task in _PUBLISH_TASKS:
        parts.append(f"{spec.start_date} {spec.window_start}~{spec.window_end}")
        if spec.interval_min == spec.interval_max:
            parts.append(f"{spec.interval_min}분 간격")
        else:
            parts.append(f"{spec.interval_min}~{spec.interval_max}분 간격")
        if spec.account_mode == "manual":
            parts.append("지정 계정 " + ",".join(spec.accounts))
        else:
            parts.append(
                f"자동 계정 {spec.account_count}개" if spec.account_count else "자동 계정"
            )
    if getattr(spec, "random_comments", False):
        parts.append("댓글 랜덤")
    if getattr(spec, "llm_backend", ""):
        parts.append({"plan": "요금제 길", "api": "API 길", "batch": "배치 길"}[spec.llm_backend])
    if getattr(spec, "approved", False):
        parts.append("승인됨")
    if spec.immediate:
        parts.append("즉시")
    parts.append("모의 실행" if spec.dry_run else "실제 발행")
    return ", ".join(parts)
