"""브랜드별 키워드 노출 현황 — 파싱/판정/DB/명령 테스트.

docs/reports/keyword-exposure-plan-2026-09-22.md §2 구현.
"""

from __future__ import annotations

from v2r.command.parser import parse_korean_command
from v2r.knowledge.keyword_exposure import (
    ExposureRow,
    check_keyword,
    parse_cafe_search_rank,
)
from v2r.store import keyword_exposure_store as store
from v2r.store.db import connect, init_schema

OUR_URL = "https://cafe.naver.com/mycafe/12345"

# 네이버 검색 카페탭 결과 HTML을 흉내 낸 고정 샘플 (실제 마크업 단순화)
SAMPLE_HTML_TOP3 = f"""
<html><body>
<ul class="lst_total">
  <li><a class="api_txt_lines" href="https://cafe.naver.com/othercafe/999">다른 글 1</a></li>
  <li><a class="api_txt_lines" href="{OUR_URL}">우리 글</a></li>
  <li><a class="api_txt_lines" href="https://m.cafe.naver.com/othercafe/1000">다른 글 2</a></li>
</ul>
</body></html>
"""

SAMPLE_HTML_NOT_FOUND = """
<html><body>
<ul class="lst_total">
  <li><a class="api_txt_lines" href="https://cafe.naver.com/othercafe/1">다른 글 1</a></li>
  <li><a class="api_txt_lines" href="https://cafe.naver.com/othercafe/2">다른 글 2</a></li>
</ul>
</body></html>
"""

SAMPLE_HTML_BLOCKED = """
<html><body><div>자동입력 방지문자를 입력해 주세요 (captcha)</div></body></html>
"""


def test_parse_cafe_search_rank_찾음():
    assert parse_cafe_search_rank(SAMPLE_HTML_TOP3, OUR_URL) == 2


def test_parse_cafe_search_rank_모바일_url도_같은_글로_인식():
    mobile_url = "https://m.cafe.naver.com/mycafe/12345"
    assert parse_cafe_search_rank(SAMPLE_HTML_TOP3, mobile_url) == 2


def test_parse_cafe_search_rank_못_찾음():
    assert parse_cafe_search_rank(SAMPLE_HTML_NOT_FOUND, OUR_URL) is None


def test_parse_cafe_search_rank_상위N_밖이면_None():
    assert parse_cafe_search_rank(SAMPLE_HTML_TOP3, OUR_URL, top_n=1) is None


def test_parse_cafe_search_rank_차단이면_예외():
    import pytest

    from v2r.sources.sheets import SourceError

    with pytest.raises(SourceError):
        parse_cafe_search_rank(SAMPLE_HTML_BLOCKED, OUR_URL)


def test_check_keyword_url_없으면_unpublished():
    row = check_keyword("우아덤", "다이어트", "씨씨앙", "")
    assert row.status == "unpublished"
    assert row.rank is None


def test_check_keyword_exposed(monkeypatch):
    import v2r.knowledge.keyword_exposure as ke_mod

    monkeypatch.setattr(ke_mod, "fetch_cafe_search_html", lambda *a, **k: SAMPLE_HTML_TOP3)
    row = check_keyword("우아덤", "다이어트", "씨씨앙", OUR_URL)
    assert row.status == "exposed"
    assert row.rank == 2


def test_check_keyword_pushed(monkeypatch):
    import v2r.knowledge.keyword_exposure as ke_mod

    monkeypatch.setattr(ke_mod, "fetch_cafe_search_html", lambda *a, **k: SAMPLE_HTML_NOT_FOUND)
    row = check_keyword("우아덤", "다이어트", "씨씨앙", OUR_URL)
    assert row.status == "pushed"
    assert row.rank is None


def test_check_keyword_실패시_unknown(monkeypatch):
    import v2r.knowledge.keyword_exposure as ke_mod

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ke_mod, "fetch_cafe_search_html", _boom)
    row = check_keyword("우아덤", "다이어트", "씨씨앙", OUR_URL)
    assert row.status == "unknown"


def test_db_save_and_pushed_keywords(tmp_path):
    conn = connect(tmp_path / "t.sqlite")
    init_schema(conn)
    store.save(
        conn,
        ExposureRow(
            "우아덤", "다이어트", "씨씨앙", OUR_URL, None, "pushed", "2026-09-22T08:00:00+09:00"
        ).as_row(),
    )
    store.save(
        conn,
        ExposureRow(
            "우아덤", "홈트", "씨씨앙", OUR_URL, 3, "exposed", "2026-09-22T08:00:05+09:00"
        ).as_row(),
    )
    pushed = store.pushed_keywords(conn, "우아덤")
    assert [p["keyword"] for p in pushed] == ["다이어트"]

    latest = store.latest_by_keyword(conn, "우아덤")
    assert len(latest) == 2


def test_db_summary_새로_밀려난_키워드(tmp_path):
    conn = connect(tmp_path / "t2.sqlite")
    init_schema(conn)
    store.save(
        conn,
        ExposureRow(
            "우아덤", "다이어트", "씨씨앙", OUR_URL, 1, "exposed", "2026-09-21T08:00:00+09:00"
        ).as_row(),
    )
    store.save(
        conn,
        ExposureRow(
            "우아덤", "다이어트", "씨씨앙", OUR_URL, None, "pushed", "2026-09-22T08:00:00+09:00"
        ).as_row(),
    )
    out = store.summary(conn)
    assert out["우아덤"]["pushed"] == 1
    assert out["우아덤"]["newly_pushed"] == ["다이어트"]


def test_load_pushed_keywords_merges_db(tmp_path, monkeypatch):
    """시트 '밀려남' ∪ DB 최신 pushed, 중복 제거, 시트 순서 우선."""
    from v2r.sources import keyword_list

    conn = connect(tmp_path / "t3.sqlite")
    init_schema(conn)
    store.save(
        conn,
        ExposureRow(
            "우아덤", "DB전용키워드", "씨씨앙", OUR_URL, None, "pushed", "2026-09-22T08:00:00+09:00"
        ).as_row(),
    )
    # 시트에도 있는 키워드는 중복으로 추가되지 않아야 한다
    store.save(
        conn,
        ExposureRow(
            "우아덤", "시트키워드", "씨씨앙", OUR_URL, None, "pushed", "2026-09-22T08:00:00+09:00"
        ).as_row(),
    )

    monkeypatch.setattr(
        keyword_list,
        "rows_from_xlsx",
        lambda path, sheet=keyword_list.EXPOSURE_SHEET: [
            {"카페": "씨씨앙", "노출상태": "밀려남", "키워드": "시트키워드"},
        ],
    )
    out = keyword_list.load_pushed_keywords(
        "우아덤", xlsx_path="dummy.xlsx", conn=conn
    )
    keywords = [o["keyword"] for o in out]
    assert keywords == ["시트키워드", "DB전용키워드"]


def test_parser_키워드_노출_현황():
    spec = parse_korean_command("키워드 노출 현황")
    assert spec is not None
    assert spec.task == "keyword_exposure"


def test_parser_브랜드_노출_현황():
    spec = parse_korean_command("우아덤 노출 현황")
    assert spec is not None
    assert spec.task == "keyword_exposure"
    assert spec.brand == "우아덤"


def test_parser_노출_현황_키워드_5개():
    spec = parse_korean_command("우아덤 노출 현황 키워드 5개")
    assert spec is not None
    assert spec.task == "keyword_exposure"
    assert spec.count == 5


def test_sidecar_light_task():
    from v2r.engine.sidecar import LIGHT_TASKS

    assert "keyword_exposure" in LIGHT_TASKS
