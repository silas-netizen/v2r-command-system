@echo off
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" scripts\gpt_login_hold.py > logs\gpt-login.log 2>&1
