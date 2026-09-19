"""이미 올라간 글에서 이모지를 걷어내는 뒷정리 (`이모지 정리`).

2026-09-19 자사 카페 일상 글 18건이 제목에 이모지를 단 채 발행됐다. 미리 만들어 둔
xlsx 원고에 이모지가 들어 있었고 검사는 "생성한 댓글"만 했기 때문이다. 발행 경로에는
`content/sanitize.py` 거름망을 세 겹으로 넣었고(재발 방지), **이미 나간 글**은 이 모듈이
찾아서 고친다.

무엇을 하나
-----------
- 발행 기록(`publications`) 중 `status = done`이고 `source_id`가 있는 것을 훑는다.
  기본은 **오늘 것만**, 명령에 `전체`가 있으면 전부.
- 글을 읽어(`get_article`) 제목과 본문에 이모지가 있으면 지운 내용으로
  `update_article` → 다시 읽어 정말 지워졌는지 확인한다.
- 댓글(`naver_cafe_article_source_comments`)에 이모지가 있으면 **고치지 않고** 보고에만
  적는다. 등록된 댓글만 따로 고치는 API가 검증되지 않았기 때문이다(api-spec §9).
- 기본은 모의 실행이다. 명령에 `실제`가 있어야 진짜 고친다.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from v2r.api import articles as api_articles
from v2r.command.spec import TaskSpec
from v2r.content import seone
from v2r.content.sanitize import has_emoji, strip_emoji
from v2r.engine.context import Runtime

log = logging.getLogger(__name__)

#: 글 사이에 쉬는 시간(초) — V2R 레이트 제한을 건드리지 않기 위해서다
SLEEP_S = 0.3

#: 정리를 마친 발행 기록의 단계
CLEANED_STAGE = "이모지 정리"


def target_publications(rt: Runtime, kst_date: str = "") -> list[dict]:
    """정리 대상 발행 기록. `kst_date`가 있으면 그날 것만."""
    sql = (
        "SELECT * FROM publications"
        " WHERE status = 'done' AND source_id IS NOT NULL AND source_id <> ''"
    )
    params: tuple = ()
    if kst_date:
        sql += " AND substr(created_at, 1, 10) = ?"
        params = (kst_date,)
    rows = rt.conn.execute(sql + " ORDER BY updated_at", params).fetchall()
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        pub = dict(row)
        sid = str(pub.get("source_id") or "")
        if sid in seen:
            continue
        seen.add(sid)
        out.append(pub)
    return out


def title_of(detail: dict) -> str:
    """글 상세의 제목."""
    src = detail.get("naver_cafe_article_source") if isinstance(detail, dict) else None
    if isinstance(src, dict):
        return str(src.get("title") or "")
    return ""


def comment_texts(detail: dict) -> list[str]:
    """등록된 댓글 본문 목록."""
    raw = detail.get("naver_cafe_article_source_comments") if isinstance(detail, dict) else None
    out: list[str] = []
    for node in api_articles.flatten_comment_nodes(raw if isinstance(raw, list) else []):
        text = node.get("contents") or node.get("text") or ""
        if text:
            out.append(str(text))
    return out


def inspect(detail: dict) -> dict:
    """글 1건에서 이모지를 찾아 (제목/본문/댓글) 상태를 정리한다."""
    title = title_of(detail)
    body_lines = api_articles.body_lines_of(detail)
    body = "\n".join(body_lines)
    dirty_comments = [t for t in comment_texts(detail) if has_emoji(t)]
    return {
        "title": title,
        "clean_title": strip_emoji(title),
        "body": body,
        "clean_body": strip_emoji(body),
        "title_dirty": has_emoji(title),
        "body_dirty": has_emoji(body),
        "comment_dirty": len(dirty_comments),
    }


def cleanup_emoji(
    rt: Runtime,
    spec: TaskSpec,
    job_id: int | None = None,
    heartbeat: Any = None,
) -> dict:
    """`cleanup_emoji` 작업 처리기."""
    scope_all = "전체" in str(spec.notes or "")
    kst_date = "" if scope_all else spec.start_date
    pubs = target_publications(rt, kst_date)

    fixed: list[dict] = []
    manual: list[dict] = []
    clean: int = 0
    errors: list[str] = []
    unreadable: list[str] = []

    for index, pub in enumerate(pubs):
        source_id = str(pub.get("source_id") or "")
        if heartbeat is not None:
            try:
                heartbeat()
            except Exception:  # 리스 연장 실패가 정리를 막지 않는다
                pass
        if index:
            time.sleep(SLEEP_S)
        try:
            detail = api_articles.get_article(rt.client, source_id)
        except Exception as exc:
            # 지워진 글(고아 글 정리 등)은 읽을 수 없다 → 실패가 아니라 건너뜀으로 기록
            unreadable.append(f"{source_id}: 읽기 실패 {str(exc)[:80]}")
            continue

        found = inspect(detail)
        item = {
            "source_id": source_id,
            "url": pub.get("url") or api_articles.article_url(source_id),
            "cafe": pub.get("cafe") or "",
            "source_key": pub.get("source_key") or "",
            "row_number": pub.get("row_number") or 0,
            "content_hash": pub.get("content_hash") or "",
            "before_title": found["title"],
            "after_title": found["clean_title"],
        }
        if found["comment_dirty"]:
            manual.append({**item, "comments": found["comment_dirty"]})
        if not (found["title_dirty"] or found["body_dirty"]):
            clean += 1
            continue
        if spec.dry_run:
            fixed.append({**item, "planned": True})
            continue

        try:
            api_articles.update_article(
                rt.client,
                source_id,
                title=found["clean_title"],
                content_json=seone.content_json(found["clean_body"]),
            )
            after = api_articles.get_article(rt.client, source_id)
        except Exception as exc:
            errors.append(f"{source_id}: 수정 실패 {exc}")
            continue

        recheck = inspect(after)
        if recheck["title_dirty"] or recheck["body_dirty"]:
            errors.append(f"{source_id}: 수정했는데 이모지가 남아 있습니다")
            continue
        item["after_title"] = recheck["title"]
        item["fixed"] = True
        fixed.append(item)
        try:
            rt.publications.mark(
                str(item["source_key"]),
                int(item["row_number"] or 0),
                str(item["content_hash"]),
                "done",
                CLEANED_STAGE,
            )
        except Exception:  # 기록 갱신 실패가 정리를 막지 않는다
            pass

    text = report(fixed, manual, clean, errors, spec.dry_run, scope_all)
    path = write_report(rt, spec, text)
    try:
        rt.events.log(job_id, "info", f"이모지 정리: 수정 {len(fixed)}건 / 수동 {len(manual)}건")
    except Exception:
        pass
    try:
        from v2r.channels import notify_all

        notify_all(rt.channels, text)
    except Exception as exc:  # 알림 실패가 본 작업을 죽이지 않는다
        log.warning("이모지 정리 보고 실패: %s", exc)

    return {
        "ok": not errors,
        "unreadable": unreadable,
        "dry_run": spec.dry_run,
        "scope": "전체" if scope_all else spec.start_date,
        "checked": len(pubs),
        "clean": clean,
        "fixed": fixed,
        "manual": manual,
        "errors": errors,
        "report_path": path,
        "report": text,
    }


def report(
    fixed: list[dict],
    manual: list[dict],
    clean: int,
    errors: list[str],
    dry_run: bool,
    scope_all: bool = False,
) -> str:
    """사람이 읽는 보고."""
    head = "이모지 정리" + (" (모의 실행)" if dry_run else "") + (" · 전체" if scope_all else " · 오늘")
    lines = [
        head,
        f"- 고칠 글 {len(fixed)}건 / 깨끗한 글 {clean}건 / 댓글 수동 확인 {len(manual)}건",
    ]
    for item in fixed:
        mark = "고침" if item.get("fixed") else "대상"
        lines.append(
            f"- [{mark}] {item['source_id']} {item['url']}"
            f"\n    {item['before_title']} → {item['after_title']}"
        )
    for item in manual:
        lines.append(
            f"- [댓글 이모지 — 수동 확인] {item['source_id']} {item['url']}"
            f" (댓글 {item['comments']}개)"
        )
    for e in errors:
        lines.append(f"- 실패: {e}")
    if dry_run:
        lines.append("실제로 고치려면 명령에 `실제`를 붙이세요 (`오늘 이모지 정리 실제`).")
    return "\n".join(lines)


def report_path(rt: Runtime, spec: TaskSpec):
    """보고서 경로 (`docs/reports/emoji-cleanup-<날짜>.md`)."""
    # data_dir의 부모 = 저장소 루트(실행) / 임시 폴더(테스트) → 테스트가 실제 docs/에 파일을 남기지 않는다
    return Path(rt.settings.data_dir).parent / "docs" / "reports" / f"emoji-cleanup-{spec.start_date}.md"


def write_report(rt: Runtime, spec: TaskSpec, text: str) -> str:
    """보고서 파일을 남기고 경로를 돌려준다."""
    path = report_path(rt, spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# 이모지 정리 ({spec.start_date})\n\n{text}\n", encoding="utf-8")
    return str(path)


__all__ = [
    "CLEANED_STAGE",
    "SLEEP_S",
    "cleanup_emoji",
    "comment_texts",
    "inspect",
    "report",
    "report_path",
    "target_publications",
    "title_of",
    "write_report",
]
