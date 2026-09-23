"""브랜드별 키워드 노출 현황 — 파싱/판정/DB/명령 테스트.

docs/reports/keyword-exposure-plan-2026-09-22.md §2 구현.
"""

from __future__ import annotations

import pytest

from v2r.command.parser import parse_korean_command
from v2r.knowledge.keyword_exposure import (
    DEFAULT_DAILY_CAP,
    ExposureRow,
    _naver_article_url,
    _prioritize,
    _title_lead_keyword,
    check_keyword,
    parse_cafe_search_rank,
    parse_cafe_search_title_rank,
    run_check,
    target_keywords,
)
from v2r.store import keyword_exposure_store as store
from v2r.store.article_index import normalize_title
from v2r.store.db import connect, init_schema

from tests.test_engine import make_runtime

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


# --------------------------------------------------------------------
# 우리 글 URL 원천 보강 (2026-09-22 2차)
# --------------------------------------------------------------------
@pytest.fixture()
def rt(tmp_path):
    runtime = make_runtime(tmp_path)
    yield runtime
    runtime.close()


def test_parse_cafe_search_rank_우리가_만든_URL도_글번호로_매칭():
    """`_naver_article_url`이 만드는 ca-fe/cafes/.. 꼴 URL도 검색 결과의
    별칭(alias) 꼴 링크와 글 번호만 같으면 같은 글로 인식해야 한다."""
    html = """
    <ul><li><a href="https://cafe.naver.com/mycafealias/12345">우리 글</a></li></ul>
    """
    built = _naver_article_url(25016228, "12345")
    assert parse_cafe_search_rank(html, built) == 1


def test_naver_article_url_카페번호_글번호로_만든다():
    assert _naver_article_url(25016228, "999") == (
        "https://cafe.naver.com/ca-fe/cafes/25016228/articles/999"
    )
    assert _naver_article_url(None, "999") == ""
    assert _naver_article_url(25016228, "") == ""


def test_title_lead_keyword_제목_맨_앞_토큰():
    assert _title_lead_keyword("다이어트 3주만에 효과 봤어요") == "다이어트"
    assert _title_lead_keyword("  [홈트] 오늘도 운동") == "홈트"
    assert _title_lead_keyword("") == ""


def test_parse_cafe_search_title_rank_제목으로_찾는다():
    html = """
    <ul>
      <li><a href="https://cafe.naver.com/othercafe/1">딴 얘기</a></li>
      <li><a href="https://cafe.naver.com/mycafe/12345">다이어트 3주만에 효과 봤어요!!</a></li>
    </ul>
    """
    target = normalize_title("다이어트 3주만에 효과 봤어요")
    assert parse_cafe_search_title_rank(html, target) == 2
    assert parse_cafe_search_title_rank(html, normalize_title("없는 제목")) is None
    assert parse_cafe_search_title_rank(html, "") is None


def test_target_keywords_시트에_없으면_article_index로_URL을_만든다(rt):
    rt.article_index.upsert(
        cafe_id=25016228, cafe="씨씨앙", source_id="s1", title="다이어트 3주만에 효과",
    )
    rt.article_index.conn.execute(
        "UPDATE article_index SET article_id = ? WHERE source_id = ?", ("999", "s1"),
    )
    rt.article_index.conn.commit()

    from v2r.sources import keyword_list

    def fake_rows_from_xlsx(path, sheet=keyword_list.EXPOSURE_SHEET):
        return [{"카페": "씨씨앙", "키워드": "다이어트", "노출상태": "밀려남"}]

    import v2r.knowledge.keyword_exposure as ke_mod

    monkeypatch_target = fake_rows_from_xlsx
    orig = ke_mod.rows_from_xlsx
    ke_mod.rows_from_xlsx = monkeypatch_target
    try:
        out = target_keywords(
            "우아덤", xlsx_path="dummy.xlsx", article_index=rt.article_index
        )
    finally:
        ke_mod.rows_from_xlsx = orig

    row = next(r for r in out if r["keyword"] == "다이어트")
    assert row["article_url"] == "https://cafe.naver.com/ca-fe/cafes/25016228/articles/999"


