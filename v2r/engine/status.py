"""상태 보고문 생성 (한국어). 비밀값은 절대 넣지 않는다."""

from __future__ import annotations

from datetime import timedelta

from v2r.engine.context import Runtime
from v2r.store.db import KST, now_iso

STATUS_LABELS = {
    "queued": "대기",
    "running": "실행중",
    "done": "완료",
    "failed": "실패",
    "uncertain": "불확실",
    "cancelled": "취소",
    "skipped": "건너뜀",
}


def _label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def _publication_counts(rt: Runtime) -> dict[str, int]:
    rows = rt.conn.execute(
        "SELECT status, COUNT(*) AS n FROM publications GROUP BY status"
    ).fetchall()
    return {r["status"]: int(r["n"]) for r in rows}


def status_report(rt: Runtime, exclude_job_id: int | None = None) -> str:
    """최근 작업·발행 현황·불확실 건·제한 계정 요약.

    `exclude_job_id`는 보고문을 만드는 작업 자신(실행중으로 보이는 것)을 뺀다.
    """
    lines: list[str] = ["[V2R 현황]"]

    jobs = [j for j in rt.jobs.recent(11) if int(j["id"]) != (exclude_job_id or -1)][:10]
    if jobs:
        lines.append("최근 작업:")
        for job in jobs:
            lines.append(
                f"  {job['id']} {_label(job['status'])} {job['task']} {job['created_at']}"
            )
    else:
        lines.append("최근 작업: 없음")

    counts = _publication_counts(rt)
    if counts:
        summary = ", ".join(f"{_label(k)} {v}건" for k, v in sorted(counts.items()))
    else:
        summary = "없음"
    lines.append(f"발행 기록: {summary}")

    uncertain = rt.publications.list_uncertain()
    if uncertain:
        lines.append(f"불확실 {len(uncertain)}건 (점검 필요):")
        for pub in uncertain[:10]:
            lines.append(
                f"  {pub['source_key']}#{pub['row_number']} 단계={pub.get('stage') or '-'}"
                f" 계정={pub.get('account') or '-'} 카페={pub.get('cafe') or '-'}"
            )
    else:
        lines.append("불확실 건 없음")

    restricted = [
        login
        for login in rt.account_state.last_used_map()
        if rt.account_state.is_restricted(login)
    ]
    lines.append(
        "제한 계정: " + (", ".join(restricted) if restricted else "없음")
    )
    return "\n".join(lines)


def inspect_failures(rt: Runtime, days: int = 7) -> str:
    """최근 실패·불확실 건 모아보기."""
    from datetime import datetime

    since = (datetime.now(KST) - timedelta(days=days)).isoformat(timespec="seconds")
    lines: list[str] = [f"[최근 {days}일 실패 점검] 기준 {now_iso()}"]

    jobs = rt.conn.execute(
        "SELECT id, task, error, updated_at FROM jobs WHERE status = 'failed'"
        " AND updated_at >= ? ORDER BY id DESC LIMIT 20",
        (since,),
    ).fetchall()
    if jobs:
        lines.append("실패한 작업:")
        for job in jobs:
            lines.append(
                f"  {job['id']} {job['task']} {job['updated_at']} :: {job['error'] or '사유 없음'}"
            )
    else:
        lines.append("실패한 작업 없음")

    pubs = rt.conn.execute(
        "SELECT * FROM publications WHERE status IN ('failed', 'uncertain')"
        " AND updated_at >= ? ORDER BY updated_at DESC LIMIT 30",
        (since,),
    ).fetchall()
    if pubs:
        lines.append("문제 있는 발행 기록:")
        for pub in pubs:
            lines.append(
                f"  {pub['source_key']}#{pub['row_number']} {_label(pub['status'])}"
                f" 단계={pub['stage'] or '-'} 계정={pub['account'] or '-'}"
                f" 링크={pub['url'] or '-'}"
            )
    else:
        lines.append("문제 있는 발행 기록 없음")

    events = rt.events.recent(limit=10)
    errors = [e for e in events if e["level"] in ("error", "warn")]
    if errors:
        lines.append("최근 오류 로그:")
        for e in errors:
            lines.append(f"  {e['created_at']} [{e['level']}] {e['message']}")
    return "\n".join(lines)


__all__ = ["inspect_failures", "status_report"]
