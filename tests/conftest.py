"""시험 전체에 걸리는 안전장치 (2026-09-22).

왜 필요했나
-----------
`LLMRouter(api_key="k", client=가짜)` 처럼 `data_dir` 없이 만든 라우터는 설정의
**진짜 `data/` 폴더**를 썼다. 그래서 시험을 한 번 돌릴 때마다 가짜 호출 11줄이
진짜 사용량 장부(`data/llm_usage-2026-09.jsonl`)에 쌓였다(실측: 11회분 121줄).
요금제 잠금 파일·잠금 알림도 같은 경로로 새 나갈 수 있었다.

그래서 **모든 시험**에서 아래 두 가지를 임시 폴더로 돌린다.

- `V2R_USAGE_LEDGER_DIR` — 사용량 장부 폴더
- `V2R_DATA_DIR` — 설정이 읽는 데이터 폴더 (`get_settings()` 캐시도 비운다)
"""

from __future__ import annotations

import os

import pytest

from v2r.llm.usage_ledger import LEDGER_DIR_ENV


@pytest.fixture(autouse=True)
def _isolate_data_dirs(tmp_path_factory, monkeypatch):
    """시험이 저장소의 진짜 `data/`를 건드리지 못하게 막는다."""
    sandbox = tmp_path_factory.mktemp("v2r-data")
    monkeypatch.setenv(LEDGER_DIR_ENV, str(sandbox))
    monkeypatch.setenv("V2R_DATA_DIR", str(sandbox))

    from v2r import config

    config.get_settings.cache_clear()
    try:
        yield sandbox
    finally:
        config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_real_data_dir_writes(_isolate_data_dirs):
    """진짜 장부에 줄이 늘어나면 바로 잡아낸다 (덧대기 감시)."""
    from pathlib import Path

    real = Path(__file__).resolve().parents[1] / "data"
    ledgers = sorted(real.glob("llm_usage-*.jsonl"))
    before = {p: p.stat().st_size for p in ledgers}
    yield
    for path, size in before.items():
        if path.exists() and path.stat().st_size != size:
            raise AssertionError(
                f"시험이 진짜 사용량 장부를 건드렸습니다: {path}"
            )
