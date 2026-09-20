"""V2R 글 목록 → `article_index` 동기화.

사용자 절대 규칙: **자사 카페 V2R 글 목록에 이미 있는 모든 글과 앞으로 발행하는 모든
글 사이에 중복이 절대 없어야 한다.** 로컬 발행 기록(`publications`)은 이 시스템이 올린
글만 안다. 그래서 서버 글 목록을 통째로 베껴 와 색인에 쌓고(여기), 발행 직전에
`duplicate.is_duplicate_against_index()`가 그 색인과 대조한다.

조회는 계정 단위 `written_articles`뿐이다(옛 `board_histories` GET은 404). 그래서
카페마다 **가입 계정 전부**를 돌며 페이지를 넘긴다.

`본문까지` 옵션을 주면 본문 해시가 비어 있는 행만 `get_article`로 하나씩 더 읽어
`content_hash(제목, 본문)`를 채운다. 원고 쪽과 **같은 해시**라 바로 비교된다.
느리고 요청이 많아 기본은 끈다(0.3초 간격).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from v2r.api import articles as api_articles
from v2r.content.manuscript import content_hash
from v2r.engine.context import Runtime

log = logging.getLogger(__name__)

#: 계정 한 개당 넘겨 볼 최대 페이지 수 (`written_articles`는 페이지 인자를 받는다)
MAX_PAGES = 30

#: 본문 조회 사이 쉬는 시간(초) — 레이트 제한을 피한다
BODY_SLEEP = 0.3

#: 한 번에 본문을 채울 최대 건수 (0이면 제한 없음)
BODY_LIMIT = 500


def _cafe_entry(rt: Runtime, cafe_name: str) -> dict:
    """`cafes.yaml`에서 카페 항목 찾기."""
    from v2r.engine.publish import find_cafe_entry

    return find_cafe_entry(cafe_name, rt.cafes_cfg) or {}


def _accounts(rt: Runtime, cafe_id: Any) -> list[str]:
    """그 카페 가입 계정 login_id 목록."""
    try:
        rows = rt.catalog.cafe_accounts(cafe_id)
    except Exception as exc:
        log.warning("카페 계정 조회 실패 (cafe_id=%s): %s", cafe_id, exc)
        return []
    out: list[str] = []
    for row in rows or []:
        login_id = getattr(row, "login_id", None) or (
            row.get("login_id") if isinstance(row, dict) else None
        )
        if login_id and login_id not in out:
            out.append(str(login_id))
    return out


def sync_cafe_index(
    rt: Runtime,
    cafe_name: str,
    *,
    with_bodies: bool = False,
    heartbeat: Any = None,
) -> dict:
    """카페 1곳의 V2R 글 목록을 색인에 모은다.

    돌려주는 값: `{"cafe", "cafe_id", "accounts", "rows", "new", "updated",
    "bodies", "errors"}`.
    """
    entry = _cafe_entry(rt, cafe_name)
    name = str(entry.get("name") or cafe_name)
    cafe_id = entry.get("cafe_id")
    out: dict = {
        "cafe": name,
        "cafe_id": cafe_id,
        "accounts": 0,
        "rows": 0,
        "new": 0,
        "updated": 0,
        "bodies": 0,
        "errors": [],
    }
    if not cafe_id:
        out["errors"].append(f"cafe_id를 찾지 못했습니다: {cafe_name}")
        return out

    def beat() -> None:
        if heartbeat is not None:
            try:
                heartbeat()
            except Exception:
                pass

    logins = _accounts(rt, cafe_id)
    out["accounts"] = len(logins)
    for login_id in logins:
        try:
            rows = api_articles.written_articles(
                rt.client, cafe_id, login_id, pages=MAX_PAGES
            )
        except Exception as exc:
            out["errors"].append(f"{login_id}: {exc}")
            beat()
            continue
        for row in rows:
            res = rt.article_index.upsert(
                cafe_id=cafe_id,
                cafe=name,
                source_id=row.get("source_id"),
                article_id=row.get("article_id"),
                login_id=row.get("naver_login_id") or login_id,
                title=row.get("title") or "",
            )
            out["rows"] += 1
            out["new" if res else "updated"] += 1
        beat()

    if with_bodies:
        out["bodies"] = _fill_bodies(rt, name, out["errors"], heartbeat=heartbeat)
    return out


def _fill_bodies(
    rt: Runtime, cafe: str, errors: list, *, heartbeat: Any = None
) -> int:
    """본문 해시가 빈 행을 `get_article`로 채운다. 채운 건수를 돌려준다."""
    filled = 0
    for row in rt.article_index.rows_missing_body(cafe, limit=BODY_LIMIT):
        source_id = str(row.get("source_id") or "")
        if not source_id or source_id.startswith("article:"):
            continue  # V2R source_id가 없는 옛 글은 상세 조회를 할 수 없다
        try:
            detail = api_articles.get_article(rt.client, source_id)
            body = "\n".join(api_articles.body_lines_of(detail))
            title = str(row.get("title") or "")
            rt.article_index.set_body_hash(
                row.get("cafe_id"), source_id, content_hash(title, body)
            )
            filled += 1
        except Exception as exc:
            errors.append(f"본문 {source_id}: {exc}")
        if heartbeat is not None:
            try:
                heartbeat()
            except Exception:
                pass
        time.sleep(BODY_SLEEP)
    return filled


def sync_all_self_cafes(
    rt: Runtime, *, with_bodies: bool = False, heartbeat: Any = None
) -> dict:
    """자사 카페 **전부**(발행 제외 카페도) 색인 동기화.

    제외 카페라도 V2R에는 그 카페 글이 실제로 있다. 중복을 막으려면 그 글도 알아야 한다.
    """
    from v2r.engine.publish import self_cafe_names

    results: list[dict] = []
    errors: list[str] = []
    for cafe in self_cafe_names(rt, include_excluded=True):
        res = sync_cafe_index(rt, cafe, with_bodies=with_bodies, heartbeat=heartbeat)
        errors.extend(res.get("errors") or [])
        results.append(res)
    return {
        "cafes": results,
        "rows": sum(r["rows"] for r in results),
        "new": sum(r["new"] for r in results),
        "updated": sum(r["updated"] for r in results),
        "bodies": sum(r["bodies"] for r in results),
        "errors": errors,
        "total": rt.article_index.count(),
    }


def record_published(
    rt: Runtime,
    *,
    cafe: str,
    cafe_id: Any = None,
    source_id: Any = None,
    login_id: str = "",
    title: str = "",
    body_hash: str = "",
) -> None:
    """이 시스템이 방금 올린 글을 색인에 바로 남긴다 (최선 노력).

    이렇게 해 두면 다시 동기화하지 않아도 색인이 늘 최신이고, 같은 날 같은 글이
    두 번 나가지 않는다. 실패해도 발행 결과를 바꾸지 않는다.
    """
    try:
        if cafe_id is None:
            cafe_id = _cafe_entry(rt, cafe).get("cafe_id")
        rt.article_index.upsert(
            cafe_id=cafe_id,
            cafe=str(_cafe_entry(rt, cafe).get("name") or cafe),
            source_id=source_id,
            login_id=login_id,
            title=title,
            body_hash=body_hash or None,
        )
    except Exception as exc:  # pragma: no cover - 방어용
        log.warning("글 목록 색인 기록 실패: %s", exc)


def duplicate_check(rt: Runtime, spec: Any, limit: int = 0) -> dict:
    """앞으로 쓸 원고 N건을 색인과 **대조만** 해 본다 (아무것도 바꾸지 않는다).

    `{"checked", "duplicates", "clean", "items", "index_total"}`.
    """
    from v2r.content import duplicate as dup_mod
    from v2r.engine import publish as publish_mod

    want = int(limit or getattr(spec, "count", 0) or 0) or 50
    items: list[dict] = []
    checked = 0
    for entry in publish_mod.select_source_entries(rt, spec):
        name = str(entry.get("name") or "")
        try:
            manuscripts = publish_mod.load_manuscripts(rt, entry, prefer_cache=True)
        except Exception as exc:
            items.append({"source": name, "reason": f"원본 적재 실패: {exc}", "duplicate": False})
            continue
        for m in manuscripts:
            if checked >= want:
                break
            checked += 1
            cafe = m.cafe or getattr(spec, "cafe", "") or ""
            dup, why = dup_mod.is_duplicate_against_index(rt, m, cafe)
            if dup:
                items.append(
                    {
                        "source": name,
                        "row": m.source_row,
                        "cafe": cafe,
                        "title": m.title,
                        "duplicate": True,
                        "reason": why,
                    }
                )
        if checked >= want:
            break
    dup_count = sum(1 for i in items if i.get("duplicate"))
    return {
        "checked": checked,
        "duplicates": dup_count,
        "clean": checked - dup_count,
        "items": items,
        "index_total": rt.article_index.count(),
    }


__all__ = [
    "duplicate_check",
    "record_published",
    "sync_all_self_cafes",
    "sync_cafe_index",
]
