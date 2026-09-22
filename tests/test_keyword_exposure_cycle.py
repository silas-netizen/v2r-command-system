"""B1~B4 — 통합검색(통검) 판정·무한 순환·CSV/summary 테스트.

docs/reports/keyword-program-plan-2026-09-22.md B절,
docs/reports/exposure-loop-2026-09-22.md(2026-09-23 재설계 2건: 자동완성 검색어
정규화·끝까지 스크롤, 카페 후보→댓글 식별어 확정) 구현.
"""

from __future__ import annotations

import csv
import json

from v2r.command.parser import parse_korean_command
from v2r.knowledge import keyword_exposure as ke
from v2r.store import keyword_exposure_store as store

from tests.test_engine import make_runtime

OUR_URL = "https://cafe.naver.com/mycafe/555"

# 네이버 통합검색(통검) 결과 HTML을 흉내 낸 고정 샘플 — 카페·블로그·VIEW가 섞여 있다.
SAMPLE_UNIFIED_TOP2 = f"""
<html><body>
<div class="api_subject_bx">
  <a href="https://blog.naver.com/someone/1">블로그 글</a>
  <a href="{OUR_URL}">우리 카페 글</a>
  <a href="https://cafe.naver.com/othercafe/2">다른 카페 글</a>
</div>
</body></html>
"""

SAMPLE_UNIFIED_NOT_FOUND = """
<html><body>
<div class="api_subject_bx">
  <a href="https://blog.naver.com/someone/1">블로그 글</a>
  <a href="https://cafe.naver.com/othercafe/2">다른 카페 글</a>
</div>
</body></html>
"""

SAMPLE_UNIFIED_BLOCKED = """
<html><body><div>비정상적인 접근으로 차단되었습니다 (captcha)</div></body></html>
"""

REGISTRY = [{"name": "마이카페", "cafe_id": 555, "aliases": ["mycafe", "마이카페"]}]
IDENTS = ["우아덤", "그린커피 아하바하"]


# --------------------------------------------------------------------
# check_keyword_unified — 저장된 article_url 기준 판정(보조 경로, html 직접 주입)
# --------------------------------------------------------------------
def test_check_keyword_unified_노출완():
    row = ke.check_keyword_unified(
        "우아덤", "비건세제", "마이카페", OUR_URL, html=SAMPLE_UNIFIED_TOP2
    )
    assert row.status == "exposed"
    # 순위는 카페 글 링크만 순서대로 센다(블로그는 다른 검사 대상이 아니라서 안 셈)
    assert row.rank == 1
    assert row.search_query == "비건세제"  # html 직접 주입 시 자동완성 안 탐


def test_check_keyword_unified_밀려남():
    row = ke.check_keyword_unified(
        "우아덤", "비건세제", "마이카페", OUR_URL, html=SAMPLE_UNIFIED_NOT_FOUND
    )
    assert row.status == "pushed"
    assert row.rank is None


def test_check_keyword_unified_미발행_검색없이():
    row = ke.check_keyword_unified("우아덤", "비건세제", "", "", html="아무거나")
    assert row.status == "unpublished"


def test_check_keyword_unified_차단이면_미확인():
    row = ke.check_keyword_unified(
        "우아덤", "비건세제", "마이카페", OUR_URL, html=SAMPLE_UNIFIED_BLOCKED
    )
    assert row.status == "unknown"


def test_integrated_search_url_인코딩():
    url = ke.integrated_search_url("비건 세제")
    assert url.startswith("https://search.naver.com/search.naver?query=")
    assert "%EB%B9%84%EA%B1%B4" in url


# --------------------------------------------------------------------
# 2026-09-23 1차 — 검색어 자동완성 정규화 / 끝까지 스크롤
# --------------------------------------------------------------------
def test_naver_autocomplete_first_첫_항목(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": [[["비건 세제", "1"], ["비건세제 추천", "1"]]]}

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "get", lambda url, timeout=5.0: FakeResp())
    assert ke.naver_autocomplete_first("비건세제") == "비건 세제"


def test_naver_autocomplete_first_실패시_빈문자열(monkeypatch):
    import httpx as _httpx

    def boom(url, timeout=5.0):
        raise RuntimeError("network down")

    monkeypatch.setattr(_httpx, "get", boom)
    assert ke.naver_autocomplete_first("비건세제") == ""


