"""글 등록·검증·삭제·이력 (api-spec §4, §5)."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

import httpx

from .client import V2RClient, field, walk_dicts
from .errors import V2RApiError, is_post_limit

log = logging.getLogger(__name__)

SITE_BASE = "https://v2r.daboja.im"

#: 프로젝트 전역 기준 시간대. naive datetime은 KST로 간주한다.
KST = ZoneInfo("Asia/Seoul")

#: POST 결과가 불확실해 이력 복구가 필요한 오류 종류
AMBIGUOUS_KINDS = {"server", "ambiguous", "rate_limited"}


class PendingError(RuntimeError):
    """아직 확인할 수 없는 상태(예약 시각이 멀어 대기 불가). 호출자가 뒤로 미룬다."""

PATH_CREATE = "/naver_cafe_articles/naver_cafe_article_source"
PATH_ARTICLE = "/naver_cafe_articles/article"
PATH_DELETE = "/naver_cafe_articles/article/delete"
PATH_HISTORIES = "/naver_cafe_articles/board_histories"
#: 라이브 서버에 `board_histories` GET 경로가 없어(404) 쓰는 대체 조회 경로.
#: 계정 단위로만 조회된다 (`live-catalog.md` §2 참고).
PATH_WRITTEN = "/naver_cafe_articles/article/written_articles"
#: 존재는 확인됐으나 요청 본문 스키마 미확인. 기본적으로 사용하지 않는다.
PATH_HISTORIES_SEARCH = "/naver_cafe_articles/board_histories/search"

#: `POST /board_histories/search` 사용 여부. 본문 스키마를 라이브에서 확인하기 전까지 끈다.
ENABLE_HISTORIES_SEARCH = False

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

#: "카페탭 검색 노출 검사" — V2R 글쓰기 화면 기본값 사용(true)을 **항상 사용으로 유지** (미사용으로 바꾸지 않음; 바디에 안 실으면 서버가 false로 저장하므로 반드시 true를 보낸다)
#: (사용자 절대 규칙 2026-09-21). 등록·수정 바디의 `use_search_exposure`에 그대로 실린다.
USE_SEARCH_EXPOSURE = True

DONE_STATUSES = {"DONE", "SUCCESS"}
IMAGE_CTYPES = {"image", "imageGroup", "imageStrip"}


def to_iso_z(dt: datetime | None) -> str | None:
    """UTC ISO(`YYYY-MM-DDTHH:MM:SSZ`). None이면 즉시 발행.

    naive datetime은 UTC가 아니라 **KST**로 간주한다(프로젝트 전역 기준).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
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
        "use_search_exposure": USE_SEARCH_EXPOSURE,  # 카페탭 검색 노출 검사: 항상 사용
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
    """글 등록 후 source_id 반환.

    POST는 비멱등이므로 클라이언트 층에서 재전송하지 않는다(`idempotent=False`).
    네트워크 오류·5xx·429 등 결과가 불확실한 모든 실패에서는 재POST 대신
    `board_histories` 이력 조회로 복구한다(api-spec §4, 중복 발행 방지).
    """
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

    def recover() -> str | None:
        return find_recent_source(
            client,
            cafe_id=destination.get("cafe_id"),
            login_id=destination.get("naver_login_id"),
            title=title,
            parent_source_id=parent_source_id,
            since=since,
        )

    try:
        response = client.post(PATH_CREATE, json=payload, idempotent=False)
    except V2RApiError as exc:
        if (exc.kind or "") not in AMBIGUOUS_KINDS:
            raise
        found = recover()
        if found:
            return found
        raise
    except (httpx.HTTPError, TimeoutError) as exc:
        found = recover()
        if found:
            return found
        raise V2RApiError(f"글 등록 실패(네트워크): {exc}", kind="ambiguous") from exc

    source_id = _source_id_from(response)
    if source_id:
        return source_id

    found = recover()
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


#: `written_articles` 행에서 정규화 결과로 옮겨가는 키(나머지는 `raw`에 남긴다)
_WRITTEN_MAPPED_KEYS = {
    "v2r_source_id",
    "subject",
    "writedt",
    "parent_source_id",
    "v2r_parent_source_id",
}

