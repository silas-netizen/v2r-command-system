@echo off
chcp 65001 > nul
rem 노출 확인 러너 작업자를 숨김 프로세스로 띄운다. 작업자 수는 인자(기본 2,
rem config\exposure.yaml의 workers와 맞춘다) — 첫 인자로 다른 수를 줄 수 있다.
rem 로그: logs\exposure-runner-<n>.log
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PLAYWRIGHT_BROWSERS_PATH=%~dp0..\.pw-browsers
if not exist "logs" mkdir "logs"

set WORKERS=%1
if "%WORKERS%"=="" set WORKERS=2

echo [%date% %time%] exposure-runner start (workers=%WORKERS%) >> "logs\exposure-runner.log"

for /L %%N in (0,1,%WORKERS%) do (
    if %%N LSS %WORKERS% (
        start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.exposure_runner --worker-id %%N >> "logs\exposure-runner-%%N.log" 2>&1
    )
)

echo [%date% %time%] exposure-runner %WORKERS% workers launched >> "logs\exposure-runner.log"
