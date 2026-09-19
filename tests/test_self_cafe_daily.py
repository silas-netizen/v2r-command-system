"""자사 카페 일상 글(카페별 발행 + 랜덤 댓글) 테스트.

기준: docs/reference/self-cafe-daily-rules.md. 실제 API·모델은 부르지 않는다.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from v2r.api.catalog import Cafe, CafeAccount, Menu
from v2r.command.parser import parse_korean_command
from v2r.command.spec import TaskSpec
from v2r.content import daily_comments as dc
from v2r.content.manuscript import Manuscript
from v2r.engine import publish as publish_mod
from v2r.engine import worker
from v2r.store.db import KST

from tests.test_engine import make_runtime

CAFES_CFG = {
    "default_board": "자유게시판",
    "self_owned": [
        {"name": "고요한 아침", "cafe_id": 1, "board": "반말일기"},
        {"name": "글로시 마이", "cafe_id": 2, "board": "자유 톡"},
        {"name": "웨딩 노트", "cafe_id": 3, "board": "토크 수다", "excluded": True},
    ],
}


# --------------------------------------------------------------------
# 1. 명령 해석
# --------------------------------------------------------------------
def test_parse_self_daily_command():
    spec = parse_korean_command("자사 카페 일상 글 카페별 50건 실제 발행 댓글 랜덤")
    assert spec is not None
    assert spec.task == "publish_daily"
    assert spec.count == 50
    assert spec.per_cafe is True
    assert spec.random_comments is True
    assert spec.dry_run is False
    assert spec.immediate is True
    # 간격을 적지 않으면 기본 2~3분 (규칙 §4)
    assert (spec.interval_min, spec.interval_max) == (2, 3)
    assert spec.cafe == ""


def test_parse_self_daily_with_interval_override():
    spec = parse_korean_command("자사 카페 일상 글 카페마다 10건 실제 발행 2~5분 간격 댓글 0~3개")
    assert spec.count == 10
    assert spec.per_cafe is True
    assert spec.random_comments is True
    assert (spec.interval_min, spec.interval_max) == (2, 5)


def test_parse_self_daily_dry_run_default():
    spec = parse_korean_command("자사 카페 일상 글 카페별 3건 댓글 랜덤")
    assert spec.dry_run is True
    assert spec.per_cafe is True


def test_parse_plain_daily_keeps_defaults():
    spec = parse_korean_command("고요한 아침 일상 글 2개 올려줘")
    assert spec.per_cafe is False
    assert spec.random_comments is False
    assert (spec.interval_min, spec.interval_max) == (2, 3)  # 일상 글은 항상 즉시·2~3분


# --------------------------------------------------------------------
# 2. 카페별 원고 선택
# --------------------------------------------------------------------
def _m(cafe: str, n: int, source: str = "각색_전체_1") -> Manuscript:
    from v2r.content.manuscript import content_hash

    title, body = f"{cafe} 글 {n}", f"{cafe} 본문 {n}"
    return Manuscript(
        title=title,
        body=body,
        cafe=cafe,
        source=source,
        source_row=n + 1,
        images_enabled=False,
        content_hash=content_hash(title, body),
    )


def _fake_sources(monkeypatch, entries: list[tuple[str, list[Manuscript]]]) -> None:
    monkeypatch.setattr(
        publish_mod,
        "select_source_entries",
        lambda rt, spec: [{"name": name, "kind": "xlsx_daily"} for name, _ in entries],
    )
    by_name = {name: items for name, items in entries}
    monkeypatch.setattr(
        publish_mod,
        "load_manuscripts",
        lambda rt, entry, prefer_cache=False: list(by_name[entry["name"]]),
    )


def test_prepare_per_cafe_fans_out_and_skips_excluded(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = CAFES_CFG
    items = [_m("고요한 아침", i) for i in range(3)] + [_m("글로시 마이", i + 10) for i in range(3)]
    items += [_m("웨딩 노트", 90)]
    _fake_sources(monkeypatch, [("각색_전체_1", items)])

    spec = TaskSpec(task="publish_daily", count=2, per_cafe=True)
    picked = publish_mod.prepare_per_cafe(rt, spec, [])
    assert [m.cafe for m in picked] == ["고요한 아침"] * 2 + ["글로시 마이"] * 2


def test_prepare_per_cafe_skips_global_hash_duplicate(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = {"self_owned": [{"name": "고요한 아침", "cafe_id": 1, "board": "반말일기"}]}
    first = _m("고요한 아침", 0, source="각색_전체_1")
    # 다른 파일·다른 행이지만 본문이 같다 → 전역 해시 검사로 걸러야 한다
    same = _m("고요한 아침", 0, source="각색_전체_2")
    same.source_row = 77
    other = _m("고요한 아침", 5, source="각색_전체_2")
    rt.publications.mark(first.source, first.source_row, first.content_hash, "done", "done")
    _fake_sources(monkeypatch, [("각색_전체_1", [first]), ("각색_전체_2", [same, other])])

    skipped: list[dict] = []
    spec = TaskSpec(task="publish_daily", count=5, per_cafe=True)
    picked = publish_mod.prepare_per_cafe(rt, spec, skipped)
    assert [m.source_row for m in picked] == [other.source_row]
    reasons = {s["reason"] for s in skipped}
    assert "중복(본문 해시 전역)" in reasons or "이미 발행됨" in reasons


def test_prepare_manuscripts_routes_to_per_cafe(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = CAFES_CFG
    _fake_sources(monkeypatch, [("각색_전체_1", [_m("고요한 아침", 0)])])
    spec = TaskSpec(task="publish_daily", count=1, per_cafe=True)
    assert len(publish_mod.prepare_manuscripts(rt, spec, [])) == 1


def test_self_cafe_names_excludes_excluded(tmp_path):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = CAFES_CFG
    assert publish_mod.self_cafe_names(rt) == ["고요한 아침", "글로시 마이"]


# --------------------------------------------------------------------
# 3. 계획: 전부 즉시 + 계정 고정/연속 금지
# --------------------------------------------------------------------
class _Acc:
    def __init__(self, login_id: str) -> None:
        self.login_id = login_id
        self.work_type = ""


def test_plan_is_always_immediate_and_rotates_accounts(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = {"self_owned": [{"name": "고요한 아침", "cafe_id": 1, "board": "반말일기"}]}
    pool = [_Acc(f"user{i}") for i in range(12)]
    monkeypatch.setattr(publish_mod, "load_accounts", lambda rt, prefer_cache=False: pool)
    monkeypatch.setattr(publish_mod, "eligible", lambda *a, **k: list(pool))
    monkeypatch.setattr(publish_mod, "_pool_for_cafe", lambda rt, spec, c, b, p: (list(p), False))

    spec = TaskSpec(
        task="publish_daily", count=6, per_cafe=True, immediate=True, dry_run=False,
        interval_min=2, interval_max=3,
    )
    slots = publish_mod.plan(rt, spec, [_m("고요한 아침", i) for i in range(6)])
    assert all(s.scheduled_at is None for s in slots)  # 예약하지 않는다
    used = [s.account for s in slots]
    assert len(set(used)) <= publish_mod.SELF_DAILY_ACCOUNTS_MAX
    assert len(set(used)) >= min(publish_mod.SELF_DAILY_ACCOUNTS_MIN, len(pool))
    assert all(a != b for a, b in zip(used, used[1:]))  # 같은 계정 연속 금지


def test_avoid_consecutive_swaps_repeat():
    cafes = ["A", "A", "A"]
    assigned = {0: "u1", 1: "u1", 2: "u2"}
    publish_mod._avoid_consecutive(cafes, assigned, {"a": ["u1", "u2"]})
    assert assigned[0] != assigned[1]


# --------------------------------------------------------------------
# 4. 랜덤 댓글
# --------------------------------------------------------------------
def test_draw_count_weights_match_rules():
    rng = random.Random(7)
    draws = [dc.draw_count(rng) for _ in range(4000)]
    share = {n: draws.count(n) / len(draws) for n in (0, 1, 2, 3)}
    assert share[0] == pytest.approx(0.50, abs=0.04)
    assert share[1] == pytest.approx(0.25, abs=0.04)
    assert share[2] == pytest.approx(0.15, abs=0.04)
    assert share[3] == pytest.approx(0.10, abs=0.04)


def test_plan_times_strictly_increasing():
    start = datetime(2026, 9, 19, 10, 0, tzinfo=KST)
    for seed in range(50):
        times = dc.plan_times(start, 3, random.Random(seed))
        assert len(times) == 3
        assert times[0] >= start + timedelta(minutes=3)
        assert times[0] < times[1] < times[2]


def test_clean_text_rules():
    assert dc.clean_text("저도 그랬어요 진짜 공감돼요") == "저도 그랬어요 진짜 공감돼요"
    assert dc.clean_text("맞아요 저도 그래요ㅋㅋ.") == "맞아요 저도 그래요ㅋㅋ"
    assert dc.clean_text("짧음") == ""  # 10자 미만
    assert dc.clean_text("가" * 40) == ""  # 30자 초과
    assert dc.clean_text("여기 좋아요 https://example.com 보세요") == ""


class _FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def complete(self, purpose, system, user, max_tokens=1200):
        self.calls.append({"purpose": purpose, "user": user})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_generate_texts_validates_and_trims():
    llm = _FakeLLM('["저도 어제 딱 그랬어요ㅋㅋ", "짧", "오늘 날씨 진짜 좋더라구요"]')
    out = dc.generate_texts(llm, "제목", "본문", 3)
    assert out == ["저도 어제 딱 그랬어요ㅋㅋ", "오늘 날씨 진짜 좋더라구요"]
    assert llm.calls[0]["purpose"] == "daily_comment"


def test_generate_texts_failure_returns_empty_and_warns():
    warned: list[str] = []
    llm = _FakeLLM(RuntimeError("모델 오류"))
    assert dc.generate_texts(llm, "t", "b", 2, on_warn=warned.append) == []
    assert warned


def test_generate_texts_zero_count_makes_no_call():
    llm = _FakeLLM("[]")
    assert dc.generate_texts(llm, "t", "b", 0) == []
    assert llm.calls == []


def _self_comment_accounts():
    from v2r.accounts.loader import Account

    return [Account(login_id=f"member{i}", work_type="자사 댓글", linked="V2R") for i in range(5)]


class _CommentCatalog:
    def __init__(self) -> None:
        self.cafe = Cafe(cafe_id=1, name="고요한 아침")
        self.menu = Menu(menu_id=101, name="반말일기")

    def resolve(self, cafe_name, board_name, login_id):
        return self.cafe, self.menu, None

    def cafe_accounts(self, cafe_id):
        return [
            CafeAccount(login_id=f"member{i}", member_key=f"k{i}", nick=f"닉{i}", level_name="카페 스탭")
            for i in range(5)
        ]

    def cafes(self):
        return [self.cafe]

    def menus(self, cafe_id, login_ids):
        return [self.menu]


def test_build_daily_comments_roots_only(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt._catalog = _CommentCatalog()
    rt._llm = _FakeLLM('["저도 어제 딱 그랬어요ㅋㅋ", "오늘 날씨 진짜 좋더라구요", "저녁은 뭐 드셨어요"]')
    slot = publish_mod.Slot(
        manuscript=_m("고요한 아침", 0),
        account="member0",
        cafe="고요한 아침",
        board="반말일기",
    )
    start = datetime(2026, 9, 19, 10, 0, tzinfo=KST)

    class _Rng(random.Random):
        def choices(self, population, weights=None, k=1):
            return [3]  # 댓글 3개로 고정

    payload = publish_mod.build_daily_comments(rt, slot, start, 1, rng=_Rng(3))
    assert len(payload) == 3
    assert all(node["comments"] == [] for node in payload)  # 루트 댓글만
    authors = [node["naver_login_id"] for node in payload]
    assert "member0" not in authors  # 글쓴이 제외
    assert len(set(authors)) == 3  # 서로 다른 계정


def test_build_daily_comments_zero_makes_no_llm_call(tmp_path):
    rt = make_runtime(tmp_path)
    rt._catalog = _CommentCatalog()
    llm = _FakeLLM("[]")
    rt._llm = llm
    slot = publish_mod.Slot(
        manuscript=_m("고요한 아침", 0), account="member0", cafe="고요한 아침", board="반말일기"
    )

    class _Zero(random.Random):
        def choices(self, population, weights=None, k=1):
            return [0]

    assert publish_mod.build_daily_comments(
        rt, slot, datetime.now(KST), 1, rng=_Zero(1)
    ) == []
    assert llm.calls == []


def test_dry_run_shows_comment_count_without_llm(tmp_path):
    rt = make_runtime(tmp_path)
    rt._catalog = _CommentCatalog()
    rt._llm = _FakeLLM(RuntimeError("모의 실행에서는 부르면 안 된다"))
    spec = TaskSpec(task="publish_daily", per_cafe=True, random_comments=True, dry_run=True)
    slot = publish_mod.Slot(
        manuscript=_m("고요한 아침", 0), account="member0", cafe="고요한 아침", board="반말일기"
    )
    out = publish_mod.run_slot(rt, spec, slot)
    assert out["status"] == "planned"
    assert 0 <= out["comments"] <= 3
    assert "comment_roles" not in out


# --------------------------------------------------------------------
# 5. 진행·보고
# --------------------------------------------------------------------
def test_order_round_robin_interleaves_cafes():
    slots = [
        publish_mod.Slot(manuscript=_m("고요한 아침", i), account="u", cafe="고요한 아침", board="b")
        for i in range(3)
    ] + [
        publish_mod.Slot(manuscript=_m("글로시 마이", i), account="u", cafe="글로시 마이", board="b")
        for i in range(2)
    ]
    order = [s.cafe for s in worker.order_round_robin(slots)]
    assert order == ["고요한 아침", "글로시 마이", "고요한 아침", "글로시 마이", "고요한 아침"]


def test_per_cafe_counts_and_report_file(tmp_path):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    spec = TaskSpec(task="publish_daily", per_cafe=True, dry_run=False, start_date="2026-09-19")
    results = [
        {
            "cafe": "고요한 아침",
            "board": "반말일기",
            "account": "member0",
            "scheduled_at": "즉시",
            "comments": 2,
            "url": "https://example.com/1",
            "status": "done",
        }
    ]
    failed = [
        (
            publish_mod.Slot(
                manuscript=_m("글로시 마이", 1), account="member1", cafe="글로시 마이", board="자유 톡"
            ),
            "발행 실패(network)",
        )
    ]
    counts = worker.per_cafe_counts(results, failed)
    assert counts == {"고요한 아침": {"ok": 1, "fail": 0}, "글로시 마이": {"ok": 0, "fail": 1}}

    path = worker.write_daily_report(rt, spec, results, failed)
    assert path.endswith("self-daily-2026-09-19.md")
    text = (tmp_path / "docs" / "reports" / "self-daily-2026-09-19.md").read_text(encoding="utf-8")
    assert "고요한 아침" in text and "https://example.com/1" in text
    assert "mem…" in text  # 계정은 가려서 적는다
    assert "실패: 발행 실패(network)" in text


def test_sleep_with_beat_chunks_and_heartbeats():
    slept: list[float] = []
    beats: list[int] = []
    worker._sleep_with_beat(70.0, lambda: beats.append(1), sleep=slept.append)
    assert sum(slept) == pytest.approx(70.0)
    assert max(slept) <= worker.SLEEP_CHUNK_S
    assert len(beats) == len(slept)


# --------------------------------------------------------------------
# 계정 구분: 자사 카페는 스탭 등급만, 댓글은 '자사 댓글' 구분만 (2026-09-19)
# --------------------------------------------------------------------
def test_is_staff_level_words():
    from v2r.accounts.rules import is_staff_level

    assert is_staff_level("카페 스탭")
    assert is_staff_level("스텝")
    assert not is_staff_level("새싹멤버")
    assert not is_staff_level("")
    assert not is_staff_level(None)


def test_self_cafe_members_only_staff(monkeypatch):
    from types import SimpleNamespace as NS

    from v2r.engine import publish

    cafe = NS(name="고요한 아침", cafe_id=1)
    members = [NS(login_id="a", level_name="카페 스탭"), NS(login_id="b", level_name="새싹멤버")]
    rt = NS(
        scratch={},
        cafes_cfg={"self_owned": [{"name": "고요한 아침"}], "affiliate": []},
        catalog=NS(cafes=lambda: [cafe], cafe_accounts=lambda cid: members),
    )
    _, logins = publish._cafe_members(rt, "고요한 아침")
    assert logins == ["a"]


def test_affiliate_cafe_members_not_filtered_by_staff():
    from types import SimpleNamespace as NS

    from v2r.engine import publish

    cafe = NS(name="씨씨앙", cafe_id=2)
    members = [NS(login_id="a", level_name="카페 스탭"), NS(login_id="b", level_name="새싹")]
    rt = NS(
        scratch={},
        cafes_cfg={"self_owned": [], "affiliate": [{"name": "씨씨앙"}]},
        catalog=NS(cafes=lambda: [cafe], cafe_accounts=lambda cid: members),
    )
    _, logins = publish._cafe_members(rt, "씨씨앙")
    assert logins == ["a", "b"]
