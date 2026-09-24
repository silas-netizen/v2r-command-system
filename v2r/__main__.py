"""CLI: python -m v2r "<한국어 명령>" | status | session | health | dashboard | serve | run | reconcile."""

from __future__ import annotations

import json
import sys

from v2r.engine.context import Runtime


def _print_result(out: dict | None) -> None:
    """실행 결과를 한국어로 출력(비밀값 없음)."""
    if out is None:
        print("실행할 작업이 없습니다.")
        return
    job_id = out.get("job_id")
    status = out.get("status", "")
    labels = {"done": "완료", "failed": "실패", "uncertain": "불확실"}
    print(f"작업 {job_id} {labels.get(status, status)}: {out.get('description', '')}")
    if out.get("error"):
        print(f"  사유: {out['error']}")
    result = out.get("result") or {}
    if result.get("report"):
        print(result["report"])
        return
    if result.get("message"):
        print(f"  {result['message']}")
    if result.get("slots") is not None:
        print(f"  슬롯 {result['slots']}건 (모의 실행: {'예' if result.get('dry_run') else '아니오'})")
        for item in result.get("results") or []:
            print(
                f"  - {item.get('title', '')} / {item.get('account', '')} /"
                f" {item.get('cafe', '')} {item.get('board', '')} /"
                f" {item.get('scheduled_at', '')} → {item.get('status', '')}"
                + (f" {item.get('url')}" if item.get("url") else "")
                + (f" [{item['manuscript_type']}]" if item.get("manuscript_type") else "")
            )
            for role in item.get("comment_roles") or []:
                mark = " (작성자)" if role.get("is_author") else ""
                reply = role.get("reply_member") or "-"
                print(
                    f"      {role['label']:<10} {role['account']}{mark}"
                    f"  ← reply_member {reply}"
                )
            if item.get("comment_note"):
                print(f"      ! {item['comment_note']}")
        for line in result.get("failures") or []:
            print(f"  ! {line}")
        for skip in result.get("skipped") or []:
            print(f"  · 건너뜀 {skip.get('source', '')}#{skip.get('row', '')}: {skip.get('reason', '')}")
        return
    leftovers = {
        k: v for k, v in result.items() if k not in {"ok", "results", "dry_run"}
    }
    if leftovers:
        print("  " + json.dumps(leftovers, ensure_ascii=False, default=str))


def _span(seconds: float | None) -> str:
    """남은 시간을 사람 말로. 음수면 '만료됨'."""
    if seconds is None:
        return "알 수 없음"
    if seconds <= 0:
        return "만료됨"
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}시간 {rest // 60}분 남음" if hours else f"{rest // 60}분 남음"


def session_report_text(info: dict) -> str:
    """세션 상태를 한국어 여러 줄로. 토큰·쿠키 값은 절대 넣지 않는다."""
    token_line = (
        f"액세스 토큰: 있음 ({_span(info.get('token_expires_in_s'))})"
        if info["has_token"]
        else "액세스 토큰: 없음 (다음 사용 때 로그인합니다)"
    )
    cookie_line = (
        f"갱신 쿠키: 있음 ({_span(info.get('refresh_expires_in_s'))})"
        if info["has_refresh_cookie"]
        else "갱신 쿠키: 없음"
    )
    lines = [
        f"세션 파일: {info['session_file']}",
        token_line,
        cookie_line,
        f"최근 24시간 로그인: {info['logins_24h']}회 / 자체 한도 {info['login_budget']}회"
        " (서버 한도 20회)",
    ]
    return "\n".join(lines)


def _session_report(rt: Runtime) -> str:
    """`python -m v2r session` 출력."""
    return session_report_text(rt.client.session_report())


def _cmd_command(rt: Runtime, text: str) -> int:
    """한 번짜리 CLI 명령: 대기열에 등록만 하고 끝난다.

    예전에는 여기서 최대 20번 `run_once(rt)`(줄 구분 없음)를 돌며 큐를
    비웠다 — 그래서 옛 OS 예약(V2R-Reconcile)이 이 경로로
    `python -m v2r "끊긴 작업 점검"` 을 띄우면, 실행기 리스를 잡고 대기열의
    **다른** 작업(예: 장시간 재채점)까지 집어 그 자리에서 돌렸다(장애
    2026-09-24). 이 CLI 경로는 이제 자기 작업을 큐에 넣기만 하고 끝난다 —
    실제 실행은 serve(본 실행기)와 사이드카(light/long 줄)가 맡는다.
    """
    from v2r.engine import worker

    accepted = worker.handle_text(rt, text)
    if not accepted.get("ok"):
        print(accepted.get("error") or "명령을 해석하지 못했습니다.")
        return 1
    if accepted.get("stopped"):  # 중지는 큐를 거치지 않고 즉시 처리된다
        print(accepted.get("message") or "중지 요청을 처리했습니다.")
        return 0

    wanted = accepted["job_id"]
    print(f"작업 {wanted} 등록됨: {accepted['description']} (실행기가 처리합니다)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print(
            '사용법: python -m v2r "<한국어 명령>" | status | session | health'
            " | dashboard | serve | run | reconcile"
        )
        return 2

    rt = Runtime.open()
    try:
        from v2r.engine import worker
        from v2r.engine.reconcile import reconcile
        from v2r.engine.status import status_report

        head = args[0]
        if head == "status":
            print(status_report(rt))
            return 0
        if head == "dashboard":
            from v2r.engine.dashboard import build_dashboard

            print(build_dashboard(rt))
            return 0
        if head == "health":
            from v2r.engine.schedule import health_report

            print(health_report(rt))
            return 0
        if head == "session":
            print(_session_report(rt))
            return 0
        if head == "serve":
            worker.serve(rt)
            return 0
        if head == "run":
            outs = worker.drain(rt)
            if not outs:
                print("실행할 작업이 없습니다.")
            for out in outs:
                _print_result(out)
            return 0
        if head == "reconcile":
            result = reconcile(rt)
            print(
                f"점검 {result['checked']}건 → 완료 {result['done']}건,"
                f" 실패 {result['failed']}건, 미확정 {len(result['unresolved'])}건"
            )
            for label in result["unresolved"]:
                print(f"  · 수동 확인 필요: {label}")
            for err in result["errors"]:
                print(f"  ! {err}")
            return 0
        return _cmd_command(rt, " ".join(args))
    finally:
        rt.close()


if __name__ == "__main__":
    raise SystemExit(main())
