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

## 5차 — 공유 큐(작업자별 정렬·선점을 근본적으로 없앰)

배경: `docs/reports/exposure-speed-2026-09-24.md` 6-7·6-8절(실측 9) — 4차
(작업자 프로세스별 캐시 + TTL 600초 + 파일 기반 완료 표시)로 큐 조회는
9.8초 → 3.2초로 빨라졌지만, 중복 재검사가 오히려 13% → **19.4%**로 늘었다.
원인: 작업자 5개가 **각자 독립적으로** 정렬한 목록의 앞쪽을(같은 상위권
키워드) 동시에 고르는 경쟁이 됐기 때문 — DB 직전 확인(3차)이 있어도, "확인
→ 브라우저 열기 → 저장" 사이 수 초의 경쟁 구간까지는 못 막았다. 코디네이터
지시대로 **근본 설계를 바꿔** 작업자별 캐시·정렬을 없애고 공유 큐로
옮겼다.

### 바꾼 구조(5차)

1. **`exposure_queue` 표 신설** — `v2r/store/db.py`에 추가(brand,
   keyword_norm 복합 PK, keyword, item_json, tier, sort_key, enqueued_at,
   claimed_by, claimed_at, done_at). item 전체를 JSON으로 저장해 판정에
   필요한 부가 필드(카페·검색량·t0_status 등)를 잃지 않는다. 인덱스
   `idx_exposure_queue_pick(brand, done_at, claimed_by, tier, sort_key)`.

2. **정렬은 브랜드당 한 프로세스가 TTL마다 한 번만** —
   `v2r/knowledge/exposure_priority.py`의 `_do_refresh_queue`가 등급 규칙
   (`priority_tier`, 손 안 댐)으로 tier·`sort_key`(다중 정렬을 표 한 칸에
   담는 단일 실수, `_sort_key_for_tier`)를 계산해
   `exposure_queue_store.upsert_candidates`로 표에 갱신한다. `maybe_refresh_queue`
   가 브랜드별 갱신 간격(기본 `queue_refresh_sec` 600초, `exposure_queue.
   enqueued_at` 최댓값 기준)과 mkdir 기반 락(`data/
   exposure_queue_refresh.lock/<브랜드>`, 죽은 락은 60초 뒤 무시)으로 "락을
   잡은 아무 작업자 하나만" 갱신하게 한다. universe·최근 발행 집합은
   여전히 `_universe_bundle`(4차의 프로세스+파일 캐시)에서 가져와 시트
   CSV 재읽기를 줄인다.

3. **선점은 원자적 sqlite 트랜잭션 한 번** —
   `exposure_queue_store.claim_batch`가 `BEGIN IMMEDIATE` 안에서
   `SELECT ... WHERE done_at IS NULL AND (claimed_by IS NULL OR claimed_at
   < 컷오프) ORDER BY tier, sort_key LIMIT n`으로 rowid를 고르고, 그 자리에서
   바로 `UPDATE ... SET claimed_by=?, claimed_at=? WHERE rowid IN (...)`를
   커밋한다 — sqlite 쓰기 트랜잭션은 직렬화되므로 같은 rowid를 두 프로세스가
   동시에 못 고른다. `exposure_priority.next_priority_batch(rt, brand, n,
   worker_id)`는 이제 "갱신 필요하면 갱신 → 선점"의 얇은 래퍼다. 2단계
   확인 대기(`due_pending`) 재확인은 `claim_specific`(없으면 최우선 등급으로
   새로 넣고 바로 선점)으로 같은 표를 공유한다.
4. **검사 직전 DB 최신 검사 시각 확인은 유지** — `exposure_priority.
   is_due_now`(3차, `store.latest_for_keyword` 단건 조회)는 그대로 둬서,
   `WorkerQueue.take()`가 배치를 소비하는 순간까지 한 번 더 확인한다.
5. **파일 기반 `claim_inflight`/`complete_inflight`/`is_inflight`/
   `release_inflight` 완전 제거** — `v2r/knowledge/exposure_runner.py`에서
   삭제. 정상 완료는 `exposure_queue_store.mark_done`, 예외 실패는
   `release_claim`으로 대체(`run_worker`의 `try/except`). 중복률 기록
   (`process_one`의 `duplicate` 판정 → `update_worker_state`)은 그대로 유지.
6. `WorkerQueue`(작업자 메모리, 4차의 브랜드별 큐 구조)는 그대로 두되,
   `next_priority_batch`가 돌려주는 항목이 **이미 이 작업자 앞으로 선점
   끝난 상태**라 더 이상 자체적으로 `claim_inflight`를 부르지 않는다 —
   `is_due_now` 재확인만 남았다.
7. 우선순위 규칙(1 노출완 6시간 → 2 최근 발행 → 3 밀려남·미확인 검색량 순,
   12시간 간격)과 작업자의 브랜드 순환(중복 급증으로 브랜드 고정을 되돌린
   전례, 6-4절)은 이번에도 전혀 건드리지 않았다.

### 왜 이번엔 근본적으로 안전한가

