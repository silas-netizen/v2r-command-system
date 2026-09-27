@echo off
chcp 65001 > nul
rem Launches relevance scoring for 6 brands, split into Claude score-workers
rem (1 per brand) and Codex cross-check workers (2 per brand), all at once
rem (user instruction 2026-09-25: Claude scoring should not be blocked by GPT check speed).
rem Total 6*(1+2)=18 processes. Duplicate-run protection is the python-side lock
rem (data\locks\score-<brand>.lock, data\locks\codex-<brand>-<n>.lock).
rem Stop: creating data\keywords\rescore_STOP stops everything before the next batch.
rem Log: logs\score-<brand>.out.log, logs\codex-<brand>-<n>.out.log
rem Progress: data\keywords\score_progress_<brand>.json, codex_progress_<brand>_<n>.json
rem Brand list comes from scripts\brands-rescore.txt (kept out of this ASCII-only file).
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"
if not exist "data\locks" mkdir "data\locks"

echo [%date% %time%] rescore(score+codex split) start >> "logs\rescore.log"

for /f "usebackq delims=" %%B in ("scripts\brands-rescore.txt") do (
    start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --score-worker %%B >> "logs\score-%%B.out.log" 2>&1
    start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --codex-worker %%B 1 >> "logs\codex-%%B-1.out.log" 2>&1
    start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --codex-worker %%B 2 >> "logs\codex-%%B-2.out.log" 2>&1
)

echo [%date% %time%] rescore 18 workers launched (6 score + 12 codex) >> "logs\rescore.log"
