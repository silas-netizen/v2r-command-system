"""글 등록/검증/이력 테스트."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from v2r.api import articles
from v2r.api.auth import AuthSession, DeviceProfile
from v2r.api.catalog import Cafe, Head, Menu
from v2r.api.client import V2RClient, clear_cache
from v2r.content import seone

BASE = "https://api-test.example"


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture()
def api(tmp_path):
    auth = AuthSession(
        device=DeviceProfile.load(tmp_path / "device.json"),
        session_path=tmp_path / "session.json",
    )
    auth.token = "TOK"
    http = httpx.Client(base_url=BASE, timeout=60.0)
    c = V2RClient(base_url=BASE, auth=auth, client=http)
    yield c
    c.close()


CAFE = Cafe(cafe_id=25016228, name="테스트카페")
MENU = Menu(menu_id=328, name="자유 수다방")
HEAD = Head(head_id=7, name="잡담")


def test_build_destination_iso() -> None:
    dt = datetime(2026, 8, 31, 1, 30, 0, tzinfo=timezone.utc)
    dest = articles.build_destination(CAFE, MENU, HEAD, "acc1", dt)
    assert dest["start_at"] == "2026-08-31T01:30:00Z"
    assert dest["cafe_id"] == 25016228
    assert dest["menu_id"] == 328
    assert dest["head_id"] == 7
    assert dest["head_name"] == "잡담"
    assert dest["naver_login_id"] == "acc1"
    assert dest["target_view_count"] == 0
    assert dest["parent_id"] is None


def test_build_destination_immediate_and_no_head() -> None:
    dest = articles.build_destination(CAFE, MENU, None, "acc1", None, 90)
    assert dest["start_at"] is None
    assert dest["head_id"] is None and dest["head_name"] is None
    assert dest["target_view_count"] == 90


def test_default_write_options() -> None:
    opts = articles.DEFAULT_WRITE_OPTIONS
    assert opts["enableComment"] is True
    assert opts["enableCopy"] is False
    assert opts["cclTypes"] == ["ATTRIBUTION", "NONCOMMERCIAL", "NO_DERIVATIVE"]
    assert opts["open"] is False and opts["naverOpen"] is True


def test_article_url() -> None:
    assert articles.article_url("abc") == "https://v2r.daboja.im/nc/articleDetail/abc"


def test_create_article_returns_source_id(httpx_mock, api) -> None:
    httpx_mock.add_response(
        url=f"{BASE}{articles.PATH_CREATE}",
        json={"naver_cafe_article_source": {"source_id": "SRC-1"}},
    )
    dest = articles.build_destination(CAFE, MENU, None, "acc1", None)
    sid = articles.create_article(
        api, title="제목", tags=["t"], content_json="{}", destination=dest
    )
    assert sid == "SRC-1"
    body = json.loads(httpx_mock.get_requests()[-1].content)
    assert body["tag_list"] == ["t"]
    assert body["likes"] == []
    assert body["parent_source_id"] is None


def test_find_recent_source_tolerates_404(httpx_mock, api, monkeypatch) -> None:
    monkeypatch.setattr(articles.time, "sleep", lambda s: None)
    httpx_mock.add_response(
        url=httpx.URL(
            f"{BASE}{articles.PATH_HISTORIES}",
            params={"cafe_id": 1, "days_ago": 1, "include_reserve": "true"},
        ),
        status_code=404,
        json={},
        is_reusable=True,
    )
    assert (
        articles.find_recent_source(api, 1, "acc1", "제목", None, attempts=2) is None
    )


def test_find_recent_source_matches_row(httpx_mock, api, monkeypatch) -> None:
    monkeypatch.setattr(articles.time, "sleep", lambda s: None)
    httpx_mock.add_response(
        url=httpx.URL(
            f"{BASE}{articles.PATH_HISTORIES}",
            params={"cafe_id": 1, "days_ago": 1, "include_reserve": "true"},
        ),
        json={
            "histories": [
                {
                    "source_id": "SRC-9",
                    "naver_account_login_id": "acc1",
                    "title": "제목",
                    "parent_source_id": None,
                    "status": "RESERVED",
                    "created_at": "2026-09-19T00:00:00Z",
                }
            ]
        },
    )
    got = articles.find_recent_source(api, 1, "acc1", "제목", None, attempts=1)
    assert got == "SRC-9"


def test_board_histories_404_returns_empty(httpx_mock, api) -> None:
    httpx_mock.add_response(
        url=httpx.URL(
            f"{BASE}{articles.PATH_HISTORIES}",
            params={"cafe_id": 5, "days_ago": 30, "include_reserve": "true"},
        ),
        status_code=404,
        json={},
    )
    assert articles.board_histories(api, 5) == []


def _detail(body: str, **over) -> dict:
    base = {
        "naver_cafe_article_source": {"title": "제목", "tag_list": ["a", "b"]},
        "naver_cafe_article_destination": {
            "menu_id": 328,
            "head_id": None,
            "start_at": "2026-08-31T01:30:00Z",
        },
        "naver_cafe_article_source_detail": {"body": body},
        "naver_cafe_article_source_comments": [{}, {}],
    }
    base.update(over)
    return base


def test_verify_article_ok() -> None:
    img = {"@ctype": "image", "id": "SE-1", "src": "http://x/a.jpg", "fileSize": 100}
    body = seone.content_json("첫 줄\n{사진}\n끝", [img])
    detail = _detail(body)
    problems = articles.verify_article(
        detail,
        title="제목",
        tags=["a", "b"],
        menu_id=328,
        head_id=None,
        body_lines=["첫 줄", "", "끝"],
        image_count=1,
        start_at=datetime(2026, 8, 31, 1, 30, tzinfo=timezone.utc),
        comments_count=2,
    )
    assert problems == []


def test_verify_article_reports_mismatches() -> None:
    body = seone.content_json("첫 줄", [])
    detail = _detail(body)
    problems = articles.verify_article(
        detail,
        title="다른 제목",
        tags=["a"],
        menu_id=999,
        head_id=3,
        body_lines=["다른 줄"],
        image_count=1,
        start_at=None,
        comments_count=0,
    )
    joined = " ".join(problems)
    assert "제목 불일치" in joined
    assert "태그 불일치" in joined
    assert "게시판 불일치" in joined
    assert "말머리 불일치" in joined
    assert "본문 문단 불일치" in joined
    assert "이미지 개수 불일치" in joined
    assert "예약시각 불일치" in joined
    assert "댓글 개수 불일치" in joined


def test_verify_article_detects_leftover_placeholder() -> None:
    # 서버가 돌려준 본문에 중괄호가 남아 있는 상황
    body = seone.content_json("사진 PH 자리", []).replace("사진 PH 자리", "사진 {사진} 자리")
    detail = _detail(body)
    problems = articles.verify_article(
        detail,
        title="제목",
        tags=["a", "b"],
        menu_id=328,
        head_id=None,
        body_lines=["사진 {사진} 자리"],
        image_count=0,
        start_at=datetime(2026, 8, 31, 1, 30, tzinfo=timezone.utc),
        comments_count=2,
    )
    assert any("플레이스홀더 잔존" in p for p in problems)


def test_delete_and_get_article(httpx_mock, api) -> None:
    httpx_mock.add_response(
        url=httpx.URL(f"{BASE}{articles.PATH_ARTICLE}", params={"source_id": "S"}),
        json={"ok": 1},
    )
    httpx_mock.add_response(url=f"{BASE}{articles.PATH_DELETE}", json={"ok": 2})
    assert articles.get_article(api, "S") == {"ok": 1}
    assert articles.delete_article(api, "S") == {"ok": 2}
