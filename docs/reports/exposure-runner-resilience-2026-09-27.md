# 노출 확인 러너 안정화 — 2026-09-27

## 왜 죽었나
09-26 12:32 재기동한 작업자 6개가 얼마 뒤 `sqlite3.OperationalError: database is locked`를 만났는데, 이 예외를 그대로 올려(`raise`) 처리해 프로세스 자체가 끝나 버렸다.
감시 스크립트도 줄 끝 손상(LF)으로 안 돌아가 재기동을 못 해, 09-27 11:17까지 약 23시간 노출 확인이 0건이었다(감시 스크립트는 이미 고쳐져 있었다).

## 고친 것
1. `v2r/knowledge/exposure_runner.py` 작업자 루프: `data/STOP` 파일이 없는 한 어떤 예외(잠금 포함)도 죽지 않고 30초 뒤 재시도한다. 브라우저 자체가 깨진 것으로 보이는 예외만 브라우저를 통째로 재기동한다(`_run_worker_browser_loop`/`_run_worker_session`).
2. `v2r/store/keyword_exposure_store.py`에 `with_sqlite_retry` 헬퍼(지수 백오프 5회)를 추가하고 `keyword_exposure` 저장(`save`)에 적용했다.
3. `v2r/store/exposure_queue_store.py`의 큐 쓰기(`upsert_candidates`, `claim_batch`, `claim_specific`, `mark_done`, `release_claim`)도 같은 헬퍼로 감쌌다.
4. 상태 파일(`data/exposure_runner_state.json`) 갱신이 작업자 생존의 유일한 판정 근거라, 별도 심장박동 스레드가 60초마다 갱신하도록 했다(`start_state_heartbeat`) — 재시도 대기 중에도 `is_alive`가 계속 참으로 유지된다.

## 20분 실측(재기동 11:53:12 → 12:15:37, 실제 경과 약 20분)
- 작업자 6개 모두 생존, `last_at`이 재확인 시점(12:14~12:15)까지 계속 최신이었다.
- 이 구간에 실제로 `sqlite3.OperationalError: database is locked`가 6개 작업자 로그 모두에서 반복 발생했지만(수정 전이었다면 죽었을 상황), 전부 재시도로 넘어가 프로세스는 그대로 살아 있었다 — 이번 수정이 정확히 겨냥한 상황을 실사용 중에 재현·확인한 셈이다.
- 처리 건수(작업자별 20여 분간 증가분): 0번 +27, 1번 +27, 2번 +32, 3번 +27, 4번 +26, 5번 +31 — 합계 약 170건, 시간당 약 470건대.
- 부가로 발견한 것: `data/exposure_runner_state.json` 임시 파일 교체가 여러 작업자·심장박동 스레드가 겹칠 때 가끔 `PermissionError`(윈도우 파일 잠금)로 5회 재시도를 다 써도 실패하는 경우가 있었다(1번 작업자에서 1회, "브라우저 세션 오류"로 오분류돼 불필요하게 브라우저를 재기동했지만 프로세스는 안 죽었다). 별도 개선 과제로 남긴다.
- 시험: `tests/test_exposure_runner.py`에 잠금 예외를 주입하는 단위 시험 5개를 추가했고, `tests/test_exposure_runner.py`(74개)·`tests/test_keyword_exposure.py`(33개)·`tests/test_keyword_exposure_cycle.py`(47개) 총 154개 모두 통과했다.

## 사용자가 할 일
없음 — 이미 재기동(작업자 6개, 11:53:12)까지 마쳤고 20분 실측도 끝났다. 위에 남긴 상태 파일 쓰기 경합(`PermissionError`)은 지금 당장 프로세스를 안 죽이므로 급하지 않지만, 계속 자주 나오면 다음에 손볼 후보로 알아 두면 된다.

## 관련 파일
- 코드: file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/exposure_runner.py
- 코드: file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/keyword_exposure_store.py
- 코드: file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/exposure_queue_store.py
- 시험: file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_exposure_runner.py
