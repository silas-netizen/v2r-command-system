"""게시글 등록 제한("ID/IP당 …") 처리 (장애 2026-09-21 peecics 4건).

무엇을 지키는 시험인가
----------------------
1. V2R 응답에 "ID/IP당 게시글 등록 제한…" 문구가 있으면 **완료로 확정하지 않는다**.
   목록에 글 번호가 보여도 그 글은 올라가지 않았다.
2. 그런 건은 `failed`(사유 `제한`)로 남고 **재발행 대기 줄**에 들어간다.
3. 한 계정이 하루 `ACCOUNT_DAILY_LIMIT`건을 채우면 그날은 더 쓰지 않는다.
4. 현황판에 "제한 걸린 글" 수가 나온다.
"""

from __future__ import annotations

import pytest

from v2r.api import articles as api_articles
from v2r.api.errors import V2RApiError, classify, is_post_limit
from v2r.config import Settings
from v2r.engine import publish as publish_mod
from v2r.engine import reconcile as reconcile_mod
from v2r.engine.context import Runtime
from v2r.store.db import connect

#: 실측 문구 (V2R 글 목록의 경고 아이콘, 2026-09-21 18:36~18:38)
LIMIT_TEXT = "ID/IP당 게시글 등록 제한을 초과해 신규 게시글 등록이 잠시 제한됩니다"


def make_runtime(tmp_path) -> Runtime:
    settings = Settings(
        v2r_email="tester@example.com",
        v2r_password="",
        data_dir=tmp_path / "data",
        warehouse_dir=tmp_path / "warehouse",
        db_path=tmp_path / "data" / "v2r.sqlite",
    )
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "warehouse").mkdir(parents=True, exist_ok=True)
    return Runtime.open(settings, connect(settings.db_path))


# --------------------------------------------------------------------
# 1) 문구 알아보기
# --------------------------------------------------------------------
def test_제한_문구를_알아본다():
    assert is_post_limit(LIMIT_TEXT)
    assert is_post_limit("게시글 등록 제한")
    assert not is_post_limit("")
    assert not is_post_limit("연속으로 등록할 수 없습니다")


def test_classify가_제한을_post_limit으로():
    exc = V2RApiError("실패", status=400, code="FAIL", reason=LIMIT_TEXT)
    assert classify(exc) == "post_limit"


def test_연속등록_제한은_그대로_consecutive_limit():
    exc = V2RApiError("실패", status=400, code="20004", reason="연속으로 등록할 수 없습니다")
    assert classify(exc) == "consecutive_limit"


# --------------------------------------------------------------------
# 2) 응답에서 제한 상태 꺼내기 (API 필드)
# --------------------------------------------------------------------
def test_상세_응답의_fail_reason에서_찾는다():
    detail = {
        "naver_cafe_article_history": {"status": "RESERVED", "fail_reason": LIMIT_TEXT},
        "naver_cafe_article_destination": {"status": "RESERVED", "fail_reason": None},
    }
    assert api_articles.limit_reason_of(detail) == LIMIT_TEXT
    assert api_articles.is_limited(detail)


def test_목록_행의_fail_reason에서도_찾는다():
    row = {"source_id": "S1", "status": "RESERVED", "fail_reason": LIMIT_TEXT}
    assert api_articles.limit_reason_of(row) == LIMIT_TEXT


def test_정상_완료건은_제한이_아니다():
    detail = {
        "naver_cafe_article_history": {"status": "DONE", "fail_reason": None},
        "naver_cafe_article_destination": {"status": "SUCCESS", "fail_reason": ""},
    }
    assert api_articles.limit_reason_of(detail) == ""


def test_예약_대기만으로는_제한이_아니다():
    """`RESERVED`("준비")는 정상 예약 대기에도 쓰인다 — 문구가 있어야 제한이다."""
    row = {"status": "RESERVED", "fail_reason": ""}
    assert not api_articles.is_limited(row)


# --------------------------------------------------------------------
# 3) reconcile: 제한이면 done이 아니라 failed(제한) + 재발행 대기
# --------------------------------------------------------------------
def test_reconcile는_제한건을_done으로_확정하지_않는다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.publications.mark(
        "각색시트",
        723,
        "hash-limit",
        "uncertain",
        "created",
        source_id="01M336SSTK5SRXQH4AXMWJR0MZ",
        account="peecics",
        cafe="글로시 마이",
        board="스킨 톡",
    )
    monkeypatch.setattr(
        reconcile_mod.api_articles,
        "get_article",
        lambda c, sid: {
            # 실측: 제한에 걸린 글은 목록에 "준비"로 남고 사유가 붙는다
            "naver_cafe_article_history": {"status": "RESERVED", "fail_reason": LIMIT_TEXT},
        },
    )
    result = reconcile_mod.reconcile(rt)

    assert result["done"] == 0
    assert result["failed"] == 1
    assert result["limited"] == 1
    assert result["limited_rows"] == ["각색시트#723"]

    pub = rt.publications.by_source_id("01M336SSTK5SRXQH4AXMWJR0MZ")
    assert pub["status"] == "failed"
    assert pub["stage"] == "제한"

    pending = rt.republish.list_pending()
    assert len(pending) == 1
    assert pending[0]["row_number"] == 723
    assert pending[0]["account"] == "peecics"
    assert "등록 제한" in pending[0]["reason"]
    rt.close()