def test_target_keywords_url칸이_상태문구면_버린다(rt):
    """실측: 일부 브랜드 시트 `url` 칸엔 '노출완'/'밀려남' 같은 상태 문구가 들어있다.
    실제 URL이 아니면 빈 값으로 취급해야 article_index 보강이 동작한다."""
    from v2r.sources import keyword_list
    import v2r.knowledge.keyword_exposure as ke_mod

    def fake_rows_from_xlsx(path, sheet=keyword_list.EXPOSURE_SHEET):
        return [{"카페": "씨씨앙", "키워드": "다이어트", "url": "노출완", "노출상태": "노출완"}]

    orig = ke_mod.rows_from_xlsx
    ke_mod.rows_from_xlsx = fake_rows_from_xlsx
    try:
        out = target_keywords("우아덤", xlsx_path="dummy.xlsx", article_index=None)
    finally:
        ke_mod.rows_from_xlsx = orig

    row = next(r for r in out if r["keyword"] == "다이어트")
    assert row["article_url"] == ""


def test_target_keywords_우리가_발행한_키워드도_더한다(rt):
    rt.article_index.upsert(
        cafe_id=25016228, cafe="씨씨앙", source_id="s2", title="홈트 매일 30분 후기",
    )

    from v2r.sources import keyword_list
    import v2r.knowledge.keyword_exposure as ke_mod

    def fake_rows_from_xlsx(path, sheet=keyword_list.EXPOSURE_SHEET):
        return [{"카페": "씨씨앙", "키워드": "다이어트", "노출상태": "밀려남"}]

    orig = ke_mod.rows_from_xlsx
    ke_mod.rows_from_xlsx = fake_rows_from_xlsx
    try:
        out = target_keywords(
            "우아덤", xlsx_path="dummy.xlsx", article_index=rt.article_index
        )
    finally:
        ke_mod.rows_from_xlsx = orig

    keywords = {r["keyword"] for r in out}
    assert "다이어트" in keywords  # 시트 키워드
    assert "홈트" in keywords  # 우리가 발행한 글에서 뽑은 키워드


def test_check_keyword_URL_없어도_후보제목으로_노출확인(monkeypatch):
    import v2r.knowledge.keyword_exposure as ke_mod

    html = """
    <ul><li><a href="https://cafe.naver.com/mycafe/1">다이어트 후기 진짜 좋아요</a></li></ul>
    """
    monkeypatch.setattr(ke_mod, "fetch_cafe_search_html", lambda *a, **k: html)
    target = normalize_title("다이어트 후기 진짜 좋아요")
    row = check_keyword(
        "우아덤", "다이어트", "씨씨앙", "", candidate_title_norm=target,
    )
    assert row.status == "exposed"
    assert row.rank == 1


def test_prioritize_밀려남과_우리글_있는_것을_앞으로(rt):
    items = [
        {"keyword": "미확인1", "article_url": "", "candidate_title_norm": ""},
        {"keyword": "우리글있음", "article_url": "https://cafe.naver.com/mycafe/1", "candidate_title_norm": ""},
        {"keyword": "미확인2", "article_url": "", "candidate_title_norm": ""},
    ]
    out = _prioritize(rt, "우아덤", items)
    assert out[0]["keyword"] == "우리글있음"


def test_run_check_하루_상한_60개로_자른다(rt, monkeypatch):
    import v2r.knowledge.keyword_exposure as ke_mod

    many = [
        {"keyword": f"kw{i}", "cafe": "씨씨앙", "article_url": "", "t0_status": "", "candidate_title_norm": ""}
        for i in range(80)
    ]
    monkeypatch.setattr(ke_mod, "target_keywords", lambda *a, **k: many)
    seen = []

    def fake_check(*a, **k):
        seen.append(a[1])
        return ExposureRow("우아덤", a[1], "씨씨앙", "", None, "unpublished", "2026-09-22T00:00:00+09:00")

    monkeypatch.setattr(ke_mod, "check_keyword", fake_check)
    results = run_check(rt, "우아덤", sleep_fn=lambda *a, **k: None)
    assert len(results) == DEFAULT_DAILY_CAP == 60
    assert len(seen) == 60


def test_sheet_row_does_not_write_zero_volume():
    from v2r.knowledge.keyword_exposure import ExposureRow, _sheet_row_from_result

    row = ExposureRow(keyword="다이어트약", brand="팥순이", cafe="", article_url="", rank=None, status="pushed", checked_at="2026-09-24 01:00:00")
    assert _sheet_row_from_result({"keyword": "다이어트약", "volume": 0}, row)["volume"] is None
    assert _sheet_row_from_result({"keyword": "다이어트약", "volume": 20450}, row)["volume"] == 20450
