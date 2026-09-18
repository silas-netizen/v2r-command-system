"""댓글 순서·계정 역할 재발 방지 테스트.

기준: docs/reference/live-comment-order.md + tests/fixtures/comment_order_reference.json
"""

import json
import random
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from v2r.api.articles import flatten_comment_nodes, verify_article
from v2r.content import comments as cm
from v2r.content.manuscript import parse_article

KST = ZoneInfo("Asia/Seoul")
ROOT = datetime(2026, 9, 19, 13, 43, tzinfo=KST)
POOL = ["che", "hun", "cho", "qui", "prt", "col"]
AUTHOR = "author1"
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "comment_order_reference.json").read_text(
        encoding="utf-8"
    )
)

SAMPLE = """제목 : 약국 식욕억제제 먹어보신분 계세요?
본문 : 요즘 저녁만 되면 폭식을 해서요.
{키워드}
조언 좀 부탁드려요.

댓글1 : 댓글1 내용입니다
대댓글1 : 대댓글1 내용입니다
댓글2 : 댓글2 내용입니다
대댓글2 : 대댓글2 내용입니다
대대댓글2 : 대대댓글2 내용입니다
대대대댓글2 : 대대대댓글2 내용입니다
댓글3 : 댓글3 내용입니다
대댓글3 : 대댓글3 내용입니다
댓글4 : 댓글4 내용입니다
대댓글4 : 대댓글4 내용입니다
댓글5 : 댓글5 내용입니다
대댓글5 : 대댓글5 내용입니다
"""


def _members(accounts):
    return {a: {"member_key": f"MK-{a}", "nick": f"닉{a}"} for a in accounts}


def _tree_from_manuscript(m):
    """publish._comment_tree와 같은 방식으로 원고 → 12노드 트리."""
    texts = {c.label: c.text for c in m.comments}
    return [
        {**node, "text": texts[node["label"]]}
        for node in cm.DEFAULT_TREE
        if texts.get(node["label"])
    ]


def build_payload(manuscript_type: str, seed: int = 3):
    m = parse_article(SAMPLE)
    tree = _tree_from_manuscript(m)
    items = cm.assign_comment_accounts(
        tree, POOL, AUTHOR, random.Random(seed), manuscript_type=manuscript_type
    )
    items = cm.schedule(items, ROOT)
    items = cm.resolve_conflicts(items)
    members = _members(POOL + [AUTHOR])
    return items, cm.to_api_payload(items, members)


# ---- 참고 트리(픽스처) 자체가 기대하는 모양인지 ----


def test_픽스처_읽는순서가_12건_모두_같다():
    want = FIXTURE["reading_order"]
    assert want == [
        "댓글1", "대댓글1", "댓글2", "대댓글2", "대대댓글2", "대대대댓글2",
        "댓글3", "대댓글3", "댓글4", "대댓글4", "댓글5", "대댓글5",
    ]
    for art in FIXTURE["articles"]:
        assert [n["label"] for n in art["nodes"]] == want
        # 답글의 api 부모는 항상 루트 댓글(네이버는 2단계까지만)
        for n in art["nodes"]:
            if n["depth"] == 0:
                assert n["api_parent"] is None
            else:
                assert n["api_parent"] == f"댓글{n['label'][-1]}"


def test_픽스처_역할_규칙():
    for art in FIXTURE["articles"]:
        by_label = {n["label"]: n for n in art["nodes"]}
        for i in range(1, 6):
            assert by_label[f"댓글{i}"]["role"] == "root_pool"
            assert by_label[f"대댓글{i}"]["role"] == "author"
        assert len(set(art["root_accounts"])) == 5
        if art["manuscript_type"] == "후기형":
            assert by_label["대대댓글2"]["role"] == "spare_pool"
            assert by_label["대대대댓글2"]["role"] == "author"
        else:
            assert by_label["대대댓글2"]["role"] == "root2_pool"
            assert by_label["대대대댓글2"]["role"] == "spare_pool"
        # reply_member는 depth 2 이상에서만, 값은 바로 위 노드의 작성 계정
        assert by_label["대대댓글2"]["reply_member"] == by_label["대댓글2"]["account"]
        assert by_label["대대대댓글2"]["reply_member"] == by_label["대대댓글2"]["account"]
        for lbl, n in by_label.items():
            if n["depth"] < 2:
                assert n["reply_member"] is None


# ---- 우리 페이로드가 같은 모양을 내는지 ----


def _roles(items):
    """노드별 역할을 픽스처와 같은 어휘로."""
    by_label = {it["label"]: it for it in items}
    roots = [it["account"] for it in items if it["depth"] == 0]
    root2 = by_label["댓글2"]["account"]
    out = []
    for it in items:
        acct = it["account"]
        if it["depth"] == 0:
            out.append("root_pool")
        elif acct == AUTHOR:
            out.append("author")
        elif acct == root2:
            out.append("root2_pool")
        else:
            assert acct not in roots, f"{it['label']}이 다른 루트 계정을 재사용했다"
            out.append("spare_pool")
    return out


@pytest.mark.parametrize("mtype", ["질문형", "후기형"])
def test_페이로드가_참고_트리와_같은_순서_부모_깊이_역할(mtype):
    ref = next(a for a in FIXTURE["articles"] if a["manuscript_type"] == mtype)
    items, payload = build_payload(mtype)
    flat = flatten_comment_nodes(payload)

    assert [it["label"] for it in items] == [n["label"] for n in ref["nodes"]]
    assert [it["depth"] for it in items] == [n["depth"] for n in ref["nodes"]]
    assert _roles(items) == [n["role"] for n in ref["nodes"]]

    # 페이로드 평탄 순서 = 라벨 읽는 순서
    assert [n["contents"] for n in flat] == [
        f"{lbl} 내용입니다" for lbl in FIXTURE["reading_order"]
    ]
    # 루트 5개, 답글은 전부 루트 밑에 1단계로 (api 부모 = 루트)
    assert len(payload) == 5
    assert sum(len(p["comments"]) for p in payload) == 7
    for root in payload:
        n = root["contents"][2]  # "댓글N 내용입니다" → N
        for child in root["comments"]:
            # 답글은 같은 번호의 스레드에만 붙는다 (api 부모 = 그 루트)
            assert child["contents"].lstrip("대").startswith(f"댓글{n}")


