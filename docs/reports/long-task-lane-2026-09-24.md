# 장시간 작업 전용 줄(long) 분리 — 2026-09-24

## 사고 요약 (08:30–09:50)

- 긴 작업 `키워드 연관도 재채점`(task `keyword_relevance_rescore_legacy`, 수 시간)이 발행과 같은 줄(`main`, 실행기 리스 1개)에 서서 09:00 아침 일상 글 발행(`publish_daily`)을 52분 막았다.
- 옛 OS 예약 `V2R-Reconcile`이 `python -m v2r "끊긴 작업 점검"`을 별도 프로세스로 띄웠고, 그 CLI 경로가 실행기 리스를 잡고 대기열의 다른 작업(재채점)까지 집어 그 자리에서 돌렸다.

## 1. `keyword_relevance_rescore_legacy` / `sheet_sync_keywords` 를 `long` 줄로 분리

- `v2r/engine/sidecar.py`: `LONG_TASKS`(두 작업)를 새로 만들고, `scope_for`가 `long` → `light` → `main` 순으로 판정하게 고침. `SidecarThread`에 독립된 `v2r-sidecar-long` 스레드를 추가해, 발행(main) 리스와 무관하게 `long` 줄 작업을 그 자리에서(블로킹) 실행한다 — 사이드카의 `LIGHT_LIMIT` 루프 안에서 join()하지 않으므로 예약·감시·light 작업·심장박동은 계속 돈다.
- `v2r/store/jobs.py`: `acquire_light`를 `acquire_scope(owner, scope, lease_seconds)`로 일반화(하위 호환으로 `acquire_light`는 그대로 남김). `light`/`long` 모두 실행기 리스(`executor_lease`)를 쓰지 않는다. `LONG_LEASE_SECONDS = 12시간`을 새로 두어, `long` 작업은 재채점처럼 몇 시간이 걸려도 중간에 리스가 끊겨 "죽은 작업"으로 오인되지 않는다(`_keyword_relevance_rescore_legacy`가 이미 5분마다 `rt.jobs.heartbeat`로 리스를 연장하지만, 최초 리스 자체도 넉넉해야 재시작 직후 안전하다).
- `v2r/engine/worker.py`의 `run_once`: `scope="long"`이면 큰 리스(`LONG_LEASE_SECONDS`)로 `acquire`하고, `scope`가 `light`/`long`이 아닐 때만 `reap_stale_running()`을 부르게 해 매 사이드카 틱마다 불필요하게 전체 running 행을 훑지 않는다.

### 동시 실행 근거 — 발행 중에도 `long` 작업이 같이 돌아도 되는가

- **키워드 DB**: `data/keywords/<브랜드>.sqlite` — 발행(`publish_daily` 등)이 쓰는 DB가 아니다. 재채점·시트 반영 모두 이 파일만 읽고 쓴다.
- **작업 큐 DB(`jobs.sqlite`)**: WAL 모드라 읽기·쓰기가 동시에 돈다(사이드카 자체가 이미 그렇게 설계돼 있음, `sidecar.py` 상단 주석 참고). `long` 줄도 `light`처럼 `executor_lease`를 안 쓰므로 발행의 메인 리스와 다툴 일이 없다.
- **구글 시트**: `sheet_sync_keywords`는 `v2r.sources.sheets_writer`로 구글 시트 API만 호출한다. 발행 경로가 쓰는 자원(카페 로그인 세션, `v2r.client`)과 겹치지 않는다.
- **네이버 프로필**: 두 작업 모두 `keyword_relevance`(LLM 판정)·`sheets_writer`(구글 API)만 쓰고 네이버 로그인/브라우저를 쓰지 않는다 — 발행이 쓰는 카페 로그인 세션과 자원 경합이 없다.
- 결론: 공유 자원이 사실상 없어 발행 중에도 동시에 돌려도 안전하다.

## 2. `python -m v2r "<명령>"` 일회성 CLI가 대기열의 다른 작업을 집어가지 않게

