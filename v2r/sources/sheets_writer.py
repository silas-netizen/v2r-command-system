"""시트 쓰기 인터페이스 (설계: keyword-program-plan-2026-09-22.md C절).

지금은 구글 OAuth 동의를 받지 않았으므로(자격증명 없음) `update_rows`/`set_cell`
은 CSV(`data/exposure/<브랜드>.csv`, `keyword_exposure.write_exposure_csv`가
이미 담당)만 갱신하고 조용히 끝난다. OAuth 토큰(`data/google-oauth/token.json`)
이 생기면 `gspread`/`google-api-python-client`로 실제 시트에 쓴다.

자격증명 없이도 이 모듈을 그대로 테스트할 수 있어야 한다 — 그래서 실제 구글
호출은 `_client()`가 토큰이 있을 때만 만들고, 없으면 `None`을 돌려주고 호출
쪽은 "CSV만 갱신했다"는 결과를 받는다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: OAuth 토큰 파일 위치(있으면 실제 시트 쓰기를 시도한다)
DEFAULT_TOKEN_PATH = "data/google-oauth/token.json"


class SheetsWriteError(RuntimeError):
    """시트 쓰기 실패(자격증명 문제 등)."""


def has_credentials(repo_root: str | Path, token_path: str = DEFAULT_TOKEN_PATH) -> bool:
    """구글 OAuth 토큰이 있는가 — 있어야만 실제 쓰기를 시도한다."""
    return (Path(repo_root) / token_path).exists()


def _client(repo_root: str | Path, token_path: str = DEFAULT_TOKEN_PATH) -> Any:
    """자격증명이 있으면 gspread 클라이언트를, 없으면 `None`을 돌려준다.

    `gspread`가 설치되어 있지 않거나 토큰이 깨졌으면 예외 대신 `None`(CSV만
    쓰는 경로로 조용히 떨어진다) — 시트 쓰기는 어디까지나 부가 기능이다.
    """
    if not has_credentials(repo_root, token_path):
        return None
    try:
        import gspread  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
    except Exception as exc:  # pragma: no cover - 선택 의존성
        log.warning("gspread/google-api-python-client 없음 — CSV만 갱신합니다: %s", exc)
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(Path(repo_root) / token_path))
        return gspread.authorize(creds)
    except Exception as exc:  # pragma: no cover - 토큰 문제
        log.warning("구글 인증 실패 — CSV만 갱신합니다: %s", exc)
        return None


def update_rows(
    spreadsheet_id: str,
    sheet: str,
    rows: list[dict[str, Any]],
    *,
    key_column: str = "키워드",
    repo_root: str | Path = ".",
    token_path: str = DEFAULT_TOKEN_PATH,
) -> dict[str, Any]:
    """`rows`(딕셔너리 목록, 헤더명 그대로)를 `sheet` 탭에 `key_column` 기준으로
    있으면 갱신·없으면 추가한다.

    자격증명이 없으면 아무것도 안 하고 `{"written": 0, "mode": "csv_only"}`를
    돌려준다 — CSV(`data/exposure/<브랜드>.csv`)는 별도로 이미 갱신되어 있다는
    전제.
    """
    client = _client(repo_root, token_path)
    if client is None:
        return {"written": 0, "mode": "csv_only"}
    try:
        ws = client.open_by_key(spreadsheet_id).worksheet(sheet)
        header = ws.row_values(1)
        existing = ws.get_all_records()
        key_idx = {str(r.get(key_column, "")): i + 2 for i, r in enumerate(existing)}
        written = 0
        for row in rows:
            key = str(row.get(key_column, ""))
            values = [row.get(h, "") for h in header]
            if key in key_idx:
                ws.update(f"A{key_idx[key]}", [values])
            else:
                ws.append_row(values)
            written += 1
        return {"written": written, "mode": "sheets"}
    except Exception as exc:  # pragma: no cover - 네트워크/권한 문제
        log.warning("시트 쓰기 실패 — CSV만 남습니다: %s", exc)
        return {"written": 0, "mode": "csv_only", "error": str(exc)}


def set_cell(
    spreadsheet_id: str,
    sheet: str,
    cell: str,
    value: Any,
    *,
    repo_root: str | Path = ".",
    token_path: str = DEFAULT_TOKEN_PATH,
) -> dict[str, Any]:
    """셀 하나(P1/Q1 합계 등)를 쓴다. 자격증명 없으면 아무 것도 안 한다."""
    client = _client(repo_root, token_path)
    if client is None:
        return {"written": False, "mode": "csv_only"}
    try:
        ws = client.open_by_key(spreadsheet_id).worksheet(sheet)
        ws.update(cell, [[value]])
        return {"written": True, "mode": "sheets"}
    except Exception as exc:  # pragma: no cover
        log.warning("셀 쓰기 실패(%s): %s", cell, exc)
        return {"written": False, "mode": "csv_only", "error": str(exc)}


__all__ = [
    "SheetsWriteError",
    "DEFAULT_TOKEN_PATH",
    "has_credentials",
    "update_rows",
    "set_cell",
]
