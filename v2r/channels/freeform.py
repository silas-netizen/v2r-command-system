"""슬랙·텔레그램 자유 대화 명령 해석기 (사용자 지시 2026-09-23).

`parse_korean_command` 문법에 안 맞는 채널 메시지를 모델(요금제 길, plan, 0원,
`freeform_command` 용도)로 넘겨 세 갈래 중 하나로 처리한다.

  - {"action": "command", "text": "<정확한 명령 문장>", "confirm": bool}
    → 그 문장을 다시 `parse_korean_command`로 해석해 `handle_text`로 실행한다.
      confirm=true(파괴적 작업)면 먼저 "실행할까요?" 되묻고, 다음 메시지가
      "네/ㅇㅇ/진행/응"이면 그제야 실행한다.
  - {"action": "answer", "text": "..."}  → 🟢 로 그대로 답한다(정보 질문).
  - {"action": "ask", "text": "..."}     → 🟢 로 되묻는다(불확실).

모델이 실패하거나 JSON을 못 읽으면 "잠깐 못 알아들었어요. 예: …" 한 줄로
되묻는다. "명령을 해석하지 못했습니다" 류의 실패 문구는 채널로 내보내지 않는다
(사용자 지시 2026-09-23 추가분).
"""

from __future__ import annotations

import json
import logging
import time as _time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 대화 상태(마지막 제안) 만료 시간(초)
STATE_TTL_SECONDS = 600

#: 확인으로 볼 답
_YES_WORDS = {"네", "예", "ㅇㅇ", "응", "그래", "진행", "고고", "ㅇㅋ", "오케이", "okay", "ok", "yes"}
_NO_WORDS = {"아니", "아니요", "ㄴㄴ", "취소", "그만", "no"}

#: 되묻기 기본 예시(모델 실패 시에도 쓴다)
FALLBACK_EXAMPLES = "현황 / 우아덤 대량 원고 3건 / 오늘 글 몇 개 나갔어?"

_STATE_PATH_CACHE: Path | None = None


def _state_path(rt: Any = None) -> Path:
    global _STATE_PATH_CACHE
    if _STATE_PATH_CACHE is not None:
        return _STATE_PATH_CACHE
    data_dir = None
    if rt is not None:
        data_dir = getattr(getattr(rt, "settings", None), "data_dir", None)
    if data_dir is None:
        data_dir = Path(__file__).resolve().parents[2] / "data"
    path = Path(data_dir) / "freeform_state.json"
    _STATE_PATH_CACHE = path
    return path


def _load_state(rt: Any = None) -> dict:
    path = _state_path(rt)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict, rt: Any = None) -> None:
    path = _state_path(rt)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("freeform 상태 저장 실패: %s", exc)


def _key(channel_name: str, chat_id: str) -> str:
    return f"{channel_name}:{chat_id}"


def get_pending(channel_name: str, chat_id: str, *, rt: Any = None) -> str | None:
    """이 방의 마지막 제안(확인 대기 중인 명령 문장)을 돌려준다. 없거나 만료면 None."""
    state = _load_state(rt)
    entry = state.get(_key(channel_name, chat_id))
    if not entry:
        return None
    if _time.time() - float(entry.get("at", 0)) > STATE_TTL_SECONDS:
        return None
    return entry.get("text") or None


def set_pending(channel_name: str, chat_id: str, text: str, *, rt: Any = None) -> None:
    state = _load_state(rt)
    state[_key(channel_name, chat_id)] = {"text": text, "at": _time.time()}
    _save_state(state, rt)


def clear_pending(channel_name: str, chat_id: str, *, rt: Any = None) -> None:
    state = _load_state(rt)
    state.pop(_key(channel_name, chat_id), None)
    _save_state(state, rt)


def _allowed_task_examples() -> str:
    """ALLOWED_TASKS + parser 패턴에서 짧은 예시 목록을 만든다."""
    try:
        from v2r.command.parser import TASK_PATTERNS, TASK_LABELS
    except Exception:  # noqa: BLE001
        return ""
    lines = []
    seen = set()
    for task, pattern in TASK_PATTERNS:
        if task in seen:
            continue
        seen.add(task)
        label = TASK_LABELS.get(task, task)
        lines.append(f"- {label}({task}): 패턴 예 `{pattern.pattern[:60]}`")
    return "\n".join(lines[:60])


