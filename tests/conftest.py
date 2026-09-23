"""시험 전체에 걸리는 안전장치 (2026-09-22).

왜 필요했나
-----------
`LLMRouter(api_key="k", client=가짜)` 처럼 `data_dir` 없이 만든 라우터는 설정의
**진짜 `data/` 폴더**를 썼다. 그래서 시험을 한 번 돌릴 때마다 가짜 호출 11줄이
진짜 사용량 장부(`data/llm_usage-2026-09.jsonl`)에 쌓였다(실측: 11회분 121줄).
요금제 잠금 파일·잠금 알림도 같은 경로로 새 나갈 수 있었다.

그래서 **모든 시험**에서 아래를 임시 폴더로 돌린다.

- `V2R_USAGE_LEDGER_DIR` — 사용량 장부 폴더
- `V2R_DATA_DIR` — 설정이 읽는 데이터 폴더 (`get_settings()` 캐시도 비운다)
- `V2R_REPO_ROOT` — `Settings.repo_root` 기본값(2026-09-23 추가). `docs/reports/*`
  처럼 `repo_root` 밑 경로에 쓰는 보고서 생성기가 `out_dir`을 안 받고 불리면
  이 가짜 저장소 폴더로 간다. (사고: 03:31 pytest 실행이 `make_runtime`류
  도우미가 `Settings(...)`에 `repo_root`를 안 넘긴 탓에 진짜
  `docs/reports/dashboard-2026-09-22.*`를 빈 DB 결과로 덮어썼다.)
"""

from __future__ import annotations

import os

import pytest

from v2r.llm.usage_ledger import LEDGER_DIR_ENV


@pytest.fixture(autouse=True)
def _isolate_data_dirs(tmp_path_factory, monkeypatch):
    """시험이 저장소의 진짜 `data/`·`docs/reports`를 건드리지 못하게 막는다."""
    sandbox = tmp_path_factory.mktemp("v2r-data")
    repo_sandbox = tmp_path_factory.mktemp("v2r-repo")
    monkeypatch.setenv(LEDGER_DIR_ENV, str(sandbox))
    monkeypatch.setenv("V2R_DATA_DIR", str(sandbox))
    monkeypatch.setenv("V2R_REPO_ROOT", str(repo_sandbox))

    from v2r import config

    config.get_settings.cache_clear()
    try:
        yield sandbox
    finally:
        config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_real_data_dir_writes(_isolate_data_dirs):
    """진짜 장부가 시험 때문에 깎이거나 사라지면 바로 잡아낸다 (덧대기 감시).

    2026-09-23: 실행기가 09:00 발행을 돌리는 동안 같은 장부 파일에 실시간으로
    줄을 계속 덧붙인다. 그래서 "크기가 조금이라도 달라지면 실패"로 두면
    시험과 무관한 정상적인 덧붙임까지 오탐으로 잡힌다. 시험이 실제로 문제를
    일으켰다면 파일이 사라지거나(삭제) 줄어드는(덮어쓰기) 방향으로만 나타나므로
    그 두 경우만 잡는다.
    """
    from pathlib import Path

    real = Path(__file__).resolve().parents[1] / "data"
    ledgers = sorted(real.glob("llm_usage-*.jsonl"))
    before = {p: p.stat().st_size for p in ledgers}
    yield
    for path, size in before.items():
        if not path.exists():
            raise AssertionError(
                f"시험이 진짜 사용량 장부를 지웠습니다: {path}"
            )
        after_size = path.stat().st_size
        if after_size < size:
            raise AssertionError(
                f"시험이 진짜 사용량 장부를 덮어썼습니다(줄어듦): {path}"
            )


@pytest.fixture(autouse=True)
def _no_real_docs_reports_writes():
    """진짜 `docs/reports/*` 파일이 시험 때문에 지워지거나 빈 내용으로
    덮어써지면 바로 잡아낸다(파수꾼, 2026-09-23 — 03:31 pytest 실행이
    `dashboard-2026-09-22.*`를 빈 DB 결과로 덮어쓴 사고 재발 방지).

    2026-09-23 보강: 실행기가 발행을 돌리는 동안 보고서 파일을 실시간으로
    새로 쓰거나 갱신한다(정상 동작, mtime·크기가 자연스레 바뀜). 크기·시각이
    "다르기만 해도" 실패로 잡으면 이런 정상 갱신까지 오탐이 된다. 시험이
    실제로 사고를 냈다면 파일이 사라지거나 내용이 줄어드는(빈 결과로 덮어쓰는)
    방향으로 나타나므로 그 경우만 잡는다."""
    from pathlib import Path

    real = Path(__file__).resolve().parents[1] / "docs" / "reports"
    files = sorted(p for p in real.glob("*") if p.is_file())
    before = {p: p.stat().st_size for p in files}
    yield
    for path, size in before.items():
        if not path.exists():
            raise AssertionError(f"시험이 진짜 보고서 파일을 지웠습니다: {path}")
        after_size = path.stat().st_size
        if after_size < size:
            raise AssertionError(
                f"시험이 진짜 보고서 파일을 건드렸습니다(docs/reports 격리 실패, 줄어듦): {path}"
            )