#: 네이버 원본 날짜 형식 (`"Sep 15, 2026 12:31:23 PM"`)
NAVER_DT_FORMAT = "%b %d, %Y %I:%M:%S %p"


def _parse_any_dt(value: Any) -> datetime | None:
    """ISO 또는 네이버 원본 형식(`Sep 15, 2026 12:31:23 PM`) → aware datetime.

    타임존이 없는 값은 프로젝트 기준대로 **KST**로 간주한다.
    """
    dt = _parse_dt(value)
    if dt is not None:
        return dt
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        naive = datetime.strptime(value.strip(), NAVER_DT_FORMAT)
    except ValueError:
        return None
    return naive.replace(tzinfo=KST)


def _normalize_written_row(row: dict, cafe_id: Any, login_id: Any) -> dict:
    """`written_articles` 행 → `board_histories` 행과 같은 dict 모양.

    행에는 작성 계정 필드가 없다(네이버 원본 필드 + `v2r_source_id`뿐). 조회를
    계정 단위로 하므로 호출 시의 `login_id`를 그대로 채운다.
    """
    written = _parse_any_dt(row.get("writedt"))
    written_iso = to_iso_z(written) if written else None
    parent = field(row, "v2r_parent_source_id", "parent_source_id")
    source_id = field(row, "v2r_source_id", "source_id")
    return {
        "source_id": str(source_id) if source_id else None,
        "naver_account_login_id": login_id,
        "naver_login_id": login_id,
        "title": row.get("subject"),
        "parent_source_id": str(parent) if parent else None,
        # 이미 게시된 글만 돌아오므로 완료로 본다(엔드포인트에 상태 필드 없음).
        "status": "DONE",
        "created_at": written_iso,
        "written_at": written_iso,
        "cafe_id": field(row, "clubid", "cafe_id", default=cafe_id),
        "article_id": field(row, "articleid", "article_id"),
        "menu_id": field(row, "menuid", "menu_id"),
        "raw": {k: v for k, v in row.items() if k not in _WRITTEN_MAPPED_KEYS},
    }


