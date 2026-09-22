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


#: FastAPI `{"detail": "..."}` 본문에서 코드로 승격하는 토큰들.
#: (실측: `detail` 형태 응답에는 `code`/`reason` 블록이 아예 없다 — live-catalog.md §0)
DETAIL_CODE_TOKENS = (
    "TOKEN_ERROR",
    "NOT_LOGIN",
    "NOT_FOUND_MODEL",
    "DELETED_NAVER_CAFE_ARTICLE_SOURCE",
    "UNAUTHORIZED",
    "FORBIDDEN",
)


def _from_detail(data: dict) -> tuple[str | None, str | None, dict | None] | None:
    """FastAPI 기본 오류 본문(`{"detail": ...}`)을 (code, reason, extra)로."""
    if "detail" not in data:
        return None
    detail = data.get("detail")
    if isinstance(detail, dict):
        code = detail.get("code")
        reason = detail.get("reason") or detail.get("message") or detail.get("detail")
        extra = detail.get("extra")
        text = " ".join(str(x) for x in (code, reason) if x)
    elif isinstance(detail, list):
        # 검증 오류 배열 — 통째로 문자열화해 reason에 담는다
        code, extra = None, None
        reason = text = json.dumps(detail, ensure_ascii=False)
    else:
        code, extra = None, None
        reason = text = str(detail or "")
    if code is None:
        upper = text.upper()
        code = next((t for t in DETAIL_CODE_TOKENS if t in upper), None)
    if code is not None and not isinstance(code, str):
        code = str(code)
    if reason is not None and not isinstance(reason, str):
        reason = str(reason)
    if not isinstance(extra, dict):
        extra = None
    return code, (reason or None), extra


def parse_error_body(body: str) -> tuple[str | None, str | None, dict | None]:
    """본문 JSON의 `error` 블록을 (code, reason, extra)로 파싱.

    `error` 블록이 없는 FastAPI 기본 형태(`{"detail": "..."}`)도 처리해
    `reason`을 채우고, 본문에 `TOKEN_ERROR`/`NOT_LOGIN` 같은 토큰이 있으면
    `code`로 끌어올린다(그래야 `classify()`가 계속 동작한다).
    """
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
        parsed = _from_detail(data)
        if parsed is not None:
            return parsed
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


#: 네이버가 "하루치 글을 너무 많이 썼다"고 막을 때 돌려주는 문구 조각들.
#: 실측(2026-09-21 18:36~18:38, peecics): V2R 글 목록의 경고 아이콘 문구가
#: "ID/IP당 게시글 등록 제한을 초과해 신규 게시글 등록이 잠시 제한됩니다"였다.
POST_LIMIT_PHRASES = (
    "게시글 등록 제한",
    "등록 제한을 초과",
    "신규 게시글 등록이",
    "id/ip당",
    "글쓰기가 제한",
)


def is_post_limit(text: Any) -> bool:
    """이 문구가 "하루 게시글 등록 제한"에 걸린 것인가.

    이 상태는 **글이 올라가지 않았다**는 뜻이다. 계정을 바꿔 다시 올려야 하며
    완료로 확정해서는 안 된다 (장애 2026-09-21 #제한).
    """
    low = str(text or "").casefold()
    if not low:
        return False
    return any(p in low for p in POST_LIMIT_PHRASES)


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
    # 숫자 코드는 본문 전체에서 찾으면 조회수·ID 같은 숫자에 오탐한다 → code/reason만
    code_field = " ".join(str(x) for x in (code, reason) if x)

    if status == 403 and "TOKEN_ERROR" in haystack:
        return "token_expired"
    if status == 429:
        return "rate_limited"
    if status is not None and 500 <= int(status) < 600:
        return "server"
    if "27000" in code_field:
        return "account_restricted"
    if is_post_limit(haystack):
        return "post_limit"
    if "20004" in code_field or "연속으로 등록" in haystack:
        return "consecutive_limit"
    if "33007" in code_field:
        return "grade"
    if "NOT_FOUND_MODEL" in haystack and "NaverJoinCafeAccoun" in haystack:
        return "no_membership"
    if "DELETED_NAVER_CAFE_ARTICLE_SOURCE" in haystack:
        return "deleted"
    if "NOT_LOGIN" in haystack or "session not found" in haystack.lower():
        return "not_login"
    del extra
    return "other"
