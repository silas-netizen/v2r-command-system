"""끊긴 작업 점검: `uncertain` 건을 V2R 이력으로 확정한다 (DESIGN §5-6).

자동 재발행은 하지 않는다. 확인만 한다.
"""

from __future__ import annotations

from typing import Any

from v2r.api import articles as api_articles
from v2r.api.client import field, walk_dicts
from v2r.api.errors import V2RApiError, classify
from v2r.engine.context import Runtime

DONE_STATUSES = {"DONE", "SUCCESS"}
RESERVED_STATUS = "RESERVED"
FAIL_STATUSES = {"FAIL", "FAILED", "DELETED"}

# 이 단계들에서는 publications.source_id가 "부모(일상 글)"를 가리킨다 (C-1)
PARENT_STAGES = {"daily_created", "daily_done", "revision_submitting"}
# 이력 폴백에서 허용하는 시각 오차(초)
MATCH_SLACK_S = 15
HISTORY_DAYS = 30


def _status_of(detail: dict) -> str:
    """상세 응답에서 등록 상태 문자열을 꺼낸다."""
    history = detail.get("naver_cafe_article_history") if isinstance(detail, dict) else None
    if not isinstance(history, dict):
        history = next(
            (d for d in walk_dicts(detail) if isinstance(d, dict) and "status" in d),
            {},
        )
    return str(field(history, "status", default="") or "").upper()


def _cafe_id_for(rt: Runtime, cafe_name: str) -> Any:
    """카페 이름 → cafe_id (설정 우선, 없으면 카탈로그)."""
    name = (cafe_name or "").strip()
    if not name:
        return None
    cfg = rt.cafes_cfg or {}
    for group in ("affiliate", "self_owned"):
        for entry in cfg.get(group) or []:
            if str(entry.get("name", "")).strip() == name:
                return entry.get("cafe_id")
    for tname, cid in (cfg.get("test") or {}).items():
        if str(tname).strip() == name:
            # 값은 cafe_id(정수) 또는 {cafe_id, board} 형태 둘 다 허용한다
            return cid.get("cafe_id") if isinstance(cid, dict) else cid
    try:
        from v2r.api.catalog import match_name

        cafe = match_name(name, rt.catalog.cafes(), key=lambda c: c.name)
        return cafe.cafe_id
    except Exception:
        return None


def _title_for(rt: Runtime, pub: dict) -> str:
    """원본 캐시에서 해당 행의 제목을 되살린다(없으면 빈 문자열)."""
    from v2r.engine.publish import _parse_rows

    payload = rt.sources_cache.get(str(pub.get("source_key") or ""))
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return ""
    try:
        items = _parse_rows(rows, str(pub.get("source_key") or ""), rt.cafes_cfg)
    except Exception:
        return ""
    for m in items:
        if m.source_row == int(pub.get("row_number") or 0):
            return m.title
    return ""


def _verdict(status: str, *, scheduled: bool) -> str | None:
    """등록 상태 → done/failed. 판단 불가면 None.

    `RESERVED`(예약 대기)는 예약 건일 때만 완료로 본다. 즉시 발행 건의 `RESERVED`는
    아직 올라가지 않은 상태이므로 미확정으로 남긴다 (DESIGN §6).
    """
    if status in DONE_STATUSES:
        return "done"
    if status in FAIL_STATUSES:
        return "failed"
    if status == RESERVED_STATUS and scheduled:
        return "done"
    return None


def _created_floor(pub: dict):
    """이 발행 기록이 만들어진 시각 - 여유(초). 모르면 None."""
    from datetime import timedelta

    from v2r.api.articles import _parse_dt

    made = _parse_dt(pub.get("created_at"))
    if made is None:
        return None
    return made - timedelta(seconds=MATCH_SLACK_S)


def _created_at_ok(row: dict, floor) -> bool:
    """이력 행이 이 발행 기록 이후에 만들어졌는가(시각 미상이면 채택 금지)."""
    from v2r.api.articles import _parse_dt

    if floor is None:
        return True
    created = _parse_dt(field(row, "created_at", "createdAt"))
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=floor.tzinfo)
    return created >= floor


def _find_child(rt: Runtime, cafe_id: Any, parent_id: str, pub: dict) -> dict | None:
    """제휴 수정글(부모=일상 글)을 이력에서 찾는다. 후보가 여럿이면 None."""
    rows = api_articles.board_histories(
        rt.client,
        cafe_id,
        days_ago=HISTORY_DAYS,
        login_id=pub.get("account") or None,
    )
    floor = _created_floor(pub)
    matches = [
        row
        for row in rows
        if str(field(row, "parent_source_id", "parentSourceId", default="") or "")
        == str(parent_id)
        and _created_at_ok(row, floor)
    ]
    return matches[0] if len(matches) == 1 else None