이전 1~4차는 모두 "얼마나 빨리, 얼마나 자주 동기화하느냐"의 문제였다 —
캐시를 아무리 빨리 갱신해도 여러 프로세스가 **동시에 읽고 나중에 따로
쓰는** 구조라면, 읽기와 쓰기 사이의 경쟁 구간은 절대 0이 될 수 없다.
5차는 "정렬 결과를 어디에 두고 선점을 어떻게 하느냐"를 바꿔 — 여러
프로세스가 **하나의 sqlite 표에 원자적 트랜잭션으로** 선점하므로, 그
트랜잭션이 커밋되는 순간 다른 프로세스는 이미 사라진 rowid를 볼 수밖에
없다. 경쟁 자체가 구조적으로 없다(같은 파일에 대한 sqlite의 쓰기
직렬화가 보장).

### 시험 결과(5차)

- `tests/test_exposure_runner.py` 47개, `tests/test_keyword_exposure.py` +
  `tests/test_keyword_exposure_cycle.py` + `tests/test_dashboard.py` 합쳐
  98개 — **145개 전부 통과**. `v2r/store/db.py`에 `exposure_queue` 표를
  추가한 뒤 신규 임시 DB에서 `init_schema`가 정상 생성하는지도 별도 확인.
  옛 `claim_inflight`/`release_inflight`/`complete_inflight` 시험은
  `exposure_queue_store.claim_specific`/`release_claim`/`mark_done`/
  `claim_batch` 기반으로 다시 썼고(`test_claim_specific_먼저_잡은쪽만_성공`,
  `test_release_claim_후_다시_잡을수있음`, `test_claim_만료되면_다시_잡을수있음`,
  `test_mark_done_후에는_claim_batch에_안뽑힘`, `test_claim_batch_동시선점_경쟁없음`
  — 마지막은 두 "작업자"가 같은 후보를 동시에 `claim_batch`해도 한쪽만
  가져가는지 확인), `next_priority_batch`가 이제 선점까지 하므로 관련
  시험도 이 동작에 맞춰 갱신했다(`test_next_priority_batch_공유큐_선점이_같은키워드_다시안뽑음`,
  `test_next_priority_batch_mark_done_후에도_큐갱신TTL안이면_다시_안뽑힘`).
- 전체 `pytest` 스위트는 **미실행**(다른 일꾼이 동시에 돌리고 있어 관련
  시험만 통과 확인).

## 6차 — 시트 1만 행대에서 처리량 급락(52건/시)·중복률 35% 원인 확정

배경: 08:13 재시작(5차+`b393cd4`의 백그라운드 정렬 갱신 반영) 후 09:50
확인 — 작업자 6개 각 52건/시(합 312건/시, 실측 11의 560건/시에서 급락),
상태 파일 `duplicate_rate` 15%→35%. 그 사이 별도 일꾼의 "시트 키워드 반영"
작업이 우아덤 4,959→12,549행, 코숨핏 254→4,854행으로 시트를 키웠다(비대상
키워드까지 붙는 결함은 그쪽에서 별도로 정리 중, 이 조사와는 무관).

### 확인 결과 (코디네이터 지시 순서대로)

**(1) 공유 큐 갱신이 universe 12k+에서 몇 초 걸리고 작업자를 얼마나 막는지.**
`b393cd4`(07:49, 이번 재시작 전에 이미 반영)가 큐 갱신을 백그라운드
스레드로 옮겨 둬서, 갱신 자체는 더 이상 그 작업자의 검사 루프를 동기로
막지 않는다(로그의 "타이밍 공유큐조회"는 `갱신=False`만 찍히는데, 이는
백그라운드로 던지면 호출자 입장에서 "동기 갱신은 안 했다"는 뜻이라
의도된 표시다 — 버그 아님, 오해하기 쉬워 본문에 명시). 다만 갱신 자체(그
스레드가 여는 별도 sqlite 연결의 쓰기 트랜잭션)는 브랜드당 1만 행대에서
여러 초 걸릴 수 있고, sqlite는 파일 하나에 동시 쓰기 트랜잭션을
직렬화하므로(WAL이라도) 그 사이 **다른 작업자**(다른 브랜드 포함)의
`claim_batch`/`mark_done`/`release_claim`이 대기할 수 있다 — 아래 수정
(청크·워터마크)으로 이 트랜잭션 자체를 줄였다.

**(2) 중복률 상승 원인.**
- *"done 항목이 다시 미완료로 되살아나는지"*: `upsert_candidates`의
  `ON CONFLICT` 절을 다시 확인 — `done_at IS NOT NULL`인 행은 이번 갱신
  후보에 다시 들어올 때만(=등급 규칙상 실제로 재검사 주기가 지나
  `priority_tier`가 `tier<99`를 준 경우만) 선점 상태가 초기화된다. 이는
  **의도된 "쿨다운이 끝나 다시 대상이 됨"** 동작이지 버그가 아니다.
  다만 `_do_refresh_queue`가 `last_checked`를 **캐시**(`last_checked_cache_sec`
  기본 20초)에서 읽고 있어, 방금(몇 초~20초 전) 완료된 검사가 갱신 시점의
  등급 계산에 아직 안 보일 좁은 창이 이론상 있었다 — 수정했다(아래 3번).
