"""V2R-* 예약 작업은 전부 숨김 실행(wscript.exe)이어야 한다 — 2026-09-27 12:35 cmd 창 노출 사고."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 예약 작업 전용")
def test_all_v2r_tasks_run_hidden():
    ps = (
        "Get-ScheduledTask -TaskName 'V2R-*' | Where-Object { $_.State -ne 'Disabled' } | "
        "ForEach-Object { [pscustomobject]@{ n=$_.TaskName; e=($_.Actions | Select-Object -First 1).Execute } } | ConvertTo-Json"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60).stdout.strip()
    if not out:
        pytest.skip("예약 작업 없음")
    data = json.loads(out)
    rows = data if isinstance(data, list) else [data]
    bad = [r["n"] for r in rows if "wscript" not in str(r["e"]).lower() and "powershell" not in str(r["e"]).lower()]
    assert not bad, f"창이 뜨는 예약 작업: {bad}"
