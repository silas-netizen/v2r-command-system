"""채널 묶음: 설정에서 사용 가능한 채널을 만들고 일괄 보고한다.

알림 등급 정책 (사용자 지시 2026-09-23, 설명은 config/notify.yaml):
  - "critical": 항상 보낸다. 앞에 "🔴"를 붙이고, 같은 사건(같은 category)은
    CRITICAL_REPEAT_SECONDS 안에 되풀이하지 않는다. 복구되면(clear_critical)
    "복구됨" 1회를 보낸다. CRITICAL_ESCALATE_SECONDS 넘게 이어지면 1회 더.
  - "summary": 채널로 보내지 않는다(0을 돌려준다) — 정기 보고에만 담는다.
  - "info"(기본값): 채널로 보내지 않는다 — 로그·DB 이벤트·현황판에만 남긴다.
  - "always": 등급 분류 밖. 사용자가 채널에서 직접 보낸 명령의 응답이라
    무조건 보낸다(기존 동작과 같다).
"""

from __future__ import annotations

import logging
import re
import time as _time
from typing import Any

from .base import Channel, IncomingCommand, format_report
from .slack import SlackChannel
from .telegram import TelegramChannel

log = logging.getLogger(__name__)

__all__ = [
    "Channel",
    "IncomingCommand",
    "format_report",
    "SlackChannel",
    "TelegramChannel",
    "build_channels",
    "notify_all",
    "notify_photo_all",
    "notify_document_all",
    "clear_critical",
    "NOTIFY_LEVELS",
]

#: 알려진 등급. 모르는 값이 들어오면 "info"로 다룬다(조용히 억제 — 과다 전송보단 안전).
NOTIFY_LEVELS = ("critical", "summary", "info", "always")

#: 등급 아이콘 (사용자 지시 2026-09-23: 텔레그램·슬랙 메시지에 등급+범주 아이콘).
LEVEL_ICONS = {"critical": "🔴", "summary": "🟡", "info": "⚪"}

#: 범주 → 아이콘 (config/notify.yaml 의 표와 같다). notify_all 의 `tag` 인자로 고른다.
CATEGORY_ICONS = {
    "publish": "📢",  # 일상·브랜드 발행 결과
    "login": "🔑",  # 로그인·세션
    "schedule": "⏰",  # 예약·실행기
    "keyword": "🔍",  # 키워드·노출
    "draft": "✍️",  # 원고·이미지 생성
    "dashboard": "📊",  # 현황판·중간/일일 보고
    "reply": "💬",  # 사용자 직접 명령 응답
    "maintenance": "🛠",  # 정비
}


def _icon_prefix(level: str, tag: str | None) -> str:
    """등급·범주 아이콘을 앞에 붙일 문구를 만든다. 없으면 빈 문자열."""
    cat_icon = CATEGORY_ICONS.get(str(tag)) if tag else ""
    if level == "always":
        # 사용자 직접 명령 응답: 범주 아이콘만(있으면). 등급 아이콘은 안 붙인다.
        return f"{cat_icon} " if cat_icon else ""
    level_icon = LEVEL_ICONS.get(level, "")
    parts = "".join(p for p in (level_icon, cat_icon) if p)
    return f"{parts} " if parts else ""

#: 같은 사건(critical) 경고를 되풀이하지 않는 간격(초)
CRITICAL_REPEAT_SECONDS = 600
#: 이 시간 넘게 이어지면 에스컬레이션(다시 한번) 보낸다(초)
CRITICAL_ESCALATE_SECONDS = 600

#: category → 마지막으로 보낸 시각(단조 시계), 첫 알림 시각, 에스컬레이션 여부
_CRITICAL_STATE: dict[str, dict[str, Any]] = {}


def clear_critical(category: str) -> bool:
    """그 사건이 끝났다는 표시. 다음에 다시 나면 "복구됨" 1회를 보낸다.

    돌려주는 값은 "그 사건이 실제로 경고 중이었는가"(복구 알림을 보낼지 판단용).
    """
    state = _CRITICAL_STATE.pop(str(category), None)
    return bool(state)


_PUSH_CACHE: dict[str, Any] = {"at": 0.0, "names": None}


def push_channels(channels: list[Channel]) -> list[Channel]:
    """푸시(보고·경고)를 실제로 보낼 채널만 고른다.

    사용자 지시 2026-09-23 "푸시는 텔레는 일단 중지, 슬랙만": config/notify.yaml 의
    `push_channels` 목록(채널 이름)에 든 채널로만 보낸다. 항목이 없으면 전부.
    명령 수신·명령에 대한 직접 답장(reply_to_origin)은 이 필터와 무관하다.
    파일은 60초마다 다시 읽어 실행기 재시작 없이 바꿀 수 있다.
    """
    now = _time.monotonic()
    if _PUSH_CACHE["names"] is None or now - _PUSH_CACHE["at"] > 60:
        names = None
        try:
            import yaml
            from pathlib import Path

            cfg_path = Path(__file__).resolve().parents[2] / "config" / "notify.yaml"
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            raw = data.get("push_channels")
            if isinstance(raw, list):
                names = {str(x).strip().lower() for x in raw if str(x).strip()}
        except Exception as exc:  # noqa: BLE001
            log.debug("push_channels 읽기 실패: %s", exc)
        _PUSH_CACHE.update(at=now, names=names if names is not None else set())
    names = _PUSH_CACHE["names"]
    if not names:
        return list(channels or [])
    # 목록에 없는 "알려진" 채널(텔레그램·슬랙)만 뺀다 — 시험용 가짜 채널 등 이름을 모르는
    # 채널은 그대로 둔다.
    known = {"telegram", "slack"}
    return [
        c for c in channels or []
        if str(getattr(c, "name", "")).lower() in names
        or str(getattr(c, "name", "")).lower() not in known
    ]


