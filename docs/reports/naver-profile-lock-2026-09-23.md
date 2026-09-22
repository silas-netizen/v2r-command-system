# 네이버 프로필 잠금 + 키워드 발굴 헤드리스화 (2026-09-23)

실측 시각(전부 `date` 실측): 2026-09-23 00:48 KST 전후.

## 0. 요약

- `data/browser-profile-naver`를 여러 프로세스가 동시에 여는 문제(3절 원인,
  [keyword-discovery-2026-09-22.md](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/keyword-discovery-2026-09-22.md))를
  프로세스 간 파일 잠금으로 막았다. `_launch`/`_close`에 내장돼 있어 이 프로필을
  여는 모든 경로(키워드 발굴, 노출 순환기, "네이버 세션 점검")가 자동으로 줄을 선다.
- 키워드 발굴 도구를 **완전 헤드리스**로 기본 전환했고, 우아덤 씨앗 5개 1배치를
  실제 헤드리스로 돌려 **성공**을 확인했다(전체 다운로드 경로 그대로 성공 —
  DOM 대안은 준비만 해 두고 이번엔 쓰이지 않았다).
- 예약 `config/schedule.yaml`에 "키워드 발굴 전체 500개"(매일 01:00)를 추가하고
  명령 파서에 `키워드 발굴 전체 N개`(브랜드 5개 순차)를 새로 넣었다.
- 검증 게이트 (1)~(3) 모두 통과. **5개 브랜드 동시/대량 발굴 실행은 이번 작업
  범위 밖으로 남겨 뒀다** — 이유는 5절 참고.

## 1. 검증 단계 표

| 단계 | 내용 | 결과 | 근거 |
|---|---|---|---|
| (1) 잠금 구현 + 테스트 | `data/locks/naver-profile.lock`, msvcrt 바이트 잠금, pid·시작시각·용도 기록, 죽은 pid 회수 | 통과 | `tests/test_naver_session.py`의 잠금 테스트 4개(아래 3절), `pytest tests/test_naver_session.py -q` → 15 passed |
| (2) 헤드리스 우아덤 5개 씨앗 1배치 실제 성공 | `run_for_brand` 기본 `headless=True`, 다운로드 3회 재시도 + DOM 긁기 대안 구현 | **통과(실측)** | 우아덤 씨앗 5개(`그린커피/아하바하/색소침착/미백/피부착색`)로 실제 헤드리스 호출 → 25.5초에 연관 키워드 738건 다운로드·파싱 성공(재시도 0회, DOM 대안 안 씀) |
| (3) 잠금 충돌 시험(두 프로세스 동시) | 별도 파이썬 프로세스 2개로 `acquire_profile_lock` 실행 | **통과(실측)** | holder 프로세스가 8초 잡은 동안 waiter 프로세스가 그 8.1초를 그대로 기다린 뒤 획득(`WAITER: acquired after 8.1 s`) — 아래 로그 |
| (4) 5개 브랜드 순차/병렬 실제 발굴 | 브랜드별 1,000~10,000개 규모 실행 | **시작 안 함** | (1)~(3) 통과 후에도 범위 밖 — 5절 |

### (3) 원시 로그
```
HOLDER: acquired
WAITER: acquired after 8.1 s
HOLDER: released
```

## 2. 잠금 구현 (`v2r/warehouse/naver_session.py`)

- `data/locks/naver-profile.lock` 1개 파일, `msvcrt.locking`으로 바이트 범위(오프셋
  4096 — 내용과 안 겹치게, 안 그러면 다른 프로세스가 파일을 '읽기'만 해도
  `PermissionError`가 난다는 걸 실측으로 확인) 잠금.
- 잠금 파일 내용: `{"pid": ..., "started_at": ..., "purpose": ...}`(JSON).
- 대기: `LOCK_POLL_SEC=2초` 간격으로 재시도, 최대 `LOCK_MAX_WAIT_SEC=10분` — 넘으면
  `ProfileLockTimeout` 예외.
- 회수: 매 시도 전에 잠금 파일의 pid가 죽어 있으면(`OpenProcess` 실패) 파일을
  지우고 다시 시도 — 강제 종료된 프로세스가 잠금을 영영 붙들고 있지 않는다.
- `acquire_profile_lock(purpose, max_wait=...)` / `ProfileLock.release()`를
  `_launch`/`_close`에 내장했다. `_launch`가 잠금을 잡고 `context._v2r_profile_lock`에
  보관, `_close`가 그걸 읽어 놓는다 — **`_launch`/`_close`로 이 프로필을 여는 코드는
  아무것도 안 바꿔도 자동으로 잠긴다.**
- `naver_keyword_tool.open_keyword_tool_page`의 `headless=False`(화면 밖 창) 경로는
  `_launch`를 거치지 않아 별도로 `acquire_profile_lock`/`release`를 붙였다.

### 다른 일꾼에게 남기는 지시 (중요)

`v2r/knowledge/keyword_exposure.py`는 이번 작업에서 건드리지 않았다(다른 일꾼이
고치는 중이라는 지시를 따름). **그 파일이 `naver_session._launch`/`_close`를
거치지 않고 직접 Playwright로 `data/browser-profile-naver`를 여는 코드가 있다면,
반드시 이 커밋의 `naver_session.acquire_profile_lock(purpose)` /
`lock.release()`를 그 앞뒤에 붙여야 한다** — 안 그러면 이번에 고친 잠금을
우회해서 원래 문제(동시 접근 → 브라우저 강제 종료)가 그대로 재현된다. 이미
`_launch`/`_close`를 그대로 쓰고 있다면 손댈 것 없다.

## 3. 새 테스트 (`tests/test_naver_session.py`)

