# 실행기 재시작 시 발행 멈춤 사고 — 원인·수정 보고 (2026-09-22)

작성 시각(실측): `Tue Sep 22 11:52:38 2026`

## 1. 사고 시간표

| 시각 | 무슨 일 |
| --- | --- |
| (그 전) | 작업 123 (`publish_daily`, 카페별 100건) 실행 중 |
| 10:39 | `scripts/restart-serve.ps1` 로 실행기 재시작 — 실행기 프로세스를 죽이고 다시 띄움 |
| 10:39 | 새 실행기가 뜨며 `reap_stale_running` 이 리스 만료된 작업 123을 **uncertain**("실행기 중단")으로 정리. 이어받는 로직이 없어 큐에 그대로 멈춤 |
| 10:53 | 감시기(`monitor.py`)가 "진행 없음 15분" 경고만 채널에 남기고 **복구는 하지 않음**(정체 경고 뒤 실제 되돌림 로직이 없었음) |
| 10:39 ~ 11:42 | **발행 정지 1시간** — 아무 카페에도 글이 올라가지 않음 |
| 11:41 | 같은 명령으로 재명령 — 그런데 `_enqueue_key` 가 `uncertain` 상태를 처리하지 않아 기존 job 123의 id만 돌려주고 상태를 바꾸지 않음(새 작업도 안 만듦, 기존 작업도 그대로 uncertain) → **여전히 아무 일도 안 일어남** |
| 11:42 | 사람이 DB에서 job 123의 status를 직접 `queued` 로 되돌려 재개시킴 |

## 2. 근본 원인 3가지

1. **`v2r/store/jobs.py` `reap_stale_running`**: 리스가 끊긴 `running` 작업을 종류를 가리지 않고 전부 `uncertain` 으로 정리했다. `publish_daily`/`publish_batch` 처럼 "남은 건수를 다시 계산"하는 idempotent 작업까지 사람 개입이 필요한 `uncertain` 으로 빠뜨려, 아무도 자동으로 이어받지 못했다.
2. **`v2r/engine/monitor.py` `_watch_running`**: `STALL_S`(15분)에서는 알림만 남기고, 실제 되돌림(재등록)은 `DEAD_S`(30분) + 리스 없음 조건에서만 일어났다. 게다가 재등록은 "실패 처리 후 새 job"을 만드는 방식이라 `publish_daily` 처럼 **이미 올라간 만큼을 제외하고 이어서** 돌아야 하는 작업에는 맞지 않았다(중복 발행 우려로 감시기 자체가 `publish_*` 는 "부분 성공이면 재등록 금지" 규칙까지 갖고 있었다).
3. **`v2r/engine/worker.py` `_enqueue_key`**: 같은 명령을 다시 보냈을 때, 발행 작업의 이전 상태가 `failed`/`cancelled` 인 경우만 "새 키를 만들어 다시 등록"했다. `uncertain` 상태는 분기에 없어서 `enqueue()` 가 기존 idem_key로 기존 job을 그대로 찾아 **id만 돌려주고 상태는 그대로 두는** 결과가 됐다(11:41 재명령이 아무 효과가 없었던 이유).

## 3. 수정 내용

### 3-1. 재시작 시 재개 가능한 작업은 이어서 실행
[`v2r/store/jobs.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/jobs.py)
- `RESUMABLE_TASKS = frozenset({"publish_daily", "publish_batch"})` 추가 — 남은 건수를 다시 계산하는 idempotent 발행 작업만 포함.
- `reap_stale_running()` 을 재작성: 리스가 끊긴 `running` 작업 중 `RESUMABLE_TASKS` 에 속하면 `uncertain` 대신 **`queued`로 되돌려 이어서 실행**. 그 외(예: `repair_comments` 등)는 기존처럼 `uncertain`. 반환값도 `int` 건수에서 `[{"id","task","action"}...]` 리스트로 바꿔, 무엇이 이어서 실행되고 무엇이 사람 확인이 필요한지 구분할 수 있게 했다.
- `revive_to_queued(job_id)` / `requeue_running(job_id)` 두 메서드 추가(각각 uncertain 되살리기, running→queued 원자적 전환).

[`v2r/engine/worker.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py)
- `run_once`/`serve_poll` 두 호출부 모두 새 리스트 반환을 받아 `_log_reaped()` 로 처리: `queued`(이어서 실행)된 작업마다 이벤트에 **"감시: 실행기 재시작 후 이어서 실행"** 을 남기고, `uncertain` 으로 정리된 작업은 기존처럼 경고 로그만 남긴다. `serve_poll` 이 돌려주는 `reaped` 키는 기존 테스트 호환을 위해 건수(len)를 유지.

