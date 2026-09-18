"""CLI: python -m v2r "<한국어 명령>" | status | serve | run | reconcile."""

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


def _cmd_command(rt: Runtime, text: str) -> int:
    from v2r.engine import worker

    accepted = worker.handle_text(rt, text)
    if not accepted.get("ok"):
        print(accepted.get("error") or "명령을 해석하지 못했습니다.")
        return 1
    if accepted.get("stopped"):  # 중지는 큐를 거치지 않고 즉시 처리된다
        print(accepted.get("message") or "중지 요청을 처리했습니다.")
        return 0

    wanted = accepted["job_id"]
    print(f"작업 {wanted} 등록됨: {accepted['description']}")
    for _ in range(20):
        out = worker.run_once(rt)
        if out is None:
            print("실행할 작업이 없습니다.")
            return 0
        if out.get("job_id") != wanted:
            print(f"이전에 남은 작업 {out.get('job_id')}을(를) 먼저 실행했습니다.")
            _print_result(out)
            continue
        _print_result(out)
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print('사용법: python -m v2r "<한국어 명령>" | status | serve | run | reconcile')
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
