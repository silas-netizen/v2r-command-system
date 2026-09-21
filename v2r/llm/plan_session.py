"""요금제 길 로그인 점검 (사용자 철칙: 모든 로그인은 **1회 후 절대 유지**).

하는 일
-------
1. `claude auth status` 를 읽어 로그인 여부를 본다. **모델을 부르지 않으므로
   토큰도 한도도 쓰지 않는다.** (`loggedIn`, `authMethod` 를 본다)
2. 그래도 못 미더우면 `claude -p` 로 글자 하나짜리 호출을 보내 실제로 모델까지
   닿는지 본다 (`deep=True`). 한도에 걸리면 그것도 알아낸다.
3. 풀렸으면 `scripts\\claude-cli-login.cmd` 를 돌리라고 알린다.

하지 않는 일
------------
로그인 비밀(자격증명 파일)을 **읽거나 복사하지 않는다.** 백업·복원은 사람이
직접 할 일이고, 이 프로그램이 건드릴 자리가 아니다. 대신 파일 방식인지
OS 자격증명 저장소 방식인지만 확인해 보고에 적는다 (`mode`).

예약: 매일 09:25 `요금제 세션 점검`.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .plan_backend import PlanBackend, PlanError, PlanLimit, PlanNotLoggedIn

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

#: 로그인이 파일로 저장되는 설치 방식일 때 그 파일이 있을 자리.
#: **읽지도 복사하지도 않는다.** 있는지 없는지만 봐서 보고에 방식을 적는다.
CREDENTIAL_CANDIDATES = (
    r"{home}\.claude\.credentials.json",
    r"{localappdata}\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\.credentials.json",
    r"{appdata}\Claude\.credentials.json",
)

#: 다시 로그인해야 할 때 사람에게 보낼 문구
RELOGIN_NOTICE = (
    "요금제(Claude Code) 로그인이 풀렸습니다. "
    "`scripts\\claude-cli-login.cmd` 를 실행해 `/login` 을 한 번만 해 주세요. "
    "그때까지 원고는 API 길로 만듭니다(비용 발생)."
)

#: 로그인이 OS 자격증명 저장소에 있을 때 보고에 남기는 설명
OS_STORE_NOTE = (
    "이 설치본은 로그인을 파일이 아니라 OS 자격증명 저장소에 둡니다."
    " 백업·복원은 하지 않고 상태 점검만 합니다."
)

#: 로그인 확인용 아주 짧은 호출 (토큰을 거의 쓰지 않는다)
PING_SYSTEM = "Answer with a single character."
PING_USER = "1"

#: 사람이 한 번만 돌리면 되는 로그인 스크립트
LOGIN_SCRIPT = r"scripts\claude-cli-login.cmd"


def credential_mode() -> str:
    """로그인 저장 방식. `file`이면 파일이 있고, `os-store`면 OS 저장소다."""
    fields = {
        "home": os.environ.get("USERPROFILE") or os.path.expanduser("~"),
        "localappdata": os.environ.get("LOCALAPPDATA", ""),
        "appdata": os.environ.get("APPDATA", ""),
    }
    for template in CREDENTIAL_CANDIDATES:
        try:
            raw = template.format(**fields)
        except (KeyError, IndexError):  # pragma: no cover - 형식 오류 방어
            continue
        if "{" in raw or not raw.strip():
            continue
        if Path(raw).is_file():
            return "file"
    return "os-store"


def auth_status(exe: str, timeout: int = 60, runner: Any | None = None) -> dict:
    """`claude auth status` 의 JSON. 못 읽으면 빈 dict.

    모델을 부르지 않는다 — 한도를 한 톨도 쓰지 않는 점검이다.
    """
    import subprocess

    if not exe:
        return {}
    try:
        if runner is not None:
            code, out, err = runner([exe, "auth", "status"])
        else:
            proc = subprocess.run(  # noqa: S603 - 우리가 정한 실행 파일뿐
                [exe, "auth", "status"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            code, out, err = proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:  # pragma: no cover - 실행 실패는 "모름"으로 본다
        log.debug("claude auth status 실행 실패: %s", exc)
        return {}
    del code, err
    text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out or "")
    start = text.find("{")
    if start < 0:
        return {}
    try:
        data = json.loads(text[start:])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def check_session(
    data_dir: str | Path = "",
    backend: PlanBackend | None = None,
    model: str = "claude-haiku-4-5",
    deep: bool = False,
) -> dict:
    """로그인·한도 상태를 확인한다.

    기본은 `claude auth status` 만 읽는다 (모델 호출 없음, 한도 0).
    `deep=True` 면 그 뒤에 글자 하나짜리 실제 호출까지 해 본다.

    돌려주는 값: `ok`, `logged_in`, `limited`, `mode`(file/os-store),
    `auth_method`, `notice`(사람에게 보낼 문구. 없으면 빈 문자열), `note`, `exe`.
    """
    # data_dir: 자격증명 백업·복원 폴더(`data/claude-cli-backup`). 사용자 허용(2026-09-21):
    # "자동 복원까지 허용. 로그인 유지가 원칙, 풀리면 알아서 방법을 찾을 것."
    from v2r.llm import cli_credentials

    backend = backend or PlanBackend()
    out: dict[str, Any] = {
        "ok": False,
        "logged_in": False,
        "limited": False,
        "mode": credential_mode(),
        "exe": backend.exe,
        "auth_method": "",
        "deep": bool(deep),
        "notice": "",
        "note": "",
        "login_script": LOGIN_SCRIPT,
        "checked_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
    }
    if out["mode"] == "os-store":
        out["note"] = OS_STORE_NOTE
    if not backend.available():
        out["note"] = "claude 실행 파일을 찾지 못했습니다 (V2R_CLAUDE_EXE 확인)"
        out["notice"] = out["note"]
        return out

    status = auth_status(backend.exe)
    if status:
        out["auth_method"] = str(status.get("authMethod") or "")
        out["logged_in"] = bool(status.get("loggedIn"))
        if not out["logged_in"] and data_dir:
            # 풀렸다 → 백업으로 되돌리고 한 번 더 본다 (사람에게 알리는 건 그다음)
            if cli_credentials.restore(data_dir):
                again = auth_status(backend.exe)
                if again and again.get("loggedIn"):
                    out["logged_in"] = True
                    out["auth_method"] = str(again.get("authMethod") or "")
                    out["restored_from_backup"] = True
                    out["note"] = "로그인이 풀려 백업 자격증명으로 복원함"
        if not out["logged_in"]:
            out["note"] = out.get("note") or "claude auth status: loggedIn=false"
            out["notice"] = RELOGIN_NOTICE
            return out
        out["ok"] = True
        if data_dir:
            cli_credentials.backup(data_dir)
        if not deep:
            return out

    try:
        backend.complete(model, PING_SYSTEM, PING_USER, max_tokens=16)
    except PlanNotLoggedIn as exc:
        out["ok"] = False
        out["logged_in"] = False
        out["note"] = str(exc)
        out["notice"] = RELOGIN_NOTICE
        return out
    except PlanLimit as exc:
        # 한도에 걸렸다는 건 모델까지 닿았다는 뜻 — 로그인은 살아 있다
        out["ok"] = False
        out["logged_in"] = True
        out["limited"] = True
        out["note"] = str(exc)
        return out
    except Exception as exc:
        out["ok"] = False
        out["note"] = str(exc)
        out["notice"] = f"요금제 길 점검 실패: {exc}"
        return out

    out["ok"] = True
    out["logged_in"] = True
    return out


__all__ = [
    "CREDENTIAL_CANDIDATES",
    "LOGIN_SCRIPT",
    "OS_STORE_NOTE",
    "PING_SYSTEM",
    "PING_USER",
    "RELOGIN_NOTICE",
    "auth_status",
    "check_session",
    "credential_mode",
    "PlanError",
]
