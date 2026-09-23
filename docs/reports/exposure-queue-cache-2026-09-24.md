# 노출 확인 큐 캐시 — 2026-09-24

배경: `docs/reports/exposure-speed-2026-09-24.md` 6절 — 러너가 검사 1건마다
`exposure_priority.next_priority_batch(rt, brand, n)`를 부르고, 그 함수가
매번 `keyword_universe(rt, brand)`(시트 실시간 CSV + 키워드 DB 전량 읽기)를
새로 읽어 건당 평균 9.3초(최대 24초)가 들던 문제.

## 바꾼 구조

1. `v2r/knowledge/exposure_priority.py`에 브랜드별 `_UNIVERSE_CACHE`(프로세스
   메모리, 모듈 전역) 추가.
   - `keyword_universe(rt, brand)` 결과 + 최근 발행 집합(`_recent_publish_keywords`,
     시트 F열·DB `publications` 대조 포함) + 검색량 임계값을 한 번에 계산해
     "번들"로 TTL(기본 120초, `config/exposure.yaml` `priority.universe_cache_sec`)
     동안 유지한다.
   - `store.latest_by_keyword`(DB, 브랜드 1건 그룹핑 쿼리)는 가벼워 캐시하지
     않고 `next_priority_batch`·`queue_counts` 모두 매번 새로 읽는다 — 그래서
     이 작업자가 방금 저장한 검사 결과는 캐시와 무관하게 항상 바로 다음
     조회에 반영된다(기존 시험
     `test_next_priority_batch_같은_키워드_연속_두번_안뽑힘`이 그대로 통과함으로
     확인).
   - 캐시 키는 `(repo_root, brand)` — 작업자는 프로세스가 분리돼 있어(브라우저
     1개당 프로세스 1개) 캐시가 서로 섞이지 않는다. 같은 브랜드를 여러
     작업자가 동시에 읽어 같은 순서의 목록을 받아도, 실제 중복 검사는 이미
     있던 `claim_inflight`(파일 기반 선점)가 그대로 막는다 — 캐시를 추가해도
     이 보장은 바뀌지 않는다(단위 시험으로 확인).
   - `invalidate_universe_cache(rt, brand=None)` 제공(전체 비우기도 가능) —
     발행/키워드 시트가 방금 바뀐 걸 알 때 수동으로 부를 수 있게 열어 뒀지만,
     매 검사 저장마다 자동으로 부르지는 않는다(그러면 배치 캐시 효과가 사라짐).

2. `v2r/knowledge/exposure_runner.py`에 `WorkerQueue` 클래스 추가.
   - 기존에는 매 반복마다 `next_priority_batch(rt, brand, n=5)`를 불러 그중
     `claim_inflight` 성공하는 첫 항목만 쓰고 나머지는 버렸다.
   - 이제 배치(`config priority.batch_size`, 기본 10)를 한 번에 받아 작업자
     메모리 큐에 쌓아 두고, `take()`가 하나씩 꺼내며 `claim_inflight` 실패한
     항목은 건너뛴다(다른 작업자가 먼저 집은 것).
   - 큐가 비거나(선점 실패로 다 소진됐거나) TTL(`priority.universe_cache_sec`와
     동일 값 사용)이 지나면 `next_priority_batch`를 다시 부른다. 브랜드가
     바뀌어도 새로 조회한다.
   - `run_worker`의 메인 루프에서 `worker_queue.take(rt, brand, worker_id)`로
     교체. "타이밍 우선순위조회" 로그는 배치 조회 시점에만 남기도록 유지.

3. 우선순위 등급 규칙(`priority_tier`, `next_priority_batch`의 정렬 로직)은
   전혀 건드리지 않았다 — 1 노출완(6시간) → 2 최근 발행 → 3 밀려남·미확인
   (검색량 순, 12시간 간격) 그대로. "타이밍" 로그 태그도 그대로 유지.

## 예상 절감(건당 초)

- 기존: 검사 1건당 `next_priority_batch` 호출 1회 = `keyword_universe`
  풀 읽기(시트 CSV + 키워드 DB) 1회. 실측 평균 9.3초(최대 24초).
