"""요금제(구독) 길 — Claude Code CLI를 비대화로 한 번 실행해 모델을 부른다.

API 키로 부르는 길(`anthropic.py`)과 **똑같은 system/user 문자열**을 그대로
넘기는 것이 이 파일의 유일한 약속이다. 프롬프트를 손대지 않는다.

왜 이렇게 부르는가
------------------
- `-p/--print` + `--output-format json`: 비대화 1회 실행, 결과를 JSON 한 덩어리로.
- 사용자 프롬프트는 **stdin**으로 넣는다. 명령줄에 실으면 윈도우의 길이 제한
  (약 32KB)에 걸리고 한글이 깨진다. `--input-format text`가 stdin을 읽는다.
- system 프롬프트도 임시 **파일**(`--system-prompt-file`)로 준다. 같은 이유다.
- `--system-prompt-file`은 기본 시스템 프롬프트(25k 토큰짜리)를 **갈아치운다**.
  우리 지침만 들어가므로 API 길과 입력이 같아진다 (실측: 캐시 입력 686토큰).
- `--setting-sources ""` + `--strict-mcp-config` + `--disable-slash-commands`:
  설정 파일·MCP·스킬을 하나도 읽지 않는다.
- **`--bare`는 절대 쓰지 않는다.** `--bare`는 인증을 ANTHROPIC_API_KEY로
  못박아 구독(OAuth) 로그인을 아예 안 읽는다 (실측: "Not logged in"). 그러면
  요금제 길이 아니라 돈 나가는 API 길이 되어 버린다.
- `--disallowedTools "*"` + `--max-turns 1`: 도구 없이 한 번만 대답하게 한다.
- 작업 폴더는 저장소 밖(`data/plan-work/`)으로 둬서 프로젝트 설정이 안 붙게 한다.

비용은 요금제 안이라 **0원**으로 집계한다. CLI가 돌려주는 `usage`·
`total_cost_usd`는 참고용으로만 기록한다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 한 번 호출에 기다리는 최대 초
DEFAULT_TIMEOUT = 240

#: 실행 파일을 찾는 곳 (윈도우 설치 경로)
WINDOWS_INSTALL_GLOB = (
    r"Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code"
)

#: 요금제 한도/속도 제한으로 읽는 문구
LIMIT_MARKERS = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "limit reached",
    "limit",
    "rate",
    "try again",
    "too many requests",
    "overloaded",
    "한도",
)

#: 로그인이 안 된 상태로 읽는 문구
LOGIN_MARKERS = (
    "not logged in",
    "please run /login",
    "please log in",
    "invalid api key",
    "authentication",
    "로그인",
)


class PlanError(RuntimeError):
    """요금제 길 호출이 실패했다 (원인 불명)."""


class PlanNotLoggedIn(PlanError):
    """Claude Code가 로그인되어 있지 않다. 다시 시도해도 소용없다."""


class PlanLimit(PlanError):
    """요금제 사용 한도/속도 제한에 걸렸다. 잠시 쉬었다 와야 한다."""


def _version_key(name: str) -> tuple:
    parts = []
    for chunk in name.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def find_claude_exe(explicit: str = "") -> str:
    """`claude` 실행 파일 경로. 못 찾으면 빈 문자열.

    찾는 차례: 직접 지정 → 환경변수 `V2R_CLAUDE_EXE` → 윈도우 설치 폴더의
    **가장 높은 버전** → PATH의 `claude`.
    """
    for candidate in (explicit, os.environ.get("V2R_CLAUDE_EXE", "")):
        candidate = (candidate or "").strip()
        if candidate and Path(candidate).exists():
            return str(Path(candidate))

    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        base = Path(local) / WINDOWS_INSTALL_GLOB
        if base.is_dir():
            versions = sorted(
                (d for d in base.iterdir() if d.is_dir()),
                key=lambda d: _version_key(d.name),
            )
            for folder in reversed(versions):
                exe = folder / "claude.exe"
                if exe.exists():
                    return str(exe)

    found = shutil.which("claude")
    return found or ""


def _classify(text: str) -> PlanError:
    """CLI가 돌려준 오류 문구를 우리 예외로 바꾼다."""
    low = (text or "").lower()
    # 로그인 먼저 본다 — "Not logged in"에도 "log"가 들어 있어 순서가 중요하다
    if any(m in low for m in LOGIN_MARKERS):
        return PlanNotLoggedIn(text.strip() or "Claude Code에 로그인되어 있지 않습니다")
    if any(m in low for m in LIMIT_MARKERS):
        return PlanLimit(text.strip() or "요금제 사용 한도에 걸렸습니다")
    return PlanError(text.strip() or "요금제 길 호출이 실패했습니다")


def parse_result(raw: str) -> tuple[str, dict]:
    """CLI의 `--output-format json` 출력에서 본문과 사용량을 꺼낸다.

    돌려주는 값은 `(본문, 정보)`. `정보`에는 `usage`, `total_cost_usd`,
    `model`, `session_id`가 들어간다. 오류면 분류된 예외를 던진다.
    """
    text = (raw or "").strip()
    if not text:
        raise PlanError("요금제 길 응답이 비어 있습니다")
    # stream-json이 섞여 들어와도 마지막 줄의 JSON 객체를 쓴다
    payload: Any = None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise _classify(text) from exc
    if not isinstance(payload, dict):
        raise PlanError("요금제 길 응답이 JSON 객체가 아닙니다")

    body = payload.get("result")
    if not isinstance(body, str):
        body = ""
    info = {
        "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
        # 요금제 안이라 실제로 우리가 더 내는 돈은 없다. 참고용으로만 남긴다.
        "reported_cost_usd": float(payload.get("total_cost_usd") or 0.0),
        "cost_usd": 0.0,
        "model": payload.get("model") or "",
        "session_id": payload.get("session_id") or "",
        "num_turns": payload.get("num_turns") or 0,
    }
    if payload.get("is_error") or payload.get("terminal_reason") == "api_error":
        raise _classify(body or payload.get("api_error_status") or "")
    if not body.strip():
        raise PlanError("요금제 길 응답의 본문이 비어 있습니다")
    return body, info


class PlanBackend:
    """Claude Code CLI 한 번 실행 = 모델 호출 한 번."""

    name = "plan"

    def __init__(
        self,
        exe: str = "",
        work_dir: str | Path | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        runner: Any | None = None,
    ) -> None:
        self.exe = find_claude_exe(exe)
        self.work_dir = Path(work_dir) if work_dir else None
        self.timeout = int(timeout or DEFAULT_TIMEOUT)
        #: 테스트에서 갈아끼우는 실행기. `(argv, stdin_bytes, cwd, env) -> (rc, out, err)`
        self.runner = runner

    def available(self) -> bool:
        """실행 파일이 있는가."""
        return bool(self.exe)

    def _cwd(self) -> Path:
        target = self.work_dir or Path(tempfile.gettempdir()) / "v2r-plan-work"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def build_argv(self, model: str, system_file: str) -> list[str]:
        """실행 명령줄. 사용자 프롬프트는 여기 없다 (stdin으로 간다)."""
        return [
            self.exe,
            "-p",
            "--model",
            model,
            "--system-prompt-file",
            system_file,
            "--output-format",
            "json",
            "--input-format",
            "text",
            "--max-turns",
            "1",
            "--no-session-persistence",
            # 설정 파일(user/project/local)을 하나도 읽지 않는다
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--permission-prompts",
            "none",
            # 도구를 전부 막는다 — 순수 모델 호출처럼 만든다
            "--disallowedTools",
            "*",
        ]

    def _env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        # 요금제(구독) 인증으로 붙어야 한다. 키가 있으면 CLI가 돈 나가는
        # API 길로 붙어 버리므로 이 실행에서만 지운다.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(key, None)
        return env

    def _run(self, argv: list[str], stdin: bytes, cwd: Path, env: dict):
        if self.runner is not None:
            return self.runner(argv, stdin, str(cwd), env)
        proc = subprocess.run(  # noqa: S603 - 경로는 우리가 정한 실행 파일뿐
            argv,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            timeout=self.timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def complete(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 1200,
        usage_out: dict | None = None,
    ) -> str:
        """system/user를 **그대로** 넘기고 본문 문자열을 돌려준다.

        `max_tokens`는 CLI가 받지 않는다 (하네스가 정한다). 프롬프트를 바꿔
        흉내 내지 않는다 — 품질 동일성이 깨지기 때문이다.
        """
        if not self.exe:
            raise PlanError("claude 실행 파일을 찾지 못했습니다 (V2R_CLAUDE_EXE 확인)")
        cwd = self._cwd()
        handle, sys_path = tempfile.mkstemp(
            prefix="v2r-sys-", suffix=".txt", dir=str(cwd)
        )
        os.close(handle)
        Path(sys_path).write_text(system or "", encoding="utf-8")
        try:
            argv = self.build_argv(model, sys_path)
            try:
                code, out, err = self._run(
                    argv, (user or "").encode("utf-8"), cwd, self._env()
                )
            except subprocess.TimeoutExpired as exc:
                raise PlanLimit(f"요금제 길 응답이 {self.timeout}초를 넘겼습니다") from exc
            except OSError as exc:
                raise PlanError(f"claude 실행에 실패했습니다: {exc}") from exc
            text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
            errtext = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err or "")
            if not text.strip() and (code or errtext):
                raise _classify(errtext or f"종료 코드 {code}")
            body, info = parse_result(text)
        finally:
            try:
                os.unlink(sys_path)
            except OSError:  # pragma: no cover - 지워지지 않아도 치명적이지 않다
                pass
        if usage_out is not None:
            record_usage(usage_out, model, info.get("usage") or {})
        self.last_info = info
        return body.strip()


def record_usage(usage_out: dict, model: str, usage: dict) -> None:
    """요금제 길 사용량을 **비용 0**으로 집계한다.

    일부러 `by_model`(단가표를 태우는 자리)과 최상위 토큰 누계에는 넣지 않는다.
    그쪽에 넣으면 `estimate_cost`가 구독으로 이미 낸 몫에 또 값을 매긴다.
    """
    per = usage_out.setdefault("by_backend", {})
    slot = per.setdefault(
        "plan",
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "calls": 0,
            "cost_usd": 0.0,
        },
    )
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        slot[field] = int(slot.get(field, 0) or 0) + int(usage.get(field, 0) or 0)
    slot["calls"] = int(slot.get("calls", 0) or 0) + 1
    slot["cost_usd"] = 0.0
    slot.setdefault("model", model)


__all__ = [
    "DEFAULT_TIMEOUT",
    "PlanBackend",
    "PlanError",
    "PlanLimit",
    "PlanNotLoggedIn",
    "find_claude_exe",
    "parse_result",
    "record_usage",
]
