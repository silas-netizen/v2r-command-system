@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
call ".venv\Scripts\activate.bat"
python scripts\enqueue.py 자사 카페 일상 글 카페별 100건 실제 발행 댓글 랜덤 >> "logs\enqueue-0900.log" 2>&1
