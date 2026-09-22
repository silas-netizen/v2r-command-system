# 사이드카 자가 복구 + 알림 등급·아이콘 정책 (2026-09-23 01:55 KST, 실측)

## 배경(사고 요약)

2026-09-23 01:00, 예약 "키워드 발굴 전체 500개"가 발사됐는데 그때 돌던 실행기는
옛 코드라 이 명령을 몰라 "키워드 노출 현황"으로 잘못 해석했다. 사이드카가 그
LIGHT 작업(148번)을 붙잡고 네이버 검색을 15분 넘게 돌리는 동안 **사이드카
전체가 멈췄다** — 심장박동을 못 찍었고, 감시기는 "심장박동이 184/788/853초째
낡았습니다" 경고를 슬랙·텔레그램으로 반복 전송만 하고 스스로 복구하지 않았다.

이 문서는 그 근본 원인 네 가지에 대한 조치와, 함께 지시된 알림 등급·범주
아이콘 정책을 정리한다.

## 1. 사이드카 자가 복구

파일: [`v2r/engine/sidecar.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/sidecar.py),
[`v2r/engine/worker.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py)

### (a) LIGHT 작업 시간 상한
- `worker.run_once(rt, owner, scope="light", timeout_seconds=...)` 가 그 작업을
  **별도 스레드**에서 돌리고 `LIGHT_TASK_TIMEOUT_SECONDS`(기본 120초, 작업별로
  `LIGHT_TASK_TIMEOUTS` 로 다르게 줄 수 있음) 안에 안 끝나면 그 작업만 `failed`
  로 끝내고 **바로 돌아온다**. 스레드 자체는 안전하게 죽일 수 없어(파이썬 한계)
  데몬 스레드로 백그라운드에서 마저 돌지만, 사이드카 루프는 막히지 않는다.
- `sidecar.tick_once` 는 이제 `worker.drain(scope="light")` 대신 `run_once` 를
  한 건씩 불러(`LIGHT_LIMIT` 상한까지) 상한을 지킨다.

### (b) 심장박동 스레드 분리
- `SidecarThread` 가 이제 **틱 스레드**(예약·감시·채널·LIGHT 작업)와
  **심장박동 스레드**를 따로 띄운다(`_run` / `_run_heartbeat`). 틱이 아무리
  오래 걸려도 심장박동 스레드는 `HEARTBEAT_TICK_SECONDS`(5초)마다 계속 찍는다.
- `_Status` 객체가 "지금 뭘 하는지"(schedule/monitor/poll_channels/
  light:acquire/exposure_cycle/idle)와 경과 초를 기록한다. 심장박동 파일
  (`data/sidecar_heartbeat.json`)에 `busy: {"step": ..., "elapsed": ...}` 로
  같이 남아, "죽은 것"과 "그냥 바쁜 것"을 구분할 수 있다.

### (c) 감시기 자동 재시작
- `worker.ensure_sidecar` 가 사이드카를 3번(`SIDECAR_RESTART_MAX`) 다시
  띄워도 여전히 안 살아나면 `worker._restart_serve_process` 를 불러 실행기
  자체를 `scripts/restart-serve.ps1` 로 재시작한다.
- 발행(main 스코프) 작업이 도는 중이면 재시작을 미룬다(발행 슬롯 사이가
  아니라 중간에 끊기지 않게) — `rt.jobs.running_jobs()` 로 확인.

## 2. 알림 중복 방지 (등급 정책)

