@echo off
chcp 65001 > nul
rem Restart only the 10 codex-workers (2026-09-25: after fixing the sqlite
rem "database is locked" crash; score-workers did not die, leave them alone).
rem Brand list (5, no gangnyeon-gi) comes from scripts\brands-fill.txt (kept out of this ASCII-only file).
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"
if not exist "data\locks" mkdir "data\locks"

for /f "usebackq delims=" %%B in ("scripts\brands-fill.txt") do (
    start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --codex-worker %%B 1 >> "logs\codex-%%B-1.out.log" 2>&1
    start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --codex-worker %%B 2 >> "logs\codex-%%B-2.out.log" 2>&1
)

echo [%date% %time%] codex workers restarted >> "logs\rescore.log"
