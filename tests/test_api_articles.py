"""글 등록/검증/이력 테스트."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from v2r.api import articles
from v2r.api.auth import AuthSession, DeviceProfile
from v2r.api.catalog import Cafe, Head, Menu
from v2r.api.client import V2RClient, clear_cache
from v2r.api.errors import V2RApiError
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


def _written_url(cafe_id, login_id, page: int = 1) -> httpx.URL:
    return httpx.URL(
        f"{BASE}{articles.PATH_WRITTEN}",
        params={"cafe_id": cafe_id, "naver_login_id": login_id, "page": page},
    )


def _histories_url(cafe_id, days_ago: int) -> httpx.URL:
    return httpx.URL(
        f"{BASE}{articles.PATH_HISTORIES}",
        params={"cafe_id": cafe_id, "days_ago": days_ago, "include_reserve": "true"},
    )


def _mock_written_empty(httpx_mock, cafe_id, login_id) -> None:
    """`written_articles` 1페이지를 빈 응답으로 세워 둔다(옛 경로 폴백 테스트용)."""
    httpx_mock.add_response(
        url=_written_url(cafe_id, login_id),
        json={"articles": [], "total_count": 0},
        is_reusable=True,
    )


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
    _mock_written_empty(httpx_mock, 1, "acc1")
    httpx_mock.add_response(
        url=_histories_url(1, 1),
        status_code=404,
        json={},
        is_reusable=True,
    )
    assert (
        articles.find_recent_source(api, 1, "acc1", "제목", None, attempts=2) is None
    )


def test_find_recent_source_matches_row(httpx_mock, api, monkeypatch) -> None:
    monkeypatch.setattr(articles.time, "sleep", lambda s: None)
    _mock_written_empty(httpx_mock, 1, "acc1")
    httpx_mock.add_response(
        url=_histories_url(1, 1),
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
    """계정을 안 넘기면 폴백할 수 없어 빈 목록."""
    httpx_mock.add_response(
        url=_histories_url(5, 30),
        status_code=404,
        json={},
    )
    assert articles.board_histories(api, 5) == []


WRITTEN_ROW = {
    "clubid": 31670254,
    "articleid": 1257,
    "menuid": 1,
    "subject": "제목",
    "writernickname": "닉",
    "writedt": "Sep 15, 2026 12:31:23 PM",
    "readcount": 3,
    "v2r_source_id": "01M2HHWSF8QSF322WR8TCCNZXS",
}


def test_written_articles_normalizes_rows(httpx_mock, api) -> None:
    httpx_mock.add_response(
        url=_written_url(31670254, "azqpale"),
        json={"articles": [dict(WRITTEN_ROW)], "total_count": 1},
    )
    rows = articles.written_articles(api, 31670254, "azqpale", pages=2)
    assert len(rows) == 1
    row = rows[0]
    assert row["source_id"] == "01M2HHWSF8QSF322WR8TCCNZXS"
    assert row["naver_account_login_id"] == "azqpale"
    assert row["title"] == "제목"
    assert row["parent_source_id"] is None
    assert row["status"] == "DONE"
    # KST 12:31 → UTC 03:31
    assert row["created_at"] == "2026-09-15T03:31:23Z"
    assert row["written_at"] == row["created_at"]
    assert row["raw"]["readcount"] == 3
    assert "v2r_source_id" not in row["raw"]


def test_board_histories_falls_back_to_written_articles(httpx_mock, api) -> None:
    httpx_mock.add_response(url=_histories_url(31670254, 30), status_code=404, json={})
    httpx_mock.add_response(
        url=_written_url(31670254, "azqpale"),
        json={"articles": [dict(WRITTEN_ROW)], "total_count": 1},
    )
    rows = articles.board_histories(api, 31670254, login_id="azqpale")
    assert [r["source_id"] for r in rows] == ["01M2HHWSF8QSF322WR8TCCNZXS"]


def test_board_histories_fallback_iterates_login_ids(httpx_mock, api) -> None:
    httpx_mock.add_response(url=_histories_url(31670254, 30), status_code=404, json={})
    for acc in ("a1", "a2"):
        httpx_mock.add_response(
            url=_written_url(31670254, acc),
            json={
                "articles": [dict(WRITTEN_ROW, v2r_source_id=f"SRC-{acc}")],
                "total_count": 1,
            },
        )
    rows = articles.board_histories(api, 31670254, login_ids=["a1", "a2"])
    assert sorted(r["source_id"] for r in rows) == ["SRC-a1", "SRC-a2"]


def test_find_recent_source_uses_written_articles(httpx_mock, api, monkeypatch) -> None:
    monkeypatch.setattr(articles.time, "sleep", lambda s: None)
    created = datetime.now(timezone.utc).astimezone(articles.KST)
    row = dict(
        WRITTEN_ROW,
        writedt=created.strftime("%b %d, %Y %I:%M:%S %p"),
        v2r_source_id="SRC-W",
    )
    httpx_mock.add_response(
        url=_written_url(31670254, "azqpale"),
        json={"articles": [row], "total_count": 1},
        is_reusable=True,
    )
    got = articles.find_recent_source(
        api,
        31670254,
        "azqpale",
        "제목",
        None,
        since=datetime.now(timezone.utc) - timedelta(minutes=5),
        attempts=1,
    )
    assert got == "SRC-W"


def test_search_board_histories_disabled_by_default(httpx_mock, api) -> None:
    assert articles.ENABLE_HISTORIES_SEARCH is False
    assert articles.search_board_histories(api, 1, {"q": 1}) == []
    assert httpx_mock.get_requests() == []


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


def _flat_live_comments() -> list[dict]:
    """라이브 응답 모양: 루트 5 + 답글 7 = 12개가 한 배열에 평탄하게 담긴다."""
    out: list[dict] = []
    for r in range(5):
        root_id = f"ROOT-{r}"
        out.append({"comment_id": root_id, "parent_comment_id": None, "comments": []})
        for k in range(2 if r < 2 else 1):
            out.append(
                {
                    "comment_id": f"REPLY-{r}-{k}",
                    "parent_comment_id": root_id,
                    "comments": [],
                }
            )
    return out


def _nested_payload_comments() -> list[dict]:
    """등록 요청 모양: 답글이 루트의 `comments`에 중첩된 5개 루트."""
    return [
        {
            "contents": f"root{r}",
            "comments": [{"contents": f"reply{r}-{k}"} for k in range(2 if r < 2 else 1)],
        }
        for r in range(5)
    ]


def test_count_comment_nodes_flat_and_nested_agree() -> None:
    assert len(_flat_live_comments()) == 12
    assert len(_nested_payload_comments()) == 5
    assert articles.count_comment_nodes(_flat_live_comments()) == 12
    assert articles.count_comment_nodes(_nested_payload_comments()) == 12
    assert articles.count_comment_nodes([]) == 0
    assert articles.count_comment_nodes(None) == 0


def _verify_comments(detail_comments: list[dict], expected: int) -> list[str]:
    body = seone.content_json("첫 줄", [])
    detail = _detail(body, naver_cafe_article_source_comments=detail_comments)
    return articles.verify_article(
        detail,
        title="제목",
        tags=["a", "b"],
        menu_id=328,
        head_id=None,
        body_lines=["첫 줄"],
        image_count=0,
        start_at=datetime(2026, 8, 31, 1, 30, tzinfo=timezone.utc),
        comments_count=expected,
    )


def test_verify_article_counts_replies_in_flat_live_shape() -> None:
    # 실제 라이브(잡 41)에서 났던 "12 != 5" 회귀: 기대값도 전체 노드 수여야 한다
    assert _verify_comments(_flat_live_comments(), 12) == []


def test_verify_article_counts_replies_in_nested_shape() -> None:
    assert _verify_comments(_nested_payload_comments(), 12) == []


def test_verify_article_still_strict_on_comment_total() -> None:
    problems = _verify_comments(_flat_live_comments()[:-1], 12)
    assert any("댓글 개수 불일치: 11 != 12" in p for p in problems)


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
        start_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
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


def test_to_iso_z_treats_naive_as_kst() -> None:
    # KST 10시 = UTC 01시
    assert articles.to_iso_z(datetime(2026, 9, 19, 10, 0)) == "2026-09-19T01:00:00Z"
    assert articles.to_iso_z(None) is None
    aware = datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc)
    assert articles.to_iso_z(aware) == "2026-09-19T01:00:00Z"


def test_verify_article_skips_start_at_when_immediate() -> None:
    body = seone.content_json("첫 줄", [])
    detail = _detail(body)  # 서버가 실제 발행 시각을 채운 상태
    problems = articles.verify_article(
        detail,
        title="제목",
        tags=["a", "b"],
        menu_id=328,
        head_id=None,
        body_lines=["첫 줄"],
        image_count=0,
        start_at=None,
        comments_count=2,
    )
    assert problems == []


def test_create_article_does_not_repost_on_5xx(httpx_mock, api, monkeypatch) -> None:
    """5xx면 재POST 없이 이력 복구로 source_id를 찾는다 (중복 발행 방지)."""
    monkeypatch.setattr(articles.time, "sleep", lambda s: None)
    monkeypatch.setattr("v2r.api.client.time.sleep", lambda s: None)
    httpx_mock.add_response(
        url=f"{BASE}{articles.PATH_CREATE}", status_code=503, json={}, is_reusable=True
    )
    _mock_written_empty(httpx_mock, 25016228, "acc1")
    created = (
        datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    httpx_mock.add_response(
        url=httpx.URL(
            f"{BASE}{articles.PATH_HISTORIES}",
            params={"cafe_id": 25016228, "days_ago": 1, "include_reserve": "true"},
        ),
        json={
            "histories": [
                {
                    "source_id": "SRC-R",
                    "naver_account_login_id": "acc1",
                    "title": "제목",
                    "parent_source_id": None,
                    "status": "RESERVED",
                    "created_at": created,
                }
            ]
        },
        is_reusable=True,
    )
    dest = articles.build_destination(CAFE, MENU, None, "acc1", None)
    sid = articles.create_article(
        api, title="제목", tags=[], content_json="{}", destination=dest
    )
    assert sid == "SRC-R"
    posts = [
        r
        for r in httpx_mock.get_requests()
        if r.url.path == articles.PATH_CREATE and r.method == "POST"
    ]
    assert len(posts) == 1  # 절대 재전송하지 않는다


def test_create_article_raises_ambiguous_when_no_history(
    httpx_mock, api, monkeypatch
) -> None:
    monkeypatch.setattr(articles.time, "sleep", lambda s: None)
    httpx_mock.add_response(
        url=f"{BASE}{articles.PATH_CREATE}", status_code=502, json={}
    )
    _mock_written_empty(httpx_mock, 25016228, "acc1")
    httpx_mock.add_response(
        url=_histories_url(25016228, 1),
        json={"histories": []},
        is_reusable=True,
    )
    dest = articles.build_destination(CAFE, MENU, None, "acc1", None)
    with pytest.raises(V2RApiError) as exc:
        articles.create_article(
            api, title="제목", tags=[], content_json="{}", destination=dest
        )
    assert exc.value.kind == "ambiguous"


def test_find_recent_source_rejects_unparsable_created_at(
    httpx_mock, api, monkeypatch
) -> None:
    monkeypatch.setattr(articles.time, "sleep", lambda s: None)
    _mock_written_empty(httpx_mock, 1, "acc1")
    httpx_mock.add_response(
        url=_histories_url(1, 1),
        json={
            "histories": [
                {
                    "source_id": "SRC-X",
                    "naver_account_login_id": "acc1",
                    "title": "제목",
                    "parent_source_id": None,
                    "status": "RESERVED",
                    "created_at": "어제쯤",
                }
            ]
        },
        is_reusable=True,
    )
    assert articles.find_recent_source(api, 1, "acc1", "제목", None, attempts=1) is None


def test_find_recent_source_ambiguous_on_multiple_matches(
    httpx_mock, api, monkeypatch
) -> None:
    monkeypatch.setattr(articles.time, "sleep", lambda s: None)
    row = {
        "naver_account_login_id": "acc1",
        "title": "제목",
        "parent_source_id": None,
        "status": "RESERVED",
        "created_at": "2026-09-19T00:00:00Z",
    }
    _mock_written_empty(httpx_mock, 1, "acc1")
    httpx_mock.add_response(
        url=_histories_url(1, 1),
        json={"histories": [{**row, "source_id": "A"}, {**row, "source_id": "B"}]},
        is_reusable=True,
    )
    assert articles.find_recent_source(api, 1, "acc1", "제목", None, attempts=1) is None


def test_wait_written_defers_far_future_schedule(api) -> None:
    far = datetime.now(timezone.utc) + timedelta(hours=3)
    with pytest.raises(articles.PendingError):
        articles.wait_written(api, "SRC-1", scheduled_at=far)


def test_delete_and_get_article(httpx_mock, api) -> None:
    httpx_mock.add_response(
        url=httpx.URL(f"{BASE}{articles.PATH_ARTICLE}", params={"source_id": "S"}),
        json={"ok": 1},
    )
    httpx_mock.add_response(url=f"{BASE}{articles.PATH_DELETE}", json={"ok": 2})
    assert articles.get_article(api, "S") == {"ok": 1}
    assert articles.delete_article(api, "S") == {"ok": 2}


def test_update_article_sends_flat_body():
    from v2r.api import articles

    class _C:
        def __init__(self):
            self.sent = None

        def get(self, path, params=None, **k):
            return {
                "naver_cafe_article_source": {"tag_list": ["a"], "parent_source_id": None},
                "naver_cafe_article_destination": {
                    "write_options": {"open": False}, "cafe_id": 1, "menu_id": 2, "head_id": None,
                    "naver_login_id": "u", "start_at": "2026-09-19T00:13:00Z",
                    "target_view_count": 0, "target_comment_count": 0,
                },
            }

        def put(self, path, json=None, **k):
            self.sent = (path, json)
            return {"is_success": True}

    c = _C()
    out = articles.update_article(c, "SID", title="새 제목", content_json="{}")
    assert out["is_success"] is True
    path, body = c.sent
    assert path == "/naver_cafe_articles/article"
    assert body["source_id"] == "SID" and body["menu_id"] == 2 and body["cafe_id"] == 1
    assert body["title"] == "새 제목" and body["tag_list"] == ["a"]
    assert "destination" not in body



def test_search_exposure_check_always_off():
    """카페탭 검색 노출 검사는 항상 미사용 (사용자 절대 규칙 2026-09-21)."""
    from v2r.api import articles as a

    dest = a.build_destination(cafe=1, menu=2, head=None, login_id="u", start_at=None)
    assert dest["use_search_exposure"] is False
    assert a.USE_SEARCH_EXPOSURE is False
