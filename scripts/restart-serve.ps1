# 실행기(serve) 깨끗이 재시작: 옛 재시작 루프(cmd) + 파이썬 serve 전부 끝내고 V2R-Serve 예약 작업으로 다시 띄운다.
# 사용: powershell -ExecutionPolicy Bypass -File scripts\restart-serve.ps1
#
# 사고 2026-09-22: 발행 중(publish_daily)에 재시작했더니 그 작업이 uncertain으로
# 정리되고 아무도 이어받지 않아 1시간 발행이 멈췄다. 이제 실행기(v2r/store/jobs.py
# reap_stale_running)가 publish_daily/publish_batch를 재시작 시 queued로 되돌려
# 이어서 실행하지만, 재시작 자체가 위험한 순간이라는 걸 눈에 보이게 알린다.
$ErrorActionPreference = "SilentlyContinue"
$root = Join-Path $PSScriptRoot ".."
$py = Join-Path $root ".venv\Scripts\python.exe"
$dbCheck = @"
import sqlite3, sys
db = sys.argv[1]
try:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, task, status FROM jobs WHERE status IN ('running','queued')"
        " AND task LIKE 'publish%' ORDER BY id"
    ).fetchall()
    for r in rows:
        print(f"{r[0]}\t{r[1]}\t{r[2]}")
except Exception as exc:
    print(f"ERR\t{exc}", file=sys.stderr)
"@
$dbPath = Join-Path $root "data\v2r.sqlite"

function Get-PublishJobs {
    if (-not (Test-Path $py) -or -not (Test-Path $dbPath)) { return @() }
    $out = & $py -c $dbCheck $dbPath 2>$null
    if (-not $out) { return @() }
    return @($out | Where-Object { $_ -and $_.Trim() -ne "" })
}

# --- 재시작 전: 발행 중인 작업이 있으면 눈에 보이게 알린다 ---
$before = Get-PublishJobs
if ($before.Count -gt 0) {
    Write-Host "진행 중 publish 작업 있음 (재시작 전):"
    foreach ($line in $before) { Write-Host ("  - {0}" -f $line) }
} else {
    Write-Host "진행 중 publish 작업 없음 (재시작 전)"
}

$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "v2r serve|serve\.cmd" }
foreach ($p in $procs) {
    Write-Host ("종료: {0} {1}" -f $p.ProcessId, $p.CommandLine.Substring(0, [Math]::Min(70, $p.CommandLine.Length)))
    Stop-Process -Id $p.ProcessId -Force -Confirm:$false
}
Start-Sleep -Seconds 3
schtasks /run /tn "V2R-Serve" | Out-Null
Start-Sleep -Seconds 25
$hb = Get-Content (Join-Path $PSScriptRoot "..\data\serve_heartbeat.json") -Raw
Write-Host "심장박동: $hb"
$left = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "serve\.cmd" }
Write-Host ("남은 재시작 루프 창: {0}개 (1개가 정상)" -f @($left).Count)

# --- 재시작 후 30초 안에 그 작업(들)이 running/queued로 복귀했는지 확인 ---
if ($before.Count -gt 0) {
    Start-Sleep -Seconds 5  # 실행기가 첫 틱을 돌 시간
    $beforeIds = $before | ForEach-Object { ($_ -split "`t")[0] }
    $ok = $true
    for ($i = 0; $i -lt 5; $i++) {
        $after = Get-PublishJobs
        $afterIds = @($after | ForEach-Object { ($_ -split "`t")[0] })
        $missing = $beforeIds | Where-Object { $afterIds -notcontains $_ }
        if ($missing.Count -eq 0) { $ok = $true; break }
        $ok = $false
        Start-Sleep -Seconds 5
    }
    if ($ok) {
        Write-Host "확인: 재시작 전 진행 중이던 publish 작업이 running/queued로 복귀했습니다."
    } else {
        Write-Host "경고: 재시작 전 진행 중이던 publish 작업이 30초 안에 running/queued로 안 돌아왔습니다!"
        Write-Host "다음을 확인하세요: '감시 상태' 명령, 또는 data\v2r.sqlite의 jobs 테이블 (id: $($missing -join ', '))"
    }
}
