"""일상 글 수집기 테스트 (pytest_httpx로 네트워크 모킹)."""

from __future__ import annotations

from pathlib import Path

from v2r.warehouse.daily_collector import (
    adapt,
    collect,
    fetch_public,
    parse_feed,
    save_to_warehouse,
    strip_html,
)
from v2r.warehouse.store import Warehouse

LONG = "가나다라마바사 아주 평범한 일상 이야기입니다. " * 80


def _page(title: str, desc: str, body: str) -> str:
    return f"""<html><head>
    <title>무시되는 제목</title>
    <meta property="og:title" content="{title}">
    <meta property="og:description" content="{desc}">
    <script>var x = 1; // 스크립트는 제거</script>
    <style>body {{ color: red }}</style>
    </head><body><article><p>{body}</p></article></body></html>"""


def test_strip_html_removes_scripts_and_styles():
    text = strip_html("<div>앞<script>bad()</script><style>.a{}</style>뒤</div>")
    assert "bad()" not in text and "color" not in text
    assert "앞" in text and "뒤" in text


def test_fetch_public_og_extraction(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/post",
        html=_page("오늘의 브런치", "짧은 설명", LONG),
    )
    result = fetch_public("https://example.com/post")
    assert result["status"] == "ok"
    assert result["title"] == "오늘의 브런치"
    assert "짧은 설명" in result["text"]
    assert "가나다라마바사" in result["text"]
    assert "bad" not in result["text"]
    assert result["source"] == "https://example.com/post"


def test_fetch_public_jsonld_article_body(httpx_mock):
    body = "제이슨엘디 본문입니다. " * 30
    html = (
        '<html><head><meta property="og:title" content="JSONLD 글">'
        '<script type="application/ld+json">'
        '{"@type":"NewsArticle","headline":"헤드라인","articleBody":"%s"}'
        "</script></head><body><p>무시</p></body></html>" % body
    )
    httpx_mock.add_response(url="https://example.com/ld", html=html)
    result = fetch_public("https://example.com/ld")
    assert result["status"] == "ok"
    assert result["text"].strip().endswith(body.strip())


def test_blocked_on_403(httpx_mock):
    httpx_mock.add_response(url="https://example.com/x", status_code=403, html="<p>Forbidden</p>")
    assert fetch_public("https://example.com/x")["status"] == "blocked"


def test_blocked_on_401(httpx_mock):
    httpx_mock.add_response(url="https://example.com/y", status_code=401, text="nope")
    assert fetch_public("https://example.com/y")["status"] == "blocked"


def test_blocked_on_login_hint_with_short_text(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/login",
        html="<html><body><h1>로그인</h1><p>회원만 볼 수 있습니다.</p></body></html>",
    )
    assert fetch_public("https://example.com/login")["status"] == "blocked"


def test_login_hint_but_long_text_is_ok(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/long",
        html=f"<html><body><a>로그인</a><article>{LONG}</article></body></html>",
    )
    assert fetch_public("https://example.com/long")["status"] == "ok"


def test_members_only_hint(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/m",
        html="<html><body><p>members only - paywall</p></body></html>",
    )
    assert fetch_public("https://example.com/m")["status"] == "blocked"


def test_error_on_500(httpx_mock):
    httpx_mock.add_response(url="https://example.com/e", status_code=500, text="boom")
    result = fetch_public("https://example.com/e")
    assert result["status"] == "error"
    assert "500" in result["error"]


def test_error_on_network_failure(httpx_mock):
    httpx_mock.add_exception(Exception("연결 끊김"), url="https://example.com/n")
    result = fetch_public("https://example.com/n")
    assert result["status"] == "error"


def test_parse_feed():
    xml = """<?xml version="1.0"?><rss><channel>
      <item><title>첫 글</title><link>https://a/1</link><description>내용1</description></item>
      <item><title>둘째 글</title><link>https://a/2</link><description>내용2</description></item>
    </channel></rss>"""
    items = parse_feed(xml)
    assert [i["title"] for i in items] == ["첫 글", "둘째 글"]
    assert items[0]["link"] == "https://a/1"


def test_fetch_public_rss(httpx_mock):
    xml = "<?xml version='1.0'?><rss><channel><item><title>피드 제목</title><description>피드 본문</description></item></channel></rss>"
    httpx_mock.add_response(
        url="https://example.com/rss",
        text=xml,
        headers={"content-type": "application/rss+xml"},
    )
    result = fetch_public("https://example.com/rss")
    assert result["status"] == "ok"
    assert result["title"] == "피드 제목"
    assert "피드 본문" in result["text"]
    assert len(result["items"]) == 1


def test_collect_multiple(httpx_mock):
    httpx_mock.add_response(url="https://example.com/a", html=_page("A", "설명A", LONG))
    httpx_mock.add_response(url="https://example.com/b", status_code=403, text="no")
    results = collect(["https://example.com/a", "https://example.com/b"])
    assert [r["status"] for r in results] == ["ok", "blocked"]


def test_adapt_without_adapter_keeps_text():
    items = [{"status": "ok", "title": "t", "text": "원문"}]
    assert adapt(items) == items
    assert adapt(items)[0]["text"] == "원문"


def test_adapt_with_adapter():
    def adapter(item):
        out = dict(item)
        out["text"] = "각색됨: " + item["text"]
        return out

    result = adapt([{"status": "ok", "title": "t", "text": "원문"}], adapter)
    assert result[0]["text"] == "각색됨: 원문"


def test_adapt_adapter_error_is_captured():
    def bad(item):
        raise ValueError("실패")

    result = adapt([{"status": "ok", "text": "원문"}], bad)
    assert result[0]["text"] == "원문"
    assert "실패" in result[0]["adapt_error"]


def test_save_to_warehouse(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    items = [
        {"status": "ok", "title": "좋은 글", "text": "본문입니다", "source": "https://a"},
        {"status": "blocked", "title": "막힌 글", "text": "", "source": "https://b"},
    ]
    saved = save_to_warehouse(wh, items)
    assert len(saved) == 1
    content = saved[0].read_text(encoding="utf-8")
    assert "제목 : 좋은 글" in content
    assert "https://a" in content
    assert "본문입니다" in content
