@echo off
chcp 65001 > nul
rem V2R runner (receives Telegram commands). Restarts itself 30s after it dies.
rem Log: logs\serve.log  /  to stop: close this window or end task V2R-Serve in Task Scheduler
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
rem 2026-09-23: AppData\Local\ms-playwright installed from the Claude app (MSIX) shell is a
rem package-virtualized folder the runner (a normal process) cannot see -> use .pw-browsers
rem inside the repo instead (also run playwright install with this path).
set PLAYWRIGHT_BROWSERS_PATH=%~dp0..\.pw-browsers
if not exist "logs" mkdir "logs"

:loop
echo [%date% %time%] serve start >> "logs\serve.log"
rem 2026-09-23: call the venv python directly instead of activate
".venv\Scripts\python.exe" -m v2r serve >> "logs\serve.log" 2>&1
echo [%date% %time%] serve stopped (exit %errorlevel%), restarting in 30s >> "logs\serve.log"
timeout /t 30 /nobreak > nul
goto loop
