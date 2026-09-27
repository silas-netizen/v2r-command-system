# 감시 통합(V2R-Supervisor) 2026-09-27

## 요약

감시가 3개(노출·키워드·실행기)로 나뉘어 있었고, 그중 노출 감시가 줄바꿈 손상으로 23시간 멈춰도 아무도 몰랐던 문제를 없앴다. 이제 하나의 예약 작업 V2R-Supervisor(5분마다, 숨김)가 실행기·노출 작업자 6개·채점/코덱스 워커 18개·채우기 순환 5개(1만 미달 브랜드만)를 모두 살피고, 심장박동이 기준 시간 넘게 멈췄고 프로세스도 없거나 멈춘 것만 재기동한다.

## 감시 대상 표

| 대상 | 살아있음 판정 | 기준 시간 | 재기동 방법 |
|---|---|---|---|
| 실행기(serve) | data/sidecar_heartbeat.json 의 at | 7분 | schtasks /run V2R-Serve |
| 노출 작업자 6개 | data/exposure_runner_state.json 의 updated_at + 프로세스 수 | 7분 | scripts/exposure-runner-hidden.cmd 6 |
| 채점 워커(score 6·codex 12) | data/locks/score-*.lock, codex-*.lock 의 mtime | 7분 | scripts/rescore-hidden.vbs(잠금으로 중복 방지) |
| 채우기 순환(1만 미달 브랜드만) | data/keywords/fill_progress.json 의 updated_at + 프로세스, 확정 수는 DB(data/keywords/<브랜드>.sqlite)로 재확인 | 60분 | scripts/keyword-fill-worker.vbs <브랜드> 10000 b |

- data/STOP 있으면 아무 것도 하지 않음.
- 같은 대상 재기동은 15분에 1회 상한(data/supervisor_last.json 으로 관리).
- 매 실행 결과 한 줄을 logs/supervisor.log 에 남김.
- 기존 예약 V2R-ExposureRunner·V2R-KeywordWorkers 는 비활성화(삭제 아님).

## 구현·검사

- [scripts/supervisor.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/supervisor.py) — 표준 라이브러리만 사용.
- [scripts/supervisor.cmd](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/supervisor.cmd), [scripts/supervisor.vbs](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/supervisor.vbs) — ASCII·CRLF만.
- 기존 scripts/*.cmd 12개(claude-cli-login·codex-restart·enqueue-0900·exposure-runner-watchdog·gpt-keepalive·gpt-login·health-check·keyword-fill-hidden·keyword-workers-watchdog·naver-login·reconcile·rescore·serve)에 있던 한글·LF 위반을 ASCII·CRLF로 다시 썼다. 동작이 필요한 한글 브랜드명·문구는 [scripts/brands-rescore.txt](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/brands-rescore.txt), [scripts/brands-fill.txt](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/brands-fill.txt), [scripts/gpt-keepalive-phrase.txt](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/gpt-keepalive-phrase.txt) 로 분리했다(내용은 그대로, 동작 동일).
- exposure-runner-watchdog.cmd 는 이번에 LF 손상(23시간 무동작 사고 원인)도 CRLF로 고쳤다.
- [tests/test_supervisor.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_supervisor.py) — 판정 로직 단위 테스트.
- [tests/test_scripts_encoding.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_scripts_encoding.py) — scripts/*.cmd 전부 ASCII·CRLF 검사.
- 두 테스트 파일 47개 모두 통과.

## 실행 확인

- 예약 작업 V2R-Supervisor 등록, V2R-ExposureRunner·V2R-KeywordWorkers 비활성화 완료.
- 수동 실행(cmd /c scripts\supervisor.cmd) 확인: logs/supervisor.log 에 정상 줄 기록, 모든 대상 정상(살아 있는 것은 건드리지 않음), fill_장으뜸만 심장박동 기록이 없어 한 번 재기동(15분 상한 정상 동작 확인).
- 처음 실행 때 supervisor.cmd 자체 리다이렉트(>>)와 파이썬이 같은 로그 파일을 동시에 열어 기록이 사라지는 문제를 발견해 supervisor.cmd 의 표준출력 리다이렉트를 logs/supervisor-error.log 로 분리해 고쳤다.
- 예약 작업 자동 실행 확인: 11:51, 11:52(누적 2회 이상) 자동 실행되어 logs/supervisor.log 에 새 줄이 쌓이는 것을 확인.

## 사용자가 할 일

- 없음.
