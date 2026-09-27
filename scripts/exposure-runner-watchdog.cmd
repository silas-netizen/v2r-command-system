@echo off
chcp 65001 > nul
rem exposure runner watchdog - relaunch workers when state file is stale. log: logsexposure-runner-watchdog.log
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PLAYWRIGHT_BROWSERS_PATH=%~dp0..\.pw-browsers
if not exist "logs" mkdir "logs"
set WORKERS=6
".venv\Scripts\python.exe" -c "from v2r.knowledge.exposure_runner import is_alive; import sys; sys.exit(0 if is_alive('.', stale_seconds=420) else 1)"
if %ERRORLEVEL% EQU 0 (
    echo [%date% %time%] watchdog: alive, no action >> "logs\exposure-runner-watchdog.log"
) else (
    echo [%date% %time%] watchdog: not alive - relaunch %WORKERS% workers >> "logs\exposure-runner-watchdog.log"
    cscript //nologo "%~dp0exposure-runner-hidden.vbs" %WORKERS% >> "logs\exposure-runner-watchdog.log" 2>&1
)
