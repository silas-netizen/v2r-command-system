@echo off
chcp 65001 > nul
rem exposure runner workers launcher (hidden). arg1 = worker count (default 6).
rem 2026-09-26: kill previous workers first so watchdog restarts never duplicate.
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PLAYWRIGHT_BROWSERS_PATH=%~dp0..\.pw-browsers
if not exist "logs" mkdir "logs"
set WORKERS=%~1
if "%WORKERS%"=="" set WORKERS=6
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { $_.CommandLine -like '*exposure_runner*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" > nul 2>&1
ping -n 4 127.0.0.1 > nul
echo [%date% %time%] exposure-runner start (workers=%WORKERS%) >> "logs\exposure-runner.log"
for /L %%N in (0,1,%WORKERS%) do (
    if %%N LSS %WORKERS% (
        start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.exposure_runner --worker-id %%N >> "logs\exposure-runner-%%N.log" 2>&1
    )
)
echo [%date% %time%] exposure-runner %WORKERS% workers launched >> "logs\exposure-runner.log"
