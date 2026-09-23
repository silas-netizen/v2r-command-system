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
  <a href="https://blog.naver.com/someone/123456">블로그 글</a>
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


# --------------------------------------------------------------------
# 2026-09-23 재현율 시험에서 발견: 실제 통검 결과 링크는 카페번호가 아니라
# 별칭(alias, 영문) 경로다 — 별칭→카페번호 조회 경로
# --------------------------------------------------------------------
def test_cafe_alias_from_url():
    assert ke.cafe_alias_from_url("https://cafe.naver.com/llchyll/12345?art=abc") == "llchyll"
    assert ke.cafe_alias_from_url("https://cafe.naver.com/ca-fe/cafes/555/articles/1") == ""
    assert ke.cafe_alias_from_url("https://blog.naver.com/someone/1") == ""
    assert ke.cafe_alias_from_url("https://cafe.naver.com/m/") == ""


def test_resolve_cafe_alias_id_캐시(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    calls = []

    class FakeResp:
        text = "... clubid=555 ..."

    import httpx as _httpx

    def fake_get(url, cookies=None, headers=None, timeout=8.0, follow_redirects=True):
        calls.append(url)
        return FakeResp()

    monkeypatch.setattr(_httpx, "get", fake_get)
    assert ke.resolve_cafe_alias_id(rt, "mycafe") == 555
    assert ke.resolve_cafe_alias_id(rt, "mycafe") == 555  # 캐시로 재사용
    assert len(calls) == 1


def test_resolve_cafe_alias_id_못찾으면_None_캐시(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path

    class FakeResp:
        text = "회원이 아닙니다"

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "get", lambda *a, **kw: FakeResp())
    assert ke.resolve_cafe_alias_id(rt, "othercafe") is None


def test_is_our_cafe_candidate_빠른경로_먼저(tmp_path):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    # 등록된 별칭("mycafe")이면 네트워크 없이도 바로 확정
    assert ke.is_our_cafe_candidate(rt, OUR_URL, REGISTRY) is True


def test_looks_like_cafe_article_url_카페홈은_제외():
    assert ke.looks_like_cafe_article_url("https://cafe.naver.com/cantsb/3566853?art=abc")
    assert ke.looks_like_cafe_article_url("https://cafe.naver.com/ca-fe/cafes/555/articles/1")
    assert not ke.looks_like_cafe_article_url("https://cafe.naver.com/cantsb")
    assert not ke.looks_like_cafe_article_url("https://cafe.naver.com/")


def test_is_our_cafe_candidate_별칭_조회로_확정(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    registry_no_alias = [{"name": "마이카페", "cafe_id": 555, "aliases": []}]
    monkeypatch.setattr(ke, "resolve_cafe_alias_id", lambda rt_, alias, cookies=None: 555 if alias == "llchyll" else None)

    assert ke.is_our_cafe_candidate(
        rt, "https://cafe.naver.com/llchyll/12345?art=abc", registry_no_alias
    ) is True
    assert ke.is_our_cafe_candidate(
        rt, "https://cafe.naver.com/othercafe/1", registry_no_alias
    ) is False


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
    # `config/brands.yaml`은 이제 `repo_root`가 아니라 `config_dir`(항상 진짜
    # 저장소 설정)에서 읽는다(2026-09-23, 시험이 `repo_root`를 가짜로 돌려도
    # 설정 확인 시험이 빈 설정을 보지 않게 분리). 여기서는 "설정 파일이
    # 없을 때 브랜드명으로 대체" 동작을 확인해야 하므로 `config_dir` 자체를
    # 빈 폴더로 돌린다.
    rt.settings.config_dir = tmp_path / "config"  # brands.yaml 없음 → 브랜드명 폴백
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
    # 2026-09-23 재지시: 식별어는 댓글에서만 찾는다 — "답글쓰기" 마커로 끝나는
    # 댓글 블록 형태로 fixture를 맞춘다(extract_comments가 쓰는 실제 UI 패턴).
    comment_page = "댓글 1\n작성자\n\n우아덤 후기입니다\n\n2026.09.10. 10:00\n답글쓰기\n댓글을 입력하세요"
    monkeypatch.setattr(ke, "fetch_article_text", lambda url, cookies_path=None: calls.append(url) or comment_page)

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
    monkeypatch.setattr(
        ke, "confirm_our_article_detail",
        lambda rt_, brand, url, idents, **kw: {"ours": url == OUR_URL, "via": "tree", "hit": None},
    )

    out = ke.judge_keyword_exposure(
        rt, "우아덤", "비건세제", dom_html=SAMPLE_UNIFIED_TOP2, search_query="비건세제",
        sleep_fn=lambda s: None,
    )
    assert out["status"] == "exposed"
    # 2026-09-23 정정: rank는 이제 일반 결과(광고·내비 제외, v2r/knowledge/serp.py)만
    # 세서 블로그(1) 다음의 카페 글(2) — rank_overall은 옛 방식(전체 링크 순번)이다.
    assert out["rank"] == 2  # 블로그(1) 다음의 카페 글(2)
    assert out["rank_overall"] == 2  # 이 고정 fixture는 광고/내비 링크가 없어 우연히 같다
    assert out["candidates"] == 1  # 다른카페는 등록 카페가 아니라 후보 아님
    assert out["opened"] == 1
    assert out["matched_url"] == OUR_URL


def test_judge_keyword_exposure_카페홈_링크는_후보에서_제외(tmp_path, monkeypatch):
    """카페 홈(글 번호 없음) 링크가 검색 결과에 섞여 있어도 후보로 열지 않는다
    (2026-09-23 재현율 시험에서 실제로 섞여 있는 걸 발견)."""
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    html = f"""
    <a href="https://cafe.naver.com/mycafe">카페 홈</a>
    <a href="{OUR_URL}">우리 글</a>
    """
    monkeypatch.setattr(ke, "load_cafe_registry", lambda rt_: REGISTRY)
    monkeypatch.setattr(ke, "brand_identifiers", lambda rt_, brand: IDENTS)
    opened_urls = []
    monkeypatch.setattr(
        ke, "confirm_our_article_detail",
        lambda rt_, brand, url, idents, **kw: opened_urls.append(url) or {"ours": url == OUR_URL, "via": "tree", "hit": None},
    )
    out = ke.judge_keyword_exposure(
        rt, "우아덤", "비건세제", dom_html=html, search_query="비건세제", sleep_fn=lambda s: None,
    )
    assert out["candidates"] == 1  # 카페 홈은 후보에서 빠지고 글 링크만 남는다
    assert opened_urls == [OUR_URL]


def test_judge_keyword_exposure_후보있어도_식별어없으면_밀려남(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    monkeypatch.setattr(ke, "load_cafe_registry", lambda rt_: REGISTRY)
    monkeypatch.setattr(ke, "brand_identifiers", lambda rt_, brand: IDENTS)
    monkeypatch.setattr(
        ke, "confirm_our_article_detail",
        lambda rt_, brand, url, idents, **kw: {"ours": False, "via": "tree", "hit": None},
    )

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
    monkeypatch.setattr(
        ke, "confirm_our_article_detail",
        lambda rt_, brand, url, idents, **kw: opened.append(url) or {"ours": False, "via": "tree", "hit": None},
    )

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


# --------------------------------------------------------------------
# 연결 3(2026-09-23): 순환 대상 = 시트 H열 ∪ DB 원고 대상(relevance 0~2).
# 무관(3)은 DB에서 애초에 안 뽑히니 순환에서 자동 제외된다.
# --------------------------------------------------------------------
def _make_relevance_db(path, rows):
    import sqlite3

    con = sqlite3.connect(str(path))
    con.execute(
        "create table keywords (keyword text, total integer, relevance_llm integer, "
        "relevance_codex integer, needs_review integer)"
    )
    con.executemany(
        "insert into keywords (keyword, total, relevance_llm, relevance_codex, needs_review) "
        "values (?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def test_relevance_eligible_keywords_무관_제외(tmp_path):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    db_path = tmp_path / "data" / "keywords" / "우아덤.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _make_relevance_db(
        db_path,
        [
            ("직접키워드", 500, 0, 1, 0),
            ("무관키워드", 300, 3, 3, 0),  # 무관(3) — 제외돼야 함
            ("검토대기", 200, 1, 1, 1),  # needs_review — 제외돼야 함
        ],
    )
    out = ke._relevance_eligible_keywords(rt, "우아덤")
    kws = {i["keyword"] for i in out}
    assert kws == {"직접키워드"}


def test_keyword_universe_시트와_DB_합친다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    monkeypatch.setattr(ke, "target_keywords", lambda brand, cfg, xlsx_path=None, article_index=None: [
        {"keyword": "시트키워드", "cafe": "c", "article_url": "", "t0_status": "", "candidate_title_norm": ""},
    ])
    db_path = tmp_path / "data" / "keywords" / "우아덤.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _make_relevance_db(db_path, [("DB키워드", 400, 2, 0, 0), ("무관", 100, 3, 3, 0)])

    universe = ke.keyword_universe(rt, "우아덤")
    kws = {i["keyword"] for i in universe}
    assert "시트키워드" in kws
    assert "DB키워드" in kws
    assert "무관" not in kws


# --------------------------------------------------------------------
# 연결 1(2026-09-23): 순환 판정 → 시트 반영은 20건 또는 5분마다 묶어서 1번
# --------------------------------------------------------------------
def test_사이클_틱_20건_미만이면_바로_시트에_안_쓴다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    items = [
        {"keyword": "키워드1", "cafe": "c", "article_url": OUR_URL, "t0_status": "", "candidate_title_norm": "", "volume": 10},
    ]
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: items)
    monkeypatch.setattr(ke, "cycle_start", ke.cycle_start)  # (실제 함수, 시트 반영 실패는 무시됨)
    _patch_judge(monkeypatch, "exposed", rank=1, matched_url=OUR_URL)

    calls = []
    from v2r.sources import sheets_writer
    monkeypatch.setattr(sheets_writer, "apply_exposure", lambda *a, **kw: calls.append(a) or {"written": 1})
    monkeypatch.setattr(sheets_writer, "sync_keywords_to_sheet", lambda *a, **kw: {"skipped": True})

    ke.cycle_start(rt, brands=["우아덤"])
    ke.cycle_tick(rt, now_mono=1000.0)
    assert calls == []  # 1건뿐이라 아직 안 흘려보낸다

    ke.flush_sheet_batch_now(rt, "우아덤")
    import time as _time
    _time.sleep(0.2)  # 배경 스레드가 apply_exposure를 부를 시간
    assert len(calls) == 1
    brand_arg, rows_arg = calls[0][0], calls[0][1]
    assert brand_arg == "우아덤"
    assert rows_arg[0]["keyword"] == "키워드1"
    assert rows_arg[0]["status"] == "노출완"


def test_사이클_틱_20건_차면_자동으로_흘려보낸다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    items = [
        {"keyword": f"키워드{i}", "cafe": "c", "article_url": OUR_URL, "t0_status": "", "candidate_title_norm": "", "volume": 10}
        for i in range(1, 25)
    ]
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: items)
    _patch_judge(monkeypatch, "exposed", rank=1, matched_url=OUR_URL)

    calls = []
    from v2r.sources import sheets_writer
    monkeypatch.setattr(sheets_writer, "apply_exposure", lambda *a, **kw: calls.append(a) or {"written": 1})
    monkeypatch.setattr(sheets_writer, "sync_keywords_to_sheet", lambda *a, **kw: {"skipped": True})

    ke.cycle_start(rt, brands=["우아덤"])
    t = 0.0
    for _ in range(20):
        t += 10.0
        ke.cycle_tick(rt, now_mono=t)

    import time as _time
    _time.sleep(0.2)
    assert len(calls) == 1  # 20건 찼을 때 한 번만
    assert len(calls[0][1]) == 20


# ---------------------------------------------------------------------------
# 2026-09-23 후속 — 카페 카드 대표/서브, 댓글2 계열 위치 (교차 검증 중 정정)
# ---------------------------------------------------------------------------

from v2r.knowledge import serp as _serp  # noqa: E402


_CARD_HTML = """
<div>
  <a data-heatmap-target="articleSourceJSX_title" href="https://cafe.naver.com/cantsb">카페이름</a>
  <a class="fds-ugc-ellipsis3" href="https://cafe.naver.com/cantsb/100?art=x">대표 글 제목</a>
  <a class="fds-reply-box" href="https://cafe.naver.com/cantsb/100?art=x">댓글 미리보기1</a>
  <a class="fds-reply-box" href="https://cafe.naver.com/cantsb/100?art=x">댓글 미리보기2</a>
  <span class="VertDivider"></span>
  <a data-heatmap-target=".series" href="https://cafe.naver.com/cantsb/200?art=y">관련 글(서브)</a>
</div>
"""


def test_extract_cafe_cards_대표와_서브를_가른다():
    cards = _serp.extract_cafe_cards(_CARD_HTML)
    assert len(cards) == 1
    card = cards[0]
    assert "cantsb/100" in card["representative_url"]
    assert len(card["sub_urls"]) == 1
    assert "cantsb/200" in card["sub_urls"][0]


def test_extract_cafe_cards_마커없으면_빈목록():
    assert _serp.extract_cafe_cards("<html><body>그냥 글</body></html>") == []


_COMMENT_TREE_HTML = (
    '<li id="1" class="CommentItem">A</li>'
    '<div class="comment_text_view"><p>첫 댓글 아무 말</p></div>'
    '<li id="2" class="CommentItem CommentItem--reply">B</li>'
    '<div class="comment_text_view"><p>첫 댓글 답글 아무 말</p></div>'
    '<li id="3" class="CommentItem">C</li>'
    '<div class="comment_text_view"><p>팥순추출물 드셔보세요</p></div>'
    '<li id="4" class="CommentItem CommentItem--reply">D</li>'
    '<div class="comment_text_view"><p>저도 먹어요 좋아요</p></div>'
)


def test_extract_comment_tree_top_index와_is_reply():
    tree = ke.extract_comment_tree(_COMMENT_TREE_HTML)
    assert [t["top_index"] for t in tree] == [1, 1, 2, 2]
    assert [t["is_reply"] for t in tree] == [False, True, False, True]
    assert tree[2]["text"] == "팥순추출물 드셔보세요"


def test_find_identifier_in_reply2_series_댓글2에서_찾으면_확정():
    tree = ke.extract_comment_tree(_COMMENT_TREE_HTML)
    hit = ke.find_identifier_in_reply2_series(tree, ["팥순추출물"])
    assert hit is not None
    assert hit["top_index"] == 2
    assert hit["out_of_position"] is False


def test_find_identifier_in_reply2_series_댓글1에만_있으면_위치이탈():
    html = (
        '<li id="1" class="CommentItem">A</li>'
        '<div class="comment_text_view"><p>팥순추출물 언급</p></div>'
        '<li id="2" class="CommentItem">B</li>'
        '<div class="comment_text_view"><p>그냥 딴 얘기</p></div>'
    )
    tree = ke.extract_comment_tree(html)
    hit = ke.find_identifier_in_reply2_series(tree, ["팥순추출물"])
    assert hit is not None
    assert hit["out_of_position"] is True
    assert hit["top_index"] == 1


def test_find_identifier_in_reply2_series_없으면_None():
    tree = ke.extract_comment_tree(_COMMENT_TREE_HTML)
    assert ke.find_identifier_in_reply2_series(tree, ["없는식별어"]) is None
