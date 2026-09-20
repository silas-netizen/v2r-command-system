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
    # 간격을 적지 않으면 기본 2~5분 (규칙 §4 — 카페별 간격)
    assert (spec.interval_min, spec.interval_max) == (2, 5)
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
    assert (spec.interval_min, spec.interval_max) == (2, 5)  # 일상 글은 항상 즉시·카페별 2~5분


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


# --- 각색 엑셀 행 순서 그대로 (사용자 절대 규칙, 규칙 §2) ---
TWO_CAFES = {
    "self_owned": [
        {"name": "고요한 아침", "cafe_id": 1, "board": "반말일기"},
        {"name": "글로시 마이", "cafe_id": 2, "board": "자유 톡"},
    ]
}


def _interleaved(source: str, cafes: list[str], start: int = 0) -> list[Manuscript]:
    """카페가 행마다 번갈아 오는 각색 엑셀 한 장."""
    return [_m(cafe, start + i, source=source) for i, cafe in enumerate(cafes)]


def test_prepare_per_cafe_keeps_sheet_order_across_files(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = TWO_CAFES
    first = _interleaved("각색_0901", ["고요한 아침", "글로시 마이", "고요한 아침"])
    second = _interleaved("각색_0902", ["글로시 마이", "고요한 아침", "글로시 마이"], start=50)
    _fake_sources(monkeypatch, [("각색_0901", first), ("각색_0902", second)])

    spec = TaskSpec(task="publish_daily", count=3, per_cafe=True)
    picked = publish_mod.prepare_per_cafe(rt, spec, [])
    # 파일 순서 → 행 순서 그대로 한 줄. 카페별로 묶지 않는다.
    assert [(m.source, m.source_row) for m in picked] == [
        ("각색_0901", 1), ("각색_0901", 2), ("각색_0901", 3),
        ("각색_0902", 51), ("각색_0902", 52), ("각색_0902", 53),
    ]
    assert [m.cafe for m in picked] == [
        "고요한 아침", "글로시 마이", "고요한 아침",
        "글로시 마이", "고요한 아침", "글로시 마이",
    ]
    seq = rt.scratch["per_cafe_sequence"]
    assert [s["seq"] for s in seq] == [1, 2, 3, 4, 5, 6]
    assert seq[3] == {
        "seq": 4, "source": "각색_0902", "row": 51, "cafe": "글로시 마이",
        "title": picked[3].title,
    }


def test_prepare_per_cafe_published_rows_skipped_without_breaking_order(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = TWO_CAFES
    items = _interleaved(
        "각색_0901",
        ["고요한 아침", "글로시 마이", "고요한 아침", "글로시 마이", "고요한 아침", "글로시 마이"],
    )
    # 2행(글로시)·3행(고요)은 이미 올렸다 → 건너뛰되 나머지 순서는 그대로
    for m in (items[1], items[2]):
        rt.publications.mark(m.source, m.source_row, m.content_hash, "done", "done")
    _fake_sources(monkeypatch, [("각색_0901", items)])

    skipped: list[dict] = []
    spec = TaskSpec(task="publish_daily", count=5, per_cafe=True)
    picked = publish_mod.prepare_per_cafe(rt, spec, skipped)
    assert [m.source_row for m in picked] == [1, 4, 5, 6]
    assert [m.cafe for m in picked] == ["고요한 아침", "글로시 마이", "고요한 아침", "글로시 마이"]
    assert {s["row"] for s in skipped if s["reason"] == "이미 발행됨"} == {2, 3}


def test_prepare_per_cafe_excluded_cafe_rows_skipped_in_place(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = CAFES_CFG  # 웨딩 노트가 excluded
    items = _interleaved(
        "각색_0901", ["고요한 아침", "웨딩 노트", "글로시 마이", "웨딩 노트", "고요한 아침"]
    )
    _fake_sources(monkeypatch, [("각색_0901", items)])

    skipped: list[dict] = []
    spec = TaskSpec(task="publish_daily", count=5, per_cafe=True)
    picked = publish_mod.prepare_per_cafe(rt, spec, skipped)
    assert [m.cafe for m in picked] == ["고요한 아침", "글로시 마이", "고요한 아침"]
    assert [m.source_row for m in picked] == [1, 3, 5]
    assert {s["row"] for s in skipped if s["reason"] == "발행 제외 카페"} == {2, 4}


def test_prepare_per_cafe_target_met_cafe_skipped_sequence_continues(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = TWO_CAFES
    _mark_today(rt, "고요한 아침", 1)  # 목표 2건 중 1건 이미 → 1건만 더
    items = _interleaved(
        "각색_0901",
        ["고요한 아침", "글로시 마이", "고요한 아침", "글로시 마이", "고요한 아침", "글로시 마이"],
    )
    _fake_sources(monkeypatch, [("각색_0901", items)])

    skipped: list[dict] = []
    spec = TaskSpec(task="publish_daily", count=2, per_cafe=True)
    picked = publish_mod.prepare_per_cafe(rt, spec, skipped)
    # 고요한 아침은 1행으로 목표 달성 → 3·5행은 건너뛰고 글로시 순서는 계속 간다
    assert [(m.source_row, m.cafe) for m in picked] == [
        (1, "고요한 아침"), (2, "글로시 마이"), (4, "글로시 마이")
    ]
    assert any("목표 달성" in s["reason"] and s.get("row") == 3 for s in skipped)
    assert rt.scratch["per_cafe_plan"]["고요한 아침"]["planned"] == 1


def test_prepare_per_cafe_uses_daily_pool_only_after_xlsx(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = TWO_CAFES
    xlsx = _interleaved("각색_0901", ["고요한 아침", "글로시 마이"])
    pool = [_m("", 80, source="일상풀"), _m("", 81, source="일상풀")]
    for m in pool:
        m.cafe = ""  # 풀 글은 카페가 비어 있다
    _fake_sources(monkeypatch, [("각색_0901", xlsx), ("일상풀", pool)])

    spec = TaskSpec(task="publish_daily", count=2, per_cafe=True)
    picked = publish_mod.prepare_per_cafe(rt, spec, [])
    assert [m.source for m in picked] == ["각색_0901", "각색_0901", "일상풀", "일상풀"]
    assert sorted(m.cafe for m in picked[2:]) == ["고요한 아침", "글로시 마이"]


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


# --- 오늘 목표 빼기 (규칙 §7) ---
def _mark_today(rt, cafe: str, n: int, source: str = "각색_전체_9", status: str = "done") -> None:
    """오늘 날짜로 발행 기록 n건을 심는다."""
    for i in range(n):
        rt.publications.mark(source, 1000 + i, f"h{cafe}{i}", status, None, cafe=cafe)


def test_count_today_counts_only_daily_and_today(tmp_path):
    rt = make_runtime(tmp_path)
    rt.sources_cfg = {"brand_sheets": {"팥순이": {"spreadsheet_id": "x"}}}
    _mark_today(rt, "고요한 아침", 3)
    _mark_today(rt, "글로시 마이", 2)
    # 브랜드 시트 글은 일상 글이 아니다 → 세지 않는다
    rt.publications.mark("팥순이", 1, "hb", "done", None, cafe="고요한 아침")
    # 실패한 건도 세지 않는다
    rt.publications.mark("각색_전체_9", 2000, "hf", "failed", None, cafe="고요한 아침")
    # 어제 올린 건도 세지 않는다
    rt.publications.mark("각색_전체_9", 2001, "hy", "done", None, cafe="고요한 아침")
    rt.conn.execute(
        "UPDATE publications SET created_at = ? WHERE content_hash = ?",
        ("2000-01-01T09:00:00+09:00", "hy"),
    )

    assert publish_mod.count_today_for_cafe(rt, "고요한 아침") == 3
    assert publish_mod.count_today_for_cafe(rt, "글로시 마이") == 2
    assert publish_mod.count_today_for_cafe(rt, "없는 카페") == 0


def test_prepare_per_cafe_subtracts_today_count(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = {
        "self_owned": [
            {"name": "고요한 아침", "cafe_id": 1, "board": "반말일기"},
            {"name": "글로시 마이", "cafe_id": 2, "board": "자유 톡"},
        ]
    }
    _mark_today(rt, "고요한 아침", 4)  # 오늘 이미 4건 → 1건만 더
    items = [_m("고요한 아침", i) for i in range(6)] + [_m("글로시 마이", i + 10) for i in range(6)]
    _fake_sources(monkeypatch, [("각색_전체_1", items)])

    spec = TaskSpec(task="publish_daily", count=5, per_cafe=True)
    picked = publish_mod.prepare_per_cafe(rt, spec, [])
    assert [m.cafe for m in picked] == ["고요한 아침"] + ["글로시 마이"] * 5

    plan_info = rt.scratch["per_cafe_plan"]
    assert plan_info["고요한 아침"] == {"requested": 5, "already": 4, "planned": 1}
    assert plan_info["글로시 마이"] == {"requested": 5, "already": 0, "planned": 5}


def test_prepare_per_cafe_target_met_picks_nothing(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = {"self_owned": [{"name": "고요한 아침", "cafe_id": 1, "board": "반말일기"}]}
    _mark_today(rt, "고요한 아침", 7)  # 목표 5건을 이미 넘었다
    _fake_sources(monkeypatch, [("각색_전체_1", [_m("고요한 아침", i) for i in range(3)])])

    skipped: list[dict] = []
    spec = TaskSpec(task="publish_daily", count=5, per_cafe=True)
    assert publish_mod.prepare_per_cafe(rt, spec, skipped) == []
    assert any("목표 달성" in str(s.get("reason")) for s in skipped)
    assert rt.scratch["per_cafe_plan"]["고요한 아침"]["planned"] == 0


def test_prepare_per_cafe_add_mode_ignores_today(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = {"self_owned": [{"name": "고요한 아침", "cafe_id": 1, "board": "반말일기"}]}
    _mark_today(rt, "고요한 아침", 4)
    _fake_sources(monkeypatch, [("각색_전체_1", [_m("고요한 아침", i) for i in range(6)])])

    spec = TaskSpec(task="publish_daily", count=5, per_cafe=True, per_cafe_mode="추가로")
    picked = publish_mod.prepare_per_cafe(rt, spec, [])
    assert len(picked) == 5
    assert rt.scratch["per_cafe_plan"]["고요한 아침"] == {
        "requested": 5, "already": 0, "planned": 5
    }


def test_parse_per_cafe_add_mode():
    spec = parse_korean_command("자사 카페 일상 글 카페별 50건 추가로 실제 발행")
    assert spec.per_cafe is True
    assert spec.per_cafe_mode == "추가로"
    assert spec.count == 50
    assert parse_korean_command("자사 카페 일상 글 카페별 50건 실제 발행").per_cafe_mode == ""


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
    def __init__(self, login_id: str, work_type: str = "자사 카페") -> None:
        self.login_id = login_id
        self.work_type = work_type
        self.excluded = False


def _patch_members(monkeypatch, members_by_cafe: dict[str, list[str]]) -> None:
    """V2R 회원 조회 대신 카페별 스탭 계정 목록을 준다."""
    monkeypatch.setattr(
        publish_mod, "_cafe_members", lambda rt, cafe: (None, list(members_by_cafe.get(cafe, [])))
    )


def test_plan_is_always_immediate_and_rotates_accounts(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = {"self_owned": [{"name": "고요한 아침", "cafe_id": 1, "board": "반말일기"}]}
    pool = [_Acc(f"user{i}") for i in range(12)]
    monkeypatch.setattr(publish_mod, "load_accounts", lambda rt, prefer_cache=False: pool)
    monkeypatch.setattr(publish_mod, "eligible", lambda *a, **k: list(pool))
    monkeypatch.setattr(publish_mod, "_pool_for_cafe", lambda rt, spec, c, b, p: (list(p), False))

    spec = TaskSpec(
        task="publish_daily", count=14, per_cafe=True, immediate=True, dry_run=False,
        interval_min=2, interval_max=3,
    )
    slots = publish_mod.plan(rt, spec, [_m("고요한 아침", i) for i in range(14)])
    assert all(s.scheduled_at is None for s in slots)  # 예약하지 않는다
    used = [s.account for s in slots]
    # 규칙 §4: 정확히 10개를 골라 고정하고 돌려 쓴다 (풀 12개 → 10개)
    assert len(set(used)) == publish_mod.SELF_DAILY_ACCOUNTS_MAX == 10
    assert all(a != b for a, b in zip(used, used[1:]))  # 같은 계정 연속 금지


def test_self_daily_accounts_are_random_ten():
    """계정 10개 고정은 LRU 순서가 아니라 **무작위**로 뽑는다 (규칙 §4)."""
    pool = [_Acc(f"user{i:02d}") for i in range(30)]
    first = publish_mod.pick_self_daily_accounts(pool, random.Random(1))
    second = publish_mod.pick_self_daily_accounts(pool, random.Random(2))
    assert len(first) == len(second) == publish_mod.SELF_DAILY_ACCOUNTS_MAX == 10
    assert len(set(first)) == 10  # 서로 다른 계정
    assert first != second  # 실행마다 다른 묶음
    assert first != [a.login_id for a in pool[:10]]  # 시트 순서 그대로가 아니다
    # 풀이 10개보다 적으면 있는 만큼만
    assert len(publish_mod.pick_self_daily_accounts(pool[:4], random.Random(3))) == 4


def test_plan_picks_ten_random_accounts_per_cafe(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = {"self_owned": [{"name": "고요한 아침", "cafe_id": 1, "board": "반말일기"}]}
    pool = [_Acc(f"user{i:02d}") for i in range(30)]
    monkeypatch.setattr(publish_mod, "load_accounts", lambda rt, prefer_cache=False: pool)
    monkeypatch.setattr(publish_mod, "eligible", lambda *a, **k: list(pool))
    monkeypatch.setattr(publish_mod, "_pool_for_cafe", lambda rt, spec, c, b, p: (list(p), False))
    _patch_members(monkeypatch, {"고요한 아침": [a.login_id for a in pool]})

    spec = TaskSpec(task="publish_daily", count=20, per_cafe=True, dry_run=False)
    slots = publish_mod.plan(rt, spec, [_m("고요한 아침", i) for i in range(20)])
    used = {s.account for s in slots}
    assert len(used) == 10
    # LRU(시트 순서) 상위 10개를 그대로 쓰지 않는다
    assert used != {a.login_id for a in pool[:10]}


def test_plan_keeps_same_ten_accounts_across_boards(tmp_path, monkeypatch):
    """게시판이 여러 개여도 같은 카페면 같은 10개 계정을 쓴다 (규칙 §4).

    09-20 실측: 게시판(메뉴)마다 10개를 새로 뽑아 카페 전체로는 21~24개가 쓰였다.
    """
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = {"self_owned": [{"name": "고요한 아침", "cafe_id": 1, "board": "반말일기"}]}
    pool = [_Acc(f"user{i:02d}") for i in range(30)]
    monkeypatch.setattr(publish_mod, "load_accounts", lambda rt, prefer_cache=False: pool)
    monkeypatch.setattr(publish_mod, "eligible", lambda *a, **k: list(pool))
    monkeypatch.setattr(publish_mod, "_pool_for_cafe", lambda rt, spec, c, b, p: (list(p), False))
    _patch_members(monkeypatch, {"고요한 아침": [a.login_id for a in pool]})
    boards = [f"게시판{i % 20}" for i in range(60)]
    monkeypatch.setattr(publish_mod, "resolve_board", lambda rt, m, spec, c: boards.pop(0))

    spec = TaskSpec(task="publish_daily", count=60, per_cafe=True, dry_run=False)
    slots = publish_mod.plan(rt, spec, [_m("고요한 아침", i) for i in range(60)])
    used = {s.account for s in slots}
    assert len(used) == 10


def test_daily_self_accounts_prefers_staff_in_more_cafes_and_sticks_for_the_day(tmp_path, monkeypatch):
    """하루 10개는 자사 카페 전체 공용: 스탭 카페 수 많은 계정 우선, 같은 날은 같은 묶음 (규칙 §4)."""
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = {"self_owned": [{"name": c, "cafe_id": i, "board": "b"} for i, c in enumerate(["A", "B", "C"])]}
    pool = [_Acc(f"user{i:02d}") for i in range(30)] + [_Acc("aff01", "제휴 작업")]
    members = {
        "A": [f"user{i:02d}" for i in range(30)] + ["aff01"],
        "B": [f"user{i:02d}" for i in range(30)],
        "C": ["user00", "user01"],  # 이 카페는 스탭이 2개뿐
    }
    _patch_members(monkeypatch, members)
    first = publish_mod.daily_self_accounts(rt, pool, random.Random(1), today="2026-09-21")
    assert len(first) == 10
    assert "user00" in first and "user01" in first  # 3곳 모두 스탭인 계정은 반드시 포함
    assert "aff01" not in first  # 제휴 계정 제외
    # 같은 날 다시 물으면(작업 재시작 등) 같은 묶음
    again = publish_mod.daily_self_accounts(rt, pool, random.Random(99), today="2026-09-21")
    assert again == first
    # 날이 바뀌면 새로 뽑는다
    tomorrow = publish_mod.daily_self_accounts(rt, pool, random.Random(5), today="2026-09-22")
    assert len(tomorrow) == 10 and "user00" in tomorrow


def test_plan_uses_one_daily_set_across_cafes(tmp_path, monkeypatch):
    """카페가 달라도 하루 10개 안에서만 글쓴이를 고른다."""
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = {"self_owned": [{"name": c, "cafe_id": i, "board": "b"} for i, c in enumerate(["A", "B"])]}
    pool = [_Acc(f"user{i:02d}") for i in range(30)]
    ids = [a.login_id for a in pool]
    monkeypatch.setattr(publish_mod, "load_accounts", lambda rt, prefer_cache=False: pool)
    monkeypatch.setattr(publish_mod, "eligible", lambda *a, **k: list(pool))
    monkeypatch.setattr(publish_mod, "_pool_for_cafe", lambda rt, spec, c, b, p: (list(p), False))
    _patch_members(monkeypatch, {"A": ids, "B": ids})
    monkeypatch.setattr(publish_mod, "resolve_board", lambda rt, m, spec, c: f"게시판{hash(m.title) % 7}")

    spec = TaskSpec(task="publish_daily", count=80, per_cafe=True, dry_run=False)
    manuscripts = [_m("A", i) for i in range(40)] + [_m("B", i) for i in range(40)]
    slots = publish_mod.plan(rt, spec, manuscripts)
    used = {s.account for s in slots}
    assert len(used) == 10
    for cafe in ("A", "B"):
        seq = [s.account for s in slots if s.cafe == cafe]
        assert all(a != b for a, b in zip(seq, seq[1:]))  # 카페 안 연속 금지


def test_avoid_consecutive_also_per_board():
    cafes = ["A", "A", "A"]
    boards = ["b1", "b2", "b1"]
    assigned = {0: "u1", 1: "u2", 2: "u1"}
    publish_mod._avoid_consecutive(cafes, assigned, {"a": ["u1", "u2", "u3"]}, boards)
    assert assigned[2] != "u1"  # 같은 게시판(b1) 직전 글과 같은 계정 금지


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


def test_run_publish_keeps_sheet_order_and_never_round_robins(tmp_path, monkeypatch):
    """자사 카페 일상 글은 라운드로빈으로 다시 섞지 않는다 (규칙 §2)."""
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    rt.cafes_cfg = TWO_CAFES
    items = _interleaved(
        "각색_0901",
        ["고요한 아침", "고요한 아침", "글로시 마이", "고요한 아침", "글로시 마이"],
    )
    _fake_sources(monkeypatch, [("각색_0901", items)])
    pool = [_Acc(f"user{i:02d}") for i in range(12)]
    monkeypatch.setattr(publish_mod, "load_accounts", lambda rt, prefer_cache=False: pool)
    monkeypatch.setattr(publish_mod, "eligible", lambda *a, **k: list(pool))
    monkeypatch.setattr(publish_mod, "_pool_for_cafe", lambda rt, spec, c, b, p: (list(p), False))

    def _boom(slots):
        raise AssertionError("이 흐름에서는 라운드로빈을 쓰면 안 된다")

    monkeypatch.setattr(worker, "order_round_robin", _boom)

    spec = TaskSpec(
        task="publish_daily", count=5, per_cafe=True, dry_run=True, start_date="2026-09-19"
    )
    out = worker._run_publish(rt, None, spec)
    assert out["slots"] == 5
    assert [r["row"] for r in out["results"]] == [1, 2, 3, 4, 5]
    assert [r["cafe"] for r in out["results"]] == [
        "고요한 아침", "고요한 아침", "글로시 마이", "고요한 아침", "글로시 마이"
    ]
    assert [s["seq"] for s in out["sequence"]] == [1, 2, 3, 4, 5]

    # 보고서에도 순서·파일·행이 그대로 나온다 (모의 실행 포함)
    text = (tmp_path / "docs" / "reports" / "self-daily-2026-09-19.md").read_text(
        encoding="utf-8"
    )
    assert "| 순서 | 파일 | 행 | 카페 |" in text
    body = [line for line in text.splitlines() if line.startswith("| 1 |")]
    assert body and "각색_0901" in body[0] and "고요한 아침" in body[0]


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

    # 모의 실행 결과에도 요청/오늘 올린 수/이번 계획이 같이 나온다 (규칙 §7)
    rt.cafes_cfg = {"self_owned": [{"name": "고요한 아침", "cafe_id": 1, "board": "반말일기"}]}
    _mark_today(rt, "고요한 아침", 50)
    dry = TaskSpec(task="publish_daily", count=50, per_cafe=True, dry_run=True)
    out = worker._run_publish(rt, None, dry)
    assert out["slots"] == 0
    assert out["per_cafe"]["고요한 아침"] == {
        "ok": 0, "fail": 0, "requested": 50, "already": 50, "planned": 0
    }

    path = worker.write_daily_report(rt, spec, results, failed)
    assert path.endswith("self-daily-2026-09-19.md")
    text = (tmp_path / "docs" / "reports" / "self-daily-2026-09-19.md").read_text(encoding="utf-8")
    assert "| 순서 | 파일 | 행 | 카페 |" in text
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


# --------------------------------------------------------------------
# 허용 시간대 08:00~02:00 (2026-09-19)
# --------------------------------------------------------------------
def test_self_window_boundaries():
    from v2r.engine.publish import KST, in_self_window, next_self_window

    assert in_self_window(datetime(2026, 9, 19, 8, 0, tzinfo=KST))
    assert in_self_window(datetime(2026, 9, 19, 23, 30, tzinfo=KST))
    assert in_self_window(datetime(2026, 9, 20, 1, 59, tzinfo=KST))
    assert not in_self_window(datetime(2026, 9, 20, 2, 0, tzinfo=KST))
    assert not in_self_window(datetime(2026, 9, 20, 7, 59, tzinfo=KST))
    nxt = next_self_window(datetime(2026, 9, 20, 3, 15, tzinfo=KST))
    assert (nxt.hour, nxt.minute) == (8, 0) and nxt.day == 20
    same = datetime(2026, 9, 20, 10, 0, tzinfo=KST)
    assert next_self_window(same) == same


def test_daily_comments_shift_out_of_window(tmp_path):
    from v2r.engine.publish import KST, in_self_window

    rt = make_runtime(tmp_path)
    rt._catalog = _CommentCatalog()
    rt._llm = _FakeLLM('["저도 어제 딱 그랬어요ㅋㅋ", "오늘 날씨 진짜 좋더라구요", "저녁은 뭐 드셨어요"]')
    slot = publish_mod.Slot(
        manuscript=_m("고요한 아침", 0), account="member0", cafe="고요한 아침", board="반말일기"
    )
    start = datetime(2026, 9, 20, 1, 30, tzinfo=KST)  # 새벽 1:30 글 → 댓글이 2시 넘길 수 있다

    class _Rng3(random.Random):
        def choices(self, population, weights=None, k=1):
            return [3]

    payload = publish_mod.build_daily_comments(rt, slot, start, 1, rng=_Rng3(3))
    assert len(payload) == 3
    for node in payload:
        at = datetime.fromisoformat(node["start_at"].replace("Z", "+00:00"))
        assert in_self_window(at)


def test_generate_texts_retries_once_when_all_rejected():
    calls = []

    class _LLM:
        def complete(self, purpose, system, user, max_tokens=0):
            calls.append(user)
            if len(calls) == 1:
                return '["' + "가" * 45 + '"]'  # 전부 30자 초과 → 탈락
            return '["두 번째엔 짧게 답했어요 ㅋㅋ"]'

    out = dc.generate_texts(_LLM(), "제목", "본문", 1)
    assert out == ["두 번째엔 짧게 답했어요 ㅋㅋ"]
    assert len(calls) == 2 and "재시도 1회째" in calls[1]


def test_generate_texts_keeps_retrying_until_count_filled():
    calls = []

    class _LLM:
        def complete(self, purpose, system, user, max_tokens=0):
            calls.append(user)
            n = len(calls)
            if n < 4:
                return '["' + "가" * 45 + '"]'
            return '["드디어 규칙에 맞는 댓글이에요"]'

    out = dc.generate_texts(_LLM(), "제목", "본문", 1)
    assert out == ["드디어 규칙에 맞는 댓글이에요"] and len(calls) == 4


def test_generate_texts_stops_at_max_attempts():
    calls = []

    class _LLM:
        def complete(self, purpose, system, user, max_tokens=0):
            calls.append(user)
            return '["' + "가" * 45 + '"]'

    out = dc.generate_texts(_LLM(), "제목", "본문", 2)
    assert out == [] and len(calls) == dc.MAX_ATTEMPTS + 1


def test_declump_breaks_runs_of_three_and_keeps_order_otherwise():
    items = list(range(8))
    seq = [{"cafe": c} for c in ["A", "A", "A", "A", "B", "C", "B", "C"]]
    out, out_seq = publish_mod.declump_by_cafe(items, seq)
    cafes = [e["cafe"] for e in out_seq]
    assert sorted(out) == items
    # 같은 카페 3연속 없음
    assert all(not (cafes[i] == cafes[i - 1] == cafes[i - 2]) for i in range(2, len(cafes)))
    # 카페 내부 순서 보존
    assert [x for x, e in zip(out, out_seq) if e["cafe"] == "A"] == [0, 1, 2, 3]
    # 몰림 없는 시트는 그대로
    seq2 = [{"cafe": c} for c in ["A", "B", "A", "B"]]
    assert publish_mod.declump_by_cafe([0, 1, 2, 3], seq2)[0] == [0, 1, 2, 3]


# --------------------------------------------------------------------
# 9. 카페별 간격 (사용자 결정 2026-09-21, 규칙 §4)
# --------------------------------------------------------------------
#: 이름이 서로 또렷하게 다른 자사 카페 5곳
FIVE_CAFES = ["고요한 아침", "글로시 마이", "웨딩 노트", "바다 마을", "숲속 정원"]


class _Clock:
    """가짜 시계 — 진짜로 자지 않고 눈금만 앞으로 돌린다."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float, beat, **kwargs) -> None:
        self.now += max(0.0, float(seconds))
        beat()


def _slots(plan: list[tuple[str, int]]) -> list:
    out = []
    for cafe, count in plan:
        for i in range(count):
            out.append(
                publish_mod.Slot(
                    manuscript=_m(cafe, i), account="u", cafe=cafe, board="b"
                )
            )
    return out


def test_pacer_는_카페마다_따로_쉬고_카페_안_순서를_지킨다():
    """카페 5곳 × 4건이 5×4×3.5분이 아니라 약 (4-1)×3.5분 만에 끝난다."""
    clock = _Clock()
    cafes = list(FIVE_CAFES)
    slots = _slots([(c, 4) for c in cafes])
    pacer = worker.CafePacer(slots, 2, 5, rng=random.Random(7), clock=clock)

    fired: list[tuple[float, str, str]] = []
    while pacer.pending():
        pos, slot = pacer.take()
        wait = pacer.wait_seconds(slot.cafe)
        clock.now += wait  # 올릴 수 있을 때까지 기다린다
        fired.append((clock.now, slot.cafe, slot.manuscript.title))
        pacer.mark(slot.cafe)

    assert len(fired) == 20
    elapsed = fired[-1][0] - fired[0][0]
    # 전체 하나의 간격이었다면 최소 19×2분=38분. 카페별이면 최대 (4-1)×5분=15분.
    assert 3 * 2 * 60 <= elapsed <= 3 * 5 * 60
    for cafe in cafes:
        mine = [(t, title) for t, c, title in fired if c == cafe]
        assert [title for _, title in mine] == [f"{cafe} 글 {i}" for i in range(4)]
        gaps = [b[0] - a[0] for a, b in zip(mine, mine[1:])]
        assert all(2 * 60 <= g <= 5 * 60 for g in gaps)  # 같은 카페는 2~5분 간격


def test_run_publish_은_카페별_간격으로_돈다(tmp_path, monkeypatch):
    """`_run_publish` 실제 경로: 5개 카페 × 4건이 카페별 간격으로 빨리 끝난다."""
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    cafes = list(FIVE_CAFES)
    rt.cafes_cfg = {
        "self_owned": [
            {"name": c, "cafe_id": i + 1, "board": "b"} for i, c in enumerate(cafes)
        ]
    }
    items = _interleaved("각색_0921", cafes * 4)
    _fake_sources(monkeypatch, [("각색_0921", items)])
    pool = [_Acc(f"user{i:02d}") for i in range(12)]
    monkeypatch.setattr(publish_mod, "load_accounts", lambda rt, prefer_cache=False: pool)
    monkeypatch.setattr(publish_mod, "eligible", lambda *a, **k: list(pool))
    monkeypatch.setattr(publish_mod, "_pool_for_cafe", lambda rt, spec, c, b, p: (list(p), False))
    monkeypatch.setattr(publish_mod, "in_self_window", lambda now: True)
    monkeypatch.setattr(worker, "notify_all", lambda ch, msg: None)

    clock = _Clock()
    monkeypatch.setattr(worker.time, "monotonic", clock)
    monkeypatch.setattr(worker, "_sleep_with_beat", clock.sleep)

    fired: list[tuple[float, str]] = []

    def fake_slot(rt_, spec_, slot, **kwargs):
        fired.append((clock.now, slot.cafe))
        return {"status": "done", "cafe": slot.cafe, "title": slot.manuscript.title}

    monkeypatch.setattr(publish_mod, "run_slot", fake_slot)

    spec = TaskSpec(
        task="publish_daily", count=4, per_cafe=True, dry_run=False,
        interval_min=2, interval_max=5, start_date="2026-09-21",
    )
    out = worker._run_publish(rt, None, spec)

    assert out["slots"] == 20 and out["ok"] is True
    elapsed = fired[-1][0] - fired[0][0]
    assert elapsed <= 3 * 5 * 60  # 카페별 간격: 최대 15분
    assert elapsed >= 3 * 2 * 60  # 그래도 같은 카페는 쉬었다
    for cafe in cafes:
        mine = [t for t, c in fired if c == cafe]
        assert len(mine) == 4
        assert all(2 * 60 <= b - a <= 5 * 60 for a, b in zip(mine, mine[1:]))


def test_모의_실행_보고에_예상_소요가_나온다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    rt.cafes_cfg = TWO_CAFES
    items = _interleaved("각색_0921", ["고요한 아침", "글로시 마이"] * 3)
    _fake_sources(monkeypatch, [("각색_0921", items)])
    pool = [_Acc(f"user{i:02d}") for i in range(12)]
    monkeypatch.setattr(publish_mod, "load_accounts", lambda rt, prefer_cache=False: pool)
    monkeypatch.setattr(publish_mod, "eligible", lambda *a, **k: list(pool))
    monkeypatch.setattr(publish_mod, "_pool_for_cafe", lambda rt, spec, c, b, p: (list(p), False))

    spec = TaskSpec(
        task="publish_daily", count=3, per_cafe=True, dry_run=True,
        interval_min=2, interval_max=5, start_date="2026-09-21",
    )
    out = worker._run_publish(rt, None, spec)
    # 카페마다 3건 → (3-1) × 평균 3.5분 = 7분
    assert out["estimate"] == "예상 소요 약 7분"
    assert out["estimate_minutes"] == 7.0
    assert "예상 소요 약 7분" in out["message"]
    # 60분을 넘으면 시간 단위로 말한다
    slots = _slots([("A", 31), ("B", 2)])
    assert worker.duration_estimate(slots, 2, 5)[1] == "예상 소요 약 1.8시간"
