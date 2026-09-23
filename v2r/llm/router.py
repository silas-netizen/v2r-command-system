"""용도별 모델 라우터 (DESIGN §7). 키가 없으면 해당 기능만 끄고 나머지는 동작한다."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .anthropic import LLMDisabled, create_message, make_client
from .plan_backend import PlanBackend, PlanError, PlanLimit, PlanNotLoggedIn
from .usage_ledger import TOKEN_FIELDS as LEDGER_TOKEN_FIELDS
from .usage_ledger import append_call

log = logging.getLogger(__name__)

#: 길(백엔드) 이름. 설계서 `quality-parity-strategy-2026-09-21.md` §길 표.
#: - `plan`  요금제(구독) — Claude Code 비대화 실행. 추가 비용 0원.
#: - `batch` 배치 API — 아직 안 만들었다. 자리만 잡아 두고 다음 길로 넘어간다.
#: - `api`   일반 API — 기존 길.
BACKENDS = ("plan", "batch", "api")

#: 설정이 없을 때 쓰는 차례
DEFAULT_BACKEND_ORDER = ("plan", "batch", "api")

#: `LLMRouter()`를 직접 만들 때의 차례 (예전 동작 그대로)
LEGACY_BACKEND_ORDER = ("api",)

#: 요금제 한도에 걸리면 이만큼 요금제 길을 쉰다 (설계서 §폴백)
PLAN_LOCK_HOURS = 5

#: 한도 문구를 한 번 봤을 때 **다시 해 보기까지** 기다리는 초 (2026-09-22).
#:
#: 2026-09-22 03:00:28에 "Claude usage limit reached" 한 줄로 5시간 잠금이
#: 걸렸는데, 15분 뒤 요금제 호출은 전부 정상이었다(v3 원고 6건 0원). 진짜
#: 한도였다면 15분 만에 풀릴 수 없다. 그래서 **1회 실패로는 잠그지 않는다** —
#: 30초 쉬고 한 번 더 해 보고, 그때도 한도면 그제야 잠근다.
PLAN_LIMIT_RETRY_SECONDS = 30

#: 한도 잠금을 적어 두는 파일 이름 (`data/` 아래)
PLAN_LOCK_NAME = "plan_lock.json"

#: 요금제 길의 임시 작업 폴더 (저장소 밖 취급 — CLAUDE.md·기억이 안 붙게)
PLAN_WORK_DIRNAME = "plan-work"

# 용도 -> 모델 ID
MODELS: dict[str, str] = {
    "ambiguous_command": "claude-haiku-4-5",
    "daily_comment": "claude-haiku-4-5",
    #: 감시견 Tier 1 — 규칙표에 없는 오류 문구 1건 분류 (하루 상한 있음)
    "error_diagnosis": "claude-haiku-4-5",
    "promo_comment": "claude-sonnet-5",
    "daily_adapt": "claude-sonnet-5",
    # 브랜드(제휴) 바이럴 원고. 지침의 모델 분리(본문/댓글)를 그대로 따른다.
    # 2026-09-19 품질 시험: 본문도 잠시 Sonnet으로 내린다(되돌리려면 claude-opus-5).
    "brand_body": "claude-sonnet-5",
    "brand_comments": "claude-sonnet-5",
    # 키워드-브랜드 연관도 재산정 (100개씩 묶음, 요금제 길 0원)
    "keyword_relevance": "claude-haiku-4-5",
    # 슬랙/텔레그램 자유 대화 명령 해석 (요금제 길 plan, 0원, 사용자 지시 2026-09-23)
    "freeform_command": "claude-sonnet-5",
}

DEFAULT_MODEL = "claude-haiku-4-5"

#: 생각(thinking)을 꺼야 하는 용도. 최신 모델은 기본이 adaptive라 글쓰기 용도에서
#: max_tokens를 전부 생각에 써 버리고 본문이 비어 돌아온다.
NO_THINKING_PURPOSES = frozenset({"brand_body", "brand_comments"})
THINKING_DISABLED = {"type": "disabled"}

#: 백만 토큰당 미국 달러 단가. **2026-09 기준 추정, 실제 요금표 확인 필요.**
#: cache_write는 입력의 1.25배, cache_read는 입력의 0.1배라는 공개 비율을 따랐다.
PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    # 2026-09 기준 추정, 실제 요금표 확인 필요
    "claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.10},
    "claude-opus-5": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
}

#: 표에 없는 모델에 쓰는 기본 단가 (Sonnet 기준)
DEFAULT_PRICE = PRICES_USD_PER_MTOK["claude-sonnet-5"]

__all__ = [
    "BACKENDS",
    "DEFAULT_BACKEND_ORDER",
    "PLAN_LOCK_HOURS",
    "MODELS",
    "LLMRouter",
    "LLMDisabled",
    "extract_json",
    "estimate_cost",
    "prompt_sha256",
    "PRICES_USD_PER_MTOK",
]


def prompt_sha256(system: str, user: str) -> str:
    """system+user 프롬프트의 지문.

    **길이 달라도 이 값이 같아야 한다**는 것이 품질 동일성의 증거다
    (설계서 §1 "프롬프트는 한 곳에서만 만든다"). 두 토막을 그냥 이어 붙이면
    경계가 흐려지므로 널 문자로 끊는다.
    """
    blob = f"{system or ''}\x00{user or ''}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _cost_one(usage: dict, model: str) -> float:
    price = PRICES_USD_PER_MTOK.get(model, DEFAULT_PRICE)
    return (
        int(usage.get("input_tokens", 0) or 0) * price["input"]
        + int(usage.get("output_tokens", 0) or 0) * price["output"]
        + int(usage.get("cache_creation_input_tokens", 0) or 0) * price["cache_write"]
        + int(usage.get("cache_read_input_tokens", 0) or 0) * price["cache_read"]
    ) / 1_000_000


def estimate_cost(usage: dict | None, model: str = "") -> float:
    """토큰 누계를 달러로 어림한다 (2026-09 기준 추정, 실제 요금표 확인 필요).

    `usage`에 모델별 누계(`by_model`)가 들어 있으면 모델마다 제 단가를 적용해
    더한다. 없으면 `model` 하나의 단가로 계산한다.
    """
    if not usage:
        return 0.0
    per_model = usage.get("by_model")
    if isinstance(per_model, dict) and per_model:
        return round(sum(_cost_one(u, name) for name, u in per_model.items()), 6)
    return round(_cost_one(usage, model), 6)


def normalize_backend_order(raw: Any) -> tuple[str, ...]:
    """설정에서 읽은 길 차례를 다듬는다. 모르는 이름은 버린다."""
    if isinstance(raw, str):
        items = [p.strip() for p in raw.replace(",", " ").split()]
    elif isinstance(raw, (list, tuple)):
        items = [str(p).strip() for p in raw]
    else:
        items = []
    out: list[str] = []
    for name in items:
        low = name.lower()
        if low in BACKENDS and low not in out:
            out.append(low)
    return tuple(out) or DEFAULT_BACKEND_ORDER


def load_backend_order(settings: Any | None = None) -> tuple[str, ...]:
    """`config/models.yaml`의 `backend_order` (없으면 기본 차례)."""
    try:
        from ..config import load_yaml

        data = load_yaml("models")
    except Exception:  # pragma: no cover - 설정을 못 읽어도 기본값으로 돈다
        data = {}
    del settings
    return normalize_backend_order((data or {}).get("backend_order"))


def _count_backend(usage_out: dict, backend: str) -> None:
    """길별 호출 수를 센다 (`api`는 토큰을 `by_model` 쪽에서 이미 센다)."""
    per = usage_out.setdefault("by_backend", {})
    slot = per.setdefault(backend, {"calls": 0})
    slot["calls"] = int(slot.get("calls", 0) or 0) + 1


def extract_json(text: str) -> dict | list:
    """텍스트에서 첫 JSON 객체/배열을 꺼낸다."""
    if not text:
        raise ValueError("모델 응답이 비어 있습니다")
    start = -1
    for idx, ch in enumerate(text):
        if ch in "{[":
            start = idx
            break
    if start < 0:
        raise ValueError("모델 응답에서 JSON을 찾지 못했습니다")

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start : idx + 1])
    raise ValueError("모델 응답의 JSON이 끝나지 않았습니다")


class LLMRouter:
    """용도 이름으로 모델을 골라 호출한다."""

    def __init__(
        self,
        api_key: str = "",
        client: Any | None = None,
        backend_order: Any = None,
        plan: Any = None,
        data_dir: str | Path = "",
    ) -> None:
        self.api_key = (api_key or "").strip()
        self._client = client
        #: 길 차례. 직접 만들 때는 예전처럼 API 길만 쓴다 (기존 동작 보존).
        self.backend_order: tuple[str, ...] = normalize_backend_order(
            backend_order if backend_order is not None else LEGACY_BACKEND_ORDER
        )
        self.data_dir = Path(data_dir) if data_dir else None
        self._plan = plan
        #: `V2R_LLM_BACKEND` / 명령의 `요금제로` 로 한 길만 쓰게 못박을 때
        self.force_backend: str = (os.environ.get("V2R_LLM_BACKEND", "") or "").strip()
        #: 이 프로세스에서 요금제 길을 포기했는가 (미로그인은 다시 해도 같다)
        self._plan_disabled = False
        self._plan_warned = False
        #: 한도 재시도까지 실패했을 때의 마지막 오류 (폴백 사유로 쓴다)
        self._last_limit_error: Exception | None = None
        #: 마지막 호출 기록 (길·모델·프롬프트 지문). 원고 JSON에 그대로 실린다.
        self.last_call: dict[str, Any] = {}
        self.enabled = bool(self._client) or bool(self.api_key) or self.plan_in_order()
        #: 이 라우터로 쓴 토큰 누계 (비용 보고용)
        self.usage: dict[str, Any] = {}

    # --- 설정 -----------------------------------------------------
    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "LLMRouter":
        """설정에서 키와 길 차례를 읽어 라우터를 만든다."""
        if settings is None:
            from ..config import get_settings

            settings = get_settings()
        return cls(
            api_key=getattr(settings, "anthropic_api_key", "") or "",
            backend_order=load_backend_order(settings),
            data_dir=getattr(settings, "data_dir", "") or "",
        )

    def plan_in_order(self) -> bool:
        """요금제 길이 차례에 들어 있는가."""
        return "plan" in self._effective_order()

    def _effective_order(self) -> tuple[str, ...]:
        forced = (self.force_backend or "").strip()
        if forced in BACKENDS:
            return (forced,)
        return self.backend_order

    def _data_dir(self) -> Path:
        if self.data_dir is not None:
            return self.data_dir
        from ..config import get_settings

        return Path(get_settings().data_dir)

    # --- 요금제 길 ------------------------------------------------
    def plan_backend(self) -> PlanBackend:
        """요금제 길 백엔드 (작업 폴더는 `data/plan-work/`)."""
        if self._plan is None:
            self._plan = PlanBackend(work_dir=self._data_dir() / PLAN_WORK_DIRNAME)
        return self._plan

    def plan_lock_path(self) -> Path:
        """한도 잠금 파일 (`data/plan_lock.json`)."""
        return self._data_dir() / PLAN_LOCK_NAME

    def plan_locked_until(self) -> datetime | None:
        """요금제 길이 언제까지 잠겨 있는가. 안 잠겼으면 None."""
        path = self.plan_lock_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        raw = (data or {}).get("until") if isinstance(data, dict) else None
        if not raw:
            return None
        try:
            until = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        return until if until > datetime.now(until.tzinfo) else None

    def lock_plan(
        self,
        reason: str = "",
        hours: int = PLAN_LOCK_HOURS,
        first_error: str = "",
    ) -> Path:
        """요금제 길을 `hours` 시간 잠근다 (한도에 걸렸을 때).

        `first_error` 는 30초 전 1차 시도의 오류 원문이다 (알림에 같이 싣는다).
        """
        until = datetime.now().astimezone() + timedelta(hours=hours)
        path = self.plan_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "until": until.isoformat(),
                    "reason": reason,
                    "first_error": first_error,
                    "locked_at": datetime.now().astimezone().isoformat(),
                    "hours": hours,
                    "pid": os.getpid(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        # 잠금 파일이 **어디에** 생겼는지 로그로 남긴다. 2026-09-22에 알림만
        # 남고 잠금 파일이 안 보이는 일이 있어, 경로를 확인할 수 있게 했다.
        log.warning(
            "요금제 한도 — %s시간 동안 요금제 길을 쉽니다 (%s) [잠금 파일: %s]",
            hours,
            reason,
            path,
        )
        self._write_lock_alert(until, reason, hours, first_error=first_error, lock_path=path)
        return path

    def _write_lock_alert(
        self,
        until: datetime,
        reason: str,
        hours: int,
        first_error: str = "",
        lock_path: Path | None = None,
    ) -> None:
        """요금제 잠금 전환을 `docs/reports/alerts/` 에 파일로 남긴다 (2026-09-22).

        잠기면 그다음 호출부터 **유료 API**로 넘어간다. 조용히 넘어가면 돈이 새므로
        사람이 바로 볼 수 있는 자리에 알림 파일을 만든다.
        """
        # 알림은 **잠금 파일이 있는 곳 기준**으로 적는다 (`data/` 의 윗 폴더).
        # 그래야 시험에서 임시 폴더를 쓰면 알림도 임시 폴더에 떨어져 저장소를
        # 더럽히지 않는다.
        root = self._data_dir().parent
        stamp = datetime.now().astimezone()
        folder = root / "docs" / "reports" / "alerts"
        path = folder / f"plan-lock-{stamp:%Y-%m-%d-%H%M}.md"
        text = (
            f"# 요금제 길 잠금 알림 ({stamp:%Y-%m-%d %H:%M} KST)\n\n"
            f"- 잠긴 시각: {stamp.isoformat(timespec='seconds')}\n"
            f"- 풀리는 시각: {until.isoformat(timespec='seconds')} ({hours}시간)\n"
            f"- 이유: {reason or '(알 수 없음)'}\n"
            f"- 잠금 파일: {lock_path or self.plan_lock_path()}\n"
            f"- 적은 프로세스: pid {os.getpid()}\n\n"
            "요금제(구독) 한도에 걸려 잠갔습니다. 이 동안의 모델 호출은"
            " **유료 API**로 넘어갑니다. 급하지 않은 묶음 생성은 잠금이 풀린 뒤로"
            " 미루는 것이 좋습니다.\n\n"
            "## claude 오류 원문\n\n"
            "1차 시도(30초 전):\n\n"
            f"```\n{first_error or '(1차 원문 없음 — 재시도 없이 잠금)'}\n```\n\n"
            "2차 시도(잠금을 부른 오류):\n\n"
            f"```\n{reason or '(원문 없음)'}\n```\n"
        )
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            log.warning("요금제 잠금 알림 파일 기록 실패: %s", exc)

    def estimated_cost(self) -> float:
        """이 라우터로 쓴 토큰의 어림 비용(USD)."""
        return estimate_cost(self.usage)

    def model_for(self, purpose: str) -> str:
        """용도에 해당하는 모델 ID."""
        return MODELS.get(purpose, DEFAULT_MODEL)

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = make_client(self.api_key)
        return self._client

    def complete(self, purpose: str, system: str, user: str, max_tokens: int = 1200) -> str:
        """길을 차례로 밟아 가며 모델을 부르고 텍스트를 돌려준다.

        **system/user 문자열은 어느 길로 가든 한 글자도 바뀌지 않는다.**
        길마다 다른 것은 "어떻게 부르는가"뿐이다 (설계서 §품질 동일성).

        폴백 규칙:
        - `PlanLimit` → `data/plan_lock.json`에 5시간 잠금을 적고 다음 길로
        - `PlanNotLoggedIn` → 경고를 **한 번만** 남기고, 이 프로세스에서는
          요금제 길을 더 시도하지 않는다 (다시 해도 결과가 같다)
        - `PlanError`(그 밖) → 그 호출만 다음 길로
        - `batch` → 아직 없다. 조용히 다음 길로.
        """
        model = self.model_for(purpose)
        fingerprint = prompt_sha256(system, user)
        order = self._effective_order()
        last_error: Exception | None = None
        for backend in order:
            if backend == "plan":
                if self._plan_disabled:
                    continue
                locked = self.plan_locked_until()
                if locked is not None:
                    log.debug("요금제 길 잠금 중 (%s까지) — 다음 길로", locked)
                    continue
                try:
                    text = self.plan_backend().complete(
                        model, system, user, max_tokens=max_tokens, usage_out=self.usage
                    )
                except PlanLimit as exc:
                    # 한 번 실패로는 잠그지 않는다 — 30초 뒤 딱 한 번 더 해 본다
                    retried = self._retry_after_limit(
                        model, system, user, max_tokens, exc
                    )
                    if retried is not None:
                        self._record(backend, purpose, model, fingerprint)
                        info = getattr(self.plan_backend(), "last_info", None) or {}
                        self._note_ledger(
                            backend, purpose, model, fingerprint, (info or {}).get("usage")
                        )
                        return retried
                    last_error = self._last_limit_error or exc
                    continue
                except PlanNotLoggedIn as exc:
                    self._plan_disabled = True
                    if not self._plan_warned:
                        self._plan_warned = True
                        log.warning(
                            "Claude Code에 로그인되어 있지 않아 요금제 길을 건너뜁니다"
                            " (scripts\\claude-cli-login.cmd 를 한 번 실행하세요): %s",
                            exc,
                        )
                    last_error = exc
                    continue
                except PlanError as exc:
                    log.warning("요금제 길 호출 실패 — 다음 길로: %s", exc)
                    last_error = exc
                    continue
                self._record(backend, purpose, model, fingerprint)
                info = getattr(self.plan_backend(), "last_info", None) or {}
                self._note_ledger(
                    backend, purpose, model, fingerprint, (info or {}).get("usage")
                )
                return text
            if backend == "batch":
                # 아직 만들지 않았다. 자리만 잡아 두고 다음 길로 넘어간다.
                last_error = last_error or NotImplementedError("배치 API 길은 아직 없습니다")
                continue
            if backend == "api":
                if not (self._client or self.api_key):
                    last_error = last_error or LLMDisabled(
                        "ANTHROPIC_API_KEY가 없어 모델 기능을 사용할 수 없습니다"
                    )
                    continue
                client = self._ensure_client()
                log.debug("LLM 호출 용도=%s 모델=%s 길=api", purpose, model)
                before_api = {
                    k: int(self.usage.get(k, 0) or 0) for k in LEDGER_TOKEN_FIELDS
                }
                text = create_message(
                    client,
                    model,
                    system,
                    user,
                    max_tokens=max_tokens,
                    usage_out=self.usage,
                    thinking=THINKING_DISABLED if purpose in NO_THINKING_PURPOSES else None,
                )
                _count_backend(self.usage, "api")
                self._record(backend, purpose, model, fingerprint)
                self._note_ledger(
                    backend,
                    purpose,
                    model,
                    fingerprint,
                    {
                        k: int(self.usage.get(k, 0) or 0) - before_api[k]
                        for k in LEDGER_TOKEN_FIELDS
                    },
                )
                return text
        if isinstance(last_error, LLMDisabled):
            raise last_error
        raise LLMDisabled(
            "모델을 부를 수 있는 길이 없습니다"
            + (f" (마지막 오류: {last_error})" if last_error else "")
        )

    def _retry_after_limit(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        first: Exception,
    ) -> str | None:
        """한도 문구를 봤을 때 30초 뒤 **한 번만** 다시 불러 본다.

        - 두 번째가 성공하면 본문을 돌려준다 (잠그지 않는다 — 오탐이었다).
        - 두 번째도 한도면 그제야 `lock_plan` 으로 5시간 잠근다.
        - 두 번째가 다른 오류면 잠그지 않고 그 호출만 다음 길로 넘긴다.

        `data/plan_lock.json` 은 `data/` 안에 있으므로 실행기·사이드카·일꾼
        스크립트가 **같은 파일**을 본다. 잠금은 이 한 자리에서만 쓴다.
        """
        import time

        self._last_limit_error = None
        log.warning(
            "요금제 한도 문구 1회 — %s초 뒤 한 번 더 해 봅니다 (아직 안 잠금): %s",
            PLAN_LIMIT_RETRY_SECONDS,
            first,
        )
        time.sleep(PLAN_LIMIT_RETRY_SECONDS)
        try:
            return self.plan_backend().complete(
                model, system, user, max_tokens=max_tokens, usage_out=self.usage
            )
        except PlanLimit as second:
            self.lock_plan(str(second), first_error=str(first))
            self._last_limit_error = second
            return None
        except PlanError as second:
            log.warning("요금제 길 재시도도 실패 — 잠그지 않고 다음 길로: %s", second)
            self._last_limit_error = second
            return None

    def _note_ledger(
        self,
        backend: str,
        purpose: str,
        model: str,
        fingerprint: str,
        counts: Any,
    ) -> None:
        """사용량 장부(`data/llm_usage-YYYY-MM.jsonl`)에 이 호출을 한 줄 적는다.

        장부는 덤이다 — 어떤 이유로든 실패해도 호출 결과를 버리지 않는다.
        """
        if not isinstance(counts, dict):
            return
        try:
            append_call(
                self._data_dir(),
                backend,
                purpose,
                model,
                counts,
                prompt_sha256=fingerprint,
            )
        except Exception as exc:  # noqa: BLE001 - 장부 때문에 호출이 죽으면 안 된다
            log.warning("사용량 장부 기록 실패: %s", exc)

    def _record(self, backend: str, purpose: str, model: str, fingerprint: str) -> None:
        """마지막 호출을 적어 둔다 (원고 JSON·검토 MD가 이걸 읽는다)."""
        self.last_call = {
            "backend": backend,
            "purpose": purpose,
            "model": model,
            "prompt_sha256": fingerprint,
            # 요금제 길은 구독 안이라 추가 비용이 0원이다
            "cost_usd": 0.0 if backend != "api" else None,
        }

    def complete_json(
        self, purpose: str, system: str, user: str, max_tokens: int = 1200
    ) -> dict | list:
        """모델 응답에서 첫 JSON 객체/배열을 꺼내 반환."""
        return extract_json(self.complete(purpose, system, user, max_tokens=max_tokens))
