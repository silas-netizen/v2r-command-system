"""명령 한 줄을 작업 큐에 넣는다 (예약 실행용). 사용: python scripts/enqueue.py "자사 카페 일상 글 카페별 100건 실제 발행 댓글 랜덤" """
import json
import sys

from v2r.engine import worker
from v2r.engine.context import Runtime

args = sys.argv[1:]
if len(args) >= 2 and args[0] == "--file":
    # cmd 예약 실행에서 한글 인자가 깨지므로 파일(UTF-8)에서 읽는다
    from pathlib import Path

    text = Path(args[1]).read_text(encoding="utf-8").strip()
else:
    text = " ".join(args).strip()
if not text:
    raise SystemExit("명령을 적어 주세요")
rt = Runtime.open()
out = worker.handle_text(rt, text)
print(json.dumps({k: v for k, v in out.items() if k in ("ok", "job_id", "error", "message")}, ensure_ascii=False))