- 변경 후: `keyword_universe` 풀 읽기는 브랜드당 TTL(120초) 동안 최대 1회.
  작업자 1개가 배치 10개를 다 소비하는 데 걸리는 시간(검색 8.5초 기준 약
  85–140초, 대기시간 포함)이 TTL보다 길므로, 대부분의 배치 주기에서
  `keyword_universe` 호출은 **배치당 1회**로 줄어든다(실측 9.3초 기준으로는
  건당 약 0.9–1초 이하로 상각 — 10배 안팎 절감). `store.latest_by_keyword`는
  여전히 건당 1회 읽지만 단일 그룹핑 쿼리라 병목이 아니었다(원 보고서에서도
  "가볍다면 매번 읽어도 된다"로 조건부 지시함).
- 실제 절감폭은 운영 중 "타이밍 우선순위조회" 로그(sheet_read=)를 배치 전/후로
  비교해 확인 필요 — 이번 변경은 코드 검증(단위 시험)까지만 완료했고 운영
  실측치는 아직 없음.

## 시험 결과

- `tests/test_exposure_runner.py`, `tests/test_keyword_exposure.py`,
  `tests/test_keyword_exposure_cycle.py` 3개 파일 직접 실행 — **117개 전부
  통과**(223.88초). 새로 추가한 시험: universe 캐시 TTL 안 재조회 안 함,
  TTL 지나면 재조회, 브랜드별로 캐시 분리, last_checked는 캐시와 무관하게
  즉시 반영(같은 키워드 연속 재선택 안 됨), `invalidate_universe_cache`
  수동 무효화, `WorkerQueue` 배치 소비·소진 시 재조회·TTL 만료 시 재조회·
  선점 실패 항목 건너뛰기.
- 전체 `pytest` 스위트는 **미실행**(다른 일꾼이 동시에 돌리고 있어 코디네이터
  지시로 관련 시험만 실행·통과 확인 후 중단).

## 2차 — last_checked·정렬 결과 캐시 (같은 날 재실측 후)

재실측 결과(`docs/reports/exposure-speed-2026-09-24.md` 6-1·6-2절): universe
캐시 후에도 "타이밍 우선순위조회" 건당 9.9초(장으뜸 8초, 팥순이 18.7초)로
개선이 없었다. 원인은 `next_priority_batch`가 매 호출마다 (1)
`store.latest_by_keyword(rt.conn, brand)` 전체 이력 쿼리와 (2) universe
1만 개 이상을 순회하며 `priority_tier`·정렬을 다시 했기 때문(1차에서는 이
두 단계를 캐시하지 않았음).

### 바꾼 구조(2차)

1. **구간별 타이밍 로그** — `next_priority_batch`가 "타이밍 우선순위조회"
   로그에 정렬캐시 조회 시간과 필터(후보별 재확인) 시간을 나눠 남긴다
   (`v2r/knowledge/exposure_priority.py`). 러너 쪽 배치 조회(라운드트립)는
   태그를 "타이밍 큐조회"로 분리해 겹치지 않게 했다
   (`v2r/knowledge/exposure_runner.py` `WorkerQueue.refill_if_needed`).

2. **last_checked(DB) TTL 캐시** — `_LAST_CHECKED_CACHE`(브랜드별, 기본
   20초, `config/exposure.yaml` `priority.last_checked_cache_sec`). 러너가
   검사 결과를 DB에 저장한 직후 새 함수 `exposure_priority.mark_checked(rt,
   brand, keyword, status, checked_at)`를 불러 그 키워드 한 건만 캐시에 바로
   갱신한다(`exposure_runner._finalize_row`에 연결). 그래서 TTL이 안 지났어도
   방금 이 작업자가 검사한 키워드는 즉시 반영돼 같은 키워드가 연속으로
   다시 뽑히지 않는다. DB 인덱스는 이미 `idx_keyword_exposure_brand
   (brand, keyword, checked_at)`가 있어(`v2r/store/db.py`) 추가 인덱스는
   필요 없었다.

