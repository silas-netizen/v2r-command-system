"""구글 시트 쓰기 Apps Script 웹앱 API 클라이언트 (2026-09-25).

`config/sheets_api.yaml`(url, enabled, timeout_sec, retries)을 읽어 POST JSON
으로 시트를 갱신한다. 코드: docs/appsscript/v2r_sheet_api.gs. 허용 작업은
"append" / "update_by_key" / "delete_by_key" / "snapshot" 네 가지뿐이고,
B~F열(비밀번호 포함)은 Apps Script 쪽에서 아예 쓰지 않는다(이 클라이언트는
그 열을 요청 본문에 절대 담지 않는다).

브라우저(Playwright) 경로와 달리 자격증명·로그인이 필요 없다 — 배포된 웹앱이
사용자 계정 소유로 이미 실행 권한을 가진다.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config/sheets_api.yaml"

_ALLOWED_ACTIONS = {
    "append", "update_by_key", "delete_by_key", "snapshot",
    # 2026-09-25 추가(docs/appsscript/v2r_sheet_api.gs 최종 배포판)
    "set_header", "reapply_format", "set_cells", "delete_blank_rows", "dedupe_by_key", "info", "apply_colors",
}


class SheetsApiError(RuntimeError):
    """시트 API 호출 실패(오류 응답·네트워크 오류·재시도 소진 포함)."""


class SheetsApiConfig:
    __slots__ = ("enabled", "url", "timeout_sec", "retries")

    def __init__(self, *, enabled: bool, url: str, timeout_sec: float, retries: int) -> None:
        self.enabled = enabled
        self.url = url
        self.timeout_sec = timeout_sec
        self.retries = retries


def load_sheets_api_config(
    repo_root: str | Path = ".", config_path: str = DEFAULT_CONFIG_PATH
) -> SheetsApiConfig:
    """설정 파일이 없거나 읽기 실패하면 `enabled=False`(기존 브라우저 경로로 폴백)."""
    import yaml  # type: ignore

    path = Path(repo_root) / config_path
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("sheets_api.yaml 읽기 실패(%s) — API 비활성으로 취급: %s", path, exc)
        return SheetsApiConfig(enabled=False, url="", timeout_sec=120.0, retries=3)
    return SheetsApiConfig(
        enabled=bool(cfg.get("enabled")) and bool(cfg.get("url")),
        url=str(cfg.get("url") or ""),
        timeout_sec=float(cfg.get("timeout_sec") or 120.0),
        retries=int(cfg.get("retries") or 3),
    )


def is_api_enabled(repo_root: str | Path = ".", config_path: str = DEFAULT_CONFIG_PATH) -> bool:
    return load_sheets_api_config(repo_root, config_path).enabled


def call_sheets_api(
    spreadsheet_id: str,
    action: str,
    payload: dict[str, Any] | None = None,
    *,
    repo_root: str | Path = ".",
    config_path: str = DEFAULT_CONFIG_PATH,
    config: SheetsApiConfig | None = None,
) -> dict[str, Any]:
    """`action`(append/update_by_key/delete_by_key/snapshot)을 호출하고 `result`를 돌려준다.

    재시도 3회(지수 백오프: 1s, 2s, 4s ...), 타임아웃 120초(설정에서 override 가능).
    응답의 `ok`가 False이거나 네트워크 오류가 재시도까지 소진되면 `SheetsApiError`.
    """
    if action not in _ALLOWED_ACTIONS:
        raise SheetsApiError(f"허용되지 않은 작업: {action}")

    cfg = config or load_sheets_api_config(repo_root, config_path)
    if not cfg.enabled or not cfg.url:
        raise SheetsApiError("시트 API 비활성(config/sheets_api.yaml enabled=false 또는 url 없음)")

    body: dict[str, Any] = {"spreadsheet_id": spreadsheet_id, "action": action}
    body.update(payload or {})

    last_exc: Exception | None = None
    for attempt in range(1, max(1, cfg.retries) + 1):
        try:
            resp = httpx.post(cfg.url, json=body, timeout=cfg.timeout_sec, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — 네트워크·JSON 오류 모두 재시도 대상
            last_exc = exc
            log.warning("시트 API 호출 시도 %d/%d 실패(%s): %s", attempt, cfg.retries, action, exc)
            if attempt < cfg.retries:
                time.sleep(5 * attempt)  # 5·10·15초 — 작업자 6개와 웹앱을 나눠 써 잠금 대기 초과(HTML 응답)가 잦다(2026-09-25)
            continue
        if not isinstance(data, dict) or not data.get("ok"):
            err = (data or {}).get("error") if isinstance(data, dict) else str(data)
            last_exc = SheetsApiError(f"시트 API 오류 응답({action}): {err}")
            log.warning("시트 API 응답 실패 시도 %d/%d(%s): %s", attempt, cfg.retries, action, err)
            if attempt < cfg.retries:
                time.sleep(5 * attempt)  # 5·10·15초 — 작업자 6개와 웹앱을 나눠 써 잠금 대기 초과(HTML 응답)가 잦다(2026-09-25)
            continue
        return data.get("result") or {}

    raise SheetsApiError(f"시트 API 호출 실패({action}, {cfg.retries}회 재시도 소진): {last_exc}") from last_exc


def api_append(
    spreadsheet_id: str, rows: list[dict[str, Any]], *, repo_root: str | Path = ".", config: SheetsApiConfig | None = None
) -> dict[str, Any]:
    return call_sheets_api(spreadsheet_id, "append", {"rows": rows}, repo_root=repo_root, config=config)


def api_update_by_key(
    spreadsheet_id: str,
    updates: list[dict[str, Any]],
    *,
    repo_root: str | Path = ".",
    config: SheetsApiConfig | None = None,
) -> dict[str, Any]:
    return call_sheets_api(spreadsheet_id, "update_by_key", {"updates": updates}, repo_root=repo_root, config=config)


def api_delete_by_key(
    spreadsheet_id: str, keywords: list[str], *, repo_root: str | Path = ".", config: SheetsApiConfig | None = None
) -> dict[str, Any]:
    return call_sheets_api(spreadsheet_id, "delete_by_key", {"keywords": keywords}, repo_root=repo_root, config=config)


def api_snapshot(
    spreadsheet_id: str, *, repo_root: str | Path = ".", config: SheetsApiConfig | None = None
) -> dict[str, Any]:
    return call_sheets_api(spreadsheet_id, "snapshot", {}, repo_root=repo_root, config=config)


def api_set_header(spreadsheet_id: str, col: str, value: str, *, repo_root: str | Path = ".", config: SheetsApiConfig | None = None) -> dict[str, Any]:
    return call_sheets_api(spreadsheet_id, "set_header", {"col": col, "value": value}, repo_root=repo_root, config=config)


def api_reapply_format(spreadsheet_id: str, *, repo_root: str | Path = ".", config: SheetsApiConfig | None = None) -> dict[str, Any]:
    return call_sheets_api(spreadsheet_id, "reapply_format", {}, repo_root=repo_root, config=config)


def api_set_cells(spreadsheet_id: str, cells: list[dict[str, Any]], *, repo_root: str | Path = ".", config: SheetsApiConfig | None = None) -> dict[str, Any]:
    """cells: [{"a1": "P2", "value": 12}] — E열·데이터 행 B~F는 서버가 거부한다."""
    return call_sheets_api(spreadsheet_id, "set_cells", {"cells": cells}, repo_root=repo_root, config=config)


def api_delete_blank_rows(spreadsheet_id: str, *, repo_root: str | Path = ".", config: SheetsApiConfig | None = None) -> dict[str, Any]:
    return call_sheets_api(spreadsheet_id, "delete_blank_rows", {}, repo_root=repo_root, config=config)


def api_dedupe_by_key(spreadsheet_id: str, *, repo_root: str | Path = ".", config: SheetsApiConfig | None = None) -> dict[str, Any]:
    return call_sheets_api(spreadsheet_id, "dedupe_by_key", {}, repo_root=repo_root, config=config)


def api_info(spreadsheet_id: str, *, repo_root: str | Path = ".", config: SheetsApiConfig | None = None) -> dict[str, Any]:
    return call_sheets_api(spreadsheet_id, "info", {}, repo_root=repo_root, config=config)


def api_apply_colors(spreadsheet_id: str, a_map: dict[str, str], g_map: dict[str, str], *, repo_root: str | Path = ".", config: SheetsApiConfig | None = None) -> dict[str, Any]:
    """A(카페)·G(노출 상태) 값별 배경색 조건부 서식. 5개 시트에 같은 맵을 보내 통일한다(config/sheet_colors.yaml)."""
    return call_sheets_api(spreadsheet_id, "apply_colors", {"a_map": a_map, "g_map": g_map}, repo_root=repo_root, config=config)


__all__ = [
    "SheetsApiError",
    "SheetsApiConfig",
    "DEFAULT_CONFIG_PATH",
    "load_sheets_api_config",
    "is_api_enabled",
    "call_sheets_api",
    "api_append",
    "api_update_by_key",
    "api_delete_by_key",
    "api_snapshot",
]
