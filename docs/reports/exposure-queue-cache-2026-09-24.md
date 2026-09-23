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

## 러너 재시작 필요

이번 변경(캐시·배치 큐)은 코드에만 반영됐고 현재 돌고 있는 러너 프로세스에는
적용되지 않았다 — 러너 재시작 필요.