3. **정렬 결과 TTL 캐시** — `_sorted_candidates`가 등급(`priority_tier`)
   매기기·정렬을 universe와 같은 TTL(기본 120초) 동안 캐시해 둔다(전체
   순회·정렬은 캐시 갱신 시점에만). `next_priority_batch`는 캐시된 정렬
   목록 앞에서부터, 최신 `last_checked`로 재확인해 이미 재검사 주기가 안
   지난 항목(`tier>=99`)과 `claim_inflight` 중인(다른 작업자가 선점) 항목만
   건너뛰고 n개를 채운다 — 후보 하나하나만 가벼운 재확인이라 1만 개 전체
   재순회보다 훨씬 싸다.

4. **배치 실제 소비 확인** — `WorkerQueue`가 다음 배치를 받기 전에 직전
   배치에서 몇 개를 실제로 소비했는지(`_consumed`)·`claim_inflight` 실패로
   버린 개수(`_claim_failed`)를 "타이밍 큐소비" 로그로 남긴다. 또한
   `next_priority_batch` 자체가 이제 배치를 만들 때 이미 `is_inflight`인
   키워드를 먼저 제외하므로(3번 항목), 다른 작업자가 선점한 키워드가 애초에
   배치에 안 들어가 버려지는 일이 줄어든다.

5. 우선순위 등급 규칙(1 노출완 6시간 → 2 최근 발행 → 3 밀려남·미확인 검색량
   순, 12시간 간격)은 이번에도 그대로다 — `priority_tier` 함수 자체는
   손대지 않았다.

### 2차 설계상 유의점

`mark_checked`를 부르지 않고 `store.save`만 직접 하는 호출 경로는 최대
`last_checked_cache_sec`(기본 20초) 동안 그 저장이 배치 조회에 안 보일 수
있다 — 정상 경로(`exposure_runner._finalize_row`)는 항상 둘을 같이 부르므로
문제 없다. 이 동작은 단위 시험
`test_last_checked_캐시_TTL안에서는_mark_checked없으면_stale`로 의도된
설계임을 확인해 뒀다.

### 예상 절감(2차, 건당 초)

- 재실측 기준 9.9초(장으뜸 8초, 팥순이 18.7초)의 나머지가 대부분
  `latest_by_keyword` 쿼리 + 1만 개 순회·정렬이었다고 보고, 이제 둘 다
  브랜드당 TTL 동안(20초/120초) 최대 1회만 실행된다. 배치(기본 10개) 소비에
  걸리는 시간이 TTL보다 길면 대부분의 조회는 "정렬캐시 히트 + 후보 몇 개만
  재확인"으로 끝나 순회·정렬 비용이 사실상 0에 수렴한다. 정확한 개선폭은
  "타이밍 우선순위조회"(정렬캐시=·필터=)와 "타이밍 큐조회"(round_trip=) 로그를
  재실측해 확인 필요 — 이번에도 코드·단위 시험까지만 완료했고 운영 실측치는
  아직 없음.

### 시험 결과(2차)

- `tests/test_exposure_runner.py` 39개, `tests/test_keyword_exposure.py` +
  `tests/test_keyword_exposure_cycle.py` 합쳐 79개 — **3개 파일 118개 전부
  통과**. 1차 시험(`test_next_priority_batch_같은_키워드_연속_두번_안뽑힘`,
  `test_universe_캐시_last_checked는_캐시와_무관하게_즉시반영`)은 새 설계에
  맞춰 `mark_checked` 호출을 추가해 갱신했고(이름도
  `test_universe_캐시_last_checked는_mark_checked로_즉시반영`으로 변경),
  `mark_checked` 없이는 TTL 안에서 stale임을 확인하는 시험을 새로 추가했다.
- 전체 `pytest` 스위트는 **미실행**(다른 일꾼이 동시에 돌리고 있어 관련
  시험만 통과 확인).

## 3차 — 작업자 프로세스 간 캐시 불일치로 인한 중복 재검사 수정

배경: `docs/reports/exposure-speed-2026-09-24.md` 6-4절 실측 — `exposure_priority`의
universe·정렬·last_checked 캐시가 **작업자 프로세스별 메모리**(모듈 전역
dict)라, 한 작업자의 검사 결과(`mark_checked`)는 그 작업자 자신의 캐시만
갱신하고 다른 작업자 프로세스의 캐시는 그대로였다. 브랜드를 자주 바꿀 때는
가려져 있었지만(캐시가 재사용되기 전에 다음 브랜드로 넘어감), 브랜드에 오래
머물게 하자(de22603) 작업자 5개가 각자 정체된 캐시로 같은 상위권 키워드를
반복해서 다시 뽑아 267건 중 180건(67%)이 중복 재검사였다. 브랜드 고정을
되돌린 뒤(ab32ce5)에도 중복 13%는 남았다 — claim_inflight는 "동시" 선점만
막을 뿐, TTL(120초) 동안 여러 번 **순차로** 다시 뽑히는 건 못 막았기 때문.