- `v2r/__main__.py`의 `_cmd_command`: 예전에는 `handle_text`로 등록한 뒤 최대 20번 `worker.run_once(rt)`(줄 구분 없음)를 반복해, 대기열에 있던 **다른** 작업(예: `long` 줄의 재채점)까지 그 자리에서 실행하며 리스를 오래 붙잡을 수 있었다(장애의 직접 원인). 이제는 `handle_text`로 큐에 등록만 하고 바로 끝낸다 — 실제 실행은 `serve`(본 실행기)와 사이드카(`light`/`long`)가 맡는다.
- `python -m v2r reconcile`/`status`/`health` 등 큐를 거치지 않는 직접 명령은 그대로 둔다(원래도 다른 작업을 집지 않음).

## 3. 재시작 후 15분 공백 제거 — 죽은 소유자의 executor_lease 즉시 회수

- `v2r/store/jobs.py`의 `release_stale_lease()`: 기존에는 리스 만료 시각(`until`)만 보고, 시간이 남아 있으면(기본 900초) 재시작 직후라도 최대 15분을 그냥 흘려보냈다. 이제 리스 소유자(`호스트:pid`)가 **같은 호스트**에서 이미 죽었으면(psutil 없이 `ctypes.OpenProcess`로 pid 생존 확인, `v2r/engine/lock.py:pid_alive`) 만료 시각과 무관하게 즉시 회수한다.
- `v2r/engine/worker.py`의 `ensure_single_serve()`: `serve` 시작 시(파일 잠금을 잡기 전에) `rt.jobs.release_stale_lease()`를 한 번 불러, 이전 실행기가 죽었으면 새 실행기가 곧바로 작업을 집을 수 있게 한다.

## 시험

새로 추가: `tests/test_sidecar.py`에 `long` 줄 관련 시험 5건 —
`test_long_작업_목록은_main_줄이_아니라_long_줄에_선다`,
`test_long_작업은_light_사이드카가_안_집고_long_전용으로만_잡힌다`,
`test_long_작업은_발행이_main_리스를_쥐고_있어도_바로_잡힌다`,
`test_long_줄_리스는_넉넉해서_실행_도중에_안_끊긴다`,
`test_run_once_이_long_줄을_돌리면_main_리스를_안_쓴다`.

실행 결과(2026-09-24 실측):

- `pytest tests/test_sidecar.py -q` → 24 passed (1회 타이밍성 실패가 있었으나 재실행 시 24 passed, 기존에도 시간 기반이라 흔들리는 시험이었음 — 이번 변경과 무관).
- `pytest tests/test_schedule.py tests/test_engine.py -q` → 87 passed.
- 전체 `pytest -q`는 이 PC에서 실제로 돌고 있는 실행기(serve)·러너 프로세스와 파일 잠금이 겹쳐 응답이 없었다(CPU 사용량 0에 가깝게 멈춤) — 지시대로 실행기·러너 프로세스는 건드리지 않고, 관련 시험 파일만 개별 실행해 확인했다. 참고: 전체 스위트를 다시 돌리려면 실행기를 잠깐 멈추거나 격리된 별도 환경에서 실행해야 한다.

## 커밋한 파일

- `v2r/engine/sidecar.py`
- `v2r/store/jobs.py`
- `v2r/engine/worker.py`
- `v2r/__main__.py`
- `tests/test_sidecar.py`

## 남은 참고 사항

- `LIGHT_TASK_TIMEOUTS`로 12시간 상한을 주는 대안 대신 새 `long` 줄을 선택했다 — 사이드카의 `LIGHT_LIMIT` 루프 안에서 `thread.join(12시간)`을 쓰면 그 루프 자체가 몇 시간 멎어 light 작업·예약·감시가 다시 막힌다(사고 2026-09-20 재현). 별도 스레드가 훨씬 안전하다.
- `long` 줄의 리스가 끊겼는데도 실제로는 아직 실행 중인 경우(호스트 크래시 등)는 `reap_stale_running`이 `uncertain`으로 정리한다(재채점·시트반영은 `RESUMABLE_TASKS`에 없음) — 사람이 확인 후 `revive_to_queued`로 재실행해야 한다. 필요하면 별도로 논의.
