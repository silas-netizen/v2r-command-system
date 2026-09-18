@echo off
chcp 65001 >nul
cd /d "D:\v2r 자동화\v2r-command-system"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
call .venv\Scripts\activate.bat
python -m v2r "gpt 세션 점검" >> logs\gpt-keepalive.log 2>&1