### 바꾼 구조(3차)

1. **검사 직전 DB 단건 확인 — 캐시와 무관하게 항상 수행.**
   `v2r/store/keyword_exposure_store.py`에 `latest_for_keyword(conn, brand,
   keyword)`(단건, 기존 `idx_keyword_exposure_brand(brand, keyword,
   checked_at)` 인덱스로 밀리초) 추가. `exposure_priority.next_priority_batch`는
   이제 배치에 넣을 후보마다(정렬 캐시가 준 순서를 그대로 쓰되) 이 단건
   조회로 최소 간격 규칙(노출완 6시간, 밀려남·미확인 12시간)을 다시 확인—
   캐시된 `last_checked` 맵을 더 이상 이 최종 판정에 쓰지 않는다. 새 함수
   `exposure_priority.is_due_now(rt, brand, item, now=None)`도 추가해
   `exposure_runner.WorkerQueue.take()`가 큐에서 항목을 꺼낼 때(실제로
   브라우저를 열기 직전) 한 번 더 같은 확인을 한다 — 정렬 캐시 구성 시점과
   실제 소비 시점 사이(배치 10개를 다 쓰는 데 최대 TTL만큼 걸릴 수 있음)의
   간극을 막는다. 걸리면 선점을 풀고(`release_inflight`) 다음 후보로 넘어간다.
   2단계 확인 대기(`due_pending`) 경로는 원래 예외이므로 이 확인을 거치지
   않는다.

2. **완료 표시(`complete_inflight`)** — `exposure_inflight.json` 항목에
   `completed_at`을 추가했다. 검사가 정상 끝나면(`run_worker`) 기존처럼
   선점을 바로 지우지 않고(`release_inflight`) `complete_inflight`로
   `completed_at`만 남긴다. `is_inflight`가 `completed_at` 기준으로도
   `INFLIGHT_TTL_SECONDS`(180초) 동안 "선점 중"으로 본다 — 이 TTL이
   `priority.universe_cache_sec`(기본 120초, 정렬 캐시 TTL)보다 크므로 별도
   설정 없이 "다른 작업자의 정렬 캐시가 만료되기 전까지 제외" 요건을
   만족한다. 검사가 예외로 실패했을 때는 `release_inflight`로 완전히 지워
   다른 작업자가 바로 다시 집을 수 있게 했다(완료가 아니므로).

3. **정렬 캐시에서 자를 때 둘 다 통과한 것만** — `next_priority_batch`의
   후보 필터 순서를 (1) DB 직전 확인(위 1번) → (2) `is_inflight`(진행 중 +
   완료 표시, 위 2번) 순으로 둬서, 두 조건을 모두 통과한 항목만 배치에
   담긴다.

4. **중복률 기록** — `exposure_runner.process_one`이 검사 시작 전에 읽어 둔
   직전 행(`prev_row`)과 이번 결과의 시간 차로 "이번 검사가 최소 간격
   규칙보다 먼저 같은 키워드를 다시 본 것인지"(`_is_duplicate_recheck`,
   2단계 확인 대기 재확인은 예외)를 판정해 반환값에 `duplicate`(bool)로
   담는다. `run_worker`가 이 값을 `update_worker_state(..., duplicate=...)`로
   넘기면 `data/exposure_runner_state.json`에 작업자별
   `checked_count`/`duplicate_count`/`duplicate_rate`와 전체
   `duplicate_totals`(`checked_count`/`duplicate_count`/`duplicate_rate`)가
   쌓인다. 참고: `duplicate_totals`는 작업자 프로세스 여럿이 같은 파일을
   잠금 없이 갱신하므로(기존 설계 — 작업자별 키는 안 겹쳐 괜찮지만 이
   합계는 전역 공유 키) 동시 쓰기가 겹치면 드물게 소폭 과소 집계될 수
   있다 — 감시용 지표로는 충분하나 정확한 감사 수치가 필요하면 DB
   `keyword_exposure.checked_at` 간격을 직접 집계하는 편이 정확하다.

