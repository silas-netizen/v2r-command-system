@echo off
chcp 65001 > nul
rem Launches the keyword-fill loop for each brand in its own hidden console
rem (scripts\keyword-fill-worker.vbs) - workers keep running after this cmd exits.
rem Brand list comes from scripts\brands-fill.txt (kept out of this ASCII-only file).
rem Log: logs\keyword-fill-<brand>.log
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PLAYWRIGHT_BROWSERS_PATH=%~dp0..\.pw-browsers
if not exist "logs" mkdir "logs"

echo [%date% %time%] keyword-fill start >> "logs\keyword-fill.log"

for /f "usebackq delims=" %%B in ("scripts\brands-fill.txt") do (
    wscript.exe "scripts\keyword-fill-worker.vbs" %%B 10000
)
echo [%date% %time%] keyword-fill workers launched >> "logs\keyword-fill.log"