def _status_summary(rt: Any) -> str:
    try:
        from v2r.engine.status import status_report

        return status_report(rt)[:1500]
    except Exception as exc:  # noqa: BLE001
        log.debug("상태 요약 실패: %s", exc)
        return "(상태 요약 불가)"


SYSTEM_PROMPT_HEADER = (
    "당신은 V2R 운영 채널의 자유 대화 명령 해석기입니다. 사용자가 슬랙/텔레그램에 "
    "정해진 명령 문법 없이 편하게 말합니다. 아래 '허용 명령 목록'에 있는 뜻으로 "
    "읽히면 action=command 로 정확한 명령 문장을 만드세요(목록에 없는 동작은 "
    "만들지 마세요). 정보를 묻는 말(예: 오늘 몇 건 나갔어?)이면 아래 '현재 상태'를 "
    "보고 action=answer 로 바로 답하세요. 애매하면 action=ask 로 되물으세요. "
    "중지/삭제/전체 재발행/실제 발행(N건 이상 실제 실행) 같은 되돌리기 어려운 "
    "작업은 confirm=true 로 표시하세요. 비밀번호·토큰·API 키를 묻거나 알려달라는 "
    "요청은 거절하고 action=answer 로 '비밀번호·토큰은 다루지 않습니다'라고 "
    "답하세요. 한국어로 짧게 답하세요. 반드시 JSON 한 줄만 출력하세요: "
    '{"action":"command","text":"...","confirm":false} 또는 '
    '{"action":"answer","text":"..."} 또는 {"action":"ask","text":"..."}'
)


def build_system_prompt(rt: Any) -> str:
    return (
        f"{SYSTEM_PROMPT_HEADER}\n\n"
        f"[허용 명령 목록]\n{_allowed_task_examples()}\n\n"
        f"[현재 상태]\n{_status_summary(rt)}"
    )


def _extract_json(text: str) -> dict | None:
    try:
        from v2r.llm.router import extract_json

        data = extract_json(text)
    except Exception:
        try:
            data = json.loads(text)
        except Exception:
            return None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def interpret(rt: Any, text: str) -> dict:
    """모델로 자유 대화 한 줄을 해석한다. 실패하면 action=ask 로 폴백."""
    router = getattr(rt, "llm", None)
    if router is None:
        return {"action": "ask", "text": f"잠깐 못 알아들었어요. 예: {FALLBACK_EXAMPLES}"}
    try:
        raw = router.complete("freeform_command", build_system_prompt(rt), text, max_tokens=500)
        data = _extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        log.info("freeform 해석 실패: %s", exc)
        data = None
    if not data or data.get("action") not in ("command", "answer", "ask"):
        return {"action": "ask", "text": f"잠깐 못 알아들었어요. 예: {FALLBACK_EXAMPLES}"}
    data.setdefault("text", "")
    data.setdefault("confirm", False)
    return data