- *"claimed_at 180초 만료로 재선점되는지(검사 1건이 느려져 180초를
  넘기는지)"*: **이게 진짜 원인이었다.** 아래 3번에서 밝힌
  `write_exposure_csv` 문제로 같은 작업자의 연속 검사 사이 간격 중앙값이
  90~120초(최대 226초)까지 늘어나 있었다 — 실제 검색+판정 자체는 여전히
  평균 9~10초인데 그 사이·직후에 매번 낀 동기 CSV 재작성이 시간을 잡아먹은
  것. 이 지연이 `CLAIM_TTL_SECONDS`(180초)에 가까워지거나 넘어가면, 아직
  같은 작업자가 붙잡고 있는(처리 중인) 키워드를 다른 작업자가 "죽은
  선점"으로 오판해 재선점 → 같은 키워드를 두 작업자가 동시에 검사하는
  진짜 중복이 났다. 3번 수정으로 검사 간격이 정상(10~15초대)으로 돌아오면
  이 경로의 중복도 함께 준다.
- *"검사 직전 DB 확인이 실제로 동작하는지"*: 코드 재확인 —
  `WorkerQueue.take()`가 배치에서 항목을 꺼낼 때마다 여전히
  `exposure_priority.is_due_now`(`store.latest_for_keyword` 단건, 인덱스)를
  부르고 있다. 3~5차에서 이 경로를 건드린 적이 없고, 별도 시험
  (`test_is_due_now_직전확인`, `test_worker_queue_직전확인에서_걸리면_다음후보`)
  도 여전히 통과한다 — 정상 동작 확인.

**(3) 작업자당 건당 소요 분해 — 어디서 느려졌는가.**
6개 작업자 로그(`logs/exposure-runner-0~5.log`, 09:30~09:52 구간)를
실제로 집계했다:

| 구간 | 평균 | 최대 | 비고 |
|---|---:|---:|---|
| 자동완성 | 0.50s | — | 정상 |
| 스크롤(카페 카드) | 5.97s | 12.16s | 정상 범위(설계 목표 안) |
| 후보 확인 | 3.23s | 23.85s | 정상 범위 |
| **같은 작업자의 연속 검사 사이 간격(중앙값)** | **90~120s** | **226s** | **비정상 — 여기가 병목** |

자동완성·스크롤·확인 세 구간 합은 여전히 평균 9~10초로 "네이버 응답이
느려졌다"는 가설과 안 맞는다. `data/locks/sheet-*.lock` 잠금 대기 경고
("시트 반영 실패... 시트 잠금 대기 시간 초과")도 로그에 반복됐지만,
`_enqueue_sheet_row`의 시트 배치 반영은 원래부터 별도 스레드(`threading.
Thread`)라 이 경고 자체는 메인 검사 루프를 막지 않는다 — "시트 쓰기 잠금
대기" 가설도 기각. 실제 원인은 `_finalize_row`가 검사 **1건마다 동기로**
`v2r/knowledge/keyword_exposure.write_exposure_csv`를 불렀던 것 —
`keyword_universe` 전체 재순회 + `store.latest_by_keyword` 전체 이력 쿼리
+ 최대 1만 행 정렬 + CSV 전량 재작성을 매번 메인 스레드에서 했다. 시트가
작을 때는 안 보이던 비용이 브랜드당 1만 행대에서 초 단위(때로 수십 초)
병목이 됐다 — 90~120초 간격과 맞아떨어진다.

### 바꾼 구조(6차)

1. **`write_exposure_csv` 스로틀 + 백그라운드화** —
   `v2r/knowledge/exposure_runner.py`에 `maybe_write_exposure_csv(brand,
   min_interval_sec=60)` 추가. `_finalize_row`가 매 건 동기 호출하던 걸
   이걸로 바꿨다 — 브랜드당 60초(기본)에 한 번만, 그것도 전용 `Runtime`을
   연 별도 스레드(`_write_exposure_csv_in_background`, 5차의
   `_refresh_queue_in_background`와 같은 패턴)에서 돈다.
   `write_exposure_csv`(`v2r/knowledge/keyword_exposure.py`) 자체는 손대지
   않았다 — 얼마나 자주·어느 스레드에서 부르느냐만 바꿨다.
2. **`exposure_queue` 갱신 청크·워터마크 삭제** —
   `v2r/store/exposure_queue_store.py`의 `upsert_candidates`를 (a) 후보를
   500개씩(`UPSERT_CHUNK_SIZE`) 나눠 여러 개의 짧은 트랜잭션으로 커밋하고
   (b) "이번 후보에 없는 옛 행 삭제"를 `NOT IN (수천 개)` 대신 이번 갱신
   시작 시각보다 `enqueued_at`이 오래된 행을 지우는 워터마크 방식으로
   바꿨다 — SQL 크기가 후보 수와 무관해 1만 행대에서도 일정하다. 진행
   중인 선점은 여전히 안 건드린다(단위 시험으로 확인).
3. **`_do_refresh_queue`가 `last_checked` 캐시를 건너뜀** — 갱신 직전
   `invalidate_last_checked_cache`를 불러 항상 방금 커밋된 값을 읽는다.
   갱신이 드물어(기본 600초) 캐시 이점이 거의 없고, "방금 완료된 검사가
   갱신에 안 보이는" 좁은 창을 없앤다.
