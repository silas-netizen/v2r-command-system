@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
call .venv\Scripts\activate.bat
rem Non-ASCII command phrase is read from a file, kept out of this ASCII-only script.
for /f "usebackq delims=" %%P in ("scripts\gpt-keepalive-phrase.txt") do set "PHRASE=%%P"
python -m v2r "%PHRASE%" >> logs\gpt-keepalive.log 2>&1
