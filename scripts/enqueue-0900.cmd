@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
call ".venv\Scripts\activate.bat"
rem 한글 인자는 cmd에서 깨진다 → 명령 문구는 파일에서 읽는다
python scripts\enqueue.py --file scripts\daily-0900.txt >> "logs\enqueue-0900.log" 2>&1
