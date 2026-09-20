# 실행기(serve) 깨끗이 재시작: 옛 재시작 루프(cmd) + 파이썬 serve 전부 끝내고 V2R-Serve 예약 작업으로 다시 띄운다.
# 사용: powershell -ExecutionPolicy Bypass -File scripts\restart-serve.ps1
$ErrorActionPreference = "SilentlyContinue"
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