def written_articles(
    client: V2RClient,
    cafe_id: Any,
    login_id: Any,
    pages: int = 3,
) -> list[dict]:
    """계정이 해당 카페에 쓴 글 목록(`board_histories` 대체, 읽기 전용).

    `GET /naver_cafe_articles/article/written_articles?cafe_id&naver_login_id&page`
    → `{"articles": [...], "total_count": int}`. 행은 네이버 원본 필드에
    `v2r_source_id`가 붙은 형태라 `board_histories` 행 모양으로 정규화해서 돌려준다.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max(1, int(pages)) + 1):
        try:
            payload = client.get(
                PATH_WRITTEN,
                params={
                    "cafe_id": cafe_id,
                    "naver_login_id": login_id,
                    "page": page,
                },
            )
        except V2RApiError as exc:
            if exc.status == 404:
                log.warning("written_articles 404 (cafe_id=%s, page=%s)", cafe_id, page)
                break
            raise
        items = payload.get("articles") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            items = [d for d in walk_dicts(payload) if "v2r_source_id" in d]
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            norm = _normalize_written_row(item, cafe_id, login_id)
            key = norm["source_id"] or f"{norm['article_id']}"
            if key in seen:
                continue
            seen.add(key)
            rows.append(norm)
        total = payload.get("total_count") if isinstance(payload, dict) else None
        if isinstance(total, int) and len(rows) >= total:
            break
    return rows


def board_histories(
    client: V2RClient,
    cafe_id: Any,
    days_ago: int = 30,
    max_pages: int = 5,
    login_id: Any = None,
    login_ids: Sequence[Any] | None = None,
) -> list[dict]:
    """등록 이력 목록.

    라이브 서버에는 `GET /naver_cafe_articles/board_histories`가 없다(항상 404).
    옛 경로를 **한 번만** 시도하고, 404면 `written_articles`로 폴백한다. 폴백은
    계정 단위 조회라 `login_id`(또는 `login_ids`)가 필요하며, 아무것도 없으면
    경고만 남기고 빈 목록을 돌려준다.
    """
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
                if rows:
                    break
                return _histories_fallback(client, cafe_id, login_id, login_ids)
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


def _histories_fallback(
    client: V2RClient,
    cafe_id: Any,
    login_id: Any,
    login_ids: Sequence[Any] | None,
) -> list[dict]:
    """`board_histories` 404 폴백: 계정별 `written_articles`를 모아 돌려준다."""
    accounts: list[Any] = []
    if login_id:
        accounts.append(login_id)
    for acc in login_ids or []:
        if acc and acc not in accounts:
            accounts.append(acc)
    if not accounts:
        log.warning(
            "board_histories 경로 없음(404)이고 조회할 계정도 없어 빈 목록을 돌려준다 "
            "(cafe_id=%s). login_id/login_ids를 넘겨야 폴백이 동작한다.",
            cafe_id,
        )
        return []
    rows: list[dict] = []
    for acc in accounts:
        rows.extend(written_articles(client, cafe_id, acc))
    return rows


def search_board_histories(
    client: V2RClient, cafe_id: Any, body: dict | None = None
) -> list[dict]:
    """`POST /naver_cafe_articles/board_histories/search` 헬퍼 (기본 비활성).

    라이브에 경로는 존재하지만(GET 시 405) **요청 본문 스키마가 미확인**이라
    `ENABLE_HISTORIES_SEARCH`가 참일 때만 실제로 호출한다.

    TODO: 라이브에서 본문 모양(필드명/필수값/페이지네이션 키)을 확인한 뒤
    기본 경로로 승격할지 결정한다. 확인 전에는 쓰기 작업 경로에서 쓰지 않는다.
    """
    if not ENABLE_HISTORIES_SEARCH:
        log.debug("search_board_histories 비활성 (ENABLE_HISTORIES_SEARCH=False)")
        return []
    payload = client.post(
        PATH_HISTORIES_SEARCH, json={"cafe_id": cafe_id, **(body or {})}
    )
    page = payload.get("histories") if isinstance(payload, dict) else None
    if not isinstance(page, list):
        page = [d for d in walk_dicts(payload) if "source_id" in d and "title" in d]
    return [d for d in page if isinstance(d, dict)]


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
    """POST 실패/타임아웃 후 이력에서 방금 만들어진 글을 찾아낸다.

    보수적으로 판정한다:
    - `created_at`이 없거나 파싱되지 않는 행은 채택하지 않는다.
    - `since`가 주어지면 그보다 이전에 만들어진 행은 제외한다.
    - 조건을 만족하는 행이 2건 이상이면 불확실하므로 `None`을 돌려준다.

    조회는 계정 단위 `written_articles`를 **먼저** 쓴다(옛 `board_histories` GET은
    라이브에 없다). 그 경로가 비면 `board_histories`(폴백 포함)로 한 번 더 본다.
    """
    for i in range(attempts):
        try:
            rows = written_articles(client, cafe_id, login_id, pages=1)
            if not rows:
                rows = board_histories(
                    client, cafe_id, days_ago=1, max_pages=1, login_id=login_id
                )
        except V2RApiError as exc:
            if exc.status == 404:
                rows = []
            else:
                raise
        matches: list[str] = []
        for row in rows:
            if field(row, "title") != title:
                continue
            row_login = field(row, "naver_account_login_id", "naver_login_id", "login_id")
            if login_id is not None and row_login != login_id:
                continue
            if (row.get("parent_source_id") or None) != (parent_source_id or None):
                continue
            created = _parse_any_dt(
                field(row, "created_at", "createdAt", "written_at", "writedt")
            )
            if created is None:
                continue  # 시각을 모르면 "방금 만든 글"이라고 볼 수 없다
            if since is not None and created < since:
                continue
            status = str(field(row, "status", default="") or "").upper()
            if status and status not in DONE_STATUSES | {"RESERVED"}:
                continue
            source_id = field(row, "source_id", "sourceId")
            if source_id and str(source_id) not in matches:
                matches.append(str(source_id))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None  # 동일 조건 복수 건 → 불확실
        if i < attempts - 1:
            time.sleep(interval)
    return None


def get_article(client: V2RClient, source_id: str) -> dict:
    """글 상세 조회."""
    return client.get(PATH_ARTICLE, params={"source_id": source_id})


#: 실패 사유가 들어오는 칸 이름들(상세 응답·글 목록 행 공통, 실측 2026-09-22)
FAIL_REASON_KEYS = (
    "fail_reason",
    "failReason",
    "reserved_comment_fail_reason",
    "error_message",
    "message",
)

#: 아직 올라가지 않은 상태(V2R 글 목록 상태 열 "준비")
PENDING_STATUSES = {"RESERVED", "READY", "WAITING", "PENDING"}


def limit_reason_of(payload: Any) -> str:
    """"게시글 등록 제한"에 걸린 흔적이 있으면 그 문구를, 없으면 빈 문자열.

    어디서 오는가 (실측 2026-09-22, peecics 4건):

    * `GET /naver_cafe_articles/article?source_id=…`
      → `naver_cafe_article_history.fail_reason`,
        `naver_cafe_article_destination.fail_reason`
      (같은 묶음의 `status`는 그때 `RESERVED`="준비"였다)
    * `POST /naver_cafe_articles/board_histories/search` (V2R 글 목록)
      → 행의 `status`, `fail_reason` (경고 아이콘 문구가 곧 `fail_reason`이다)

    상태값만으로는 구분할 수 없다. `RESERVED`는 정상 예약 대기에도 쓰이므로
    **실패 사유 문구**로 판단한다.
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload if is_post_limit(payload) else ""
    for d in walk_dicts(payload):
        if not isinstance(d, dict):
            continue
        for key in FAIL_REASON_KEYS:
            text = d.get(key)
            if isinstance(text, str) and is_post_limit(text):
                return text.strip()
    return ""


