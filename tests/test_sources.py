"""원본 시트 적재·파서 테스트."""

import httpx
import pytest

from v2r.sources import sheets
from v2r.sources.local_files import load_csv_rows, load_xlsx_rows
from v2r.sources.sheets import (
    SourceError,
    fetch_csv,
    gviz_csv_url,
    load_source,
    parse_adapted_rows,
    parse_affiliate_rows,
    parse_daily_rows,
)

CAFES = {
    "affiliate": [{"name": "쌍둥이맘 모여라", "aliases": []}],
    "self_owned": [{"name": "고요한 아침"}],
    "test": {"태극": 31670254},
}


def test_gviz_csv_url():
    url = gviz_csv_url("SID", 123)
    assert "SID" in url and "gid=123" in url and "out:csv" in url


def test_제외문서는_거부():
    with pytest.raises(SourceError):
        gviz_csv_url("1DLQgLWBo1c4CDkgvH4fjkuDrRM1C03XT", 0)
    with pytest.raises(SourceError):
        fetch_csv("https://docs.google.com/x/1DLQgLWBo1c4CDkgvH4fjkuDrRM1C03XT/csv")
    with pytest.raises(SourceError):
        load_source({"name": "제외", "spreadsheet_id": "1DLQgLWBo1c4CDkgvH4fjkuDrRM1C03XT",
                     "gid": 0})


def test_fetch_csv(monkeypatch):
    def fake_get(url, **kw):
        return httpx.Response(200, text="제목,본문\n가,나\n",
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(sheets.httpx, "get", fake_get)
    rows = fetch_csv("https://example.test/csv")
    assert rows == [{"제목": "가", "본문": "나"}]


def test_fetch_csv_실패는_SourceError(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("no net")

    monkeypatch.setattr(sheets.httpx, "get", boom)
    with pytest.raises(SourceError):
        fetch_csv("https://example.test/csv")


def test_load_source_실패시_캐시대체(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("no net")

    monkeypatch.setattr(sheets.httpx, "get", boom)
    cached = [{"제목": "캐시", "본문": "내용"}]
    got = load_source({"name": "일상글목록", "spreadsheet_id": "SID", "gid": 1},
                      cache_get=lambda k: cached)
    assert got == cached


def test_load_source_성공시_캐시저장(monkeypatch):
    monkeypatch.setattr(
        sheets.httpx, "get",
        lambda url, **kw: httpx.Response(200, text="제목,본문\n가,나\n",
                                         request=httpx.Request("GET", url)),
    )
    saved = {}
    rows = load_source({"name": "일상글목록", "spreadsheet_id": "SID", "gid": 1},
                       cache_put=lambda k, v: saved.update({k: v}))
    assert saved["일상글목록"] == rows


def test_parse_daily_rows():
    rows = [
        {"제목": "가을 산책", "본문": "첫 줄\n둘째 줄", "카페명": "고요한 아침",
         "게시판명": "자유게시판"},
        {"제목": "", "본문": ""},
        {"제목": "겨울", "내용": "눈"},
    ]
    out = parse_daily_rows(rows, source="일상글목록")
    assert [m.source_row for m in out] == [2, 4]
    assert out[0].cafe == "고요한 아침" and out[0].board == "자유게시판"
    assert out[0].content_hash
    assert out[1].body == "눈"


def test_parse_daily_rows_합쳐진_셀():
    rows = [{"원고": "제목 : 합본 제목\n본문 : 합본 본문"}]
    out = parse_daily_rows(rows)
    assert out[0].title == "합본 제목" and out[0].body == "합본 본문"


def test_parse_adapted_rows_필수헤더와_카페정규화():
    rows = [{"카페명": "쌍둥이맘모여라", "게시판명": "가족업체 자유게시판",
             "각색제목": "T", "각색본문": "B"}]
    out = parse_adapted_rows(rows, CAFES)
    assert out[0].cafe == "쌍둥이맘 모여라" and out[0].source_row == 2
    with pytest.raises(SourceError):
        parse_adapted_rows([{"카페명": "x", "각색제목": "T"}], CAFES)


def test_parse_affiliate_rows_건너뛰기_규칙():
    rows = [
        {"A": "키워드 하나", "B": "본문1", "C": "씨씨앙", "D": "acct1", "E": "후기형",
         "F": "", "G": "말머리", "H": "실명", "I": "", "J": "자유수다방(구)"},
        {"A": "완료행", "B": "본문2", "C": "씨씨앙", "D": "acct2", "E": "",
         "F": "https://cafe.example/1", "G": "", "H": "실명", "I": "", "J": ""},
        {"A": "계정없음", "B": "본문3", "C": "씨씨앙", "D": "", "E": "",
         "F": "", "G": "", "H": "", "I": "", "J": ""},
        {"A": "이미지없음", "B": "본문4", "C": "양평맘", "D": "acct3", "E": "질문형",
         "F": "", "G": "", "H": "비실명", "I": "Y", "J": ""},
    ]
    out = parse_affiliate_rows(rows, source="제휴시트")
    assert [m.keyword for m in out] == ["키워드 하나", "이미지없음"]
    assert out[0].tags == ["키워드하나"]
    assert out[0].images_enabled is True and out[1].images_enabled is False
    assert out[0].head == "말머리" and out[0].board == "자유수다방(구)"
    assert out[0].account == "acct1" and out[0].account_type == "실명"
    assert out[0].source_row == 2 and out[1].source_row == 5


def test_local_files(tmp_path):
    from openpyxl import Workbook

    xlsx = tmp_path / "a.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["제목", "본문"])
    ws.append(["가", "나"])
    ws.append([None, None])
    wb.save(xlsx)
    assert load_xlsx_rows(xlsx) == [{"제목": "가", "본문": "나"}]

    csv_path = tmp_path / "a.csv"
    csv_path.write_text("제목,본문\n가,나\n", encoding="utf-8-sig")
    assert load_csv_rows(csv_path) == [{"제목": "가", "본문": "나"}]