4. 우선순위 규칙·공유 큐 원자적 선점 구조(5차)·`is_due_now` 직전 확인은
   전혀 건드리지 않았다. `jobs` 표는 건드리지 않았다.

### 예상 효과(6차)

- 매 건 1만 행 CSV 재작성이 사라지면, 검사 간격이 다시 (설계 목표대로)
  10~15초대로 돌아올 것으로 본다 — 이론상 작업자 6개 기준 시간당
  1,000건대까지 가능(지연 3~5초 + 검사 9~10초 기준). 실제로는 브랜드
  순환·2단계 확인 대기 등이 더해지므로 이보다는 낮게 나올 것.
- 간격이 정상화되면 `CLAIM_TTL_SECONDS`(180초)를 넘는 처리 중 재선점도
  사라져, `duplicate_rate`가 다시 5차 수준(1.4~13%대)으로 돌아올 것으로
  기대한다.
- `exposure_queue` 갱신 트랜잭션이 짧아지면 다른 작업자의 DB 쓰기 대기도
  줄어든다.
- 정확한 수치는 재시작 후 재실측 필요 — 이번에도 코드·단위 시험·로그
  집계까지만 했고 이 변경을 반영한 운영 실측치는 아직 없음.

### 시험 결과(6차)

- `tests/test_exposure_runner.py` 53개, `tests/test_keyword_exposure.py` +
  `tests/test_keyword_exposure_cycle.py` + `tests/test_dashboard.py` 합쳐
  99개 — **152개 전부 통과**. 새로 추가: `maybe_write_exposure_csv`
  스로틀·브랜드별 독립, `upsert_candidates`가 청크로 나눠도 전부 반영되는지,
  워터마크 삭제가 안 쓰인 옛 행은 지우되 진행 중인 선점은 안 지우는지,
  `_do_refresh_queue`가 매번 `invalidate_last_checked_cache`를 부르는지.
- 전체 `pytest` 스위트는 **미실행**(다른 일꾼이 동시에 돌리고 있어 관련
  시험만 통과 확인). `v2r/__main__.py`·`v2r/engine/sidecar.py`·
  `v2r/store/jobs.py`·`tests/test_sidecar.py`는 다른 일꾼이 같은 저장소에서
  동시에 수정 중이라(git status로 확인) 이번 커밋에 포함하지 않았다 —
  지시대로 `jobs` 표는 건드리지 않았다.

## 7차 — `duplicate_rate` 정의 수정 + 10:20 이후 창 실제 중복률 실측

배경: 10:20 재시작(6차 반영, `write_exposure_csv` 스로틀 등) 후 30분 —
작업자당 90~135건/시(합 약 630건/시), 큐 조회 0.18초로 크게 좋아졌다. 그런데
상태 파일 `duplicate_rate`가 29%(68/234)로 여전히 높게 나왔다 — 이 지표가
"오늘 앞선 라운드(여러 번 재시작한 실측 세션)에서 본 키워드"까지 중복으로
세는 정의였기 때문에(6-5절에서 이미 지적) 운영 지표로 못 쓴다는 문제가
그대로 남아 있었다.

### (1) `duplicate_rate` 정의 수정

- **`duplicate`(운영 지표)** — "이 러너 실행(작업자 프로세스가 실제로
  시작된 시각 `run_started_at` 이후) 안에서 같은 (브랜드, 키워드)를 두 번
  이상 검사했는가"로 다시 정의했다. `run_worker`가 시작할 때
  `run_started_at = datetime.now(timezone.utc)`를 한 번 재고(상태 파일에
  남아 있을 수 있는 예전 `started_at`은 재사용 안 함), `process_one`에
  넘긴다. 직전 검사 행(`prev_row`)이 있고 그 `checked_at`이
  `run_started_at` 이후면 중복(`_is_duplicate_since_run_start`) —
  재시작 이전 검사는 `prev_row`가 있어도 안 센다. `run_started_at`을 안
  주면(옛 호출 경로 호환) 항상 `False`.
- **`min_gap_violation`(새 지표, 별도)** — "그 순간 등급 규칙상 실제로
  재검사 대상이었는지"를 `exposure_priority.priority_tier`를 그대로
  재사용해 판정한다(`_min_gap_violation`). 옛 `_is_duplicate_recheck`처럼
  문턱값(6시간/12시간)만 비교하지 않고, **2등급(최근 발행)은 설계상
  원래 최소 간격이 없다는 것까지 반영**한다 — 안 그러면 이 지표도
  똑같이 부풀려진다(아래 실측 참고). `update_worker_state`가
  `duplicate`·`min_gap_violation`을 같은 호출에서 받아 같은
  `checked_count`(분모)를 공유해 집계한다(`data/exposure_runner_state.json`
  `workers.<n>.min_gap_violation_rate`, `duplicate_totals.
  min_gap_violation_rate`).

### (2) 10:20 이후 창 기준 실제 중복 — DB로 직접 셈

`data/v2r.sqlite`의 `keyword_exposure`를 `checked_at >= '2026-09-24T10:20:00'`
으로 직접 집계했다(별도 스크립트, 커밋 안 함):

| 지표 | 값 |
|---|---:|
| 이 창의 총 검사 | 357건 |
| 고유 (브랜드,키워드) | 333쌍 |
| 2번 이상 검사된 쌍 | 21쌍 |
| 중복(초과) 검사 수 | 24건 |
| **`duplicate`(새 정의) 비율** | **6.72%**(24/357) |

