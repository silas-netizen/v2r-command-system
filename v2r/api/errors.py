"""V2R API 오류 표현과 분류 (api-spec §7)."""

from __future__ import annotations

import json
from typing import Any


class V2RApiError(Exception):
    """V2R API 호출 실패. 응답 본문의 `error={code,reason,extra}`를 보존한다."""

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = None,
        code: str | None = None,
        reason: str | None = None,
        extra: dict | None = None,
        body: str = "",
        kind: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message or reason or code or f"HTTP {status}")
        self.status = status
        self.code = code
        self.reason = reason
        self.extra = extra
        self.body = body or ""
        self.kind = kind
        self.retry_after = retry_after

    def __repr__(self) -> str:  # pragma: no cover - 디버깅용
        return (
            f"V2RApiError(status={self.status!r}, code={self.code!r}, "
            f"kind={self.kind!r})"
        )

    @classmethod
    def from_response(cls, response: Any, message: str = "") -> "V2RApiError":
        """httpx.Response에서 오류 객체 생성."""
        body = _response_text(response)
        code, reason, extra = parse_error_body(body)
        err = cls(
            message,
            status=getattr(response, "status_code", None),
            code=code,
            reason=reason,
            extra=extra,
            body=body,
        )
        err.kind = classify(err)
        err.retry_after = _retry_after(response, extra)
        return err


def _response_text(response: Any) -> str:
    try:
        return response.text or ""
    except Exception:  # pragma: no cover - 방어
        return ""


def parse_error_body(body: str) -> tuple[str | None, str | None, dict | None]:
    """본문 JSON의 `error` 블록을 (code, reason, extra)로 파싱."""
    if not body:
        return None, None, None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None, None, None
    if not isinstance(data, dict):
        return None, None, None
    error = data.get("error")
    if not isinstance(error, dict):
        # 최상위에 code/reason만 있는 형태도 허용
        error = data
    code = error.get("code")
    reason = error.get("reason") or error.get("message")
    extra = error.get("extra")
    if code is not None and not isinstance(code, str):
        code = str(code)
    if reason is not None and not isinstance(reason, str):
        reason = str(reason)
    if not isinstance(extra, dict):
        extra = None
    return code, reason, extra


def _retry_after(response: Any, extra: dict | None) -> float | None:
    """`Retry-After` 헤더 또는 `error.extra.retry_after`(초)."""
    headers = getattr(response, "headers", None)
    raw = None
    if headers is not None:
        try:
            raw = headers.get("Retry-After")
        except Exception:  # pragma: no cover - 방어
            raw = None
    if raw is None and isinstance(extra, dict):
        raw = extra.get("retry_after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def classify(exc_or_response: Any) -> str:
    """오류를 표준 종류 문자열로 분류한다."""
    if isinstance(exc_or_response, V2RApiError):
        status = exc_or_response.status
        code = exc_or_response.code or ""
        reason = exc_or_response.reason or ""
        extra = exc_or_response.extra
        body = exc_or_response.body or ""
    else:
        response = exc_or_response
        status = getattr(response, "status_code", None)
        body = _response_text(response)
        code, reason, extra = parse_error_body(body)
        code = code or ""
        reason = reason or ""

    haystack = " ".join(str(x) for x in (code, reason, body) if x)

    if status == 403 and "TOKEN_ERROR" in haystack:
        return "token_expired"
    if status == 429:
        return "rate_limited"
    if status is not None and 500 <= int(status) < 600:
        return "server"
    if "27000" in (code or "") or "27000" in haystack:
        return "account_restricted"
    if "20004" in (code or "") or "20004" in haystack or "연속으로 등록" in haystack:
        return "consecutive_limit"
    if "33007" in (code or "") or "33007" in haystack:
        return "grade"
    if "NOT_FOUND_MODEL" in haystack and "NaverJoinCafeAccoun" in haystack:
        return "no_membership"
    if "DELETED_NAVER_CAFE_ARTICLE_SOURCE" in haystack:
        return "deleted"
    if "NOT_LOGIN" in haystack or "session not found" in haystack.lower():
        return "not_login"
    del extra
    return "other"
