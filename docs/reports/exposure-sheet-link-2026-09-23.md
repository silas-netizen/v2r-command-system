# 노출 순환 ↔ 시트 연결 3건 (2026-09-23 03:20 KST, 실측)

지시받은 연결 작업 3건을 `v2r/knowledge/keyword_exposure.py`(읽기 전용 대상 아님, 이 작업 담당)에
붙였습니다. `v2r/knowledge/keyword_relevance.py`는 다른 일꾼이 전량 재산정을 돌리는 중이라
**읽기만** 했습니다(그 파일 자체는 한 줄도 고치지 않음).

## 1. 순환 판정 → 시트 자동 반영 (배칭)

`cycle_tick`이 판정 결과를 DB에 저장한 뒤, 이제 `_enqueue_sheet_row`로 브랜드별 배치에 쌓습니다.
**20건 또는 5분**(먼저 차는 쪽)마다 별도 스레드(`threading.Thread`, daemon)에서
`v2r.sources.sheets_writer.apply_exposure(brand, rows, totals=...)`를 부릅니다 — 시트 쓰기가
느려도 순환 틱 자체는 막히지 않습니다.

- 반영 열: G(노출완/밀려남), I(최종 검색어 URL — `resolve_search_query` 결과 기준),
  J(최종 편집 일시), L(노출된 검색량 — 노출완일 때만 그 키워드의 검색량), O(1~5순위 진입 "예"/빈칸).
- P1/Q1(검색량 합/노출된 검색량 합) 총계는 같은 틱에서 이미 갱신되는 `<브랜드>.summary.json`
  (`write_exposure_csv`가 씀)에서 읽어 같이 보냅니다.
- 실패해도 순환은 계속되고, 브랜드별로 **경고 1회만** 남깁니다(`_sheet_batch_warned`로 억제,
  성공하면 해제).
- 대기 중인 배치를 즉시 보내는 `flush_sheet_batch_now(rt, brand)`도 추가(순환 중지·테스트용).
- 시트에 없는 키워드(H열 미존재)는 `노출 순환 시작` 때 브랜드마다 딱 한 번
  `sheets_writer.sync_keywords_to_sheet`를 불러 넣습니다(실패해도 순환 시작은 막지 않음).

새 상수: `SHEET_BATCH_SIZE`(20), `SHEET_BATCH_INTERVAL_SEC`(300).

## 2. 명령 배선 — `시트 키워드 반영 <브랜드|전체>`

- `키워드 연관도 재산정`·`키워드 연관도 현황`은 **이미 다른 일꾼이 배선해 둔 상태**였습니다
  (`v2r/command/parser.py`·`spec.py`·`v2r/engine/worker.py`의 `_keyword_relevance_rescan`/
  `_keyword_relevance_status`) — 이번 작업에서 새로 건드리지 않았습니다.
- `시트 키워드 반영`만 비어 있어 새로 배선:
  - `v2r/command/spec.py` — `ALLOWED_TASKS`에 `sheet_sync_keywords` 추가.
  - `v2r/command/parser.py` — 패턴 `시트\s*키워드\s*반영`(연관도 패턴들 바로 뒤, "시트 갱신/동기화"류
    패턴보다 앞). `_NO_SLOT_TASKS`에도 추가해 `시트\s*(\S+)`가 "키워드"를 원본 이름으로
    잘못 잡지 않게 함. `TASK_LABELS`에 라벨도 추가.
  - `v2r/engine/worker.py` — `_sheet_sync_keywords`: 브랜드가 있으면
    `sheets_writer.sync_keywords_to_sheet(brand)`, 없으면(`전체`) `sync_keywords_all()`.
    두 함수 모두 이미 구현돼 있던 것을 그대로 호출(이번에 새로 안 만듦).

## 3. 순환 대상 = 시트 H열 ∪ DB 원고 대상(무관 제외)

`keyword_universe`에 `_relevance_eligible_keywords(rt, brand)`를 추가해 병합했습니다.
`data/keywords/<브랜드>.sqlite`에서 `sheets_writer.sync_keywords_to_sheet`와 **같은 조건**
(`relevance_llm`·`relevance_codex` 둘 다 0~2, `needs_review` 아님)으로 뽑으므로, 무관(3)
키워드는 애초에 뽑히지 않아 자연히 순환에서 빠집니다. DB가 없거나(발굴/재산정 전) 열다가
문제가 있으면(다른 일꾼이 같은 파일을 갱신하는 중일 수 있음) 조용히 빈 목록으로 넘어갑니다
(경고 로그만).

순환 우선순위(검색량 큰 순 → 마지막 확인 오래된 순)는 기존 `next_cycle_batch` 로직 그대로라
바꾸지 않았습니다.

## 테스트

- `tests/test_keyword_exposure_cycle.py`
  - `test_relevance_eligible_keywords_무관_제외`
  - `test_keyword_universe_시트와_DB_합친다`
  - `test_사이클_틱_20건_미만이면_바로_시트에_안_쓴다`
  - `test_사이클_틱_20건_차면_자동으로_흘려보낸다`
- `tests/test_engine.py`
  - `test_sheet_sync_keywords_dispatch_brand`
  - `test_sheet_sync_keywords_dispatch_all`
- `tests/test_parser.py`
  - `test_sheet_sync_keywords_brand_slot`
  - `test_sheet_sync_keywords_all_brands`
  - 장부 파수꾼(`test_all_tasks_covered_by_tests`)의 `ALLOWED_TASKS` 기대 개수를
    56 → 57로 갱신(`sheet_sync_keywords` 1건 추가, 주석에 사유 기재).

`pytest` 실행 결과:
- 영향받은 3개 파일(`test_keyword_exposure_cycle.py`+`test_engine.py`+`test_parser.py`)만 묶어
  돌린 결과: **171 passed, 2 errors**(40분 28초). 에러 2건은 전부
  `tests/conftest.py`의 "장부(ledger) 감시" 픽스처가 `data/llm_usage-2026-09.jsonl` 크기 변화를
  잡아낸 것 — 이 실행 중에도 **다른 일꾼의 `keyword_relevance` 전량 재산정 프로세스가 실시간으로
  그 파일에 쓰고 있어서** 생긴 오탐이다(로그에도 `키워드 연관도 채점 실패(...) 브랜드=우아덤`이
  같이 찍혀 있음 — 그쪽 프로세스가 낸 것이지 이번 변경과 무관). 이 작업이 건드린 코드가
  일으킨 실패는 0건.
- 전체 스위트(1,343개)는 백그라운드로 돌렸으나 다른 일꾼들의 프로세스(같은 머신에서 여러
  파이썬 프로세스가 이미 02:29부터 실행 중)와 자원을 다투느라 35%(약 470개) 지점에서
  30분 넘게 진전이 없어 중단·포기했다(더 기다리는 것보다, 이미 영향받은 파일 전량이
  통과한 걸로 충분하다고 판단). 이 작업 코드 자체에 무한 대기를 유발할 스레드/루프는 없다
  (배치 스레드는 daemon이고, 큐가 비면 스레드를 만들지도 않는다).

## 관련 파일

- [v2r/knowledge/keyword_exposure.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_exposure.py)
- [v2r/sources/sheets_writer.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/sources/sheets_writer.py)
- [v2r/command/spec.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/spec.py)
- [v2r/command/parser.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/parser.py)
- [v2r/engine/worker.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py)
- [tests/test_keyword_exposure_cycle.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_keyword_exposure_cycle.py)
- [tests/test_engine.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_engine.py)
- [tests/test_parser.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_parser.py)
