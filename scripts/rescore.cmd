@echo off
chcp 65001 > nul
rem 브랜드 5개(우아덤·코숨핏·뉴더미스·장으뜸·팥순이) 연관도 재채점(구 3=무관 →
rem 3=당위성/4=무관 분리 + GPT 교차검증)을 각각 별도 숨김 프로세스로 동시에 띄운다.
rem 중복 실행 방지는 파이썬 쪽 잠금(data\locks\rescore-<브랜드>.lock)이 맡는다
rem (이미 신선한 잠금이 있으면 그 프로세스는 조용히 곧바로 끝난다).
rem 로그: logs\rescore-<브랜드>.log, 진행: data\keywords\rescore_progress_<브랜드>.json
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"
if not exist "data\locks" mkdir "data\locks"

echo [%date% %time%] rescore start >> "logs\rescore.log"

start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --rescore-worker 우아덤 >> "logs\rescore-우아덤.out.log" 2>&1
start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --rescore-worker 코숨핏 >> "logs\rescore-코숨핏.out.log" 2>&1
start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --rescore-worker 뉴더미스 >> "logs\rescore-뉴더미스.out.log" 2>&1
start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --rescore-worker 장으뜸 >> "logs\rescore-장으뜸.out.log" 2>&1
start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --rescore-worker 팥순이 >> "logs\rescore-팥순이.out.log" 2>&1

echo [%date% %time%] rescore 5 workers launched >> "logs\rescore.log"