def is_limited(payload: Any) -> bool:
    """`limit_reason_of`가 무언가 찾았는가."""
    return bool(limit_reason_of(payload))


PATH_UPDATE = "/naver_cafe_articles/article"


def update_article(
    client: V2RClient,
    source_id: str,
    *,
    title: str,
    content_json: str,
    tags: list[str] | None = None,
) -> dict:
    """등록/발행된 글의 제목·본문·태그를 바꾼다 (`PUT /naver_cafe_articles/article`).

    실측(2026-09-19): 바디는 **평면**이다 — `destination` 묶음이 아니라 `menu_id`,
    `cafe_id`, `naver_login_id`, `start_at` 등이 최상위에 온다. 나머지 값은 현재 글에서
    그대로 가져온다. 성공 시 `{"is_success": true}`. 이미 발행된 글(SUCCESS)도 통과했다.
    """
    detail = get_article(client, source_id)
    dest = detail.get("naver_cafe_article_destination") or {}
    src = detail.get("naver_cafe_article_source") or {}
    payload = {
        "source_id": source_id,
        "title": title,
        "content_json": content_json,
        "tag_list": list(tags if tags is not None else (src.get("tag_list") or [])),
        "cafe_write_options": dest.get("write_options") or dict(DEFAULT_WRITE_OPTIONS),
        "cafe_id": dest.get("cafe_id"),
        "menu_id": dest.get("menu_id"),
        "head_id": dest.get("head_id"),
        "naver_login_id": dest.get("naver_login_id"),
        "start_at": dest.get("start_at"),
        "target_view_count": dest.get("target_view_count") or 0,
        "target_comment_count": dest.get("target_comment_count") or 0,
        "use_search_exposure": USE_SEARCH_EXPOSURE,  # 수정할 때도 사용으로 맞춘다
        "comments": [],
        "likes": [],
        "parent_source_id": src.get("parent_source_id"),
    }
    return client.put(PATH_UPDATE, json=payload)


def delete_article(client: V2RClient, source_id: str) -> dict:
    """글 삭제."""
    return client.post(PATH_DELETE, json={"source_id": source_id})


#: 등록 확인 폴링 중 "아직 기다리는 중" 이벤트를 남기는 간격(초)
WAIT_WRITTEN_EVENT_S = 30.0


