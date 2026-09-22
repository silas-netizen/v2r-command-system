"""B1~B4 — 통합검색(통검) 판정·무한 순환·CSV/summary 테스트.

docs/reports/keyword-program-plan-2026-09-22.md B절 구현.
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


def test_check_keyword_unified_노출완():
    row = ke.check_keyword_unified(
        "우아덤", "비건세제", "마이카페", OUR_URL, html=SAMPLE_UNIFIED_TOP2
    )
    assert row.status == "exposed"
    # 순위는 카페 글 링크만 순서대로 센다(블로그는 다른 검사 대상이 아니라서 안 셈)
    assert row.rank == 1


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


def test_cycle_tick_하나씩_진행(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    items = [
        {"keyword": "키워드1", "cafe": "c", "article_url": OUR_URL, "t0_status": "", "candidate_title_norm": "", "volume": 10},
    ]
    monkeypatch.setattr(ke, "keyword_universe", lambda rt_, brand: items)
    monkeypatch.setattr(ke, "fetch_integrated_search_html", lambda kw, cookies=None: SAMPLE_UNIFIED_TOP2)

    ke.cycle_start(rt, brands=["우아덤"])
    out = ke.cycle_tick(rt, now_mono=1000.0)
    assert out["brand"] == "우아덤"
    assert out["keyword"] == "키워드1"
    assert out["status"] == "exposed"

    # 최소 간격(3~6초) 안 지났으면 다음 호출은 대기
    out2 = ke.cycle_tick(rt, now_mono=1001.0)
    assert out2 == {"waiting": True}

    # DB에 이력이 남았는지
    rows = store.latest_by_keyword(rt.conn, "우아덤")
    assert len(rows) == 1
    assert rows[0]["status"] == "exposed"

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
    monkeypatch.setattr(ke, "fetch_integrated_search_html", lambda kw, cookies=None: SAMPLE_UNIFIED_BLOCKED)

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
