"""글 등록·검증·삭제·이력 (api-spec §4, §5)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from .client import V2RClient, field, walk_dicts
from .errors import V2RApiError

SITE_BASE = "https://v2r.daboja.im"

PATH_CREATE = "/naver_cafe_articles/naver_cafe_article_source"
PATH_ARTICLE = "/naver_cafe_articles/article"
PATH_DELETE = "/naver_cafe_articles/article/delete"
PATH_HISTORIES = "/naver_cafe_articles/board_histories"

DEFAULT_WRITE_OPTIONS: dict[str, Any] = {
    "enableComment": True,
    "externalOpen": True,
    "enableScrap": True,
    "enableCopy": False,
    "useAutoSource": True,
    "useCcl": True,
    "cclTypes": ["ATTRIBUTION", "NONCOMMERCIAL", "NO_DERIVATIVE"],
    "open": False,
    "naverOpen": True,
}

DONE_STATUSES = {"DONE", "SUCCESS"}
IMAGE_CTYPES = {"image", "imageGroup", "imageStrip"}


def to_iso_z(dt: datetime | None) -> str | None:
    """UTC ISO(`YYYY-MM-DDTHH:MM:SSZ`). None이면 즉시 발행."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def article_url(source_id: str) -> str:
    """글 상세 URL."""
    return f"{SITE_BASE}/nc/articleDetail/{source_id}"


def build_destination(
    cafe: Any,
    menu: Any,
    head: Any,
    login_id: str,
    start_at: datetime | None,
    target_view_count: int = 0,
    parent_id: Any = None,
) -> dict:
    """등록 페이로드의 `destination` 블록."""
    return {
        "cafe_id": getattr(cafe, "cafe_id", cafe),
        "cafe_name": getattr(cafe, "name", None),
        "head_id": getattr(head, "head_id", None) if head is not None else None,
        "head_name": getattr(head, "name", None) if head is not None else None,
        "menu_id": getattr(menu, "menu_id", menu),
        "menu_name": getattr(menu, "name", None),
        "naver_login_id": login_id,
        "start_at": to_iso_z(start_at),
        "target_view_count": target_view_count,
        "use_comment_ai": True,
        "parent_id": parent_id,
    }


def create_article(
    client: V2RClient,
    *,
    title: str,
    tags: list[str],
    content_json: str,
    destination: dict,
    comments: list[dict] | None = None,
    parent_source_id: str | None = None,
    write_options: dict | None = None,
) -> str:
    """글 등록 후 source_id 반환. 네트워크 실패 시 이력 조회로 복구 시도."""
    payload = {
        "tag_list": list(tags or []),
        "title": title,
        "content_json": content_json,
        "cafe_write_options": dict(write_options or DEFAULT_WRITE_OPTIONS),
        "comments": list(comments or []),
        "destination": destination,
        "likes": [],
        "parent_source_id": parent_source_id,
    }
    since = datetime.now(timezone.utc) - timedelta(seconds=15)
    try:
        response = client.post(PATH_CREATE, json=payload)
    except (httpx.HTTPError, TimeoutError) as exc:
        found = find_recent_source(
            client,
            cafe_id=destination.get("cafe_id"),
            login_id=destination.get("naver_login_id"),
            title=title,
            parent_source_id=parent_source_id,
            since=since,
        )
        if found:
            return found
        raise V2RApiError(f"글 등록 실패(네트워크): {exc}") from exc

    source_id = _source_id_from(response)
    if source_id:
        return source_id

    found = find_recent_source(
        client,
        cafe_id=destination.get("cafe_id"),
        login_id=destination.get("naver_login_id"),
        title=title,
        parent_source_id=parent_source_id,
        since=since,
    )
    if found:
        return found
    raise V2RApiError("글 등록 응답에 source_id 없음")


def _source_id_from(payload: Any) -> str | None:
    """응답 트리에서 source_id 추출."""
    if isinstance(payload, dict):
        block = payload.get("naver_cafe_article_source")
        if isinstance(block, dict) and block.get("source_id"):
            return str(block["source_id"])
    for d in walk_dicts(payload):
        value = d.get("source_id")
        if isinstance(value, str) and value:
            return value
    return None