def wait_written(
    client: V2RClient,
    source_id: str,
    scheduled_at: datetime | None = None,
    poll: tuple[float, float] = (2.0, 5.0),
    timeout_after_sched: float = 1800.0,
    max_wait_s: float = 120.0,
    *,
    on_wait: Any = None,
) -> dict:
    """등록 완료까지 폴링. 예약 15초 전까지는 호출하지 않는다.

    예약 시각이 `max_wait_s`(기본 120초)보다 멀면 스레드를 오래 붙잡지 않고
    `PendingError`를 던진다. 호출자(reconcile)가 나중에 다시 확인해야 한다.
    naive `scheduled_at`은 KST로 간주한다.

    **전체 대기 상한도 `max_wait_s`다** (장애 2026-09-20 #3). 예전에는
    폴링만 `timeout_after_sched`(30분)까지 돌아, 서버가 상태를 안 돌려주면
    슬롯 하나가 10분 넘게 조용히 멈춰 있었다. 이제 예약 대기 + 폴링을 합쳐
    `max_wait_s`를 넘으면 `등록 확인 시간 초과`로 빠져나온다. `on_wait(초)`를
    주면 `WAIT_WRITTEN_EVENT_S`마다 불러 진행을 남길 수 있다.
    """
    began = time.monotonic()
    budget = max(0.0, min(float(max_wait_s), float(timeout_after_sched)))

    def _notify(waited: float) -> None:
        if on_wait is None:
            return
        try:
            on_wait(waited)
        except Exception:  # pragma: no cover - 알림 실패가 확인을 막지 않는다
            pass

    if scheduled_at is not None:
        target = scheduled_at
        if target.tzinfo is None:
            target = target.replace(tzinfo=KST)
        wait = (target - datetime.now(timezone.utc)).total_seconds() - 15.0
        if wait > max_wait_s:
            raise PendingError(
                f"예약 시각이 {wait:.0f}초 뒤라 대기하지 않습니다(상한 {max_wait_s:.0f}초): {source_id}"
            )
        if wait > 0:
            time.sleep(wait)
    deadline = began + budget

    first, later = poll
    interval = first
    next_event = WAIT_WRITTEN_EVENT_S
    while True:
        waited = time.monotonic() - began
        if waited >= next_event:
            next_event += WAIT_WRITTEN_EVENT_S
            _notify(waited)
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
        limit_reason = limit_reason_of(detail)
        if limit_reason:
            # 글이 **올라가지 않았다**. 완료로 보면 안 되고, 계정을 바꿔 다시 올려야 한다.
            raise V2RApiError(
                f"게시글 등록 제한: {limit_reason}",
                code="POST_LIMIT",
                reason=limit_reason,
                kind="post_limit",
            )
        if status == "FAIL":
            reason = str(field(history, "fail_reason", default=""))
            raise V2RApiError(
                f"글 등록 실패: {reason or '사유 없음'}",
                code="FAIL",
                reason=reason,
                kind="post_limit" if is_post_limit(reason) else None,
            )
        if time.monotonic() > deadline:
            raise V2RApiError(
                f"등록 확인 시간 초과({budget:.0f}초): {source_id}"
            )
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


#: 답글이 중첩될 수 있는 키(요청 페이로드는 `comments` 아래에 답글을 넣는다)
COMMENT_CHILD_KEYS = ("comments", "children", "replies")


def count_comment_nodes(nodes: Any) -> int:
    """댓글 목록의 전체 노드 수(루트 + 답글)를 센다.

    등록 요청 페이로드는 답글을 루트의 ``comments``에 **중첩**해서 보내고,
    라이브 GET 응답은 루트와 답글을 한 배열에 **평탄하게** 담아 돌려준다
    (답글은 ``parent_comment_id``로 부모를 가리킨다). 두 모양 모두에서 같은
    총계(예: 루트 5 + 답글 7 = 12)가 나오도록 재귀로 센다.
    """
    if not isinstance(nodes, list):
        return 0
    total = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        total += 1
        for key in COMMENT_CHILD_KEYS:
            total += count_comment_nodes(node.get(key))
    return total


