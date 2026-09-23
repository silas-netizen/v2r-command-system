"""`v2r/content/top_reference.py` 테스트 — 네트워크 없이 저장된 픽스처만 쓴다.

픽스처 `tests/fixtures/top_reference_search_dom.html`,
`tests/fixtures/top_reference_article.html`는 2026-09-23에 키워드
"다이어트 보조제 추천"으로 실제 헤드리스 조회 1회를 해서 저장한 것이다
(계정명·개인정보 없음 — 카페 공개 글 본문만).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from v2r.content import top_reference as tr

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_DOM = (FIXTURES / "top_reference_search_dom.html").read_text(encoding="utf-8")
ARTICLE_HTML = (FIXTURES / "top_reference_article.html").read_text(encoding="utf-8")


def _rt(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(repo_root=str(tmp_path), data_dir=str(tmp_path / "data")))


def _make_keywords_db(tmp_path: Path, brand: str, rows: list[tuple[str, float, float]]) -> None:
    db_dir = tmp_path / "data" / "keywords"
    db_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_dir / f"{brand}.sqlite"))
    con.execute("CREATE TABLE keywords (keyword TEXT, total REAL, relevance REAL)")
    con.executemany("INSERT INTO keywords (keyword, total, relevance) VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# 1등 글 추출
# ---------------------------------------------------------------------------


def test_pick_top_link_from_fixture_dom():
    picked = tr._pick_top_link(SEARCH_DOM)
    assert picked is not None
    assert picked["source"] == "cafe"
    assert "cafe.naver.com" in picked["url"]
    assert "imsanbu" in picked["url"]


def test_pick_top_link_excludes_ads_and_nav():
    html = (
        '<a href="https://shopping.naver.com/catalog/123">광고</a>'
        '<a href="https://blog.naver.com/MyBlog.naver">내 블로그</a>'
        '<a href="https://cafe.naver.com/imsanbu/80102656">진짜 글</a>'
    )
    picked = tr._pick_top_link(html)
    assert picked is not None
    assert picked["url"] == "https://cafe.naver.com/imsanbu/80102656"


def test_pick_top_link_none_when_nothing_matches():
    assert tr._pick_top_link("<a href='https://shopping.naver.com/x'>광고</a>") is None
    assert tr._pick_top_link("") is None


def test_pick_top_link_default_is_cafe_only_ignores_earlier_blog():
    # 2026-09-23 정정 — 블로그가 화면에 먼저 나와도 카페만 대상이면 블로그는 무시한다
    html = (
        '<a href="https://blog.naver.com/someone/224403640216">먼저 나오는 블로그</a>'
        '<a href="https://cafe.naver.com/imsanbu/80102656">나중에 나오는 카페</a>'
    )
    picked = tr._pick_top_link(html)  # sources 생략 → 기본 config(카페만)
    assert picked is not None
    assert picked["source"] == "cafe"
    assert picked["url"] == "https://cafe.naver.com/imsanbu/80102656"


def test_pick_top_link_none_when_only_blog_and_sources_is_cafe():
    html = '<a href="https://blog.naver.com/someone/224403640216">블로그만 있음</a>'
    assert tr._pick_top_link(html, sources=["cafe"]) is None


def test_pick_top_link_sources_param_can_widen():
    html = '<a href="https://blog.naver.com/someone/224403640216">블로그</a>'
    picked = tr._pick_top_link(html, sources=["cafe", "blog"])
    assert picked is not None
    assert picked["source"] == "blog"


def test_allowed_sources_default_and_config(monkeypatch):
    monkeypatch.setattr(tr, "_cfg", lambda: {})
    assert tr._allowed_sources() == ["cafe"]
    monkeypatch.setattr(tr, "_cfg", lambda: {"sources": ["cafe", "blog"]})
    assert tr._allowed_sources() == ["cafe", "blog"]


# ---------------------------------------------------------------------------
# SERP 순위 추출 (2026-09-23 재정정: 카페 이름 배지 오탐 수정)
# ---------------------------------------------------------------------------


def test_extract_serp_ignores_cafe_badge_link_before_real_article():
    # 실제 통검 DOM 모양 — 카페 이름 배지(글 번호 없음)가 진짜 글 링크 바로
    # 앞에 따로 박혀 있다. 배지는 "카페" 결과로 세면 안 된다(2026-09-23 재정정
    # 지시 — rank_in_source가 항상 2로 나오던 버그의 원인).
    html = (
        '<a href="https://cafe.naver.com/gpsf">카페이름배지</a>'
        '<a href="https://cafe.naver.com/gpsf/1169447?art=xyz">진짜 첫 글</a>'
        '<a href="https://cafe.naver.com/gloseems1">카페이름배지2</a>'
        '<a href="https://cafe.naver.com/gloseems1/1299492">진짜 둘째 글</a>'
    )
    serp = tr.extract_serp(html, max_n=15)
    cafe_items = [it for it in serp if it["source"] == "cafe"]
    assert len(cafe_items) == 2
    assert cafe_items[0]["url"] == "https://cafe.naver.com/gpsf/1169447"
    assert cafe_items[1]["url"] == "https://cafe.naver.com/gloseems1/1299492"


def test_extract_serp_ads_and_shopping_and_order():
    html = (
        '<a href="https://ader.naver.com/v1/AAAAAAAAAAAAAAAAtoken1">광고1</a>'
        '<a href="https://shopping.naver.com/catalog/999">쇼핑</a>'
        '<a href="https://cafe.naver.com/imsanbu">배지</a>'
        '<a href="https://cafe.naver.com/imsanbu/80102656">진짜 글</a>'
    )
    serp = tr.extract_serp(html, max_n=15)
    sections = [(it["section"], it["is_ad"]) for it in serp]
    assert sections == [("파워링크/광고", True), ("쇼핑", False), ("카페", False)]


def test_extract_serp_dedups_same_ad_slot():
    html = (
        '<a href="https://ader.naver.com/v1/AAAAAAAAAAAAAAAAtoken1">썸네일</a>'
        '<a href="https://ader.naver.com/v1/AAAAAAAAAAAAAAAAtoken2">제목</a>'
    )
    serp = tr.extract_serp(html, max_n=15)
    assert len(serp) == 1  # 리다이렉트 토큰 앞 16자가 같으면 한 광고 칸


def test_extract_serp_respects_max_n():
    html = "".join(
        f'<a href="https://cafe.naver.com/c{i}/{100 + i}">글{i}</a>' for i in range(20)
    )
    serp = tr.extract_serp(html, max_n=5)
    assert len(serp) == 5
    assert serp[0]["rank"] == 1 and serp[-1]["rank"] == 5


# ---------------------------------------------------------------------------
# 형식 지표 계산
# ---------------------------------------------------------------------------


def test_analyze_article_from_fixture():
    text = tr._strip_tags(ARTICLE_HTML)
    assert len(text) > 30
    ref = tr._analyze_article(text, "cafe", "https://cafe.naver.com/imsanbu/80102656", "테스트 제목")
    assert ref["source"] == "cafe"
    assert ref["length"] == len(text)
    assert ref["paragraphs"] >= 1
    assert ref["title_type"] in {"질문형", "후기형", "정보형", "기타"}
    assert ref["opening"] in {"질문", "결론 먼저", "상황 서술", "알수없음"}
    assert ref["closing_type"] in {"질문", "요청", "정리", "알수없음"}
    assert isinstance(ref["flow"], list)
    assert "fetched_at" in ref


@pytest.mark.parametrize(
    "title,expected",
    [
        ("이거 효과 있을까요?", "질문형"),
        ("다이어트 보조제 3주 후기", "후기형"),
        ("보조제 고르는 방법 총정리", "정보형"),
        ("그냥 제목", "기타"),
    ],
)
def test_title_type(title, expected):
    assert tr._title_type(title) == expected


# ---------------------------------------------------------------------------
# brief 요약 — 문장 인용 없이 형식만, 마지막 줄 고정 문구
# ---------------------------------------------------------------------------


def test_format_brief_shape_and_disclaimer():
    ref = {
        "source": "cafe",
        "title_type": "질문형",
        "length": 650,
        "paragraphs": 4,
        "images": 2,
        "opening": "상황 서술",
        "closing_type": "질문",
        "has_list": False,
        "has_subhead": False,
    }
    brief = tr.format_brief(ref)
    assert 0 < len(brief) <= 400
    assert brief.splitlines()[-1] == (
        "※ 형식만 참고. 문장·표현·사례는 절대 베끼지 말고 우리 브랜드 가이드 논리로 새로 쓴다."
    )
    # 원문 문장을 인용하지 않는다 — 실제 카페 글 문장이 안 들어가야 한다
    assert "단유 후 다이어트" not in brief


def test_format_brief_empty_for_error_or_none():
    assert tr.format_brief(None) == ""
    assert tr.format_brief({"error": "실패"}) == ""


# ---------------------------------------------------------------------------
# 검색량 조건(top_pct/min_volume)
# ---------------------------------------------------------------------------


def test_volume_gate_min_volume(tmp_path):
    rt = _rt(tmp_path)
    _make_keywords_db(tmp_path, "우아덤", [("키워드A", 150, 1), ("키워드B", 10, 2)])
    assert tr._volume_gate(rt, "우아덤", "키워드A") is True
    assert tr._volume_gate(rt, "우아덤", "키워드B") is False


def test_volume_gate_top_pct(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    # min_volume(기본 100)보다 다 낮게 둬서 top_pct 경로만 시험한다
    _make_keywords_db(
        tmp_path,
        "장으뜸",
        [("k1", 90, 1), ("k2", 80, 1), ("k3", 70, 1), ("k4", 10, 1)],
    )
    monkeypatch.setattr(tr, "_cfg", lambda: {"top_pct": 50, "min_volume": 1000})
    assert tr._volume_gate(rt, "장으뜸", "k1") is True
    assert tr._volume_gate(rt, "장으뜸", "k4") is False


def test_volume_gate_no_db_is_false(tmp_path):
    rt = _rt(tmp_path)
    assert tr._volume_gate(rt, "없는브랜드", "아무키워드") is False


# ---------------------------------------------------------------------------
# reference_for — 캐시·검색량 조건·실패 시 빈 문자열
# ---------------------------------------------------------------------------


def test_reference_for_returns_empty_when_below_volume(tmp_path):
    rt = _rt(tmp_path)
    _make_keywords_db(
        tmp_path,
        "우아덤",
        [("높은키워드1", 90, 1), ("높은키워드2", 80, 1), ("높은키워드3", 70, 1), ("낮은키워드", 1, 1)],
    )
    assert tr.reference_for(rt, "우아덤", "낮은키워드") == ""


def test_reference_for_uses_cache_and_meta(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    _make_keywords_db(tmp_path, "우아덤", [("높은키워드", 500, 1)])

    ref = tr._analyze_article(
        tr._strip_tags(ARTICLE_HTML), "cafe", "https://cafe.naver.com/imsanbu/80102656", "제목"
    )
    called = {"n": 0}

    def _fake_fetch(keyword, *, cookies_path=None, headless=True):
        called["n"] += 1
        return ref

    monkeypatch.setattr(tr, "fetch_top_article", _fake_fetch)
    meta: dict = {}
    brief1 = tr.reference_for(rt, "우아덤", "높은키워드", meta=meta)
    assert brief1
    assert meta["reference_url"] == ref["url"]
    assert called["n"] == 1

    # 두 번째 호출은 캐시를 써서 네트워크(가짜 fetch)를 다시 부르지 않는다
    brief2 = tr.reference_for(rt, "우아덤", "높은키워드")
    assert brief2 == brief1
    assert called["n"] == 1


def test_reference_for_caches_failure_and_returns_empty(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    _make_keywords_db(tmp_path, "우아덤", [("실패키워드", 500, 1)])

    def _boom(keyword, *, cookies_path=None, headless=True):
        raise RuntimeError("네트워크 실패")

    monkeypatch.setattr(tr, "fetch_top_article", _boom)
    assert tr.reference_for(rt, "우아덤", "실패키워드") == ""
    cache_path = tr._cache_path(tmp_path, "실패키워드")
    assert cache_path.exists()


def test_reference_for_disabled_by_config(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    _make_keywords_db(tmp_path, "우아덤", [("키워드", 500, 1)])
    monkeypatch.setattr(tr, "_cfg", lambda: {"enabled": False})
    assert tr.reference_for(rt, "우아덤", "키워드") == ""


# ---------------------------------------------------------------------------
# 프롬프트 삽입 — brand_writer.build_body_prompt
# ---------------------------------------------------------------------------


def test_build_body_prompt_inserts_reference_brief():
    from v2r.content import brand_writer as bw

    brief = "1위 글 출처: cafe, 제목 유형: 질문형\n※ 형식만 참고. 문장·표현·사례는 절대 베끼지 말고 우리 브랜드 가이드 논리로 새로 쓴다."
    sys_p, user_p = bw.build_body_prompt(
        "우아덤", "테스트키워드", "", "", "", None, reference_brief=brief
    )
    assert "【참고 형식(통검 1등 글)】" in user_p
    assert brief in user_p
    # system(공용, 캐시 대상)에는 절대 안 들어간다
    assert "【참고 형식(통검 1등 글)】" not in sys_p


def test_build_body_prompt_no_block_when_brief_empty():
    from v2r.content import brand_writer as bw

    sys_p, user_p = bw.build_body_prompt("우아덤", "테스트키워드", "", "", "", None, reference_brief="")
    assert "【참고 형식(통검 1등 글)】" not in user_p


# ---------------------------------------------------------------------------
# 캐시 버전(2026-09-23 정정: 카페 전용으로 스키마가 바뀌어 옛 캐시는 무효)
# ---------------------------------------------------------------------------


def test_load_cache_rejects_old_version(tmp_path):
    import json

    cache_path = tr._cache_path(tmp_path, "옛키워드")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"fetched_at": tr.now_iso(), "source": "blog"}), encoding="utf-8"
    )  # version 필드 없음 = 구버전
    assert tr._load_cache(tmp_path, "옛키워드") is None


def test_save_cache_then_load_roundtrip_has_current_version(tmp_path):
    tr._save_cache(tmp_path, "새키워드", {"fetched_at": tr.now_iso(), "source": "cafe"})
    loaded = tr._load_cache(tmp_path, "새키워드")
    assert loaded is not None
    assert loaded["version"] == tr.CACHE_VERSION
