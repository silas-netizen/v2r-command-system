"""CLI: python -m v2r "<한국어 명령>" | status | serve."""

from __future__ import annotations

import hashlib
import sys

from v2r.command.parser import describe_spec, parse_korean_command
from v2r.config import get_settings
from v2r.store.db import connect, init_schema
from v2r.store.jobs import JobStore
from v2r.command.spec import today_kst


def _idem_key(text: str) -> str:
    """같은 날 같은 명령은 한 번만."""
    return hashlib.sha256((text + today_kst()).encode("utf-8")).hexdigest()


def _open_store() -> JobStore:
    conn = connect(get_settings().db_path)
    init_schema(conn)
    return JobStore(conn)


def _cmd_status() -> int:
    store = _open_store()
    jobs = store.recent(10)
    if not jobs:
        print("등록된 작업이 없습니다.")
        return 0
    for job in jobs:
        print(f"{job['id']}\t{job['status']}\t{job['task']}\t{job['created_at']}")
    return 0


def _cmd_run(text: str) -> int:
    spec = parse_korean_command(text)
    if spec is None:
        print("명령을 해석하지 못했습니다. (묶음 5에서 모델 폴백 연결)")
        return 1
    print(spec.to_json())
    print(describe_spec(spec))
    job_id = _open_store().enqueue(spec, _idem_key(text))
    print(f"작업 {job_id} 등록됨")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print('사용법: python -m v2r "<한국어 명령>" | status | serve')
        return 2
    if args[0] == "status":
        return _cmd_status()
    if args[0] == "serve":
        print("serve: 채널 모듈은 묶음 5에서 연결됩니다")
        return 0
    return _cmd_run(" ".join(args))


if __name__ == "__main__":
    raise SystemExit(main())
