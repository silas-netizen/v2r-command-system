"""이모지(그림문자) 제거 — 사용자 절대 규칙 (2026-09-19).

> **이모지는 우리가 쓰는 모든 원고에서 제외.**

2026-09-19 자사 카페 일상 글 18건이 제목에 이모지를 달고 나갔다. 원인은
"생성한 댓글만 검사"했기 때문이다. 미리 만들어 둔 xlsx 원고에 이미 이모지가
들어 있었고, 발행 경로에는 아무 거름망이 없었다.

그래서 거름망을 **세 겹**으로 둔다.

1. 생성 단계 — 모델이 만든 글/댓글은 이모지가 있으면 버리거나 다시 시킨다.
2. 적재 단계 — xlsx 일상 글은 읽을 때 지운다(모의 실행에도 깨끗한 글이 보이게).
3. 발행 단계 — 무슨 경로로 왔든 등록 직전에 한 번 더 지우고, 그래도 남아 있으면
   발행을 멈춘다(`PublishError`).

한글 이모티콘(ㅋㅋ/ㅠㅠ/ㅎㅎ)과 문장부호는 **지우지 않는다**. 그것은 사람이 쓰는
말이지 그림문자가 아니다.
"""

from __future__ import annotations

import re
from typing import Any

#: 키캡 연속열 (`1️⃣`, `#️⃣`) — 숫자까지 통째로 지운다
_KEYCAP = r"[0-9#*]️?⃣"

#: 그림문자로 보는 코드 구간
#:
#: - `1F000–1FAFF` 이모지 본진(얼굴·손·동물·음식·깃발·지역 지시자 `1F1E6–1F1FF` 포함)
#: - `2600–27BF` 기타 기호와 딩뱃 (`✅` 2705, `❌` 274C, `❤` 2764, `✨` 2728)
#: - `2B00–2BFF` 화살표·별 (`⭐` 2B50, `⬆` 2B06)
#: - `2190–21FF` 화살표 (`←` `→` `↔`)
#: - `2300–23FF` 시계·재생 기호 (`⌚` `⏰` `▶`)
#: - `FE0F`/`FE0E` 변이 선택자, `200D` ZWJ(가족·직업 조합), `20E3` 키캡, `3030`/`303D`
_RANGES = (
    "\U0001f000-\U0001faff"
    "☀-➿"
    "⬀-⯿"
    "←-⇿"
    "⌀-⏿"
    "️︎‍⃣〰〽"
)

#: 이모지 1개(또는 키캡 연속열 1개)
EMOJI_RE = re.compile(f"{_KEYCAP}|[{_RANGES}]")

#: 이모지를 지운 뒤 생긴 겹공백
_DOUBLE_SPACE = re.compile(r"[ \t]{2,}")


def has_emoji(text: Any) -> bool:
    """문자열에 이모지가 있는가. 한글 이모티콘(ㅋㅋ)은 이모지가 아니다."""
    return bool(EMOJI_RE.search(str(text or "")))


def strip_emoji(text: Any) -> str:
    """이모지를 지우고, 그 때문에 생긴 겹공백·끝공백을 정리한다.

    이모지가 없으면 원문을 **그대로** 돌려준다(들여쓰기 같은 서식을 건드리지 않기 위해서다).
    """
    raw = str(text or "")
    if not EMOJI_RE.search(raw):
        return raw
    out = EMOJI_RE.sub("", raw)
    lines = [_DOUBLE_SPACE.sub(" ", line).strip() for line in out.splitlines()]
    return "\n".join(lines).strip()


def _clean_comment(node: Any) -> tuple[Any, int]:
    """댓글 노드 1개(및 자식)를 정리한다. `(새 노드, 지운 곳 수)`."""
    removed = 0
    text = getattr(node, "text", "")
    changes: dict[str, Any] = {}
    if has_emoji(text):
        changes["text"] = strip_emoji(text)
        removed += 1
    children = getattr(node, "children", None)
    if isinstance(children, list) and children:
        new_children = []
        for child in children:
            cleaned, n = _clean_comment(child)
            new_children.append(cleaned)
            removed += n
        if removed:
            changes["children"] = new_children
    if not changes:
        return node, 0
    return node.model_copy(update=changes), removed


def emoji_spots(manuscript: Any) -> list[str]:
    """이모지가 든 자리 이름 목록 (`제목`, `본문`, `댓글2` …). 보고·경고용."""
    spots: list[str] = []
    for name, attr in (("제목", "title"), ("본문", "body"), ("말머리", "head")):
        if has_emoji(getattr(manuscript, attr, "")):
            spots.append(name)
    if any(has_emoji(t) for t in (getattr(manuscript, "tags", None) or [])):
        spots.append("태그")

    def walk(nodes: Any) -> None:
        for node in nodes or []:
            if has_emoji(getattr(node, "text", "")):
                spots.append(str(getattr(node, "label", "댓글")))
            walk(getattr(node, "children", None))

    walk(getattr(manuscript, "comments", None))
    return spots


def sanitize_manuscript(manuscript: Any) -> Any:
    """원고의 제목·본문·말머리·태그·모든 댓글에서 이모지를 지운 **새 원고**.

    `content_hash`는 손대지 않는다. 해시는 "이 행을 이미 발행했는가"를 보는 열쇠라
    글자를 고쳤다고 바꾸면 같은 글이 두 번 올라간다(발행 기록 대조가 어긋난다).
    """
    if manuscript is None:
        return manuscript
    changes: dict[str, Any] = {}
    for attr in ("title", "body", "head"):
        value = getattr(manuscript, attr, None)
        if isinstance(value, str) and has_emoji(value):
            changes[attr] = strip_emoji(value)
    tags = getattr(manuscript, "tags", None)
    if isinstance(tags, list) and any(has_emoji(t) for t in tags):
        changes["tags"] = [t for t in (strip_emoji(t) for t in tags) if t]

    comments = getattr(manuscript, "comments", None)
    if isinstance(comments, list) and comments:
        new_nodes = []
        touched = 0
        for node in comments:
            cleaned, n = _clean_comment(node)
            new_nodes.append(cleaned)
            touched += n
        if touched:
            changes["comments"] = new_nodes

    if not changes:
        return manuscript
    return manuscript.model_copy(update=changes)


__all__ = [
    "EMOJI_RE",
    "emoji_spots",
    "has_emoji",
    "sanitize_manuscript",
    "strip_emoji",
]
