"""한국어 명령 정규식 해석기 (토큰 0). DESIGN §4 표 순서대로 첫 매치."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from v2r.command.spec import KST, TaskSpec

# --- 작업 패턴 (표 순서 고정) ---
TASK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("inspect_failures", re.compile(r"(실패|미완성|불확실).*(점검|재시도|모아|확인)")),
    ("reconcile", re.compile(r"(끊긴|미완료).*(이어|재개|점검)")),
    ("sync_all_sources", re.compile(r"전체\s*(원본|시트).*(동기화|갱신)")),
    ("sync_sources", re.compile(r"(원본|시트).*(동기화|갱신)")),
    # 브랜드 원고 생성 (`브랜드 원고 생성 우아덤 1건` / `우아덤 원고 1개 만들어줘`).
    # `일상 글 생성`보다 앞서지만 `원고`라는 낱말이 있어야만 잡힌다.
    ("generate_brand", re.compile(r"원고.*(생성|만들어|작성)|(생성|만들어|작성).*원고")),
    # 제휴 카페 일상 글은 ChatGPT 웹 세션으로 만든다 (자사 xlsx 일상 글과 별개)
    ("generate_affiliate_daily", re.compile(r"제휴.*일상\s*글.*(생성|만들어)")),
    ("generate_daily", re.compile(r"일상\s*글.*(생성|만들어)")),
    ("collect_daily", re.compile(r"일상\s*글.*(수집|가져와)")),
    ("collect_new_photos", re.compile(r"새\s*(사진|이미지)\s*(수거|회수|가져오기|가져와)")),
    ("gpt_keepalive", re.compile(r"(gpt|지피티).*(유지|점검)", re.I)),
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
    ("cleanup_orphans", re.compile(r"(고아|찌꺼기).*(정리|삭제)")),
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
#: `댓글 랜덤` / `댓글 무작위` / `댓글 0~3개`
RE_RANDOM_COMMENTS = re.compile(r"댓글\s*(?:랜덤|무작위|\d+\s*~\s*\d+\s*개)")
#: 개수 인식 전에 지우는 댓글 개수 표현 (`댓글 0~3개`가 글 수로 잡히지 않게)
RE_COMMENT_COUNT = re.compile(r"댓글\s*\d+\s*(?:~\s*\d+\s*)?개")
#: 자사 카페 일상 글의 기본 글 사이 대기(분) — self-cafe-daily-rules §4
SELF_DAILY_INTERVAL = (2, 3)
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
RE_KEYWORD_SLOT = re.compile(r"키워드\s+(\S+)")
#: 사진 생성 승인 문구 (`사진 생성 승인 우아덤 키워드 2장`)
RE_PHOTO_APPROVE_GEN = re.compile(r"사진\s*생성\s*승인")
#: 승인/반려 명령의 자리값 형태 (`사진 승인 우아덤 키워드`)
RE_PHOTO_POSITIONAL = re.compile(
    r"사진\s*(?:생성\s*승인|승인|반려)\s+(\S+)(?:\s+(\S+))?"
)
#: `2장` / `3개`처럼 개수로 읽어야 할 토큰 (자리값 브랜드·키워드에서 제외)
RE_COUNT_TOKEN = re.compile(r"^\d+\s*(?:장|개|건)?$")
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
        "cleanup_orphans",
        "repair_comments",
        "gpt_keepalive",
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
        "generate_affiliate_daily",
        "generate_daily",
        "collect_daily",
        "collect_photos",
        "generate_photos",
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
    spec["immediate"] = RE_IMMEDIATE.search(raw) is not None

    # 자사 카페 일상 글: `카페별 N건` / `댓글 랜덤` (self-cafe-daily-rules §1·§5)
    if task in PUBLISH_TASKS:
        spec["per_cafe"] = RE_PER_CAFE.search(raw) is not None
        spec["random_comments"] = RE_RANDOM_COMMENTS.search(raw) is not None
        if spec["per_cafe"] or task == "publish_daily":
            # 규칙 §4: 자사 카페 일상 글은 (카페별이든 카페 하나든) 예약하지 않고 항상 즉시 발행하며,
            # 글과 글 사이를 기본 2~3분 쉰다(`N~M분 간격`을 적으면 그 값).
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
    "generate_affiliate_daily": "제휴 일상 글 생성(GPT)",
    "generate_daily": "일상 글 생성",
    "collect_daily": "일상 글 수집",
    "collect_photos": "사진 수집",
    "collect_new_photos": "새 사진 수거",
    "generate_photos": "사진 생성(GPT)",
    "approve_photos": "사진 승인",
    "reject_photos": "사진 반려",
    "gpt_keepalive": "GPT 세션 점검",
    "request_photos": "사진 요청",
    "wash_photos": "사진 세탁",
    "learn_guides": "지침 학습",
    "cleanup_orphans": "고아 글 정리",
    "repair_comments": "댓글 복구",
    "open_login": "로그인 창 열기",
    "stop": "작업 중지",
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
    if getattr(spec, "approved", False):
        parts.append("승인됨")
    if spec.immediate:
        parts.append("즉시")
    parts.append("모의 실행" if spec.dry_run else "실제 발행")
    return ", ".join(parts)
