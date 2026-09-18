"""일상 글 수집기. 공개 페이지만 읽는다.

원칙: 로그인 우회·TLS 위장·캡차/WAF 우회는 하지 않는다.
로그인이 필요해 보이면 `blocked`으로 표시하고 넘어간다.
각색(LLM)은 훅으로만 열어둔다 — 실제 어댑터는 묶음 5가 제공한다.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from v2r.warehouse.store import Warehouse

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

LOGIN_HINTS = ("로그인", "log in", "sign in", "paywall", "구독자만", "members only")
MIN_TEXT_LEN = 1200

_SCRIPT_RE = re.compile(r"(?is)<(script|style|noscript|template)\b.*?</\1\s*>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")
_BLOCK_RE = re.compile(r"(?i)</?(p|br|div|li|tr|h[1-6]|section|article)\b[^>]*>")
_LDJSON_RE = re.compile(
    r'(?is)<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>'
)
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")


def _meta(content: str, prop: str) -> str:
    """og:/name 메타 태그 값을 찾는다."""
    pattern = re.compile(
        r'(?is)<meta[^>]+(?:property|name)\s*=\s*["\']%s["\'][^>]*>' % re.escape(prop)
    )
    match = pattern.search(content)
    if not match:
        return ""
    value = re.search(r'(?is)content\s*=\s*["\'](.*?)["\']', match.group(0))
    return html.unescape(value.group(1)).strip() if value else ""


def strip_html(content: str) -> str:
    """스크립트/스타일을 지우고 태그를 제거해 본문 텍스트만 남긴다."""
    cleaned = _SCRIPT_RE.sub(" ", content)
    cleaned = _BLOCK_RE.sub("\n", cleaned)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = _WS_RE.sub(" ", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n"))
    return _BLANK_RE.sub("\n\n", cleaned).strip()


def _iter_ldjson(content: str):
    for raw in _LDJSON_RE.findall(content):
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                for value in node.values():
                    if isinstance(value, (list, dict)):
                        stack.append(value)


def _ldjson_article(content: str) -> tuple[str, str]:
    """JSON-LD에서 (headline, articleBody)를 찾는다."""
    for node in _iter_ldjson(content):
        body = node.get("articleBody")
        if isinstance(body, str) and body.strip():
            headline = node.get("headline") or node.get("name") or ""
            return (str(headline).strip(), body.strip())
    return ("", "")


def parse_feed(content: str) -> list[dict]:
    """RSS/Atom 항목 목록을 뽑는다."""
    items: list[dict] = []
    for match in re.finditer(r"(?is)<(item|entry)\b[^>]*>(.*?)</\1\s*>", content):
        block = match.group(2)

        def pick(tag: str) -> str:
            found = re.search(r"(?is)<%s\b[^>]*>(.*?)</%s\s*>" % (tag, tag), block)
            return strip_html(found.group(1)) if found else ""

        link = pick("link")
        if not link:
            href = re.search(r'(?is)<link[^>]+href\s*=\s*["\'](.*?)["\']', block)
            link = href.group(1) if href else ""
        text = pick("content:encoded") or pick("description") or pick("summary") or pick("content")
        items.append({"title": pick("title"), "text": text, "link": link})
    return items


def _looks_blocked(status_code: int, text: str) -> bool:
    if status_code in (401, 403):
        return True
    lowered = text.lower()
    hinted = any(hint.lower() in lowered for hint in LOGIN_HINTS)
    return hinted and len(text) < MIN_TEXT_LEN


def fetch_public(url: str, timeout: float = 10, client: httpx.Client | None = None) -> dict:
    """공개 페이지 한 건을 읽는다. 반환 status는 ok|blocked|error."""
    result: dict[str, Any] = {
        "status": "error",
        "title": "",
        "text": "",
        "source": url,
        "items": [],
    }
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko,en;q=0.8"}
    try:
        if client is not None:
            response = client.get(url, headers=headers, timeout=timeout)
        else:
            with httpx.Client(follow_redirects=True, timeout=timeout) as http:
                response = http.get(url, headers=headers)
    except Exception as exc:
        result["error"] = str(exc)
        return result

    content = response.text or ""
    content_type = response.headers.get("content-type", "")

    # RSS/Atom
    if "xml" in content_type.lower() or re.search(r"(?is)<(rss|feed)\b", content[:2000]):
        items = parse_feed(content)
        if items:
            result.update(
                status="ok",
                title=items[0]["title"],
                text="\n\n".join(i["text"] for i in items if i["text"]),
                items=items,
            )
            return result

    title = _meta(content, "og:title")
    if not title:
        found = _TITLE_RE.search(content)
        title = html.unescape(strip_html(found.group(1))) if found else ""

    ld_title, ld_body = _ldjson_article(content)
    text = ld_body
    if not text:
        text = strip_html(content)
    description = _meta(content, "og:description") or _meta(content, "description")
    if description and description not in text:
        text = f"{description}\n\n{text}".strip()

    result["title"] = title or ld_title
    result["text"] = text

    if response.status_code >= 400 and response.status_code not in (401, 403):
        result["status"] = "error"
        result["error"] = f"HTTP {response.status_code}"
        return result
    result["status"] = "blocked" if _looks_blocked(response.status_code, text) else "ok"
    return result


def collect(urls: list[str], timeout: float = 10) -> list[dict]:
    """여러 공개 URL을 순서대로 읽는다."""
    return [fetch_public(url, timeout=timeout) for url in urls]


def adapt(items: list[dict], adapter: Callable[[dict], dict] | None = None) -> list[dict]:
    """각색 훅. adapter가 None이면 원문 그대로 둔다(묶음 5가 어댑터 제공)."""
    if adapter is None:
        return list(items)
    out: list[dict] = []
    for item in items:
        try:
            adapted = adapter(item)
        except Exception as exc:
            adapted = dict(item)
            adapted["adapt_error"] = str(exc)
        out.append(adapted if isinstance(adapted, dict) else dict(item))
    return out


def save_to_warehouse(wh: Warehouse, items: list[dict]) -> list[Path]:
    """수집 결과(ok인 항목)를 창고 원고 폴더에 텍스트로 저장한다."""
    wh.ensure_dirs()
    saved: list[Path] = []
    for index, item in enumerate(items):
        if item.get("status") != "ok":
            continue
        title = (item.get("title") or "").strip() or f"수집_{index + 1}"
        body = item.get("text") or ""
        text = f"제목 : {title}\n출처 : {item.get('source', '')}\n\n본문 :\n{body}\n"
        saved.append(wh.save_manuscript(f"{index + 1:03d}_{title[:60]}", text))
    return saved


__all__ = [
    "adapt",
    "collect",
    "fetch_public",
    "parse_feed",
    "save_to_warehouse",
    "strip_html",
]