def flatten_comment_nodes(nodes: Any) -> list[dict]:
    """댓글 목록을 읽는 순서(부모 → 그 답글들)의 평탄 목록으로 편다.

    라이브 GET 응답은 이미 평탄하고, 등록 요청 페이로드는 ``comments``에 중첩돼
    있다. 두 모양 모두 같은 순서를 돌려준다.
    """
    out: list[dict] = []
    if not isinstance(nodes, list):
        return out
    for node in nodes:
        if not isinstance(node, dict):
            continue
        out.append(node)
        for key in COMMENT_CHILD_KEYS:
            out.extend(flatten_comment_nodes(node.get(key)))
    return out


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


def body_lines_of(detail: dict) -> list[str]:
    """글 상세 응답의 본문을 `verify_article(body_lines=...)`용 줄 목록으로.

    같은 본문을 그대로 다시 등록할 때(댓글 복구 등) 기대값을 원본에서 그대로
    뽑아 쓰기 위한 공개 헬퍼다.
    """
    return _paragraph_lines(_iter_components(detail))


def image_count_of(detail: dict) -> int:
    """글 상세 응답의 유효 이미지 컴포넌트 수."""
    return _count_images(_iter_components(detail))


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
    expected_comment_sequence: list[str] | None = None,
) -> list[str]:
    """등록 후 GET 결과 검증. 불일치 항목 목록을 반환(빈 목록 = 정상).

    `comments_count`는 **답글까지 포함한 전체 댓글 수**다(`count_comment_nodes`).
    `expected_comment_sequence`를 주면 응답 댓글 배열의 **순서**까지 본다. 값은
    각 댓글 본문의 앞부분(접두사) 목록이고, 응답 순서대로 `startswith`로 맞춘다
    (docs/reference/live-comment-order.md §1).
    """
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

    # 즉시 발행(start_at=None)은 서버가 실제 시각을 채우므로 예약시각 검사를 생략한다.
    if start_at is not None:
        expected_start = to_iso_z(start_at)
        got_start = field(destination, "start_at", "startAt")
        if _parse_dt(got_start) != _parse_dt(expected_start) and (
            got_start or None
        ) != (expected_start or None):
            problems.append(f"예약시각 불일치: {got_start} != {expected_start}")

    # 응답은 평탄, 요청은 중첩이므로 양쪽 모두 "전체 노드 수"로 맞춰 비교한다
    got_comments = detail.get("naver_cafe_article_source_comments")
    n_comments = count_comment_nodes(got_comments)
    if n_comments != int(comments_count):
        problems.append(f"댓글 개수 불일치: {n_comments} != {comments_count}")

    if expected_comment_sequence:
        expected = list(expected_comment_sequence)
        got_seq = [
            str(field(n, "contents", "content", default="") or "")
            for n in flatten_comment_nodes(got_comments)
        ]
        if len(got_seq) != len(expected):
            problems.append(f"댓글 순서 길이 불일치: {len(got_seq)} != {len(expected)}")
        else:
            for i, (got, want) in enumerate(zip(got_seq, expected)):
                if not got.startswith(want):
                    problems.append(
                        f"댓글 순서 불일치 #{i}: {got[:20]!r} != {want!r}"
                    )
                    break

    return problems


__all__ = [
    "AMBIGUOUS_KINDS",
    "DEFAULT_WRITE_OPTIONS",
    "ENABLE_HISTORIES_SEARCH",
    "PATH_WRITTEN",
    "search_board_histories",
    "written_articles",
    "KST",
    "PendingError",
    "article_url",
    "board_histories",
    "body_lines_of",
    "build_destination",
    "image_count_of",
    "count_comment_nodes",
    "create_article",
    "delete_article",
    "find_recent_source",
    "flatten_comment_nodes",
    "get_article",
    "is_limited",
    "limit_reason_of",
    "FAIL_REASON_KEYS",
    "PENDING_STATUSES",
    "to_iso_z",
    "verify_article",
    "wait_written",
    "WAIT_WRITTEN_EVENT_S",
]