@pytest.mark.parametrize("mtype", ["질문형", "후기형"])
def test_reply_member는_바로_위_노드의_작성계정(mtype):
    items, payload = build_payload(mtype)
    by_label = {it["label"]: it for it in items}
    flat = {n["contents"].split()[0]: n for n in flatten_comment_nodes(payload)}

    for lbl in ("댓글1", "대댓글1", "댓글2", "대댓글2", "댓글3"):
        assert flat[lbl]["reply_member"] is None
    assert flat["대대댓글2"]["reply_member"]["naver_login_id"] == by_label["대댓글2"]["account"]
    assert flat["대대대댓글2"]["reply_member"]["naver_login_id"] == by_label["대대댓글2"]["account"]


def test_질문형_대대댓글2는_댓글2_계정_대대대댓글2는_여분계정():
    items, _ = build_payload("질문형")
    by_label = {it["label"]: it for it in items}
    roots = {it["account"] for it in items if it["depth"] == 0}
    assert by_label["대대댓글2"]["account"] == by_label["댓글2"]["account"]
    assert by_label["대대대댓글2"]["account"] not in roots
    assert by_label["대대대댓글2"]["account"] != AUTHOR


def test_후기형_대대댓글2는_여분계정_대대대댓글2는_작성자():
    items, _ = build_payload("후기형")
    by_label = {it["label"]: it for it in items}
    roots = {it["account"] for it in items if it["depth"] == 0}
    assert by_label["대대댓글2"]["account"] not in roots
    assert by_label["대대댓글2"]["account"] != AUTHOR
    assert by_label["대대대댓글2"]["account"] == AUTHOR


def test_작성자가_댓글2_스레드를_혼자_쓰지_않는다():
    """라이브 사고 재현 방지: 대댓글2·대대댓글2·대대대댓글2가 전부 작성자면 실패."""
    for mtype in ("질문형", "후기형"):
        items, _ = build_payload(mtype)
        by_label = {it["label"]: it for it in items}
        thread = {by_label[l]["account"] for l in ("대댓글2", "대대댓글2", "대대대댓글2")}
        assert thread != {AUTHOR}


def test_오프셋은_참고글과_같은_간격():
    items, _ = build_payload("질문형")
    at = {it["label"]: it["start_at"] for it in items}
    for i in range(1, 6):
        assert (at[f"댓글{i}"] - at["댓글1"]).total_seconds() == (i - 1) * 60
        assert (at[f"대댓글{i}"] - at[f"댓글{i}"]).total_seconds() == 600
    assert (at["대대댓글2"] - at["대댓글2"]).total_seconds() == 600
    assert (at["대대대댓글2"] - at["대대댓글2"]).total_seconds() == 600
    # 읽는 순서는 시간순이 아니다 — 댓글2 스레드가 댓글3보다 앞이지만 더 늦다
    labels = [it["label"] for it in items]
    assert labels.index("대대대댓글2") < labels.index("댓글3")
    assert at["대대대댓글2"] > at["댓글3"]


def test_root_start_at은_스레드_루트시각():
    items, payload = build_payload("질문형")
    by_label = {it["label"]: it for it in items}
    for root in payload:
        assert root["root_start_at"] is None
        for child in root["comments"]:
            assert child["root_start_at"] == root["start_at"]
    assert by_label["댓글2"]["root_start_at"] == ROOT


# ---- verify_article 순서 검사 ----


def _detail_from(payload, reorder=None):
    """라이브 GET 응답 흉내: 댓글을 평탄 배열로."""
    flat = flatten_comment_nodes(payload)
    if reorder is not None:
        flat = [flat[i] for i in reorder]
    return {
        "naver_cafe_article_source": {"title": "T", "tag_list": []},
        "naver_cafe_article_destination": {"menu_id": 1, "head_id": None},
        "naver_cafe_article_source_comments": [
            {"contents": n["contents"], "comments": []} for n in flat
        ],
    }


def _verify(detail, payload, **kw):
    from v2r.api.articles import count_comment_nodes

    return verify_article(
        detail,
        title="T",
        tags=[],
        menu_id=1,
        head_id=None,
        body_lines=[],
        image_count=0,
        start_at=None,
        comments_count=count_comment_nodes(payload),
        **kw,
    )


def test_verify_article_순서_일치하면_통과():
    _, payload = build_payload("질문형")
    detail = _detail_from(payload)
    problems = _verify(
        detail, payload, expected_comment_sequence=cm.payload_sequence(payload)
    )
    assert [p for p in problems if "댓글" in p] == []


def test_verify_article_개수는_같아도_순서가_다르면_잡는다():
    _, payload = build_payload("질문형")
    order = list(range(12))
    order[4], order[5] = order[5], order[4]  # 대대댓글2 ↔ 대대대댓글2
    detail = _detail_from(payload, reorder=order)
    problems = _verify(detail, payload)
    assert not any("순서" in p for p in problems)  # 기대 순서를 안 주면 통과(= 옛 동작)
    problems = _verify(
        detail, payload, expected_comment_sequence=cm.payload_sequence(payload)
    )
    assert any("댓글 순서 불일치 #4" in p for p in problems)
