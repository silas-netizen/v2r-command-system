@echo off
chcp 65001 > nul
rem codex-worker 10개만 다시 띄운다(2026-09-25: sqlite "database is locked" 크래시
rem 수정 후 재기동, score-worker는 죽지 않았으므로 그대로 둔다).
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"
if not exist "data\locks" mkdir "data\locks"

for %%B in (우아덤 코숨핏 뉴더미스 장으뜸 팥순이) do (
    start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --codex-worker %%B 1 >> "logs\codex-%%B-1.out.log" 2>&1
    start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --codex-worker %%B 2 >> "logs\codex-%%B-2.out.log" 2>&1
)

echo [%date% %time%] codex 10 workers restarted >> "logs\rescore.log"