def test_resolve_search_query_자동완성_우선(monkeypatch):
    monkeypatch.setattr(ke, "naver_autocomplete_first", lambda kw, timeout=5.0: "비건 세제")
    assert ke.resolve_search_query("비건세제") == "비건 세제"


def test_resolve_search_query_자동완성_없으면_원문_폴백(monkeypatch):
    monkeypatch.setattr(ke, "naver_autocomplete_first", lambda kw, timeout=5.0: "")
    # pykospacing 미설치 환경이면 원문 그대로
    assert ke.resolve_search_query("비건세제") == "비건세제"


# --------------------------------------------------------------------
# 2026-09-23 2차 — 카페 후보(1차 관문) → 댓글 식별어(2차 확정)
# --------------------------------------------------------------------
def test_is_our_cafe_url_카페번호로_판별():
    assert ke.is_our_cafe_url("https://cafe.naver.com/ca-fe/cafes/555/articles/1", REGISTRY)
    assert not ke.is_our_cafe_url("https://cafe.naver.com/othercafe/2", REGISTRY)
    assert not ke.is_our_cafe_url("https://blog.naver.com/someone/1", REGISTRY)


def test_extract_ordered_result_links_화면_순서대로_중복제거():
    html = f"""
    <a href="https://blog.naver.com/a/1">블로그</a>
    <a href="{OUR_URL}">우리글</a>
    <a href="{OUR_URL}">우리글(중복)</a>
    <a href="https://cafe.naver.com/other/2">다른카페</a>
    """
    links = ke.extract_ordered_result_links(html)
    assert [l["url"] for l in links] == [
        "https://blog.naver.com/a/1", OUR_URL, "https://cafe.naver.com/other/2",
    ]


def test_brand_identifiers_설정에서_읽는다(tmp_path):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path  # config/brands.yaml 없음 → 브랜드명 폴백
    assert ke.brand_identifiers(rt, "우아덤") == ["우아덤"]


def test_article_has_identifier():
    assert ke.article_has_identifier("이거 우아덤 제품 써봤어요", ["우아덤"])
    assert not ke.article_has_identifier("그냥 딴 얘기", ["우아덤"])


def test_confirm_our_article_article_index_있으면_바로_확정(tmp_path):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path

    class FakeIndex:
        def has_article_id(self, aid):
            return aid == "555"

    assert ke.confirm_our_article(rt, "우아덤", OUR_URL, IDENTS, article_index=FakeIndex()) is True


