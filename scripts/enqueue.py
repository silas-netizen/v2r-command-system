"""명령 한 줄을 작업 큐에 넣는다 (예약 실행용). 사용: python scripts/enqueue.py "자사 카페 일상 글 카페별 100건 실제 발행 댓글 랜덤" """
import json
import sys

from v2r.engine import worker
from v2r.engine.context import Runtime

text = " ".join(sys.argv[1:]).strip()
if not text:
    raise SystemExit("명령을 적어 주세요")
rt = Runtime.open()
out = worker.handle_text(rt, text)
print(json.dumps({k: v for k, v in out.items() if k in ("ok", "job_id", "error", "message")}, ensure_ascii=False))
