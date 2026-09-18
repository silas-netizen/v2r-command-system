"""예약 수정글 댓글 역할 복구(`repair_comments`) 테스트. 네트워크는 쓰지 않는다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from v2r.api import articles as api_articles
from v2r.command.parser import describe_spec, parse_korean_command
from v2r.command.spec import TaskSpec
from v2r.content import comments as comment_mod
from v2r.engine import repair as repair_mod
from v2r.engine import worker

from tests.test_engine import make_runtime

SOURCE_ID = "01M2TT7K8ECC6T7WV856PRVJGY"
PARENT_ID = "01M2TT7F4KEGX3K9VPF1G6VQF9"
AUTHOR = "molitan"
CAFE_ID = 25016228
#: 항상 미래로 두기 위해 지금을 기준으로 잡는다
BASE = datetime(2026, 9, 19, 4, 12, tzinfo=timezone.utc)

POOL = ["quilliant", "hunnede", "prtchht", "chocobbn", "chenallo", "colpith"]

#: 잘못된 상태: 댓글2 스레드가 전부 작성자
WRONG_ACCOUNTS = [
    "chenallo", AUTHOR,
    "hunnede", AUTHOR, AUTHOR, AUTHOR,
    "quilliant", AUTHOR,
    "prtchht", AUTHOR,
    "chocobbn", AUTHOR,
]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _live_comments(base: datetime = BASE) -> list[dict]:
    out = []
    for i, node in enumerate(comment_mod.DEFAULT_TREE):
        label = node["label"]
        account = WRONG_ACCOUNTS[i]
        rm = (
            {"member_key": "K", "naver_login_id": AUTHOR, "nick": "n"}
            if (node["depth"] or 0) >= 2
            else None
        )
        out.append(
            {
                "comment_id": f"C{i:02d}",
                "contents": f"{label} 본문 내용 {i}",
                "naver_login_id": account,
                "start_at": _iso(base + timedelta(minutes=comment_mod.OFFSETS_MIN[label])),
                "status": "RESERVED",
                "reply_member": rm,
                "comments": [],
            }
        )
    return out


BODY = (
    '{"document":{"version":"2.9.0","components":[{"id":"SE-1","@ctype":"text",'
    '"value":[{"id":"SE-2","@ctype":"paragraph","nodes":[{"id":"SE-3",'
    '"value":"\\ubcf8\\ubb38 \\ud55c \\uc904","@ctype":"textNode"}]}]}]}}'
)


def _detail(
    base: datetime = BASE,
    status: str = "RESERVED",
    comments: list[dict] | None = None,
    parent: str | None = PARENT_ID,
) -> dict:
    return {
        "naver_cafe_article_source": {
            "source_id": SOURCE_ID,
            "parent_source_id": parent,
            "child_source_id": None,
            "title": "공복혈당수치 다이어트랑 관련있나요",
            "tag_list": ["공복혈당수치"],
        },
        "naver_cafe_article_destination": {
            "cafe_id": CAFE_ID,
            "cafe_name": "씨씨앙",
            "menu_id": 328,
            "menu_name": "자유 수다방",
            "head_id": None,
            "head_name": None,
            "naver_login_id": AUTHOR,
            "start_at": _iso(base),
            "target_view_count": 82,
            "status": status,
            "use_comment_ai": True,
            "parent_id": None,
            "write_options": dict(api_articles.DEFAULT_WRITE_OPTIONS),
        },
        "naver_cafe_article_history": None,
        "naver_cafe_article_source_detail": {"body": BODY, "content_html": ""},
        "naver_cafe_article_source_comments": comments
        if comments is not None
        else _live_comments(base),
    }


class FakeCatalog:
    def cafe_accounts(self, cafe_id):
        from v2r.api.catalog import CafeAccount

        return [
            CafeAccount(login_id=a, member_key=f"MK-{a}", nick=f"닉{a}")
            for a in POOL + [AUTHOR]
        ]


def _runtime(tmp_path, monkeypatch, detail_fn=None, base: datetime = BASE):
    rt = make_runtime(tmp_path)
    rt._catalog = FakeCatalog()
    rt.cafes_cfg = {**rt.cafes_cfg, "comment_accounts": {"affiliate": list(POOL)}}
    monkeypatch.setattr(
        repair_mod.api_articles,
        "get_article",
        detail_fn or (lambda client, sid: _detail(base)),
    )
    return rt


def _now(base: datetime = BASE) -> datetime:
    return base - timedelta(hours=1)


# --- 파서 -----------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "댓글 틀린 것들 댓글 삭제 후 다시 세팅 할 것",
        "댓글 재설정 해줘",
        "댓글 복구해줘",
    ],
)
def test_댓글복구_명령을_알아듣는다(text):
    spec = parse_korean_command(text)
    assert spec is not None and spec.task == "repair_comments"
    assert spec.dry_run is True
    assert "댓글 복구" in describe_spec(spec)


def test_실제_발행_문구가_있어야_진짜_고친다():
    spec = parse_korean_command("댓글 다시 세팅 실제 발행")
    assert spec.task == "repair_comments" and spec.dry_run is False


def test_명령문에서_source_id를_뽑는다():
    text = f"댓글 재설정 {SOURCE_ID} 와 {PARENT_ID} 그리고 {SOURCE_ID}"
    assert repair_mod.source_ids_in(text) == [SOURCE_ID, PARENT_ID]
    assert repair_mod.source_ids_in("댓글 재설정") == []


# --- 재구성 ---------------------------------------------------------
def test_모의실행은_본문과_시각을_보존한다(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    out = repair_mod.repair_revision_comments(rt, SOURCE_ID, now=_now())
    assert out["ok"] and out["dry_run"] is True
    before, after = out["before"], out["after"]
    assert [r["start_at"] for r in after] == [r["start_at"] for r in before]
    assert [r["label"] for r in after] == [n["label"] for n in comment_mod.DEFAULT_TREE]
    # 계정만 바뀐다: 댓글2 스레드가 더 이상 작성자 일색이 아니다
    assert after[4]["account"] != AUTHOR and after[5]["account"] != AUTHOR
    rt.close()


def test_질문형_역할_배정(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    state = repair_mod.capture(rt, SOURCE_ID)
    items, payload = repair_mod.rebuild_comments(rt, state, "질문형")
    assert repair_mod.role_problems(items, AUTHOR, "질문형") == []
    by = {it["label"]: it for it in items}
    assert by["대대댓글2"]["account"] == by["댓글2"]["account"]
    roots = {by[f"댓글{i}"]["account"] for i in range(1, 6)}
    assert len(roots) == 5 and AUTHOR not in roots
    assert by["대대대댓글2"]["account"] not in roots | {AUTHOR}
    # 본문은 그대로, 페이로드는 루트 5개 / 전체 12노드
    assert [it["text"] for it in items] == [
        c["contents"] for c in state["comments"]
    ]
    assert len(payload) == 5
    assert api_articles.count_comment_nodes(payload) == 12
    rt.close()


def test_후기형_역할_배정(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    state = repair_mod.capture(rt, SOURCE_ID)
    items, _ = repair_mod.rebuild_comments(rt, state, "후기형")
    assert repair_mod.role_problems(items, AUTHOR, "후기형") == []
    by = {it["label"]: it for it in items}
    roots = {by[f"댓글{i}"]["account"] for i in range(1, 6)}
    assert by["대대대댓글2"]["account"] == AUTHOR
    assert by["대대댓글2"]["account"] not in roots | {AUTHOR}
    rt.close()


def test_reply_member는_바로_위_노드의_계정(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    state = repair_mod.capture(rt, SOURCE_ID)
    items, payload = repair_mod.rebuild_comments(rt, state, "질문형")
    flat = comment_mod.flatten_payload(payload)
    by = {it["label"]: it for it in items}
    assert flat[4]["reply_member"]["naver_login_id"] == by["대댓글2"]["account"]
    assert flat[5]["reply_member"]["naver_login_id"] == by["대대댓글2"]["account"]
    assert flat[0]["reply_member"] is None
    rt.close()


# --- 중단 조건 -------------------------------------------------------
def test_예약상태가_아니면_중단(tmp_path, monkeypatch):
    rt = _runtime(
        tmp_path, monkeypatch, detail_fn=lambda c, s: _detail(status="DONE")
    )
    out = repair_mod.repair_revision_comments(rt, SOURCE_ID, now=_now())
    assert out["ok"] is False and "RESERVED" in out["aborted"]
    rt.close()


def test_이미_지난_댓글이_있으면_중단(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    later = BASE + timedelta(hours=2)  # 기준 시각을 댓글 뒤로 옮긴다
    out = repair_mod.repair_revision_comments(rt, SOURCE_ID, now=later)
    assert out["ok"] is False and "지난 댓글" in out["aborted"]
    rt.close()


def test_수정글이_아니면_중단(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, detail_fn=lambda c, s: _detail(parent=None))
    out = repair_mod.repair_revision_comments(rt, SOURCE_ID, now=_now())
    assert out["ok"] is False and "수정글이 아닙니다" in out["aborted"]
    rt.close()


def test_댓글이_12개가_아니면_중단(tmp_path, monkeypatch):
    rt = _runtime(
        tmp_path,
        monkeypatch,
        detail_fn=lambda c, s: _detail(comments=_live_comments()[:5]),
    )
    out = repair_mod.repair_revision_comments(rt, SOURCE_ID, now=_now())
    assert out["ok"] is False and "12개짜리" in out["aborted"]
    rt.close()


# --- 실제 실행 -------------------------------------------------------
def _fixed_detail(payload: list[dict], base: datetime = BASE) -> dict:
    """새로 등록된 글의 GET 응답 대역: 보낸 페이로드를 평탄하게 돌려준다."""
    flat = comment_mod.flatten_payload(payload)
    live = []
    for node in flat:
        live.append({**node, "status": "RESERVED", "comments": []})
    return _detail(base, comments=live)


def test_실제실행은_삭제후_재등록하고_검증한다(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    rt.publications.mark(
        "팥순이", 3, "h3", "done", "done", source_id=SOURCE_ID, account=AUTHOR
    )
    calls: list[tuple] = []

    def fake_delete(client, sid):
        calls.append(("delete", sid))
        return {"ok": True}

    def fake_create(client, **kw):
        calls.append(("create", kw))
        return "NEW-SOURCE-ID"

    def fake_get(client, sid):
        calls.append(("get", sid))
        if sid == "NEW-SOURCE-ID":
            return _fixed_detail(calls[-2][1]["comments"])
        return _detail()

    monkeypatch.setattr(repair_mod.api_articles, "delete_article", fake_delete)
    monkeypatch.setattr(repair_mod.api_articles, "create_article", fake_create)
    monkeypatch.setattr(repair_mod.api_articles, "get_article", fake_get)

    out = repair_mod.repair_revision_comments(
        rt, SOURCE_ID, dry_run=False, now=_now()
    )
    kinds = [c[0] for c in calls]
    assert kinds == ["get", "delete", "create", "get"]  # 삭제 → 등록 → 검증
    assert out["ok"] and out["problems"] == []
    assert out["new_source_id"] == "NEW-SOURCE-ID"

    kw = next(c[1] for c in calls if c[0] == "create")
    assert kw["title"] == "공복혈당수치 다이어트랑 관련있나요"
    assert kw["tags"] == ["공복혈당수치"]
    assert kw["content_json"] == BODY
    assert kw["parent_source_id"] == PARENT_ID
    assert kw["destination"]["start_at"] == _iso(BASE)
    assert kw["destination"]["target_view_count"] == 82
    assert kw["destination"]["menu_id"] == 328

    pub = rt.publications.by_source_id("NEW-SOURCE-ID")
    assert pub and pub["stage"] == repair_mod.REPAIRED_STAGE
    assert pub["url"].endswith("NEW-SOURCE-ID")
    rt.close()


def test_라이브_역할이_계획과_다르면_실패(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)

    def fake_get(client, sid):
        if sid == "NEW-SOURCE-ID":
            return _detail()  # 서버가 예전(잘못된) 모양 그대로 돌려준 셈
        return _detail()

    monkeypatch.setattr(repair_mod.api_articles, "delete_article", lambda c, s: {})
    monkeypatch.setattr(
        repair_mod.api_articles, "create_article", lambda c, **kw: "NEW-SOURCE-ID"
    )
    monkeypatch.setattr(repair_mod.api_articles, "get_article", fake_get)

    with pytest.raises(repair_mod.RepairError):
        repair_mod.repair_revision_comments(rt, SOURCE_ID, dry_run=False, now=_now())
    rt.close()


def test_live_role_problems는_계정과_reply_member를_본다():
    items = [
        {"label": "댓글2", "depth": 0, "parent": None, "account": "hunnede"},
        {"label": "대댓글2", "depth": 1, "parent": "댓글2", "account": AUTHOR},
        {"label": "대대댓글2", "depth": 2, "parent": "대댓글2", "account": "hunnede"},
    ]
    good = [
        {"naver_login_id": "hunnede", "reply_member": None},
        {"naver_login_id": AUTHOR, "reply_member": None},
        {"naver_login_id": "hunnede", "reply_member": {"naver_login_id": AUTHOR}},
    ]
    assert repair_mod.live_role_problems(good, items) == []
    bad = [dict(g) for g in good]
    bad[2]["reply_member"] = {"naver_login_id": "hunnede"}
    assert repair_mod.live_role_problems(bad, items)


# --- 작업 연결 -------------------------------------------------------
def test_worker_dispatch로_연결된다(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        repair_mod, "repair_revision_comments",
        lambda rt_, sid, dry_run=True, job_id=None: {"ok": True, "source_id": sid},
    )
    spec = TaskSpec(task="repair_comments", notes=f"댓글 재설정 {SOURCE_ID}")
    out = worker.dispatch(rt, {"id": 1, "spec_json": spec.to_json()})
    assert out["ok"] and [r["source_id"] for r in out["results"]] == [SOURCE_ID]
    rt.close()


def test_source_id가_없으면_안내만_한다(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    out = repair_mod.repair_comments(rt, TaskSpec(task="repair_comments", notes="댓글 재설정"))
    assert out["ok"] is False and "source_id" in out["report"]
    rt.close()
