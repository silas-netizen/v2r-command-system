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


# --------------------------------------------------------------------
# gviz 머리글 추측 끄기 (팥순이 `게시글 쓰기 원본` 탭 사고)
# --------------------------------------------------------------------
def test_gviz_url_탭이름과_headers():
    url = gviz_csv_url("SID", sheet="게시글 쓰기 원본", headers=0)
    assert "gid=" not in url
    assert "headers=0" in url
    assert "%EA%B2%8C%EC%8B%9C%EA%B8%80" in url  # 탭 이름이 URL 인코딩된다


def test_load_source_는_sheet와_headers를_넘긴다(monkeypatch):
    seen: dict = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return httpx.Response(200, text="A,B\n1,2\n", request=httpx.Request("GET", url))

    monkeypatch.setattr(sheets.httpx, "get", fake_get)
    load_source({"name": "t", "spreadsheet_id": "SID", "sheet": "탭", "headers": 0})
    assert "headers=0" in seen["url"] and "gid=" not in seen["url"]


def test_중복_머리글도_열_자리를_지킨다(monkeypatch):
    # 빈 머리글이 두 번 나와도 뒤엣것이 앞엣것을 덮지 않아야 한다
    text = "키워드,본문,,\n가,나,다,라\n"

    def fake_get(url, **kw):
        return httpx.Response(200, text=text, request=httpx.Request("GET", url))

    monkeypatch.setattr(sheets.httpx, "get", fake_get)
    rows = fetch_csv("https://example.test/csv")
    assert len(rows[0]) == 4
    assert list(rows[0].values()) == ["가", "나", "다", "라"]


def test_머리글_없는_제휴시트도_A_J를_자리로_읽는다():
    # 첫 줄이 머리글이 아니라 데이터인 시트 (gviz가 머리글로 먹어 버린 모양)
    rows = [
        {
            "첫키워드": "둘째키워드",
            "본문 하나": "본문 둘",
            "씨씨앙": "씨씨앙",
            "acct1": "acct2",
            "후기형": "질문형",
            "": "",
            "_6": "",
            "실명": "실명",
            "Y": "Y",
            "자유수다방": "자유수다방",
        }
    ]
    assert sheets.header_row_is_data(rows) is True
    out = parse_affiliate_rows(rows, source="제휴시트")
    assert [m.keyword for m in out] == ["첫키워드", "둘째키워드"]
    assert [m.manuscript_type for m in out] == ["후기형", "질문형"]
    assert [m.source_row for m in out] == [1, 2]


def test_머리글이_있으면_그대로_2행부터():
    rows = [
        {"키워드": "가", "본문": "본문", "카페명": "씨씨앙", "작성계정": "a",
         "원고유형": "후기형", "완료 링크": "", "말머리": "", "계정유형": "실명",
         "이미지 없음": "Y", "게시판명": "자유수다방"},
    ]
    assert sheets.header_row_is_data(rows) is False
    out = parse_affiliate_rows(rows)
    assert [m.source_row for m in out] == [2]