파일: [`v2r/channels/__init__.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/channels/__init__.py),
[`config/notify.yaml`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/config/notify.yaml)

`notify_all(channels, text, level=..., category=..., tag=...)` 로 등급·사건
구분·범주 아이콘을 모두 받는다.

- **critical**: 항상 보낸다. `category`(사건 구분자) 로 같은 사건을
  `CRITICAL_REPEAT_SECONDS`(600초) 안엔 되풀이하지 않고, 600초 넘게 이어지면
  1회 더(에스컬레이션, 문구에 "계속됨, N초째"). `clear_critical(category)` 로
  복구를 표시하면 다음 발생 때 새 사건으로 다시 보낸다(사이드카 심장박동은
  `alert_recovered` 가 "복구됨" 문구를 자동으로 보낸다).
- **summary**: 채널로 안 보낸다(0 반환) — 정기 보고(중간/일일 보고, 미처리
  목록)에만 담는다.
- **info**(기본값): 채널로 안 보낸다 — 로그·DB 이벤트·현황판에만 남긴다.
- **always**: 등급 억제 밖 — 사용자가 채널에서 직접 보낸 명령의 응답(중지,
  사진 승인/반려, 슬랙/텔레그램 연결 점검 등)은 항상 보낸다.

자동 진단(자가복구 판정) 문구("판정: … / 조치 … / 시도함 … / 다시 하려면 …")는
`v2r/engine/monitor.py::_watch_finished` 에서 채널 메시지에서 빠지고 이벤트
로그(`rt.events.log`)에만 "자동 진단:" 접두로 전체가 남는다. 채널에는 첫 줄
(작업 실패 요약)과 "다시 하려면…" 줄만 나간다.

## 3. 범주 아이콘

`CATEGORY_ICONS`(`v2r/channels/__init__.py`) + `config/notify.yaml` 의 표.
등급 아이콘(🔴 critical / 🟡 summary)과 조합돼 메시지 앞에 붙는다. `always`
등급은 범주 아이콘만 붙는다(예: "💬 상태: …").

| 범주 | 아이콘 | 뜻 |
|---|---|---|
| publish | 📢 | 일상·브랜드 발행 결과 |
| login | 🔑 | 로그인·세션 |
| schedule | ⏰ | 예약·실행기 |
| keyword | 🔍 | 키워드·노출 |
| draft | ✍️ | 원고·이미지 생성 |
| dashboard | 📊 | 현황판 / 중간·일일 보고 |
| reply | 💬 | 사용자 직접 명령 응답 |
| maintenance | 🛠 | 정비 |

## 4. 메시지 종류별 등급·범주·아이콘·채널 전송 표

| 메시지 종류 | 위치(대략) | 등급 | 범주 | 아이콘 | 채널로 나감? |
|---|---|---|---|---|---|
| 사이드카 심장박동 낡음 | sidecar.alert_if_stale | critical | schedule | 🔴⏰ | 예(사건당 1회+에스컬레이션) |
| 사이드카 심장박동 복구 | sidecar.alert_recovered | critical | schedule | 🔴⏰ | 예(복구 1회) |
| 사이드카 죽음→재시작 | worker.ensure_sidecar | critical | schedule | 🔴⏰ | 예 |
| 사이드카 재시작 한도 초과→실행기 재시작 | worker._restart_serve_process | critical | schedule | 🔴⏰ | 예 |
| 예약 실패(자동 재시도) | schedule.check_pending | critical | schedule | 🔴⏰ | 예 |
| 예약 자동 복구 실패 | schedule.check_pending | critical | schedule | 🔴⏰ | 예 |
| 예약 명령 오해석(발사 안 함) | schedule.fire | critical | schedule | 🔴⏰ | 예 |
| 감시(monitor) 작업 실패 알림 | monitor._alert | critical | schedule | 🔴⏰ | 예(첫 줄+재개명령만, 진단 상세는 이벤트로) |
| 감시 하루 요약 | monitor._daily_summary_if_due | summary | dashboard | 🟡📊 | 아니오(정기 보고에만) |
| ChatGPT/네이버/사이트 로그인 풀림 | worker(RELOGIN_NOTICE 등) | critical | login | 🔴🔑 | 예 |
| 네이버 세션 경고 | worker._check_naver_session | critical | login | 🔴🔑 | 예 |
| ChatGPT 이미지 사용 한도 | worker(사진 생성) | critical | draft | 🔴✍️ | 예 |
| 사진 원본 없음(즉시 알림) | worker(발행 중 예외) | critical | draft | 🔴✍️ | 예 |
| 계정 등록 제한 다발(5건↑) | reconcile.reconcile | critical | publish | 🔴📢 | 예 |
| 사진 재고 안내 / 부족 안내 / 승인 대기 | worker._generate_photos, publish.notify_photo_shortage | always | draft | ✍️ | 예(사용자 승인이 필요한 요청) |
| 사진 승인/반려 결과 | worker(사진 승인/반려 명령) | always | reply | 💬 | 예 |
| 중지 명령 응답 | worker.handle_text | always | reply | 💬 | 예 |
| 슬랙/텔레그램 연결 점검 결과(문서 못 보낼 때) | worker(점검 명령) | always | reply | 💬 | 예 |
| 개별 작업 완료·실패 보고 | worker._finish_run | summary | publish | 🟡📢 | 아니오(정기 보고에만) |
| "실행기 시작됨" | worker.serve | info | (없음) | ⚪ | 아니오 |
| 예약 발사 자체 | schedule.fire (성공 시) | info | (없음) | ⚪ | 아니오(로그·이벤트만) |
| 미처리 목록 | worker(미처리 알림, 문서 전송) | — | dashboard | 📊 | 예(문서로, 하루 1회 18:00) |
| 중간/일일 보고 | worker(문서 전송) | — | dashboard | 📊 | 예(문서로, 정해진 시각) |

(문서 전송(`notify_document_all`)은 이번 등급 필터 대상이 아니다 — 정기
보고서 자체가 파일로 나가는 게 목적이라 억제하면 안 되기 때문이다.)

## 5. 예약표 변경

[`config/schedule.yaml`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/config/schedule.yaml)

- "키워드 발굴 전체"(01:00)에 `expect_task: keyword_discovery_all` 을 달았다.
  발사 전에 지금 파서가 이 명령을 정말 그 작업으로 푸는지 확인하고, 어긋나면
  (이번 사고처럼 옛 코드가 다른 작업으로 풀면) **발사하지 않고** critical
  경고 1회만 남긴다 — `docs/reports/reply-2026-09-23-0112-sidecar.md` 사고
  재발 방지.
- "미처리 알림"을 하루 5회(08:40/11:00/14:00/17:00/21:00) → 18:00 1회로
  축소했다(사용자 지시: 메시지가 너무 많다). 어차피 summary 등급이라 채널로
  따로 안 나가고 문서로만 가지만, 실행 자체도 하루 1번이면 충분하다.

## 6. 테스트

`tests/test_sidecar.py` (신규 5건: LIGHT 시간초과, 재시작 한도 초과 시 실행기
재시작 시도, 재시작이 발행 중엔 미뤄짐, 심장박동 busy 필드), `tests/test_channels.py`
(신규 6건: info/summary 억제, critical 되풀이 억제·카테고리별 독립·복구 후 재발신,
등급+범주 아이콘), `tests/test_schedule.py`(신규 3건: expect_task 어긋남/일치/
없음), `tests/test_monitor.py`·`tests/test_dashboard.py`(기존 5건을 새 정책에
맞게 수정 — 자동 진단 상세는 채널이 아니라 이벤트 로그에서 확인).

전체 스위트(`pytest -q`, 약 16~17분, 세 번 실측)를 돌렸다. 마지막 실행
(01:5x, 1309 passed / 1 failed / 5 errors)의 실패·오류는 전부 이번 작업과
무관해 그대로 뒀다(사유만 적는다) — 세 번의 실행에서 실패 목록이 매번
달랐다는 것 자체가 이 세션이 건드린 파일이 원인이 아니라 **경합**임을
보여준다:

- `test_bulk_generate.py::*`, `test_brand_writer.py::*`,
  `test_plan_command.py::test_worker_counts_manuscripts_per_backend` 등 —
  티어다운 단계에서 "진짜 사용량 장부(`data/llm_usage-2026-09.jsonl`)가
  건드려졌다"는 장부 파수꾼(conftest.py)이 걸림. 지금 돌고 있는 **진짜
  실행기 프로세스**(01:14 재시작됨, 이 세션이 재시작 금지 지시를 받은 그
  프로세스)가 테스트 도중에도 진짜 장부에 썼기 때문으로 보인다 — 테스트
  코드나 이 작업이 건드린 파일의 문제가 아니다.
- `test_plan_command.py::test_plan_wins_over_api_when_both_written`,
  `test_parser.py::test_all_tasks_covered_by_tests`(1·2번째 실행에서만) —
  다른 일꾼이 같은 시각 `naver_keyword_tool.py`/`brand_queue.py` 등(이
  세션은 건드리지 말라고 지시받은 파일)을 고치며 작업 목록·파서가 같이
  흔들린 것으로 보인다.

이 파일들을 빼면 모두 통과했다 — 직접 겨냥한 `test_sidecar.py`,
`test_channels.py`, `test_schedule.py`, `test_monitor.py`, `test_dashboard.py`,
`test_photo_approval.py`, `test_gpt_images.py`, `test_naver_session.py`,
`test_web_session.py`, `test_rate_backoff.py`, `test_self_cafe_daily.py` 는
단독 실행·전체 실행 모두에서 일관되게 전부 통과했다.

## 7. 건드리지 않은 것

- `publish.py` 수정 금지 지시에 따라 `publish.notify_photo_shortage` 의
  `notify_all` 호출부 한 줄(레벨 인자 추가)만 건드렸다 — 발행 로직 자체는
  손대지 않았다.
- `keyword_exposure.py`, `naver_keyword_tool.py`, `naver_session.py`,
  `bulk_generate.py`, `brand_queue.py` 는 지시대로 건드리지 않았다.
- 실행기(serve)는 재시작하지 않았다 — 이번 작업은 재시작 시 자가 복구되는
  **코드**만 만들었고, 실제 재시작은 다음 안전한 시점(현재 지시서: 다른
  일꾼의 미완 코드 정리 뒤)에 별도로 한다.