def test_reconcile는_정상건은_그대로_done(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.publications.mark("각색시트", 5, "hash-ok", "uncertain", "created", source_id="SRC-OK")
    monkeypatch.setattr(
        reconcile_mod.api_articles,
        "get_article",
        lambda c, sid: {"naver_cafe_article_history": {"status": "DONE", "fail_reason": ""}},
    )
    result = reconcile_mod.reconcile(rt)
    assert result["done"] == 1
    assert result["limited"] == 0
    assert rt.republish.count_pending() == 0
    rt.close()


# --------------------------------------------------------------------
# 4) 등록 확인(wait_written)에서 제한을 바로 알아본다
# --------------------------------------------------------------------
def test_wait_written은_제한을_post_limit으로_던진다(monkeypatch):
    detail = {
        "naver_cafe_article_history": {"status": "RESERVED", "fail_reason": LIMIT_TEXT}
    }
    monkeypatch.setattr(api_articles, "get_article", lambda c, sid: detail)
    with pytest.raises(V2RApiError) as err:
        api_articles.wait_written(object(), "SRC-1", max_wait_s=1.0)
    assert err.value.kind == "post_limit"
    assert classify(err.value) == "post_limit"


# --------------------------------------------------------------------
# 5) 계정별 하루 상한
# --------------------------------------------------------------------
def _fill(rt, login: str, n: int, day: str) -> None:
    ts = f"{day}T10:00:00+09:00"
    for i in range(n):
        rt.conn.execute(
            "INSERT INTO publications (source_key, row_number, content_hash, status,"
            " stage, account, cafe, created_at, updated_at)"
            " VALUES ('시트', ?, ?, 'done', 'done', ?, '고요한 아침', ?, ?)",
            (i, f"h{login}{i}", login, ts, ts),
        )


def test_하루_상한을_채운_계정은_제외된다(tmp_path):
    rt = make_runtime(tmp_path)
    day = publish_mod.today_kst()
    _fill(rt, "peecics", publish_mod.ACCOUNT_DAILY_LIMIT, day)
    _fill(rt, "cambrude", 3, day)

    over = publish_mod.accounts_over_daily_limit(rt, day)
    assert "peecics" in over
    assert "cambrude" not in over
    assert rt.publications.count_today_for_account("PEECICS", day) == (
        publish_mod.ACCOUNT_DAILY_LIMIT
    )
    rt.close()


def test_상한_미만이면_제외되지_않는다(tmp_path):
    rt = make_runtime(tmp_path)
    day = publish_mod.today_kst()
    _fill(rt, "peecics", publish_mod.ACCOUNT_DAILY_LIMIT - 1, day)
    assert publish_mod.accounts_over_daily_limit(rt, day) == set()
    rt.close()


def test_상한은_150보다_넉넉히_낮다():
    """실측 상한(약 150)에 닿기 전에 멈춰야 한다."""
    assert publish_mod.ACCOUNT_DAILY_LIMIT <= 120


def test_제한에_걸리면_그_계정을_오늘_하루_뺀다(tmp_path):
    rt = make_runtime(tmp_path)
    publish_mod.block_for_today(rt, "peecics", "게시글 등록 제한(하루 제외)")
    assert rt.account_state.is_restricted("peecics")
    assert "peecics" in publish_mod.restricted_accounts(rt)
    assert "peecics" in (rt.scratch.get("restricted_now") or set())
    rt.close()


# --------------------------------------------------------------------
# 6) 현황판·집계
# --------------------------------------------------------------------
def test_현황판에_제한_걸린_글_열이_있다(tmp_path):
    from v2r.engine import dashboard

    rt = make_runtime(tmp_path)
    day = publish_mod.today_kst()
    ts = f"{day}T10:00:00+09:00"
    rt.conn.execute(
        "INSERT INTO publications (source_key, row_number, content_hash, status, stage,"
        " account, cafe, created_at, updated_at)"
        " VALUES ('시트', 1, 'h1', 'failed', '제한', 'peecics', '글로시 마이', ?, ?)",
        (ts, ts),
    )
    assert rt.publications.count_limited(day) == 1
    assert len(rt.publications.list_limited(day)) == 1

    html = dashboard.render_html(rt)
    assert "제한 걸린 글" in html
    rt.close()


def test_재발행_대기_줄은_같은_행을_두_번_넣지_않는다(tmp_path):
    rt = make_runtime(tmp_path)
    assert rt.republish.add("시트", 1, "h", reason="게시글 등록 제한: x") is True
    assert rt.republish.add("시트", 1, "h", reason="게시글 등록 제한: y") is False
    assert rt.republish.count_pending() == 1
    rt.republish.resolve("시트", 1, "h")
    assert rt.republish.count_pending() == 0
    rt.close()