def _parse_dt(value: Any) -> datetime | None:
    """ISO 문자열 → aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def board_histories(
    client: V2RClient, cafe_id: Any, days_ago: int = 30, max_pages: int = 5
) -> list[dict]:
    """등록 이력 목록. 404는 빈 목록으로 취급한다."""
    rows: list[dict] = []
    next_token: Any = None
    for _ in range(max_pages):
        params: dict[str, Any] = {
            "cafe_id": cafe_id,
            "days_ago": days_ago,
            "include_reserve": "true",
        }
        if next_token:
            params["next_token"] = next_token
        try:
            payload = client.get(PATH_HISTORIES, params=params)
        except V2RApiError as exc:
            if exc.status == 404:
                break
            raise
        page = payload.get("histories") if isinstance(payload, dict) else None
        if not isinstance(page, list):
            page = [
                d
                for d in walk_dicts(payload)
                if "source_id" in d and "title" in d
            ]
        rows.extend(d for d in page if isinstance(d, dict))
        next_token = payload.get("next_token") if isinstance(payload, dict) else None
        if not next_token:
            break
    return rows


def find_recent_source(
    client: V2RClient,
    cafe_id: Any,
    login_id: Any,
    title: str,
    parent_source_id: str | None = None,
    since: datetime | None = None,
    attempts: int = 8,
    interval: float = 0.5,
) -> str | None:
    """POST 실패/타임아웃 후 이력에서 방금 만들어진 글을 찾아낸다."""
    for i in range(attempts):
        try:
            rows = board_histories(client, cafe_id, days_ago=1, max_pages=1)
        except V2RApiError as exc:
            if exc.status == 404:
                rows = []
            else:
                raise
        for row in rows:
            if field(row, "title") != title:
                continue
            row_login = field(row, "naver_account_login_id", "naver_login_id", "login_id")
            if login_id is not None and row_login != login_id:
                continue
            if (row.get("parent_source_id") or None) != (parent_source_id or None):
                continue
            created = _parse_dt(field(row, "created_at", "createdAt"))
            if since is not None and created is not None and created < since:
                continue
            status = str(field(row, "status", default="") or "").upper()
            if status and status not in DONE_STATUSES | {"RESERVED"}:
                continue
            source_id = field(row, "source_id", "sourceId")
            if source_id:
                return str(source_id)
        if i < attempts - 1:
            time.sleep(interval)
    return None


def get_article(client: V2RClient, source_id: str) -> dict:
    """글 상세 조회."""
    return client.get(PATH_ARTICLE, params={"source_id": source_id})


def delete_article(client: V2RClient, source_id: str) -> dict:
    """글 삭제."""
    return client.post(PATH_DELETE, json={"source_id": source_id})


def wait_written(
    client: V2RClient,
    source_id: str,
    scheduled_at: datetime | None = None,
    poll: tuple[float, float] = (2.0, 5.0),
    timeout_after_sched: float = 1800.0,
) -> dict:
    """등록 완료까지 폴링. 예약 15초 전까지는 호출하지 않는다."""
    if scheduled_at is not None:
        target = scheduled_at
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        wait = (target - datetime.now(timezone.utc)).total_seconds() - 15.0
        if wait > 0:
            time.sleep(wait)
        deadline = time.monotonic() + timeout_after_sched
    else:
        deadline = time.monotonic() + timeout_after_sched

    first, later = poll
    interval = first
    while True:
        try:
            detail = get_article(client, source_id)
        except V2RApiError as exc:
            if (exc.kind or "") == "rate_limited":
                time.sleep(10.0)
                continue
            raise
        history = detail.get("naver_cafe_article_history")
        if not isinstance(history, dict):
            history = next(
                (
                    d
                    for d in walk_dicts(detail)
                    if "status" in d and "fail_reason" in d
                ),
                {},
            )
        status = str(field(history, "status", default="") or "").upper()
        if status in DONE_STATUSES:
            return detail
        if status == "FAIL":
            raise V2RApiError(
                f"글 등록 실패: {field(history, 'fail_reason', default='사유 없음')}",
                code="FAIL",
                reason=str(field(history, "fail_reason", default="")),
            )
        if time.monotonic() > deadline:
            raise V2RApiError(f"등록 확인 시간 초과: {source_id}")
        time.sleep(interval)
        interval = later


def _iter_components(detail: dict) -> Iterable[dict]:
    """상세 응답의 SE-ONE 본문 컴포넌트."""
    body = None
    for d in walk_dicts(detail):
        if "body" in d and isinstance(d.get("body"), str):
            body = d["body"]
            break
    if not body:
        return []
    try:
        doc = json.loads(body)
    except ValueError:
        return []
    document = doc.get("document") if isinstance(doc, dict) else None
    components = document.get("components") if isinstance(document, dict) else None
    return components if isinstance(components, list) else []


def _paragraph_lines(components: Iterable[dict]) -> list[str]:
    """text 컴포넌트의 문단을 줄 목록으로 복원."""
    lines: list[str] = []
    for comp in components:
        if not isinstance(comp, dict) or comp.get("@ctype") != "text":
            continue
        for para in comp.get("value") or []:
            if not isinstance(para, dict):
                continue
            text = "".join(
                str(node.get("value") or "")
                for node in (para.get("nodes") or [])
                if isinstance(node, dict)
            )
            lines.append(text)
    return lines


def _count_images(components: Iterable[dict]) -> int:
    """유효한 이미지 컴포넌트 개수."""
    count = 0
    for comp in components:
        if not isinstance(comp, dict) or comp.get("@ctype") not in IMAGE_CTYPES:
            continue
        for d in walk_dicts(comp):
            src = d.get("src") or d.get("path") or d.get("fileName")
            size = d.get("fileSize")
            try:
                size_ok = float(size) > 0
            except (TypeError, ValueError):
                size_ok = False
            if src and size_ok:
                count += 1
                break
    return count


def verify_article(
    detail: dict,
    *,
    title: str,
    tags: list[str],
    menu_id: Any,
    head_id: Any,
    body_lines: list[str],
    image_count: int,
    start_at: datetime | None,
    comments_count: int,
) -> list[str]:
    """등록 후 GET 결과 검증. 불일치 항목 목록을 반환(빈 목록 = 정상)."""
    problems: list[str] = []
    source = detail.get("naver_cafe_article_source")
    if not isinstance(source, dict):
        source = next((d for d in walk_dicts(detail) if "title" in d), {})
    destination = detail.get("naver_cafe_article_destination")
    if not isinstance(destination, dict):
        destination = next((d for d in walk_dicts(detail) if "menu_id" in d), {})

    if field(source, "title") != title:
        problems.append(f"제목 불일치: {field(source, 'title')!r} != {title!r}")

    got_tags = list(field(source, "tag_list", "tags", default=[]) or [])
    if got_tags != list(tags or []):
        problems.append(f"태그 불일치: {got_tags} != {list(tags or [])}")

    got_menu = field(destination, "menu_id", "menuId")
    if menu_id is not None and str(got_menu) != str(menu_id):
        problems.append(f"게시판 불일치: {got_menu} != {menu_id}")

    got_head = field(destination, "head_id", "headId")
    if (got_head or None) != (head_id or None):
        problems.append(f"말머리 불일치: {got_head} != {head_id}")

    components = list(_iter_components(detail))
    got_lines = _paragraph_lines(components)
    if got_lines != list(body_lines or []):
        problems.append(f"본문 문단 불일치: {got_lines} != {list(body_lines or [])}")
    for line in got_lines:
        if "{" in line and "}" in line:
            problems.append(f"본문에 플레이스홀더 잔존: {line!r}")
            break

    got_images = _count_images(components)
    if got_images != int(image_count):
        problems.append(f"이미지 개수 불일치: {got_images} != {image_count}")

    expected_start = to_iso_z(start_at)
    got_start = field(destination, "start_at", "startAt")
    if _parse_dt(got_start) != _parse_dt(expected_start) and (
        got_start or None
    ) != (expected_start or None):
        problems.append(f"예약시각 불일치: {got_start} != {expected_start}")

    got_comments = detail.get("naver_cafe_article_source_comments")
    n_comments = len(got_comments) if isinstance(got_comments, list) else 0
    if n_comments != int(comments_count):
        problems.append(f"댓글 개수 불일치: {n_comments} != {comments_count}")

    return problems


__all__ = [
    "DEFAULT_WRITE_OPTIONS",
    "article_url",
    "board_histories",
    "build_destination",
    "create_article",
    "delete_article",
    "find_recent_source",
    "get_article",
    "to_iso_z",
    "verify_article",
    "wait_written",
]
