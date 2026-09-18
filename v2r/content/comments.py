"""댓글 트리 12노드 구성·예약·충돌 해소. legacy §5, api-spec §5 기준."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

#: 12노드 기본 트리 (label, depth, parent, role)
DEFAULT_TREE: list[dict] = (
    [
        {"label": f"댓글{i}", "depth": 0, "parent": None, "role": "comment"}
        for i in range(1, 6)
    ]
    + [
        {"label": f"대댓글{i}", "depth": 1, "parent": f"댓글{i}", "role": "reply"}
        for i in range(1, 6)
    ]
    + [
        {"label": "대대댓글2", "depth": 2, "parent": "대댓글2", "role": "reply2"},
        {"label": "대대대댓글2", "depth": 3, "parent": "대대댓글2", "role": "reply3"},
    ]
)

#: 라벨별 고정 오프셋(분)
OFFSETS_MIN: dict[str, int] = {
    **{f"댓글{i}": 4 + i for i in range(1, 6)},
    **{f"대댓글{i}": 14 + i for i in range(1, 6)},
    "대대댓글2": 26,
    "대대대댓글2": 36,
}

MAX_SHIFT_MIN = 24 * 60


class CommentError(RuntimeError):
    """댓글 구성 실패."""


def assign_comment_accounts(
    tree: list[dict],
    comment_pool: list[str],
    author: str,
    rng: random.Random | None = None,
) -> list[dict]:
    """루트 댓글은 댓글 전용 계정 무작위(작성자 제외), 답글은 본문 작성계정."""
    rng = rng or random.Random()
    pool = [p for p in (comment_pool or []) if p.casefold() != (author or "").casefold()]
    if not pool:
        raise CommentError("사용 가능한 댓글 계정이 없습니다")

    roots = [n for n in tree if n["depth"] == 0]
    picks = rng.sample(pool, len(roots)) if len(pool) >= len(roots) else [
        rng.choice(pool) for _ in roots
    ]
    root_accounts = {n["label"]: a for n, a in zip(roots, picks)}

    out: list[dict] = []
    for node in tree:
        item = dict(node)
        item["account"] = root_accounts[node["label"]] if node["depth"] == 0 else author
        out.append(item)
    return out


def schedule(tree: list[dict], root_start: datetime) -> list[dict]:
    """노드별 start_at 계산 (root_start + 고정 오프셋)."""
    items: list[dict] = []
    for node in tree:
        label = node["label"]
        if label not in OFFSETS_MIN:
            raise CommentError(f"오프셋 미정의 라벨: {label}")
        item = dict(node)
        item["root_start_at"] = root_start
        item["start_at"] = root_start + timedelta(minutes=OFFSETS_MIN[label])
        items.append(item)
    return items


def _slots(items: list[dict]) -> list[tuple[str, str]]:
    """(계정, 분) 키 목록."""
    return [
        (
            str(it.get("account", "")).casefold(),
            it["start_at"].strftime("%Y-%m-%dT%H:%M"),
        )
        for it in items
    ]


def _has_conflict(items: list[dict], occupied: set[tuple[str, str]]) -> bool:
    """같은 계정이 같은 분에 두 번 등장하는지."""
    keys = _slots(items)
    return len(set(keys)) != len(keys) or bool(set(keys) & occupied)


def resolve_conflicts(
    items: list[dict], occupied: set[tuple[str, str]] | None = None
) -> list[dict]:
    """같은 계정·같은 분 충돌 시 번들 전체를 1분씩 민다(최대 24시간).

    `occupied`는 이미 잡혀 있는 (계정, "YYYY-MM-DDTHH:MM") 집합.
    번들 내부에서 두 항목이 완전히 같은 분이면 이동으로 풀 수 없으므로 즉시 에러.
    """
    out = [dict(it) for it in items]
    occupied = {(a.casefold(), m) for a, m in (occupied or set())}

    inner = _slots(out)
    if len(set(inner)) != len(inner):
        raise CommentError("번들 내부 충돌은 일괄 이동으로 해소할 수 없습니다")

    shift = 0
    while _has_conflict(out, occupied):
        shift += 1
        if shift > MAX_SHIFT_MIN:
            raise CommentError("24시간 내 충돌을 해소할 수 없습니다")
        for it in out:
            it["start_at"] = it["start_at"] + timedelta(minutes=1)
            if isinstance(it.get("root_start_at"), datetime):
                it["root_start_at"] = it["root_start_at"] + timedelta(minutes=1)
    return out


def _iso(dt: datetime | None) -> str | None:
    """API용 UTC ISO 문자열. naive datetime은 KST로 간주한다."""
    from v2r.api.articles import to_iso_z

    return to_iso_z(dt)


def to_api_payload(items: list[dict], members: dict[str, dict]) -> list[dict]:
    """api-spec §5 comments 배열(1단계 중첩, 깊은 계층은 reply_member).

    `root_start_at`은 답글일 때만 값을 갖고, 값은 **루트 댓글의 예약 시각**이다.
    루트 댓글 자신은 `None`(기준이 곧 자기 자신)으로 둔다.
    """
    by_label = {it["label"]: it for it in items}

    def root_of(item: dict) -> dict:
        cur = item
        while cur.get("parent"):
            cur = by_label[cur["parent"]]
        return cur

    payload_by_root: dict[str, dict] = {}
    result: list[dict] = []

    for it in items:
        if it["depth"] != 0:
            continue
        node = {
            "contents": it.get("text", ""),
            "naver_login_id": it.get("account", ""),
            "start_at": _iso(it["start_at"]),
            "repeat_count": 0,
            "interval_seconds": 0,
            "root_start_at": None,  # 답글일 때만 값을 갖는다
            "reply_member": None,
            "comments": [],
        }
        payload_by_root[it["label"]] = node
        result.append(node)

    for it in items:
        if it["depth"] == 0:
            continue
        root = root_of(it)
        parent = by_label[it["parent"]]
        reply_member = None
        if it["depth"] >= 2:
            parent_account = parent.get("account", "")
            info = members.get(parent_account, {})
            member_key = info.get("member_key") or ""
            if not member_key:
                raise CommentError(
                    f"답글 대상 계정의 member_key를 알 수 없습니다: {parent_account or '(미지정)'}"
                )
            reply_member = {
                "member_key": member_key,
                "naver_login_id": parent_account,
                "nick": info.get("nick", ""),
            }
        payload_by_root[root["label"]]["comments"].append(
            {
                "contents": it.get("text", ""),
                "naver_login_id": it.get("account", ""),
                "start_at": _iso(it["start_at"]),
                "repeat_count": 0,
                "interval_seconds": 0,
                "root_start_at": _iso(root["start_at"]),
                "reply_member": reply_member,
                "comments": [],
            }
        )

    return result