**3%를 넘어 원인을 추적했다.** 중복된 21쌍의 상세(정확히 같은 키워드
텍스트, 상태는 전부 `pushed`, 재검사 간격은 20~50분)를 실제로
`exposure_priority.priority_tier`(그 순간 실제 `recent_norm`·`last_checked`
포함)에 다시 넣어 등급을 재계산했다:

| 원인 후보 | 확인 결과 |
|---|---|
| claim TTL(180초) 만료로 재선점 | **기각** — 재검사 간격이 20~50분으로 180초보다 훨씬 길다(6차로 처리 속도가 정상화돼 더는 근접 안 함) |
| 큐 갱신 시 done이 잘못 리셋됨 | **기각** — 리셋은 `priority_tier`가 실제로 tier<99(재검사 대상)를 준 경우에만 일어나고, 그 계산 자체는 정확했다 |
| **2등급(최근 발행) 키워드는 설계상 재검사 간격이 없음** | **확인됨 — 31건의 "추가 검사" 중 27건(87%)이 `recent_norm`에 포함된 2등급 키워드였다.** 나머지 4건만 등급상 실제로 아직 대상이 아니었는데 재검사된 진짜 `min_gap_violation`(장으뜸 "배란점액 임신"·"화학적유산 시기", 코숨핏 "역류성식도염약"·"캠핑매트") |

