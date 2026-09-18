"""댓글 트리·예약·충돌 테스트."""

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from v2r.content.comments import (
    DEFAULT_TREE,
    OFFSETS_MIN,
    CommentError,
    assign_comment_accounts,
    resolve_conflicts,
    schedule,
    to_api_payload,
)

KST = ZoneInfo("Asia/Seoul")
POOL = ["c1", "c2", "c3", "c4", "c5", "c6"]
ROOT = datetime(2026, 9, 19, 10, 0, tzinfo=KST)


def test_기본트리_12노드():
    assert len(DEFAULT_TREE) == 12
    depths = [n["depth"] for n in DEFAULT_TREE]
    assert depths.count(0) == 5 and depths.count(1) == 5
    assert depths.count(2) == 1 and depths.count(3) == 1
    assert [n["label"] for n in DEFAULT_TREE][-2:] == ["대대댓글2", "대대대댓글2"]
    by_label = {n["label"]: n for n in DEFAULT_TREE}
    assert by_label["대댓글3"]["parent"] == "댓글3"
    assert by_label["대대댓글2"]["parent"] == "대댓글2"
    assert by_label["대대대댓글2"]["parent"] == "대대댓글2"


def test_오프셋_표():
    assert [OFFSETS_MIN[f"댓글{i}"] for i in range(1, 6)] == [5, 6, 7, 8, 9]
    assert [OFFSETS_MIN[f"대댓글{i}"] for i in range(1, 6)] == [15, 16, 17, 18, 19]
    assert OFFSETS_MIN["대대댓글2"] == 26 and OFFSETS_MIN["대대대댓글2"] == 36


def test_계정배정_작성자제외_답글은_작성자():
    tree = assign_comment_accounts(DEFAULT_TREE, POOL + ["author"], "author",
                                   random.Random(5))
    roots = [n for n in tree if n["depth"] == 0]
    assert all(n["account"] != "author" for n in roots)
    assert len({n["account"] for n in roots}) == 5  # 중복 없이
    assert all(n["account"] == "author" for n in tree if n["depth"] > 0)


def test_계정배정_풀없으면_에러():
    with pytest.raises(CommentError):
        assign_comment_accounts(DEFAULT_TREE, ["author"], "author")


def test_schedule_오프셋_적용():
    items = schedule(DEFAULT_TREE, ROOT)
    by_label = {i["label"]: i for i in items}
    assert by_label["댓글1"]["start_at"] == ROOT + timedelta(minutes=5)
    assert by_label["대대대댓글2"]["start_at"] == ROOT + timedelta(minutes=36)
    assert all(i["root_start_at"] == ROOT for i in items)


def test_충돌시_번들_전체이동():
    tree = assign_comment_accounts(DEFAULT_TREE, POOL, "author", random.Random(1))
    items = schedule(tree, ROOT)
    root1 = items[0]
    occupied = {(root1["account"], root1["start_at"].strftime("%Y-%m-%dT%H:%M"))}
    before = [i["start_at"] for i in items]
    fixed = resolve_conflicts(items, occupied)
    shifts = {f["start_at"] - b for f, b in zip(fixed, before)}
    assert shifts == {timedelta(minutes=1)}  # 12개 전체가 같은 양만큼 이동
    assert [i["start_at"] for i in items] == before  # 원본 불변
    assert fixed[0]["root_start_at"] == ROOT + timedelta(minutes=1)


def test_충돌_해소불가면_에러():
    base = datetime(2026, 9, 19, 10, 0, tzinfo=KST)
    items = [
        {"label": "댓글1", "depth": 0, "parent": None, "account": "x",
         "start_at": base, "root_start_at": base},
        {"label": "댓글2", "depth": 0, "parent": None, "account": "x",
         "start_at": base, "root_start_at": base},
    ]
    with pytest.raises(CommentError):
        resolve_conflicts(items)


def test_충돌_24시간초과면_에러():
    base = datetime(2026, 9, 19, 10, 0, tzinfo=KST)
    items = [{"label": "댓글1", "depth": 0, "parent": None, "account": "x",
              "start_at": base, "root_start_at": base}]
    occupied = {
        ("x", (base + timedelta(minutes=k)).strftime("%Y-%m-%dT%H:%M"))
        for k in range(0, 24 * 60 + 2)
    }
    with pytest.raises(CommentError):
        resolve_conflicts(items, occupied)


def test_api_payload_중첩구조():
    tree = assign_comment_accounts(DEFAULT_TREE, POOL, "author", random.Random(2))
    for n in tree:
        n["text"] = n["label"] + " 내용"
    items = schedule(tree, ROOT)
    members = {"author": {"member_key": "MK", "nick": "닉네임"}}
    payload = to_api_payload(items, members)

    assert len(payload) == 5  # 루트 5개
    assert sum(len(p["comments"]) for p in payload) == 7  # 답글 7개
    c2 = next(p for p in payload if p["contents"].startswith("댓글2"))
    labels = [c["contents"] for c in c2["comments"]]
    assert "대댓글2 내용" in labels and "대대댓글2 내용" in labels
    deep = next(c for c in c2["comments"] if c["contents"].startswith("대대댓글2"))
    assert deep["reply_member"]["member_key"] == "MK"  # 깊은 계층은 reply_member
    shallow = next(c for c in c2["comments"] if c["contents"].startswith("대댓글2 "))
    assert shallow["reply_member"] is None
    assert payload[0]["start_at"].endswith("Z")


def test_root_start_at는_루트댓글_시각이고_루트는_None():
    tree = assign_comment_accounts(DEFAULT_TREE, POOL, "author", random.Random(2))
    for n in tree:
        n["text"] = n["label"]
    items = schedule(tree, ROOT)
    payload = to_api_payload(items, {"author": {"member_key": "MK", "nick": "닉"}})
    for root in payload:
        assert root["root_start_at"] is None  # 답글일 때만 값을 갖는다
        for child in root["comments"]:
            assert child["root_start_at"] == root["start_at"]


def test_iso_는_naive를_KST로_본다():
    naive = datetime(2026, 9, 19, 10, 0)
    items = schedule(
        [{"label": "댓글1", "depth": 0, "parent": None, "role": "comment"}], naive
    )
    payload = to_api_payload([{**items[0], "account": "a", "text": "t"}], {})
    # KST 10:05 = UTC 01:05
    assert payload[0]["start_at"] == "2026-09-19T01:05:00Z"


def test_member_key_없으면_에러():
    tree = assign_comment_accounts(DEFAULT_TREE, POOL, "author", random.Random(2))
    for n in tree:
        n["text"] = n["label"]
    items = schedule(tree, ROOT)
    with pytest.raises(CommentError):
        to_api_payload(items, {})  # member_key 미상 → 전송 금지