5. 우선순위 등급 규칙은 이번에도 전혀 건드리지 않았다.

### 예상 절감/효과(3차)

- 목표는 속도가 아니라 **정확성**(중복 재검사 근절)이다 — 13%(브랜드 고정
  시 67%)의 헛수고를 없애면 그만큼 유효 처리량이 늘어난다(같은 시간에 실제로
  더 많은 고유 키워드를 검사).
- DB 단건 조회는 인덱스가 있어 밀리초 단위이므로(이미 존재하던
  `idx_keyword_exposure_brand`), `next_priority_batch` 필터 루프에 후보당
  1회씩 추가돼도 "타이밍 우선순위조회" 로그의 "DB직전확인=" 카운트로 비용을
  확인할 수 있게 로그에 남긴다(기존 로그에 필드 추가).
- 실제 중복률 개선폭은 `data/exposure_runner_state.json`의
  `duplicate_totals.duplicate_rate`를 재시작 후 실측해 확인 필요 — 이번에도
  코드·단위 시험까지만 완료했고 운영 실측치는 아직 없음.

### 시험 결과(3차)

- `tests/test_exposure_runner.py` 46개, `tests/test_keyword_exposure.py` +
  `tests/test_keyword_exposure_cycle.py` 합쳐 79개, `tests/test_dashboard.py`
  19개(`keyword_exposure_store` 변경 영향 확인차 추가 실행) — **3개 관련
  파일 + 대시보드 총 144개 전부 통과**. 2차 시험 중
  `test_universe_캐시_last_checked는_mark_checked로_즉시반영`은 이제
  `mark_checked` 없이도 DB 직전 확인이 즉시 반영함을 확인하는
  `test_next_priority_batch_mark_checked_없이도_DB직전확인으로_즉시반영`으로
  바꿨다(3차 설계가 캐시 의존을 없앴으므로). 새로 추가: `is_due_now` 단위
  동작, `complete_inflight` 완료 표시·TTL 동작, `WorkerQueue`가 직전 확인에
  걸린 후보를 건너뛰고 선점을 풀어 주는지, `update_worker_state` 중복률
  집계, `process_one`이 최소 간격 안/밖 재검사를 각각 `duplicate` True/False로
  판정하는지.
- 전체 `pytest` 스위트는 **미실행**(다른 일꾼이 동시에 돌리고 있어 관련
  시험만 통과 확인).

## 4차 — 캐시가 TTL 안에 재사용되지 못한 진짜 원인 확정·수정

배경: `docs/reports/exposure-speed-2026-09-24.md` 6-5·6-6절 — 3차 후에도 "타이밍
우선순위조회"(당시 이름, 지금은 "타이밍 큐조회")의 round_trip이 평균 9.8초
(최대 25.7초)로 개선이 로그에 안 보였다. `logs/exposure-runner-0~4.log`(실측
8 구간, 06:21–06:37)를 실제로 집계해 원인을 확정했다.

### 로그 집계 표

| 작업자 로그 | 큐조회(round_trip) n / 평균 / 최대 | CSV export HTTP 요청 수 | "타이밍 큐소비" 중 "소비=1"(=9개 버림) 비율 |
|---|---:|---:|---:|
| exposure-runner-0.log | 26 / 9.86s / 23.02s | 119 | 26/26 (100%) |
| exposure-runner-1.log | 26 / 9.47s / 22.19s | 100 | 26/26 (100%) |
| exposure-runner-2.log | 26 / 9.80s / 22.96s | 114 | 26/26 (100%) |
| exposure-runner-3.log | 27 / 10.04s / 25.74s | 129 | 26/26 (100%) |
| exposure-runner-4.log | 27 / 10.26s / 23.13s | 130 | 26/26 (100%) |

`exposure-runner-0.log`에서 브랜드별 "타이밍 큐조회" 재방문 간격(같은 브랜드로
돌아오기까지 걸린 시간)을 직접 계산:

| 브랜드 | 재방문 횟수 | 평균 간격 |
|---|---:|---:|
| 뉴더미스 | 6 | 184.6초 |
| 우아덤 | 5 | 177.5초 |
| 장으뜸 | 5 | 182.9초 |
| 코숨핏 | 5 | 184.6초 |
| 팥순이 | 5 | 190.1초 |

### 원인 확정

4개 예상 후보 중 **(a) 브랜드 매번 전환이 지배적 원인**임을 로그로 확정했다:

- **(a) 확정.** 작업자가 5개 브랜드를 매 반복 바꿔 도는데(브랜드 고정은
  중복 급증으로 이미 되돌림, 6-4절), 같은 브랜드로 돌아오기까지 평균
  177–190초 걸렸다 — 당시 `universe_cache_sec` 기본값 **120초보다 항상
  길어** 캐시가 거의 매번 만료돼 있었다. 게다가 3차의 `WorkerQueue`는
  브랜드 하나짜리 큐만 갖고 있어서, 브랜드가 바뀔 때마다(사실상 매번)
  배치 10개 중 1개만 쓰고 나머지 9개를 버렸다 — "타이밍 큐소비" 로그가
  5개 파일 전부 "소비=1 ... 남은채로재조회=9"로 **100%** 일치했다. 배치
  캐시가 사실상 전혀 작동하지 않고 있었다.
- **(b) 기각.** `mark_checked`/`complete_inflight`가 universe·정렬 캐시를
  무효화하는 코드는 3차 이후에도 없었다(코드 확인 — 둘 다 last_checked
  캐시나 선점 파일만 건드리고 `_UNIVERSE_CACHE`/`_SORTED_CACHE`는 손대지
  않는다). 원인 아님.
- **(c) 기각.** 로그의 "타이밍 우선순위조회" 줄(3차부터 있던 "필터=" 필드,
  이번에 DB 확인 시간만 따로 뗀 "DB직전확인=")을 보면 후보 10여 건의 DB
  단건 확인이 수 ms~수십 ms 수준이었다 — sqlite 잠금 대기로 느려진 흔적
  없음. 원인 아님(사소한 기여조차 측정 안 됨).
- **(d) 부분 확정, (a)의 결과물.** CSV export HTTP 요청이 "타이밍 큐조회"
  1회당 평균 4.4–4.8건(리다이렉트 포함) 찍혔다 — `keyword_universe`(시트
  본문)와 `_recent_publish_keywords_from_db`(F열 대조용)가 **캐시 미스 때마다
  각각 별도로** 시트를 읽기 때문(2회 논리적 읽기 × 리다이렉트 1회 = 4회).
  이 자체는 (a)로 캐시가 거의 항상 미스라서 매번 일어난 것 — (a)를 고치면
  자동으로 줄어들지만, 작업자 5개가 각자 별도 프로세스라 캐시가 살아 있는
  동안에도 서로의 결과를 못 쓰는 문제는 남아 별도로 고쳤다(아래 2번).

### 바꾼 구조(4차)

1. **구간별 타이밍 로그 세분화** — `next_priority_batch`의 로그를
   "정렬=…(hit=True/False 재계산=…)", "universe=process_cache|file_cache|live(…)",
   "DB직전확인=…(N건)", "선점확인=…"으로 나눴다(`v2r/knowledge/exposure_priority.py`
   `_UNIVERSE_BUNDLE_META`/`_SORTED_CACHE_META` 곁가지 딕셔너리로 값을 넘겨
   함수 시그니처는 그대로 뒀다). `WorkerQueue`의 "타이밍 큐조회"(round_trip)는
   그대로 유지.
2. **`WorkerQueue`를 브랜드별 큐로 재구성** — 이전엔 큐 하나(`self.items`/
   `self.brand`)만 있어 브랜드가 바뀌면 통째로 버렸다. 이제 `_by_brand:
   dict[브랜드, _BrandQueueState]`로 브랜드마다 배치·TTL·소비 통계를 따로
   들고 있어, 5개 브랜드를 순환해도 각 브랜드의 배치 10개가 실제로 다
   소비될 때까지 유지된다(단위 시험으로 확인 — 10라운드×5브랜드를 돌려도
   브랜드당 `next_priority_batch` 호출이 정확히 1번).
