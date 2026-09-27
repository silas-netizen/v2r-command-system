@echo off
chcp 65001 > nul
rem Checks that the score (5) + codex (10) keyword workers are alive; relaunches if short.
rem Scheduled task V2R-KeywordWorkers (every 5 min) runs this via keyword-workers-watchdog.vbs, hidden.
rem 2026-09-26 incident: all workers had exited and 27,592 candidates piled up unscored.
rem Workers are now resident (idle 60s if no work, retry 60s after an error); this watchdog is the backup.
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"

rem If the stop file exists, do nothing (deliberately set by the user)
if exist "data\STOP" (
    echo [%date% %time%] keyword-watchdog: stop file present, no action >> "logs\keyword-workers-watchdog.log"
    exit /b 0
)

for /f %%N in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { $_.CommandLine -like '*keyword_relevance*' }).Count"') do set ALIVE=%%N
if "%ALIVE%"=="" set ALIVE=0

if %ALIVE% GEQ 15 (
    echo [%date% %time%] keyword-watchdog: %ALIVE% workers running, no action >> "logs\keyword-workers-watchdog.log"
) else (
    echo [%date% %time%] keyword-watchdog: %ALIVE% workers (expected 15) - topping up via rescore.cmd >> "logs\keyword-workers-watchdog.log"
    rem rescore.cmd skips already-running workers via per-brand lock files (data\locks\score-*, codex-*)
    cscript //nologo "%~dp0rescore-hidden.vbs" >> "logs\keyword-workers-watchdog.log" 2>&1
)
