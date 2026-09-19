"""자사 카페 일상 글에 다는 랜덤 댓글 (docs/reference/self-cafe-daily-rules.md §5).

- 개수 추첨: 0개 50% / 1개 25% / 2개 15% / 3개 10% (평균 0.85개)
- 시각: 글 시각 + [3~25], [15~60], [40~120]분 (앞 댓글보다 항상 뒤)
- 내용: Haiku(`daily_comment`)가 10~30자 한 줄씩 생성. 실패하면 0개로 발행한다.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta

from v2r.content import sanitize

log = logging.getLogger(__name__)

#: 개수별 가중치 (개수, 가중치)
COUNT_WEIGHTS: list[tuple[int, int]] = [(0, 50), (1, 25), (2, 15), (3, 10)]
#: n번째 댓글의 지연 범위(분)
DELAY_RANGES: list[tuple[int, int]] = [(3, 25), (15, 60), (40, 120)]
#: 댓글 한 줄 길이 (공백 포함)
MIN_LEN = 10
MAX_LEN = 30
#: 규칙 위반 시 다시 묻는 최대 횟수(성공할 때까지 반복하되 무한 루프는 막는다)
MAX_ATTEMPTS = 6

#: 이모지 판단은 `content/sanitize.py` 한곳에서 한다 (한글 이모티콘 ㅋㅋ/ㅠㅠ는 허용)
#: 광고·링크로 보는 조각
_BANNED = ("http://", "https://", "www.", "@", "010-", "카톡", "문의주세요")


def draw_count(rng: random.Random | None = None) -> int:
    """댓글 개수 추첨 (0:50% 1:25% 2:15% 3:10%)."""
    rng = rng or random.Random()
    counts = [c for c, _ in COUNT_WEIGHTS]
    weights = [w for _, w in COUNT_WEIGHTS]
    return int(rng.choices(counts, weights=weights, k=1)[0])


def plan_times(
    root_start: datetime, count: int, rng: random.Random | None = None
) -> list[datetime]:
    """글 시각 기준 댓글 시각 목록. 항상 앞 댓글보다 1분 이상 뒤."""
    rng = rng or random.Random()
    out: list[datetime] = []
    previous: datetime | None = None
    for i in range(max(0, min(count, len(DELAY_RANGES)))):
        low, high = DELAY_RANGES[i]
        at = root_start + timedelta(minutes=rng.randint(low, high))
        if previous is not None and at <= previous:
            at = previous + timedelta(minutes=1)
        out.append(at)
        previous = at
    return out


def clean_text(raw: object) -> str:
    """댓글 한 줄 정리. 규칙에 어긋나면 빈 문자열."""
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    text = sanitize.strip_emoji(text).strip()
    if sanitize.has_emoji(text):  # 지웠는데도 남았다 → 통째로 버린다
        return ""
    text = text.rstrip(".·…")  # 마침표 없음
    text = text.strip()
    if not text:
        return ""
    low = text.casefold()
    if any(bad in low for bad in _BANNED):
        return ""
    if len(text) > MAX_LEN:
        return ""  # 너무 길면 자르지 않고 버린다(문장이 끊기는 것보다 낫다)
    if len(text) < MIN_LEN:
        return ""
    return text


def _user_prompt(title: str, body: str, count: int) -> str:
    return (
        f"댓글 {count}개를 JSON 배열로 만들어라.\n\n"
        f"제목: {title}\n본문:\n{(body or '')[:1500]}"
    )


def generate_texts(
    llm,
    title: str,
    body: str,
    count: int,
    *,
    on_warn=None,
    _retry: int = 0,
) -> list[str]:
    """Haiku로 댓글 문구 `count`개.

    규칙 위반이면 위반 내용을 알려 주며 **성공할 때까지** 다시 묻는다(사용자 규칙 2026-09-19:
    실패 보고 대신 검증·수정 반복). 무한 루프 방지로 `MAX_ATTEMPTS`까지만 돌고, 그때까지도
    안 되면 마지막에 통과한 것이 없으므로 빈 목록(경고)로 끝낸다.
    """
    if count <= 0:
        return []
    if llm is None:
        if on_warn:
            on_warn("모델이 없어 댓글을 만들지 못했습니다 (댓글 0개로 발행)")
        return []
    from v2r.llm.prompts import DAILY_RANDOM_COMMENT_SYSTEM
    from v2r.llm.router import extract_json

    try:
        raw = llm.complete(
            purpose="daily_comment",
            system=DAILY_RANDOM_COMMENT_SYSTEM,
            user=_user_prompt(title, body, count)
            + (
                f"\n\n주의(재시도 {_retry}회째): 각 댓글은 반드시 공백 포함 {MIN_LEN}~{MAX_LEN - 2}자,"
                " 마침표·이모지·링크 금지. 이전 답은 이 규칙을 어겨 버렸다. 규칙에 맞게 다시 써라."
                if _retry
                else ""
            ),
            max_tokens=400,
        )
        data = extract_json(raw)
    except Exception as exc:
        log.warning("일상 글 댓글 생성 실패: %s", exc)
        if on_warn:
            on_warn(f"댓글 생성 실패로 댓글 0개로 발행합니다: {exc}")
        return []
    if not isinstance(data, list):
        if on_warn:
            on_warn("댓글 응답이 배열이 아니어서 댓글 0개로 발행합니다")
        return []
    texts: list[str] = []
    for item in data:
        if isinstance(item, dict):  # {"text": "..."} 형태도 받아준다
            item = item.get("text") or item.get("contents") or ""
        cleaned = clean_text(item)
        if cleaned and cleaned not in texts:
            texts.append(cleaned)
        if len(texts) >= count:
            break
    if len(texts) < count and _retry < MAX_ATTEMPTS:
        # 규칙 위반으로 개수가 모자라면 위반을 알리고 다시 묻는다(성공할 때까지, 상한 있음)
        more = generate_texts(llm, title, body, count, on_warn=on_warn, _retry=_retry + 1)
        for t in more:
            if t not in texts and len(texts) < count:
                texts.append(t)
    if not texts and on_warn:
        on_warn("쓸 수 있는 댓글 문구가 없어 댓글 0개로 발행합니다")
    return texts


__all__ = [
    "COUNT_WEIGHTS",
    "DELAY_RANGES",
    "clean_text",
    "draw_count",
    "generate_texts",
    "plan_times",
]
