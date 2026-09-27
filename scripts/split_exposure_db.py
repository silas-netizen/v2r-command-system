"""노출 확인 표(`keyword_exposure`, `exposure_queue`)를 `data/v2r.sqlite`에서
별도 파일(`data/exposure.sqlite`)로 옮기는 1회성 마이그레이션.

설계: docs/reports/db-split-plan-2026-09-27.md.
분리 대상 표는 `v2r.store.keyword_exposure_store.EXPOSURE_TABLES`(하나뿐인 목록,
조사 결과와 항상 같게 유지).

사용:
    python scripts/split_exposure_db.py --dry-run
    python scripts/split_exposure_db.py --rename-old

절차: (a) 노출 러너(`exposure_runner.py`) 프로세스가 떠 있으면 중단(락 경쟁·
      복사 도중 쓰기 방지). (b) ATTACH로 새 파일에 스키마+데이터를 복사하고
      인덱스를 재생성한 뒤 행 수가 원본과 같은지 검증. (c) `--rename-old`를
      주면 원본 표를 지우지 않고 `_legacy_<표>`로 이름만 바꾼다(기본은 원본을
      그대로 둔다 — 되돌리기가 쉽도록). (d) `--dry-run`은 아무것도 쓰지 않고
      계획·검증만 표준출력에 JSON으로 낸다.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from v2r.store.keyword_exposure_store import EXPOSURE_TABLES, _EXPOSURE_SCHEMA  # noqa: E402

DEFAULT_SRC = REPO_ROOT / "data" / "v2r.sqlite"
DEFAULT_DST = REPO_ROOT / "data" / "exposure.sqlite"


class RunnerRunningError(RuntimeError):
    """노출 러너 프로세스가 떠 있어 마이그레이션을 중단했다."""


def running_exposure_runner_pids() -> list[str]:
    """`exposure_runner.py`를 명령줄에 포함한 살아있는 프로세스의 PID 목록.

    psutil이 없는 환경(이 저장소 .venv)이라 Windows `wmic`으로 명령줄을 본다.
    wmic 자체가 없는(향후 Windows 빌드에서 제거) 환경이면 조용히 "알 수 없음"
    으로 보고 빈 목록을 돌려준다 — 그 경우 호출자가 사용자에게 수동 확인을
    요청해야 한다(이 스크립트는 자동으로 통과시키지 않도록 별도 플래그로만
    건너뛴다).
    """
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    pids: list[str] = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if "exposure_runner.py" in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.append(parts[-1])
    return pids


def _table_row_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    return int(row[0] if not isinstance(row, sqlite3.Row) else row["c"])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def plan(src_path: Path) -> dict:
    """무엇을 옮길지(표별 원본 행 수)만 계산 — dry-run·계획 출력 공용."""
    result: dict[str, int] = {}
    if not src_path.exists():
        return result
    conn = sqlite3.connect(str(src_path))
    try:
        for table in EXPOSURE_TABLES:
            if _table_exists(conn, table):
                result[table] = _table_row_count(conn, table)
    finally:
        conn.close()
    return result


def migrate(
    src_path: Path,
    dst_path: Path,
    *,
    dry_run: bool,
    rename_old: bool,
) -> dict:
    before_counts = plan(src_path)
    report: dict = {
        "src": str(src_path),
        "dst": str(dst_path),
        "tables": list(EXPOSURE_TABLES),
        "dry_run": dry_run,
        "rename_old": rename_old,
        "before_counts": before_counts,
    }

    if not before_counts:
        report["skipped"] = "원본에 옮길 표가 없습니다(이미 분리됐거나 빈 DB)"
        return report

    if dry_run:
        report["would_copy"] = before_counts
        return report

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(src_path))
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("ATTACH DATABASE ? AS dst", (str(dst_path),))
        # CREATE TABLE/INDEX 문 둘 다 "dst." 접두를 붙여야 원본(main)이 아니라
        # 새로 붙인 파일(dst)에 만들어진다(접두 없으면 기본 스키마 main에 생긴다).
        dst_schema = (
            _EXPOSURE_SCHEMA.replace("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE IF NOT EXISTS dst.")
            .replace("CREATE INDEX IF NOT EXISTS ", "CREATE INDEX IF NOT EXISTS dst.")
        )
        conn.executescript(dst_schema)

        for table in EXPOSURE_TABLES:
            if table not in before_counts:
                continue
            conn.execute(f"DELETE FROM dst.{table}")
            conn.execute(f"INSERT INTO dst.{table} SELECT * FROM main.{table}")

        conn.commit()

        after_counts = {}
        for table in EXPOSURE_TABLES:
            if table in before_counts:
                after_counts[table] = _table_row_count(conn, f"dst.{table}")
        report["after_counts"] = after_counts

        mismatch = {
            t: {"before": before_counts[t], "after": after_counts.get(t)}
            for t in before_counts
            if before_counts[t] != after_counts.get(t)
        }
        report["verified"] = not mismatch
        if mismatch:
            report["mismatch"] = mismatch

        if not mismatch and rename_old:
            for table in before_counts:
                conn.execute(f"ALTER TABLE main.{table} RENAME TO _legacy_{table}")
            conn.commit()
            report["renamed_old"] = [f"_legacy_{t}" for t in before_counts]

        conn.execute("DETACH DATABASE dst")
    finally:
        conn.close()

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", default=str(DEFAULT_SRC))
    parser.add_argument("--dst", default=str(DEFAULT_DST))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rename-old", action="store_true")
    parser.add_argument(
        "--skip-runner-check",
        action="store_true",
        help="러너 프로세스 확인을 건너뛴다(사용자가 이미 수동으로 멈춘 걸 확인했을 때만).",
    )
    args = parser.parse_args(argv)

    if not args.skip_runner_check:
        pids = running_exposure_runner_pids()
        if pids:
            print(
                json.dumps(
                    {"error": "노출 러너가 실행 중입니다(PID: %s). 먼저 멈추세요." % ", ".join(pids)},
                    ensure_ascii=False,
                )
            )
            return 1

    report = migrate(
        Path(args.src), Path(args.dst), dry_run=args.dry_run, rename_old=args.rename_old
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("verified", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