3. **`universe_cache_sec` 기본값 120→600초** — 정확성(중복 재검사 방지)은
   이제 `next_priority_batch`의 후보별 DB 직전 확인(3차)과 `is_due_now`가
   캐시 나이와 무관하게 지키므로, 이 캐시는 순전히 "정렬 순서·최근 발행
   판단"의 신선도에만 영향을 준다 — 600초로 늘려도 안전하다.
4. **작업자 프로세스 간 공유 파일 캐시** — `v2r/store/keyword_exposure_store.py`는
   손대지 않고, `exposure_priority._universe_bundle`에 2단 캐시를 추가했다:
   (1) 프로세스 메모리(기존), (2) `data/exposure_universe_cache/<브랜드>.json`
   (mtime 기준 신선도, `priority.universe_file_cache_sec` 기본 600초) — 한
   작업자가 이미 읽어 둔 시트를 다른 4개 작업자 프로세스가 네트워크 없이
   재사용한다. 파일은 pid로 고유한 임시 이름 + `os.replace`로 원자적으로
   쓴다(상태 파일 크래시 교훈 재사용). `keyword_universe`/`target_keywords`
   (`v2r/knowledge/keyword_exposure.py`)는 여전히 호출만 하고 건드리지
   않았다 — 캐시는 그 호출을 감싸는 바깥쪽에서만 한다.
5. 우선순위 등급 규칙은 이번에도 전혀 건드리지 않았다.

### 예상 효과(4차)

- 배치 재사용이 정상화되면(브랜드당 10개 소비) "타이밍 큐조회" 빈도가
  약 1/10로 줄고, 그 각각도 대부분 process_cache 히트(hit=True, 0.000s에
  가까움)가 될 것으로 예상 — 실측 8의 회당 9.8초는 사실상 매번 캐시
  미스였던 결과이므로, 미스 빈도가 줄면 평균이 크게 낮아질 것으로 본다.
- 그래도 미스가 나는 경우(TTL 600초 지남, 또는 이 작업자가 그 브랜드를
  처음 보는 경우)에도 파일 캐시가 있으면 네트워크 없이 로컬 파일 읽기로
  끝나 훨씬 빠르다.
- 정확한 개선폭(회당 평균·최대, 파일 캐시 히트율)은 재시작 후 새 로그의
  "정렬=…(hit=…)", "universe=file_cache|process_cache|live(…)" 필드로
  재실측 필요 — 이번에도 코드·단위 시험·과거 로그 집계까지만 했고, 이
  변경 자체를 반영한 운영 실측치는 아직 없음.

### 시험 결과(4차)

- `tests/test_exposure_runner.py` 49개, `tests/test_keyword_exposure.py` +
  `tests/test_keyword_exposure_cycle.py` + `tests/test_dashboard.py` 합쳐
  98개 — **147개 전부 통과**. 2차의 TTL 관련 시험 2개
  (`test_universe_캐시_TTL지나면_다시_조회`, `test_invalidate_universe_cache_비우면_다시조회`)는
  새로 생긴 파일 캐시가 간섭하지 않도록 `_read_universe_file_cache`를
  일시적으로 끄고(순수 프로세스 캐시 동작만 확인), TTL도 새 기본값(600초)
  기준으로 갱신했다. 새로 추가: `WorkerQueue`가 브랜드를 매번 바꿔도 각
  브랜드 배치를 다 소비할 때까지 재조회하지 않는지(10라운드×5브랜드 →
  브랜드당 호출 1회), universe 파일 캐시가 프로세스 메모리 캐시 없이도
  (다른 프로세스인 것처럼 흉내내) `keyword_universe`를 다시 안 부르는지,
  `universe_cache_sec` 기본값이 600인지.
- 전체 `pytest` 스위트는 **미실행**(다른 일꾼이 동시에 돌리고 있어 관련
  시험만 통과 확인).

## 러너 재시작 필요

1차·2차·3차·4차 변경(캐시·배치 큐·last_checked 캐시·정렬 캐시·DB 직전 확인·
완료 표시·중복률 집계·브랜드별 큐·TTL 600초·작업자 간 공유 파일 캐시) 모두
코드에만 반영됐고 현재 돌고 있는 러너 프로세스에는 적용되지 않았다 — 러너
재시작 필요.