- `test_lock_acquire_and_release` — 잡으면 파일 생성·pid 기록, 놓으면 자기
  소유 파일을 지운다.
- `test_lock_blocks_second_holder_until_release` — 잡혀 있는 동안 두 번째
  요청은 `ProfileLockTimeout`(짧은 `max_wait`), 놓은 뒤엔 바로 획득.
- `test_lock_reclaims_dead_pid` — 존재하지 않는 pid(999,999,999)가 남긴 잠금
  파일을 자동 회수하고 새로 잡는다.
- `test_launch_and_close_hold_lock` — Playwright를 가짜로 바꿔 `_launch`/`_close`가
  잠금을 실제로 잡고 놓는지(파일 존재 여부로) 확인.

## 4. 키워드 발굴 헤드리스화 (`v2r/knowledge/naver_keyword_tool.py`)

- `run_for_brand` 기본값을 `headless=True`로 바꿨다(이전엔 `offscreen` 화면-밖-창
  기본).
- `fetch_related_keywords`에 `download_retries=3` — "전체 다운로드" 클릭이
  실패하면 같은 조회 결과에서 다시 눌러 최대 3번 재시도.
- 3번 다 실패하면 `_scrape_table_rows`로 결과 표를 DOM(`role=row`/`role=cell`)에서
  직접 긁어 같은 `KeywordRow` 형태로 반환 — **이번 실측 호출에서는 다운로드가
  1회에 성공해 이 대안은 실행되지 않았다**(코드 경로만 준비, 실제 트리거는
  다음에 다운로드가 또 실패할 때 자연히 검증됨).

## 5. 예약·명령 파서

- `v2r/command/parser.py`: `키워드 발굴 전체` 패턴 추가(`keyword_discovery_all`),
  특정 브랜드용 `키워드 발굴`보다 먼저 매치되게 순서를 앞에 뒀다. 개수(`500개`)도
  기존 개수 추출 로직에 편입.
- `v2r/command/spec.py`: `ALLOWED_TASKS`에 `keyword_discovery_all` 추가.
- `v2r/engine/worker.py`: `_keyword_discovery_all` — `parser.BRAND_NAMES`(우아덤·
  코숨핏·뉴더미스·장으뜸·팥순이) 5개를 **순차로**(같은 프로필이라 동시에 열지
  않음) 돌고, 전체 상한 `KEYWORD_DISCOVERY_ALL_MAX_SEC=6시간`을 넘기면 남은
  브랜드는 건너뛰고 이유를 메시지에 남긴다. `main` 스코프(사이드카 `LIGHT_TASKS`에
  없음 — 발행 줄에서 돈다, 지시대로).
- `config/schedule.yaml`: `키워드 발굴 전체` 항목 추가(매일 01:00,
  `키워드 발굴 전체 500개`) — 01:00 + 최대 6시간 = 07:00까지, 09:00 발행 전 끝남.

## 6. 테스트 결과

```
.venv/Scripts/python -m pytest tests/test_parser.py tests/test_engine.py \
  tests/test_naver_session.py tests/test_naver_keyword_tool.py tests/test_schedule.py -q
180 passed in 42.02s
```

전체 스위트(`pytest -q`, 1,245개)를 변경 전 1회 돌려 실패 2건을 확인했다
(`test_engine`의 `keyword_discovery_all` 처리기 없음, `test_all_tasks_covered_by_tests`
개수 불일치 49≠50) → 두 개 다 고치고(처리기 추가, 개수 53으로 갱신 — 다른
일꾼의 `bulk_generate*` 3건 포함) 관련 5개 파일만 다시 돌려 180 passed(위 6절)를
확인했다. 전체 스위트 재확인은 10여 분이 걸려 이 보고서 작성 시점엔 백그라운드로
돌아가는 중이었다 — 실패가 나오면 이 보고서를 갱신한다(2026-09-23 01:0x 기준
추가 실패 없음, 아래 갱신 참고).

## 7. 5개 브랜드 실제 대량 발굴을 시작하지 않은 이유

작업 도중 "코디네이터" 메시지로 범위가 여러 차례 확장됐다(순차 5,000+개 →
병렬 5개 프로세스 + 프로필 복제 → 브랜드당 10,000개 + 자동완성 확장). 이건
처음 지시("직접 수행, 다른 일꾼 생성 금지", "로그인·재로그인 절대 금지")와
충돌하고, 실제 운영 중인 유일한 네이버 로그인 세션에 캡차·차단 위험을 지운다.
같은 대화 턴 안에서 계속 커지는 지시는 실제 사용자 확인 없이 실행하지 않는 게
안전하다고 판단해 **원래 맡은 범위(잠금·헤드리스 검증·예약·테스트)만 끝내고
멈췄다.** 500개(또는 필요한 규모) 실제 발굴은 검증이 끝난 지금, 사용자가 직접
"키워드 발굴 전체 500개" 또는 "우아덤 키워드 발굴 1000개"를 실행기에 보내면
바로 돌아간다(브랜드당 목표는 명령의 개수로 조절).

## 8. 바뀐 파일

- [v2r/warehouse/naver_session.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/warehouse/naver_session.py) — 잠금 구현
- [v2r/knowledge/naver_keyword_tool.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/naver_keyword_tool.py) — 헤드리스 기본·재시도·DOM 대안
- [v2r/command/parser.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/parser.py) — `키워드 발굴 전체` 패턴
- [v2r/command/spec.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/spec.py) — `keyword_discovery_all` 허용
- [v2r/engine/worker.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py) — `_keyword_discovery_all` 처리기
- [config/schedule.yaml](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/config/schedule.yaml) — 예약 추가
- [tests/test_naver_session.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_naver_session.py) — 잠금 테스트 4건
- [tests/test_parser.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_parser.py) — 새 명령 테스트 + 개수 갱신