def build_channels(settings: Any | None = None) -> list[Channel]:
    """설정에서 활성 채널 목록을 만든다. 비활성 채널은 제외."""
    if settings is None:
        from ..config import get_settings

        settings = get_settings()

    data_dir = getattr(settings, "data_dir", None)
    channels: list[Channel] = []

    telegram = TelegramChannel(
        token=getattr(settings, "telegram_bot_token", "") or "",
        allowed_chat_ids=set(getattr(settings, "telegram_allowed_chat_ids", []) or []),
        data_dir=data_dir,
    )
    if telegram.enabled:
        channels.append(telegram)
    else:
        log.info("텔레그램 채널 비활성(토큰 또는 허용 chat_id 없음)")

    slack = SlackChannel(
        bot_token=getattr(settings, "slack_bot_token", "") or "",
        allowed_channel_ids=set(getattr(settings, "slack_allowed_channel_ids", []) or []),
        webhook_url=getattr(settings, "slack_webhook_url", "") or "",
        data_dir=data_dir,
    )
    if slack.enabled:
        channels.append(slack)
    else:
        log.info("슬랙 채널 비활성(봇 토큰+허용 채널 또는 webhook 없음)")

    return channels



_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)(token=)[^&\s]+"),
    re.compile(r"(?i)(password[=:]\s*)\S+"),
    re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
]


def sanitize(text: str, limit: int = 500) -> str:
    """보고 문구에서 토큰·비밀번호·이메일을 가리고 길이를 제한한다."""
    out = str(text or "")
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "***", out)
    return out[:limit]

def notify_all(
    channels: list[Channel],
    text: str,
    level: str = "info",
    *,
    category: str | None = None,
    tag: str | None = None,
) -> int:
    """모든 채널에 같은 보고를 보낸다. 성공 건수 반환.

    `level` 이 등급 정책을 정한다(모듈 docstring 참고). "critical" 은
    `category`(없으면 text 자체) 로 사건을 구분해 되풀이 전송을 막는다.
    `tag` 는 범주 아이콘(📢 발행 · 🔑 로그인 · ⏰ 예약 · 🔍 키워드 · ✍️ 원고 ·
    📊 현황판 · 💬 명령응답 · 🛠 정비, `CATEGORY_ICONS` 참고) — 실제로 보내는
    메시지(critical/summary/always)에만 앞에 붙는다.
    """
    level = str(level or "info")
    if level not in NOTIFY_LEVELS:
        level = "info"
    if level in ("info", "summary"):
        return 0  # 채널로 안 보낸다 — 로그·DB·정기 보고가 대신한다

    out_text = text
    if level == "critical":
        cat = str(category or text)
        now = _time.monotonic()
        state = _CRITICAL_STATE.get(cat)
        if state is not None:
            since_first = now - state["first"]
            since_last = now - state["last"]
            escalate = since_first >= CRITICAL_ESCALATE_SECONDS and not state.get("escalated")
            if since_last < CRITICAL_REPEAT_SECONDS and not escalate:
                return 0  # 같은 사건, 되풀이 억제
            if escalate:
                state["escalated"] = True
                out_text = f"{text} (계속됨, {int(since_first)}초째)"
        else:
            state = {"first": now, "escalated": False}
            _CRITICAL_STATE[cat] = state
        state["last"] = now

    out_text = f"{_icon_prefix(level, tag)}{out_text}"

    sent = 0
    for channel in push_channels(channels):
        try:
            sent += int(channel.broadcast(sanitize(out_text)) or 0)
        except Exception as exc:  # 알림 실패로 본 작업이 죽지 않게
            log.warning("채널 %s 보고 실패: %s", getattr(channel, "name", "?"), exc)
    return sent


def notify_document_all(channels: list[Channel], path: Any, caption: str = "") -> int:
    """모든 채널에 파일 한 개를 보낸다. 파일을 못 보내는 채널은 건너뛴다."""
    sent = 0
    for channel in push_channels(channels):
        fn = getattr(channel, "broadcast_document", None)
        if not callable(fn):
            continue
        try:
            sent += int(fn(path, sanitize(caption)) or 0)
        except Exception as exc:  # 파일 전송 실패로 본 작업이 죽지 않게
            log.warning("채널 %s 파일 전송 실패: %s", getattr(channel, "name", "?"), exc)
    return sent


def notify_photo_all(channels: list[Channel], path: Any, caption: str = "") -> int:
    """모든 채널에 사진 한 장을 보낸다. 사진을 못 보내는 채널은 건너뛴다."""
    sent = 0
    for channel in push_channels(channels):
        fn = getattr(channel, "broadcast_photo", None)
        if not callable(fn):
            continue
        try:
            sent += int(fn(path, sanitize(caption)) or 0)
        except Exception as exc:  # 사진 전송 실패로 본 작업이 죽지 않게
            log.warning("채널 %s 사진 전송 실패: %s", getattr(channel, "name", "?"), exc)
    return sent
