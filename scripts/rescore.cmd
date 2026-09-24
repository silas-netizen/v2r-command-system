@echo off
chcp 65001 > nul
rem 브랜드 5개(우아덤·코숨핏·뉴더미스·장으뜸·팥순이) 연관도 채점을 클로드 채점
rem 워커(브랜드당 1개)와 Codex 교차검증 워커(브랜드당 2개)로 분리해 동시에 띄운다
rem (사용자 지시 2026-09-25: 클로드 채점이 GPT 검증 속도에 발목 잡히지 않도록).
rem 총 5*(1+2)=15개 프로세스. 중복 실행 방지는 파이썬 쪽 잠금
rem (data\locks\score-<브랜드>.lock, data\locks\codex-<브랜드>-<n>.lock)이 맡는다.
rem 정지: data\keywords\rescore_STOP 파일을 만들면 다음 묶음 전에 모두 멈춘다.
rem 로그: logs\score-<브랜드>.out.log, logs\codex-<브랜드>-<n>.out.log
rem 진행: data\keywords\score_progress_<브랜드>.json, codex_progress_<브랜드>_<n>.json
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"
if not exist "data\locks" mkdir "data\locks"

echo [%date% %time%] rescore(score+codex 분리) start >> "logs\rescore.log"

for %%B in (우아덤 코숨핏 뉴더미스 장으뜸 팥순이) do (
    start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --score-worker %%B >> "logs\score-%%B.out.log" 2>&1
    start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --codex-worker %%B 1 >> "logs\codex-%%B-1.out.log" 2>&1
    start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_relevance --codex-worker %%B 2 >> "logs\codex-%%B-2.out.log" 2>&1
)

echo [%date% %time%] rescore 15 workers launched (5 score + 10 codex) >> "logs\rescore.log"