def test_confirm_our_article_캐시_24시간_재사용(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    calls = []
    monkeypatch.setattr(ke, "fetch_article_text", lambda url, cookies_path=None: calls.append(url) or "우아덤 후기입니다")

    first = ke.confirm_our_article(rt, "우아덤", OUR_URL, IDENTS)
    assert first is True
    assert len(calls) == 1

    second = ke.confirm_our_article(rt, "우아덤", OUR_URL, IDENTS)  # 캐시로 재사용
    assert second is True
    assert len(calls) == 1  # 다시 열지 않았다


def test_confirm_our_article_식별어_없으면_거짓(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    monkeypatch.setattr(ke, "fetch_article_text", lambda url, cookies_path=None: "그냥 딴 얘기")
    assert ke.confirm_our_article(rt, "우아덤", OUR_URL, IDENTS) is False


def test_judge_keyword_exposure_후보중_확정되면_노출완(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    monkeypatch.setattr(ke, "load_cafe_registry", lambda rt_: REGISTRY)
    monkeypatch.setattr(ke, "brand_identifiers", lambda rt_, brand: IDENTS)
    monkeypatch.setattr(ke, "confirm_our_article", lambda rt_, brand, url, idents, **kw: url == OUR_URL)

    out = ke.judge_keyword_exposure(
        rt, "우아덤", "비건세제", dom_html=SAMPLE_UNIFIED_TOP2, search_query="비건세제",
        sleep_fn=lambda s: None,
    )
    assert out["status"] == "exposed"
    assert out["rank"] == 2  # 블로그(1) 다음의 카페 글(2)
    assert out["candidates"] == 1  # 다른카페는 등록 카페가 아니라 후보 아님
    assert out["opened"] == 1
    assert out["matched_url"] == OUR_URL


def test_judge_keyword_exposure_후보있어도_식별어없으면_밀려남(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    monkeypatch.setattr(ke, "load_cafe_registry", lambda rt_: REGISTRY)
    monkeypatch.setattr(ke, "brand_identifiers", lambda rt_, brand: IDENTS)
    monkeypatch.setattr(ke, "confirm_our_article", lambda rt_, brand, url, idents, **kw: False)

    out = ke.judge_keyword_exposure(
        rt, "우아덤", "비건세제", dom_html=SAMPLE_UNIFIED_TOP2, search_query="비건세제",
        sleep_fn=lambda s: None,
    )
    assert out["status"] == "pushed"
    assert out["candidates"] == 1
    assert out["opened"] == 1


def test_judge_keyword_exposure_후보없으면_열지않고_밀려남(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    monkeypatch.setattr(ke, "load_cafe_registry", lambda rt_: REGISTRY)
    monkeypatch.setattr(ke, "brand_identifiers", lambda rt_, brand: IDENTS)
    opened = []
    monkeypatch.setattr(ke, "confirm_our_article", lambda rt_, brand, url, idents, **kw: opened.append(url) or False)

    out = ke.judge_keyword_exposure(
        rt, "우아덤", "비건세제", dom_html=SAMPLE_UNIFIED_NOT_FOUND, search_query="비건세제",
    )
    assert out["status"] == "pushed"
    assert out["candidates"] == 0
    assert out["opened"] == 0
    assert opened == []  # 후보가 없으니 아예 안 연다


def test_judge_keyword_exposure_DOM_실패시_미확인(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path

    def boom(query, cookies_path=None):
        raise RuntimeError("차단")

    monkeypatch.setattr(ke, "fetch_integrated_search_dom", boom)
    out = ke.judge_keyword_exposure(rt, "우아덤", "비건세제", search_query="비건세제")
    assert out["status"] == "unknown"


# --------------------------------------------------------------------
# B2 무한 순환 순서
# --------------------------------------------------------------------
def test_next_cycle_batch_검색량_큰_순(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    monkeypatch.setattr(
        ke,
        "keyword_universe",
        lambda rt_, brand: [
            {"keyword": "적게", "cafe": "c", "article_url": OUR_URL, "t0_status": "", "candidate_title_norm": "", "volume": 10},
            {"keyword": "많이", "cafe": "c", "article_url": OUR_URL, "t0_status": "", "candidate_title_norm": "", "volume": 1000},
            {"keyword": "중간", "cafe": "c", "article_url": OUR_URL, "t0_status": "", "candidate_title_norm": "", "volume": 100},
        ],
    )
    batch = ke.next_cycle_batch(rt, "우아덤", n=3)
    assert [i["keyword"] for i in batch] == ["많이", "중간", "적게"]


def test_next_cycle_batch_확인한_것은_뒤로_밀린다_순환(tmp_path, monkeypatch):
    """같은 검색량이면 마지막 확인이 오래된(=미확인) 것부터 → 확인하고 나면
    다음 배치에서 뒤로 밀린다(커서 없이도 무한 순환이 되는지 확인)."""
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    items = [
        {"keyword": "A", "cafe": "c", "article_url": OUR_URL, "t0_status": "", "candidate_title_norm": "", "volume": 50},
        {"keyword": "B", "cafe": "c", "article_url": OUR_URL, "t0_status": "", "candidate_title_norm": "", "volume": 50},
    ]
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: items)

    first = ke.next_cycle_batch(rt, "우아덤", n=1)
    assert first[0]["keyword"] == "A"

    row = ke.check_keyword_unified("우아덤", "A", "c", OUR_URL, html=SAMPLE_UNIFIED_TOP2)
    store.save(rt.conn, row.as_row())

    second = ke.next_cycle_batch(rt, "우아덤", n=1)
    assert second[0]["keyword"] == "B"  # A는 방금 확인해서 뒤로 밀림


def test_cycle_start_stop_status(tmp_path):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    assert ke.cycle_status(rt) == {"enabled": False}

    state = ke.cycle_start(rt, brands=["우아덤", "장으뜸"])
    assert state["enabled"] is True
    assert state["brands"] == ["우아덤", "장으뜸"]

    state2 = ke.cycle_status(rt)
    assert state2["enabled"] is True

    ke.cycle_stop(rt)
    assert ke.cycle_status(rt)["enabled"] is False


def _patch_judge(monkeypatch, status, rank=None, matched_url="", search_query="키워드1", candidates=1, opened=1):
    monkeypatch.setattr(
        ke,
        "judge_keyword_exposure",
        lambda rt_, brand, keyword, **kw: {
            "search_query": search_query, "candidates": candidates, "opened": opened,
            "status": status, "rank": rank, "matched_url": matched_url,
        },
    )


def test_cycle_tick_하나씩_진행(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    items = [
        {"keyword": "키워드1", "cafe": "c", "article_url": OUR_URL, "t0_status": "", "candidate_title_norm": "", "volume": 10},
    ]
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: items)
    _patch_judge(monkeypatch, "exposed", rank=1, matched_url=OUR_URL)

    ke.cycle_start(rt, brands=["우아덤"])
    out = ke.cycle_tick(rt, now_mono=1000.0)
    assert out["brand"] == "우아덤"
    assert out["keyword"] == "키워드1"
    assert out["status"] == "exposed"
    assert out["candidates"] == 1
    assert out["opened"] == 1

    # 최소 간격(3~6초) 안 지났으면 다음 호출은 대기
    out2 = ke.cycle_tick(rt, now_mono=1001.0)
    assert out2 == {"waiting": True}

    # DB에 이력이 남았는지(검색어도 같이)
    rows = store.latest_by_keyword(rt.conn, "우아덤")
    assert len(rows) == 1
    assert rows[0]["status"] == "exposed"
    assert rows[0]["search_query"] == "키워드1"

    # CSV/summary도 같이 갱신됐는지
    csv_path = ke.exposure_dir(rt) / "우아덤.csv"
    assert csv_path.exists()
    with csv_path.open(encoding="utf-8-sig") as fh:
        rows_csv = list(csv.DictReader(fh))
    assert rows_csv[0]["키워드"] == "키워드1"
    assert rows_csv[0]["노출 상태"] == "노출완"
    assert rows_csv[0]["비밀번호"] == ""

    summary_path = ke.exposure_dir(rt) / "우아덤.summary.json"
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    assert data["counts"]["exposed"] == 1
    assert data["total_volume_p1"] == 10
    assert data["exposed_volume_q1"] == 10


def test_cycle_tick_꺼져있으면_아무일도_안함(tmp_path):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    assert ke.cycle_tick(rt) is None


def test_cycle_tick_연속_차단이면_30분_휴식(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    items = [
        {"keyword": f"키워드{i}", "cafe": "c", "article_url": OUR_URL, "t0_status": "", "candidate_title_norm": "", "volume": 10}
        for i in range(1, 12)
    ]
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: items)
    _patch_judge(monkeypatch, "unknown")

    ke.cycle_start(rt, brands=["우아덤"])
    t = 0.0
    last = None
    for _ in range(ke.BLOCK_STREAK_LIMIT):
        t += 10.0
        last = ke.cycle_tick(rt, now_mono=t)
        assert last["status"] == "unknown"

    state = ke.cycle_status(rt)
    assert state.get("paused_until_mono") is not None

    # 휴식 중엔 검사 안 함
    paused = ke.cycle_tick(rt, now_mono=t + 10.0)
    assert paused == {"paused": True}


def test_명령어_노출_순환_시작_중지_상태():
    spec = parse_korean_command("노출 순환 시작")
    assert spec.task == "exposure_cycle_start"
    spec2 = parse_korean_command("노출 순환 중지")
    assert spec2.task == "exposure_cycle_stop"
    spec3 = parse_korean_command("노출 순환 상태")
    assert spec3.task == "exposure_cycle_status"


def test_명령어_기존_노출_현황은_그대로():
    spec = parse_korean_command("키워드 노출 현황")
    assert spec.task == "keyword_exposure"


def test_known_brands_시트파일_목록(tmp_path):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    (tmp_path / "data" / "brand_sheet_우아덤.xlsx").write_bytes(b"")
    (tmp_path / "data" / "brand_sheet_장으뜸.xlsx").write_bytes(b"")
    assert ke.known_brands(rt) == ["우아덤", "장으뜸"]