→ **`min_gap_violation` 비율은 4/357 = 1.12%로 3% 미만.** 즉 겉보기
`duplicate_rate`(6.72%)가 높아 보였던 진짜 이유는 코드 버그가 아니라
**"최근 발행 키워드는 빠르게 반복 확인"이라는 우선순위 규칙 2등급 자체가
원래 최소 간격 없이 자주 재검사하도록 설계돼 있기 때문**이었다(1절
설계 문서 그대로, 이번에도 안 바꿨다). `duplicate`(엄격한 "이 실행 안에서
두 번째냐"는 정의)는 이 정상 동작까지 세므로 6.72%로 나오지만,
등급 규칙을 반영한 `min_gap_violation`은 진짜 이상만 걸러 1.12%로
나온다 — **3% 밑이라 추가 코드 수정은 필요 없다는 결론.** 나머지 4건
(1.12%)은 발생 빈도가 낮아(각 1회) 개별 조사 없이 지켜보기로 한다.

### 시험 결과(7차)

- `tests/test_exposure_runner.py` 57개, `tests/test_keyword_exposure.py` +
  `tests/test_keyword_exposure_cycle.py` + `tests/test_dashboard.py` 합쳐
  99개 — **156개 전부 통과**. 옛 `_is_duplicate_recheck`/`duplicate` 관련
  시험 2개를 `min_gap_violation` 기준으로 다시 썼고, 새로 추가:
  `run_started_at` 이전/이후 경계로 `duplicate` 판정이 갈리는지, 2등급
  (최근 발행) 키워드는 `min_gap_violation`이 거짓인지, `update_worker_state`가
  `duplicate`·`min_gap_violation`을 같이 넘겨도 분모(`checked_count`)를
  이중으로 안 세는지.
- 전체 `pytest` 스위트는 **미실행**(다른 일꾼이 동시에 돌리고 있어 관련
  시험만 통과 확인).

## 8차 — 2등급(최근 발행) 재검사 간격 도입 + 오탐 원천 제거

배경: 7차 실측(10:20 이후 창) — 초과 검사 31건 중 27건(87%)이 2등급(최근
발행) 키워드였다. 코디네이터 지시: (1) 2등급도 창(2h/6h/24h)마다 최대
1회·최소 간격 90분을 두고, (2) "최근 발행" 판정이 오늘 발행된 **자사 카페
일상 글**(브랜드 키워드와 무관)까지 잘못 잡는지 원천을 밝혀 오탐이면
브랜드 태그·시트 F열 일치만 인정하도록 고치라는 지시.

### (2) 먼저 원천 확인 — 오탐 2건 확인

`_recent_publish_keywords`(당시)는 두 경로의 합집합이었다:

- **(a) `article_index` "제목 맨 앞 낱말" 휴리스틱.** `article_index`
  (`v2r/store/article_index.py`)는 "그 카페에 올라간 모든 글"을 담는
  중복 방지용 색인 — V2R이 발행한 글이든, **자사 카페 일상 글**이든, 이
  시스템 이전에 수동으로 올린 글이든 **브랜드 태그가 전혀 없이** 섞여
  있다. 오늘 발행된 일상 글의 제목 맨 앞 낱말이 우연히 universe의 어떤
  키워드와 겹치면 그 키워드가 그대로 "최근 발행"으로 잡혔다 — **구조적
  오탐 경로**, 코드로 브랜드 여부를 가릴 방법 자체가 없었다.
- **(b) `publications` 표 × 시트 F열 URL 대조.** 이 경로는 시트 F열과
  대조하니 브랜드 키워드 매칭 자체는 맞았지만, **DB 조회에 브랜드 필터가
  없었다**(`_publications_recent_article_ids`가 `WHERE status IN (...)`만
  걸고 `source_key` 조건이 없음) — 다른 브랜드의 발행은 물론, 자사 카페
  일상 글(`v2r.engine.publish.brand_source_keys`가 이미 "브랜드 시트
  이름이 아닌 `source_key`"로 정의해 둔, 카페 활성화용 필러 글) 발행
  기록까지 전부 후보에 들어갔다가, F열 URL이 우연히 안 겹쳐서 대부분
  걸러졌을 뿐 — 그래도 **잠재적 오탐 경로**였다.

**둘 다 고쳤다:**

1. **(a) 완전 제거.** `article_index` "제목 맨 앞 낱말" 휴리스틱을
   `_recent_publish_keywords`에서 뺐다 — 이제 (b) 경로(브랜드 태그 +
   시트 F열 일치)만 쓴다.
2. **(b) 브랜드 필터 추가.** `_publications_recent_article_ids`를
   `_publications_recent_pub_times`로 바꾸며 `WHERE source_key = ?`(이
   브랜드)를 추가했다 — `source_key`는 브랜드 키워드 원고를 발행할 때만
   브랜드 이름 그 자체를 쓴다(`brand_source_keys`가 이미 이 기준으로
   "일상 글이 아닌 것"을 정의해 뒀다, `v2r/engine/publish.py`). 이제 이
   브랜드의 실제 키워드 원고 발행만 "최근 발행"의 씨앗이 된다.

### (1) 2등급 재검사 간격 도입

`priority_tier`의 2등급 분기를 다시 짰다(등급 규칙 파일 자체,
`v2r/knowledge/exposure_priority.py`는 손대도 되는 이 모듈이 맞다 — 판정
규칙 원본인 `keyword_exposure.py`는 여전히 안 건드렸다):

- `recent_publish_norm`이 이제 `{정규화 키워드: 발행 시각}` 매핑이다
  (예전엔 `set[str]`) — "지금이 어느 창인지"·"그 창에서 이미 봤는지"를
  판정 시점(`now`)마다 새로 계산하기 위해서다. 수집 함수
  (`_recent_publish_keywords_from_db`)는 창 판정을 안 하고 발행 시각만
  넉넉히(가장 긴 창+여유) 모아 돌려준다 — 창 로직은 전부
  `priority_tier` 한 곳에만 있다.
- 발행 시각 기준 지금 경과 시간이 2h/6h/24h(설정
  `priority.recent_publish_hours`) 중 하나의 ±2시간(`priority.
  recent_publish_window_hours`, 기본 기존과 동일) 안이면 "활성 창".
  활성 창이 없으면 2등급이 아니라 3등급(밀려남·미확인) 규칙으로 넘어간다.
- 활성 창이 있어도 **최소 간격 90분**(`priority.
  recent_publish_min_gap_hours`, 기본 1.5시간)이 안 지났으면 99등급(제외).
- 90분은 지났어도, **직전 검사가 같은 창 범위 안**(예: 지금 6시간 창이면
  직전 검사도 발행 후 4~8시간 사이)이었으면 그 창은 이미 썼으므로
  99등급 — 다음 창(또는 3등급 규칙)까지 기다린다.
- `queue_refresh_sec` 갱신 시 계산되는 것도, `is_due_now`(검사 직전 DB
  단건 확인)에서 계산되는 것도 같은 `priority_tier` 한 함수라 규칙이
  어긋날 일이 없다.

### 바뀌지 않은 것

우선순위 등급 순서(1 노출완 → 2 최근 발행 → 3 밀려남·미확인) 자체, 등급
1·3의 재검사 규칙(6시간/12시간), 브랜드 순환, 공유 큐 원자적 선점(5차)은
이번에도 그대로다. `keyword_exposure.py`(판정 규칙 원본)도 호출만 했다.

### 시험 결과(8차)

- `tests/test_exposure_runner.py` 63개, `tests/test_keyword_exposure.py` +
  `tests/test_keyword_exposure_cycle.py` + `tests/test_dashboard.py` 합쳐
  99개 — **162개 전부 통과**. `_publications_recent_article_ids`/
  `_recent_publish_keywords_from_db`/`_recent_publish_keywords` 관련 옛
  시험을 새 반환 타입(딕셔너리)·브랜드 필터에 맞춰 다시 썼고, 새로 추가:
  최근 발행 창 활성 시 2등급(`test_priority_tier_최근발행이_2순위`), 최소
  간격 90분 안 제외, 같은 창에서 이미 검사했으면 제외, 다른 창이면 다시
  대상, 창 밖이면 2등급이 아니라 3등급으로 넘어가는지, `article_index`
  휴리스틱이 결과에 더는 안 섞이는지(오탐 2건 확인용), 다른
  브랜드(source_key)의 발행은 "최근 발행"에 안 섞이는지.
- 전체 `pytest` 스위트는 **미실행**(다른 일꾼이 동시에 돌리고 있어 관련
  시험만 통과 확인).

## 9차 — 21시간 가동 실측: 작업자 사망 + 선점 만료 중복

배경: 11:23(09-24) 재시작본이 21시간 연속 가동. 09-25 08:20 상태 파일
`duplicate_rate`(7·8차 새 정의)가 작업자별 0.12~0.69, `rate_per_hour`가
157~266으로 튀었다 — "중복이 처리량을 부풀린다"는 지적대로 실제로는
문제였다.

### (1) 이 실행 안 실제 중복률·등급·간격 분포 (DB 직접 집계)

`checked_at >= 2026-09-24T20:20:00`(작업자 시작 시각 근방, KST)로
`keyword_exposure`를 직접 집계했다(스크립트, 커밋 안 함):

| 지표 | 값 |
|---|---:|
| 이 구간 총 검사 | 6,951건 |
| 고유 (브랜드,키워드) | 6,044쌍 |
| 2회 이상 검사된 쌍 | 866쌍 |
| 중복(초과) 검사 | 907건 |
| **중복률** | **13.05%**(907/6,951) |

재검사 간격(초과 검사 907건) 분포:

| 간격 | 건수 | 해석 |
|---|---:|---|
| <6분(대부분 0~3초) | 209 | **이상 — 아래 원인 2 참고** |
| 3~7시간(중앙값 6.25h) | 698 | 1등급(노출완, 6시간 재검사) 정상 순환 |
| 그 외 | 0 | 가동 12시간 남짓이라 12h/24h 순환은 아직 안 돎 |

77%(698/907)는 1등급 6시간 재검사가 정상적으로 도는 것 —
`min_gap_violation_rate`가 0.16~0.53%로 낮게 유지된 것과 일치한다(등급
규칙 자체는 어긋나지 않음). 문제는 **209건, <6분(대부분 0~3초) 안 재검사**
였다.

### (2) 원인 확정 — 후보 4개 중 2개 확인

로그(`logs/exposure-runner-0~5.log`)를 실제로 대조했다:

| 후보 | 결과 |
|---|---|
| 공유 큐 갱신(600초)마다 done이 미완료로 되살아남 | **기각** — `upsert_candidates`의 `ON CONFLICT` 케이스(진행 중 선점 보존)는 그대로 맞게 동작(6차에서 이미 확인). 등급 재계산도 정확(`min_gap_violation` 낮음). |
| 1·2등급 재검사 창이 장시간 가동에서 반복 | **부분 확인, 정상** — 698건(77%)이 이 경로지만 6시간 간격이 정확해 **버그 아님**(순환 설계 그대로 동작). |
| `exposure_queue` 표 비대(행 수·인덱스) | **기각** — 21시간 가동에도 브랜드당 수천 행 수준, `idx_exposure_queue_pick` 인덱스로 `claim_batch` 쿼리는 여전히 밀리초대(로그 "타이밍 공유큐조회"로 확인). |
| **`claimed_at` 180초 만료로 재선점** | **확인됨 — 다만 원인은 더 근본적이었다.** 209건의 <6분 재검사를 실제로 로그에서 대조하니, **서로 다른 두 작업자**(작업자 1·2)가 **같은 키워드를 0~1초 차이로 동시에 검사**하고 있었다(예: "원적외선 좌욕기" 22:38:07.309 vs 22:38:07.785). 이건 순수한 180초 만료 타이밍이 아니라, **작업자 6개 중 4개(0·3·4·5)가 가동 1~2시간 만에 죽어 있었다**(아래) — 남은 작업자 1·2만 21시간 내내 일하며 배치(10개)를 순서대로 처리하는 동안, 뒤쪽 항목의 선점이 180초를 넘겨 서로의 몫을 재선점하는 경쟁이 반복된 것. |

**작업자 4개 사망 — 더 근본적인 원인.** `logs/exposure-runner-{0,3,4,5}.log`
꼬리를 보니:

- 작업자 3·4: **`sqlite3.OperationalError: database is locked`**로
  크래시(잡히지 않은 예외 → 프로세스 종료). `busy_timeout`(5000ms)을 넘는
  쓰기 잠금 경합이 있었다는 뜻.
- 작업자 0·5: Playwright/Node 쪽 `EPIPE`로 크래시(별개 원인, 이번 조사
  범위 밖 — 브라우저 프로세스 종료 관련, 노출 판정 로직과 무관).

`database is locked`의 진짜 원인을 추적했다: 6차에서 추가한 백그라운드
스레드 두 개(정렬 큐 갱신 `_refresh_queue_in_background`, CSV 갱신
`_write_exposure_csv_in_background`)가 매번 **완전히 새
`Runtime.open()`**을 여는데, `Runtime.open()`은 항상 `init_schema`
(`migrate` + `executescript`)를 다시 돌린다(`v2r/engine/context.py`).
CSV 갱신은 브랜드당 최대 60초에 한 번, 정렬 갱신은 브랜드당 최대 600초에
한 번 — 작업자 6개 × 브랜드 5개가 다 살아 있으면 분당 최대 30번씩 각자
새 연결로 스키마를 다시 만들려 든 셈이다. `executescript`가 암묵적으로
잡는 트랜잭션이 다른 연결의 `BEGIN IMMEDIATE`(`claim_batch`/`mark_done`
등)와 자주 부딪혀, 5초 안에 안 풀리면 `OperationalError`로 그 프로세스가
죽었다.

### (3) 수정

1. **`Runtime.open(skip_schema_init=True)` 추가**(`v2r/engine/context.py`)
   — 스키마는 메인 작업자가 시작할 때 이미 만들어 뒀으므로, 백그라운드
   스레드의 짧은 수명 `Runtime`은 다시 만들 필요가 없다.
   `exposure_priority._open_refresh_runtime`·`exposure_runner.
   _open_csv_runtime` 둘 다 이 플래그로 열도록 바꿨다 — 매번
   `executescript`를 돌리던 걸 없애 "database is locked" 경합의 빈도를
   크게 줄인다.
2. **`exposure_queue_store.renew_claim` 추가 + `WorkerQueue.take()`에
   연결** — 배치에서 항목을 실제로 넘기기(검사 시작) 직전에 그 항목의
   `claimed_at`을 "지금"으로 되돌린다. 배치 뒤쪽 항목이 앞쪽 항목들
   처리하느라 180초를 넘겨도, 실제로 넘겨지는 순간 선점 창이 다시
   180초로 리셋되므로 다른 작업자가 재선점할 수 없다 — 이번 사고(두
   작업자가 같은 키워드를 초 단위로 동시 검사)를 구조적으로 막는다.
   자기 선점이 아니게 됐으면(이미 다른 작업자가 가져갔으면) 갱신하지
   않고 넘어간다(`is_due_now`가 이어서 확인).
3. 우선순위 등급 규칙, 공유 큐 원자적 선점(5차), `duplicate`/
   `min_gap_violation` 지표 정의(7차)는 그대로다.

### 시험 결과(9차)

- `tests/test_exposure_runner.py` 68개(관련 파일) — 통과. `Runtime.open`·
  `exposure_queue_store`·`test_engine`·`test_keyword_exposure`·
  `test_keyword_exposure_cycle`·`test_dashboard` 관련 227개(중복 제외 실제
  합산 기준 227개 파일 합) 실행 — **전부 통과**. 새로 추가:
  `skip_schema_init=True`가 `init_schema`를 다시 안 부르는지(스파이로
  확인), `renew_claim`이 내 선점만 갱신하고 다른 작업자 선점은 안
  건드리는지, 선점이 이미 만료돼 다른 작업자가 가져간 뒤엔 원래 작업자의
  `renew_claim`이 실패하는지, `WorkerQueue.take()`가 항목을 돌려주기
  직전에 `renew_claim`을 실제로 부르는지.
- 전체 `pytest` 스위트는 **미실행**(다른 일꾼이 동시에 돌리고 있어 관련
  시험만 통과 확인). `tests/test_keyword_relevance.py`·
  `v2r/knowledge/keyword_fill_loop.py`·`v2r/knowledge/keyword_relevance.py`
  는 다른 일꾼이 동시에 수정 중이라(git status로 확인) 이번 커밋에
  포함하지 않았다.

### 러너 재시작(지시대로 1회) + 30분 뒤 재실측

원인 수정 커밋·푸시(`7d072e4`) 후, 살아 있던 작업자 1·2를 포함해 기존
`exposure_runner` 프로세스를 모두 정지하고 `scripts\exposure-runner-
hidden.vbs 6`으로 작업자 6개를 새로 띄웠다(08:42:05 KST 09-25). 30분
뒤(09:12 KST) DB(`keyword_exposure`)를 `checked_at >= 2026-09-25T08:42:05+09:00`
로 직접 집계했다 — 상태 파일(`data/exposure_runner_state.json`)의
`duplicate_rate`/`rate_per_hour`는 재시작 후에도 같은 작업자 ID(0~5) 아래
21시간치 누적 카운터가 그대로 이어져(파일이 프로세스 재시작으로 안
지워짐) 이번 30분만의 수치를 못 보여주므로, DB 직접 집계만 신뢰했다:

| 지표 | 재시작 전(21시간 누적, 9차 (1)) | **재시작 후 30분** |
|---|---:|---:|
| 총 검사 | 6,951건(약 12시간 구간) | **273건** |
| 고유 쌍 | 6,044 | **273** |
| 중복 쌍 | 866 | **0** |
| 중복(초과) 검사 | 907 | **0** |
| **중복률** | 13.05% | **0.00%** |
| <6분 안 재검사(진짜 이상 신호) | 209 | **0** |

30분 동안 273건 처리 = 약 **546건/시**(작업자 6개 합산, 3~5차 목표치인
517~700건/시 범위 안). 같은 30분 사이 작업자 2·3의 프로세스 PID가
바뀐 걸 확인했다(watchdog 또는 자체 재시작으로 추정) — 그래도 죽은 채
방치되지 않고 다시 붙어 계속 처리했다는 뜻이고, 그 짧은 재시작 구간에도
DB 기준 중복은 0건이었다.

**결론: 9차 수정(스키마 재초기화 제거 + 선점 갱신)으로 중복률이 13.05%
→ 0%로, `<6분 안 재검사`(진짜 사고 신호)가 209건 → 0건으로 없어졌다.**
처리량도 21시간 누적치에 가려 있던 실제 순간 처리량(546건/시)이 확인됐다
— 6차~8차가 목표로 한 범위 안에 든다.

## 러너 재시작 필요 → 9차에서 재시작 완료

1~9차 변경 모두 코드에 반영됐고, 9차 원인 수정 커밋 뒤 지시대로 1회
재시작했다(위 절 참고) — 이 항목은 9차부로 해소.