def reconcile(rt: Runtime) -> dict:
    """미확정 발행 건을 done/failed로 확정한다."""
    result = {"checked": 0, "done": 0, "failed": 0, "unresolved": [], "errors": []}

    for pub in rt.publications.list_uncertain():
        result["checked"] += 1
        key = (pub["source_key"], int(pub["row_number"]), pub["content_hash"])
        source_id = pub.get("source_id")
        stage = str(pub.get("stage") or "")
        scheduled = bool(pub.get("scheduled_at"))
        label = f"{pub['source_key']}#{pub['row_number']}"

        if source_id and stage in PARENT_STAGES:
            # source_id는 일상 글(부모)이다. 홍보 원고는 그 자식(수정글)이어야 한다 (C-1)
            cafe_id = _cafe_id_for(rt, str(pub.get("cafe") or ""))
            if cafe_id is None:
                result["unresolved"].append(label)
                continue
            try:
                child = _find_child(rt, cafe_id, str(source_id), pub)
            except V2RApiError as exc:
                result["errors"].append(f"{label}: {exc}")
                result["unresolved"].append(label)
                continue
            if child is None:
                result["unresolved"].append(label)
                continue
            child_status = str(field(child, "status", default="") or "").upper()
            verdict = _verdict(child_status, scheduled=True)  # 수정글은 항상 예약 등록
            child_sid = field(child, "source_id", "sourceId")
            if verdict == "done" and child_sid:
                rt.publications.mark(
                    *key,
                    "done",
                    "done",
                    source_id=str(child_sid),
                    url=api_articles.article_url(str(child_sid)),
                )
                result["done"] += 1
            elif verdict == "failed":
                rt.publications.mark(*key, "failed", "revision_failed")
                result["failed"] += 1
            else:
                result["unresolved"].append(label)
            continue

        if source_id:
            try:
                detail = api_articles.get_article(rt.client, str(source_id))
            except V2RApiError as exc:
                if classify(exc) == "deleted":
                    rt.publications.mark(*key, "failed", "done")
                    result["failed"] += 1
                    continue
                result["errors"].append(f"{label}: {exc}")
                result["unresolved"].append(label)
                continue
            status = _status_of(detail)
            verdict = _verdict(status, scheduled=scheduled)
            if verdict == "done":
                rt.publications.mark(
                    *key, "done", "done", url=api_articles.article_url(str(source_id))
                )
                result["done"] += 1
            elif verdict == "failed":
                rt.publications.mark(*key, "failed", "done")
                result["failed"] += 1
            else:
                result["unresolved"].append(label)
            continue

        # source_id를 모르면 최근 이력에서 계정+제목으로 찾는다
        cafe_id = _cafe_id_for(rt, str(pub.get("cafe") or ""))
        if cafe_id is None:
            result["unresolved"].append(label)
            continue
        title = _title_for(rt, pub)
        if not title:
            # 제목을 모르면 계정만으로 확정하지 않는다 (C-2) — 사람이 확인한다
            result["unresolved"].append(label)
            continue

        try:
            # board_histories GET 경로는 라이브에 없다 → 계정 기준 폴백이 필요하다
            rows = api_articles.board_histories(
                rt.client,
                cafe_id,
                days_ago=HISTORY_DAYS,
                login_id=pub.get("account") or None,
            )
        except V2RApiError as exc:
            result["errors"].append(f"{label}: {exc}")
            result["unresolved"].append(label)
            continue

        floor = _created_floor(pub)
        candidates = []
        for row in rows:
            login = field(row, "naver_account_login_id", "naver_login_id", "login_id")
            if pub.get("account") and login != pub.get("account"):
                continue
            if field(row, "title") != title:
                continue
            if (row.get("parent_source_id") or None) is not None:
                continue  # 제휴 수정글은 부모 경로로만 찾는다
            if not _created_at_ok(row, floor):
                continue
            candidates.append(row)

        if len(candidates) != 1:  # 0건이거나 모호하면 사람이 확인한다 (M-8)
            result["unresolved"].append(label)
            continue
        found = candidates[0]
        status = str(field(found, "status", default="") or "").upper()
        sid = field(found, "source_id", "sourceId")
        if _verdict(status, scheduled=scheduled) == "done" and sid:
            rt.publications.mark(
                *key,
                "done",
                "done",
                source_id=str(sid),
                url=api_articles.article_url(str(sid)) if sid else None,
            )
            result["done"] += 1
        elif status in FAIL_STATUSES:
            rt.publications.mark(*key, "failed", "done")
            result["failed"] += 1
        else:
            result["unresolved"].append(label)

    return result


__all__ = ["reconcile"]
