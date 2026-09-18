"""고아 글 정리: 일상 글만 올라가고 수정글이 끝내 안 만들어진 찌꺼기를 지운다.

제휴 워크플로는 `일상 글(부모) → 수정글(자식)` 두 단계로 등록된다.
두 단계 사이에서 끊기면 V2R에는 부모 일상 글만 남는다. 이때
`naver_cafe_article_source.child_source_id`가 `None`이다
(`docs/reference/live-affiliate-sample.md` 잡 40 사례).

발행 기록이 `failed`(또는 `등록 전 실패…` 단계)인데 그 `source_id`의 글이
아직 서버에 살아 있고 자식이 없다면 → 아무도 쓰지 않는 고아다.

기본은 **모의 실행**이라 목록만 뽑는다. `spec.dry_run`이 False일 때만 지운다
(명령에 `실제 발행`/`실제 삭제` 같은 문구가 있어야 한다).
"""

from __future__ import annotations

import logging
from typing import Any

from v2r.api import articles as api_articles
from v2r.api.client import field, walk_dicts
from v2r.api.errors import V2RApiError, classify
from v2r.command.spec import TaskSpec
from v2r.engine.context import Runtime

log = logging.getLogger(__name__)

#: 고아 후보로 보는 단계 접두사
FAILED_STAGE_PREFIX = "등록 전 실패"


def child_source_id(detail: Any) -> str | None:
    """글 상세에서 자식(수정글) source_id를 꺼낸다. 없으면 None."""
    for node in walk_dicts(detail):
        value = field(node, "child_source_id", "childSourceId")
        if value:
            return str(value)
    return None


def orphan_candidates(rt: Runtime) -> list[dict]:
    """`failed`/`등록 전 실패…`이면서 source_id가 있는 발행 기록."""
    rows = rt.conn.execute(
        "SELECT * FROM publications"
        " WHERE source_id IS NOT NULL AND source_id <> ''"
        "   AND (status = 'failed' OR stage LIKE ?)"
        " ORDER BY updated_at",
        (FAILED_STAGE_PREFIX + "%",),
    ).fetchall()
    return [dict(r) for r in rows]


def _label(pub: dict) -> str:
    return f"{pub.get('source_key', '')}#{pub.get('row_number', '')}"


def cleanup_orphans(rt: Runtime, spec: TaskSpec) -> dict:
    """고아 일상 글을 찾아 목록을 만들고, 실제 실행이면 삭제한다."""
    candidates = orphan_candidates(rt)
    orphans: list[dict] = []
    skipped: list[dict] = []
    deleted: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()

    for pub in candidates:
        source_id = str(pub.get("source_id") or "")
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        try:
            detail = api_articles.get_article(rt.client, source_id)
        except V2RApiError as exc:
            if classify(exc) == "deleted":
                skipped.append({**_brief(pub), "reason": "이미 삭제된 글"})
                continue
            errors.append(f"{_label(pub)}: {exc}")
            continue
        except Exception as exc:  # 네트워크 등
            errors.append(f"{_label(pub)}: {exc}")
            continue

        child = child_source_id(detail)
        if child:
            skipped.append({**_brief(pub), "reason": f"수정글이 있습니다({child})"})
            continue
        orphans.append({**_brief(pub), "title": _title_of(detail)})

    if not spec.dry_run:
        for item in orphans:
            source_id = item["source_id"]
            try:
                api_articles.delete_article(rt.client, source_id)
            except Exception as exc:
                errors.append(f"{item['label']}: 삭제 실패 {exc}")
                continue
            deleted.append(source_id)
            item["deleted"] = True
            rt.publications.mark(
                item["source_key"],
                int(item["row_number"]),
                item["content_hash"],
                "failed",
                "고아 정리 완료",
            )

    out: dict[str, Any] = {
        "ok": not errors,
        "dry_run": spec.dry_run,
        "checked": len(seen),
        "orphans": orphans,
        "skipped": skipped,
        "deleted": deleted,
        "errors": errors,
        "report": _report(orphans, deleted, spec.dry_run),
    }
    return out


def _brief(pub: dict) -> dict:
    return {
        "label": _label(pub),
        "source_key": pub.get("source_key", ""),
        "row_number": pub.get("row_number", 0),
        "content_hash": pub.get("content_hash", ""),
        "source_id": str(pub.get("source_id") or ""),
        "cafe": pub.get("cafe") or "",
        "account": pub.get("account") or "",
        "stage": pub.get("stage") or "",
        "url": api_articles.article_url(str(pub.get("source_id") or "")),
    }


def _title_of(detail: Any) -> str:
    for node in walk_dicts(detail):
        title = field(node, "title")
        if title:
            return str(title)
    return ""


def _report(orphans: list[dict], deleted: list[str], dry_run: bool) -> str:
    if not orphans:
        return "지울 고아 글이 없습니다."
    lines = [f"고아 글 {len(orphans)}건" + (" (모의 실행: 지우지 않았습니다)" if dry_run else "")]
    for item in orphans:
        mark = "삭제함" if item.get("deleted") else ("삭제 대상" if dry_run else "삭제 실패")
        lines.append(
            f"- {item['label']} / {item['cafe']} / {item['account']}"
            f" / {item['source_id']} / {item.get('title', '')} → {mark}"
        )
    if not dry_run:
        lines.append(f"실제로 지운 글 {len(deleted)}건")
    else:
        lines.append("정말 지우려면 명령 끝에 `실제 발행`을 붙이세요.")
    return "\n".join(lines)


__all__ = ["cleanup_orphans", "child_source_id", "orphan_candidates"]
