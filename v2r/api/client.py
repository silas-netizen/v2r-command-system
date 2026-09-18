"""V2R HTTP 클라이언트 — 전역 레이트 게이트·GET 캐시·재시도 (api-spec §6)."""

from __future__ import annotations

import copy
import threading
import time
from typing import Any, Iterator

import httpx

from .auth import AuthSession, api_base
from .errors import V2RApiError, classify

RATE_INTERVAL = 0.25
CACHE_TTL = 300.0
MAX_RETRY_WAIT = 900.0
RETRY_WAITS = (10.0, 30.0)
MAX_ATTEMPTS = 3

CACHED_PATHS = {
    "/navers/accounts",
    "/naver_cafes/naver_join_cafes",
    "/naver_cafes/menus",
    "/naver_cafes/heads",
}

_rate_lock = threading.Lock()
_last_call_at = 0.0

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def _rate_gate() -> None:
    """프로세스 전역 0.25초 간격 게이트."""
    global _last_call_at
    with _rate_lock:
        now = time.monotonic()
        wait = RATE_INTERVAL - (now - _last_call_at)
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_call_at = now


def clear_cache() -> None:
    """GET 캐시 비우기 (테스트·재동기화용)."""
    with _cache_lock:
        _cache.clear()


def walk_dicts(obj: Any) -> Iterator[dict]:
    """중첩 구조의 모든 dict를 재귀적으로 내보낸다."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_dicts(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from walk_dicts(value)


def field(d: dict, *names: str, default: Any = None) -> Any:
    """snake_case/camelCase 중 먼저 존재하는 키의 값."""
    for name in names:
        if isinstance(d, dict) and name in d and d[name] is not None:
            return d[name]
    return default


class V2RClient:
    """인증 토큰을 붙여 V2R API를 호출하는 동기 클라이언트."""

    def __init__(
        self,
        base_url: str | None = None,
        auth: AuthSession | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or api_base()).rstrip("/")
        self.auth = auth if auth is not None else AuthSession()
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=60.0,
            headers={"Accept": "application/json"},
        )

    # ---- 컨텍스트 ----
    def __enter__(self) -> "V2RClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """내부 httpx 클라이언트 종료."""
        self._client.close()

    # ---- 캐시 ----
    def _cache_key(self, path: str, params: dict | None) -> str | None:
        if path not in CACHED_PATHS:
            return None
        return str(self._client.build_request("GET", path, params=params).url)

    # ---- 요청 ----
    def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json: Any = None,
        retry_auth: bool = True,
        idempotent: bool | None = None,
        **kwargs: Any,
    ) -> dict:
        """API 호출 후 JSON dict 반환.

        `idempotent`가 참이면 429/5xx를 최대 3회까지 재시도한다. 기본값은
        GET이면 True, 그 외(POST/PUT 등)는 False다. 비멱등 요청은 **한 번만**
        보내고, 5xx/타임아웃처럼 서버 처리 여부를 알 수 없는 응답은
        `kind="ambiguous"`인 `V2RApiError`로 올려 호출자가 이력 조회 등으로
        복구할 수 있게 한다(중복 발행 방지, api-spec §4).
        토큰 만료(403 TOKEN_ERROR)는 서버가 요청을 처리하지 않은 것이므로
        비멱등 요청도 1회 재로그인 후 재전송한다.
        """
        method = method.upper()
        if idempotent is None:
            idempotent = method == "GET"
        cache_key = self._cache_key(path, params) if method == "GET" else None
        if cache_key:
            with _cache_lock:
                hit = _cache.get(cache_key)
                if hit and (time.monotonic() - hit[0]) < CACHE_TTL:
                    return copy.deepcopy(hit[1])

        extra_headers = kwargs.pop("headers", None)
        attempt = 0
        relogin_used = not retry_auth
        while True:
            attempt += 1
            token = self.auth.ensure_token(self._client)
            headers = {"Authorization": f"Bearer {token}"}
            if method != "GET":
                headers.update(self.auth.device.signal_headers())
            if extra_headers:
                headers.update(extra_headers)

            _rate_gate()
            response = self._client.request(
                method, path, params=params, json=json, headers=headers, **kwargs
            )
            if response.status_code < 400:
                data = _as_dict(response)
                if cache_key:
                    with _cache_lock:
                        _cache[cache_key] = (time.monotonic(), copy.deepcopy(data))
                return data

            err = V2RApiError.from_response(response, f"{method} {path} 실패")
            kind = err.kind or classify(err)

            if kind == "token_expired" and not relogin_used:
                relogin_used = True
                self.auth.invalidate()
                attempt -= 1  # 재로그인은 재시도 횟수에서 제외
                continue

            if kind in {"rate_limited", "server"}:
                if idempotent and attempt < MAX_ATTEMPTS:
                    time.sleep(self._retry_wait(err, attempt))
                    continue
                if not idempotent and kind == "server":
                    # 서버가 이미 처리했을 수 있다 → 재전송 금지, 복구는 호출자 몫
                    raise V2RApiError(
                        f"{method} {path} 응답 불확실(HTTP {err.status}) — 재시도하지 않음",
                        status=err.status,
                        code=err.code,
                        reason=err.reason,
                        extra=err.extra,
                        body=err.body,
                        kind="ambiguous",
                        retry_after=err.retry_after,
                    ) from err

            err.kind = kind
            raise err

    @staticmethod
    def _retry_wait(err: V2RApiError, attempt: int) -> float:
        """`Retry-After` 우선(최대 900초), 없으면 (10, 30)초."""
        if err.retry_after is not None:
            return min(float(err.retry_after), MAX_RETRY_WAIT)
        idx = min(attempt - 1, len(RETRY_WAITS) - 1)
        return RETRY_WAITS[idx]

    def get(self, path: str, params: dict | None = None, **kwargs: Any) -> dict:
        """GET 호출."""
        return self.request("GET", path, params=params, **kwargs)

    def post(self, path: str, json: Any = None, **kwargs: Any) -> dict:
        """POST 호출."""
        return self.request("POST", path, json=json, **kwargs)

    def put(self, path: str, json: Any = None, **kwargs: Any) -> dict:
        """PUT 호출."""
        return self.request("PUT", path, json=json, **kwargs)


def _as_dict(response: httpx.Response) -> dict:
    """응답 본문을 dict로. 리스트/빈 본문도 dict로 감싼다."""
    if not response.content:
        return {}
    try:
        data = response.json()
    except ValueError:
        return {"raw": response.text}
    if isinstance(data, dict):
        return data
    return {"data": data}


__all__ = ["V2RClient", "clear_cache", "field", "walk_dicts", "CACHED_PATHS"]
