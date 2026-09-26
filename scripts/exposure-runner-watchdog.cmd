@echo off
chcp 65001 > nul
rem 노출 확인 러너(작업자들)가 살아 있는지 확인하고, 없으면 다시 띄운다.
rem 예약 작업 V2R-ExposureRunner(로그온 시 + 5분 간격)가 이 파일을
rem exposure-runner-watchdog.vbs로 숨김 실행한다. 로그: logs\exposure-runner-watchdog.log
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PLAYWRIGHT_BROWSERS_PATH=%~dp0..\.pw-browsers
if not exist "logs" mkdir "logs"

set WORKERS=6

rem 상태 파일이 180초 안에 갱신됐으면(작업자가 살아서 처리 중이면) 살아있다고
rem 본다 — exposure_runner.is_alive()를 그대로 재사용(복잡한 인라인 PowerShell
rem 따옴표 중첩 대신, 이미 시험된 코드 재사용).
".venv\Scripts\python.exe" -c "from v2r.knowledge.exposure_runner import is_alive; import sys; sys.exit(0 if is_alive('.', stale_seconds=420) else 1)"
if %ERRORLEVEL% EQU 0 (
    echo [%date% %time%] watchdog: 러너 작업자 실행 중, 조치 없음 >> "logs\exposure-runner-watchdog.log"
) else (
    echo [%date% %time%] watchdog: 러너 작업자 없음 - 작업자 %WORKERS%개로 재기동 >> "logs\exposure-runner-watchdog.log"
    cscript //nologo "%~dp0exposure-runner-hidden.vbs" %WORKERS% >> "logs\exposure-runner-watchdog.log" 2>&1
)