### 3-2. 같은 idem_key로 재명령하면 uncertain 작업을 되살린다
[`v2r/engine/worker.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py) `_enqueue_key()`
- 기존 작업이 `uncertain` 이면 새 키를 만들지 않고 `rt.jobs.revive_to_queued(id)` 로 그 작업 자체를 `queued` 로 되살려 반환한다. 오늘 11:41처럼 "재명령이 job id만 돌려주고 아무 일도 안 하는" 문제가 재발하지 않는다.

### 3-3. 감시기: 정체 15분에서 바로 자동 복구
[`v2r/engine/monitor.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/monitor.py) `_watch_running()`
- `RESUMABLE_TASKS` 인 작업이 `STALL_S`(15분) 정체 + 리스 만료/없음이면, 기존 "경고만" 대신 `rt.jobs.requeue_running(job_id)` 로 **즉시** `queued` 로 되돌린다(30분까지 기다리지 않음). `requeue_running` 은 `status='running' AND 리스 만료` 조건에서만 원자적으로 바뀌므로 중복 실행 위험이 없고(기존 감시 규칙의 "중복 방지" 취지와 같음), 추가로 같은 idem_key로 이미 다른 `queued` 작업이 있으면 건드리지 않는다.
- 리스가 아직 살아 있으면(다른 실행기가 실제로 작업 중) 기존처럼 "정체" 알림만 남기고 손대지 않는다(테스트 `test_publish_daily_정체여도_리스가_살아있으면_그대로_둔다`).
- 재개 불가 작업(`publish_daily`/`publish_batch` 가 아닌 것)은 기존 그대로: 15분 경고 → 30분 뒤 실패 처리 + 재등록 1회.

### 3-4. restart-serve.ps1: 재시작 전후 확인 문구
[`scripts/restart-serve.ps1`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/restart-serve.ps1)
- 재시작 **전**: `data/v2r.sqlite` 를 직접 조회해 `queued`/`running` 상태인 `publish_*` 작업이 있으면 "진행 중 publish 작업 있음" 과 목록을 출력.
- 재시작 **후** 30초 안에(5초 간격 재확인) 그 작업(들)이 `running`/`queued` 로 남아있는지(=사라지지 않고 살아있는지) 확인. 안 되면 **경고**를 출력하고 "감시 상태" 명령이나 DB 확인을 안내.

## 4. 테스트

추가한 테스트:
- [`tests/test_engine.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_engine.py) `test_리스가_끊긴_publish_daily는_uncertain이_아니라_이어서_실행된다` — 재시작 시 publish_daily 재큐잉.
- [`tests/test_engine.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_engine.py) `test_uncertain_발행_작업에_재명령하면_새로_안_만들고_되살린다` — uncertain 재명령이 되살아나는지.
- [`tests/test_monitor.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_monitor.py) `test_publish_daily_정체시_즉시_큐로_되돌려_이어서_실행한다` — 감시기 15분 정체 자동 복구.
- [`tests/test_monitor.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_monitor.py) `test_publish_daily_정체여도_리스가_살아있으면_그대로_둔다` — 살아있는 리스는 안 건드림(오탐 방지).
- 기존 `test_리스가_끊긴_running_작업을_정리한다` 는 새 반환 타입(list)에 맞춰 갱신.
- 기존 `test_running_job_stall_alert_then_reap_and_requeue` 는 "재개 불가 작업"(`repair_comments`)으로 바꿔 옛 30분-재등록 경로를 그대로 검증하도록 수정(재개 가능 작업의 즉시 복구는 새 테스트로 분리).

### 실행 결과
```
D:\v2r 자동화\v2r-command-system> .venv\Scripts\python -m pytest -q
...
7 failed, 1130 passed in 134.28s
```
- 실패 7건은 모두 `tests/test_brand_writer.py`, `tests/test_plan_command.py` 의 `_generate_brand` 관련 테스트로, 이번 수정(재시작/재큐잉/감시)과 **무관**하다. 직접 재현해 확인한 원인은 `load_pushed_keywords` 목(mock)이 실제 호출부가 넘기는 `conn` 키워드 인자를 받지 않아 나는 기존 버그(`TypeError: <lambda>() got an unexpected keyword argument 'conn'`)다. 이번 작업 범위(`v2r/store/jobs.py`, `v2r/engine/worker.py`, `v2r/engine/monitor.py`, `scripts/restart-serve.ps1`)와 전혀 겹치지 않는 코드이므로 손대지 않았다.
- `tests/test_engine.py` 를 단독 실행하면 teardown에서 "장부(발행 사용량 로그) 기록이 바뀌었습니다" 오류가 1건 난다. 이는 지금 실제로 발행 중(작업 123, publish_daily)이라 `data/llm_usage-2026-09.jsonl` 이 테스트 도중에도 실제로 갱신되기 때문이며(`tests/conftest.py` 의 "진짜 데이터 디렉터리 안 건드림" 감시 픽스처가 잡아낸 것), 이번 수정과 무관하고 발행이 끝나면 재현되지 않는다. 지시대로 이유만 적고 무시했다.
- 새로 추가/수정한 테스트를 포함한 `tests/test_monitor.py`, `tests/test_engine.py` 단독 실행은 위 장부 teardown 오류 1건을 빼고 전부 통과(98 passed, 1 error — 원인 상동).

## 5. 지금 발행 중인 작업(123)에 대한 영향

이번 작업 동안 **실행기를 재시작하지 않았고**, `publish.py`(발행 경로)도 건드리지 않았다. 작업 123은 그대로 진행 중이다. 다음에 실행기를 재시작해야 하는 상황이 오면(또는 다시 이런 리스 끊김이 생기면) 이번 수정으로 `publish_daily`/`publish_batch` 는 자동으로 `queued` 로 이어받아 돌게 된다.

## 6. 바뀐 파일

- [`v2r/store/jobs.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/jobs.py)
- [`v2r/engine/worker.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py)
- [`v2r/engine/monitor.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/monitor.py)
- [`scripts/restart-serve.ps1`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/restart-serve.ps1)
- [`tests/test_engine.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_engine.py)
- [`tests/test_monitor.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_monitor.py)