def handle_channel_message(rt: Any, channel: Any, chat_id: str, text: str) -> dict:
    """채널 원문 한 줄을 처리해 그 방에 직접 답한다(필요하면).

    돌려주는 값은 진단용(테스트에서 확인). 실제 응답은 이 함수가 채널에 직접 보낸다.
    """
    from v2r.channels import sanitize
    from v2r.command.parser import parse_korean_command
    from v2r.command.spec import today_kst  # noqa: F401  (일부 파서가 참조)

    channel_name = str(getattr(channel, "name", "?"))

    def _reply(msg: str, *, tag: str = "reply") -> None:
        try:
            channel.send(chat_id, sanitize(msg))
        except Exception as exc:  # noqa: BLE001
            log.warning("freeform 답장 실패(%s): %s", channel_name, exc)

    stripped = text.strip()
    low = stripped.replace(" ", "")

    # 1) 확인 대기 중이던 제안에 대한 답인가?
    pending = get_pending(channel_name, chat_id, rt=rt)
    if pending is not None:
        if low in _YES_WORDS or any(low.startswith(w) for w in _YES_WORDS):
            clear_pending(channel_name, chat_id, rt=rt)
            return _run_command(rt, channel, chat_id, pending)
        if low in _NO_WORDS or any(low.startswith(w) for w in _NO_WORDS):
            clear_pending(channel_name, chat_id, rt=rt)
            _reply("취소했습니다.")
            return {"action": "cancelled"}
        # 그 외 응답이면 대기를 지우고 새 메시지로 다시 해석한다(오래 붙잡지 않는다)
        clear_pending(channel_name, chat_id, rt=rt)

    # 2) 기존 명령 문법에 맞는가?
    spec = parse_korean_command(stripped)
    if spec is not None:
        return _run_command(rt, channel, chat_id, stripped, spec=spec)

    # 3) 자유 대화 해석기로 넘긴다
    data = interpret(rt, stripped)
    action = data.get("action")
    if action == "answer":
        _reply(f"🟢 {data.get('text') or ''}".strip())
        return data
    if action == "ask":
        _reply(data.get("text") or f"잠깐 못 알아들었어요. 예: {FALLBACK_EXAMPLES}")
        return data
    # action == "command"
    cmd_text = str(data.get("text") or "").strip()
    if not cmd_text:
        _reply(f"잠깐 못 알아들었어요. 예: {FALLBACK_EXAMPLES}")
        return {"action": "ask", "text": FALLBACK_EXAMPLES}
    cmd_spec = parse_korean_command(cmd_text)
    if cmd_spec is None:
        _reply(f"잠깐 못 알아들었어요. 예: {FALLBACK_EXAMPLES}")
        return {"action": "ask", "text": FALLBACK_EXAMPLES}
    if data.get("confirm"):
        set_pending(channel_name, chat_id, cmd_text, rt=rt)
        _reply(f"'{cmd_text}' — 실행할까요? '네'라고 답하면 진행합니다.")
        return {"action": "confirm_pending", "text": cmd_text}
    return _run_command(rt, channel, chat_id, cmd_text, spec=cmd_spec)


def _run_command(rt: Any, channel: Any, chat_id: str, text: str, *, spec: Any = None) -> dict:
    """명령 문장을 실행기에 넣는다. 긴 작업이면 '접수' 1줄, 아니면 조용히 기다린다."""
    from v2r.channels import sanitize
    from v2r.command.spec import ALLOWED_TASKS  # noqa: F401
    from v2r.engine import worker

    out = worker.handle_text(rt, text, via_channel=True)
    if not out.get("ok"):
        try:
            channel.send(chat_id, sanitize(f"잠깐 못 알아들었어요. 예: {FALLBACK_EXAMPLES}"))
        except Exception as exc:  # noqa: BLE001
            log.warning("freeform 답장 실패: %s", exc)
        return {"action": "ask", "text": FALLBACK_EXAMPLES}
    if out.get("stopped"):
        # handle_text 가 이미 notify_all(level="always")로 모든 채널에 알렸다 — 중복 전송 안 함.
        return out
    worker.clear_stop(rt)
    job_id = out.get("job_id")
    worker.remember_origin(job_id, channel, chat_id)
    task = getattr(spec, "task", None) if spec is not None else None
    eta = _long_job_eta(task)
    if eta:
        try:
            channel.send(
                chat_id,
                sanitize(f"접수 — 약 {eta}분 예상 ({out.get('description') or text})"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("freeform 접수 답장 실패: %s", exc)
    # 접수 답장은 여기서 끝. 결과는 작업이 끝나면 reply_to_origin 이 1건만 보낸다.
    return out


_LONG_JOB_CACHE: dict[str, Any] = {"at": 0.0, "tasks": None}


def _long_job_eta(task: str | None) -> int:
    """긴 작업이면 예상 분(config/notify.yaml long_job_tasks), 아니면 0."""
    if not task:
        return 0
    now = _time.monotonic()
    if _LONG_JOB_CACHE["tasks"] is None or now - _LONG_JOB_CACHE["at"] > 60:
        tasks: dict[str, int] = {}
        try:
            import yaml

            cfg_path = Path(__file__).resolve().parents[2] / "config" / "notify.yaml"
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            raw = data.get("long_job_tasks") or {}
            if isinstance(raw, dict):
                tasks = {str(k): int(v) for k, v in raw.items()}
        except Exception as exc:  # noqa: BLE001
            log.debug("long_job_tasks 읽기 실패: %s", exc)
        _LONG_JOB_CACHE.update(at=now, tasks=tasks)
    return int(_LONG_JOB_CACHE["tasks"].get(str(task), 0) or 0)
