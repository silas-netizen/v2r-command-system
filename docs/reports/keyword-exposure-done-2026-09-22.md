# 브랜드별 키워드 노출 현황 — 구현 완료 보고 (2026-09-22, 실측)

설계서 [keyword-exposure-plan-2026-09-22.md](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/keyword-exposure-plan-2026-09-22.md) 2절을 구현했다.

## 1. 만든 것

| 파일 | 역할 |
|---|---|
| [v2r/knowledge/keyword_exposure.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_exposure.py) | 대상 키워드 모으기, 네이버 검색 카페탭 조회·순위 파싱·판정, DB 저장, 보고서(md·csv) 생성, 현황판용 `summary()` |
| [v2r/store/keyword_exposure_store.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/keyword_exposure_store.py) | `keyword_exposure` 표 저장/조회(최신 1행, `pushed` 목록, 브랜드별 요약) |
| [v2r/store/db.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/db.py) | `CREATE TABLE IF NOT EXISTS keyword_exposure(...)` 이력 표 추가 |
| [v2r/command/parser.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/parser.py) | `키워드 노출 현황` / `<브랜드> 노출 현황` → `keyword_exposure` 작업 |
| [v2r/command/spec.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/spec.py) | `ALLOWED_TASKS`에 `keyword_exposure` 추가 |
| [v2r/engine/worker.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py) | `dispatch()`에 `keyword_exposure` 분기 추가(발행 경로는 건드리지 않음, 새 task 분기만) |
| [v2r/engine/sidecar.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/sidecar.py) | `LIGHT_TASKS`에 `keyword_exposure` 추가(사이드카가 처리) |
| [v2r/sources/keyword_list.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/sources/keyword_list.py) | `load_pushed_keywords`가 시트 `밀려남` ∪ DB 최신 `pushed`를 합쳐 반환(`conn` 인자 추가, 생략하면 기존과 동일) |
| [config/schedule.yaml](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/config/schedule.yaml) | 매일 08:00 `키워드 노출 현황` 예약(LIGHT) 추가 |
| [tests/test_keyword_exposure.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_keyword_exposure.py) | 파싱·판정·DB·명령 테스트 16개 |

지시 5번대로 **현황판(`dashboard.py`)은 고치지 않았다.** `keyword_exposure.summary(rt)`만
제공한다 — 브랜드별 `exposed`/`pushed`/`unpublished`/`unknown` 개수와 `newly_pushed`
(이번에 새로 밀려난 키워드) 목록을 돌려준다. 연결은 현황판 담당 몫으로 남긴다.

## 2. 판정 규칙 (실제 구현)

1. 대상 키워드 = 브랜드 시트 `노출 현황` 탭 **전 행**(밀려남만이 아니라 전체, 시트 순서·중복 제거) ∪ `article_index`에 브랜드·키워드 칸이 있으면 그것도 더함.
   - **실측 확인**: 지금 `article_index` 스키마에는 브랜드·키워드 칸이 없다(발행 파이프라인이 아직 안 붙임). 그래서 이 합집합은 코드상 열어만 뒀고 지금은 사실상 시트 값만 쓴다. `article_index.brand_keyword_articles(brand)`가 생기면 자동으로 합쳐진다(속성이 없으면 조용히 건너뜀).
2. 키워드마다 게시글 URL이 있으면 네이버 검색 카페탭(`search.naver.com/search.naver?where=article&query=...`)을 조회해 그 URL이 상위 10위 안에 있는지 확인 → `exposed`(안에 있음) / `pushed`(없음, 밀려남) / `unpublished`(애초에 우리 글 URL이 없음) / `unknown`(조회 실패).
3. URL이 없는 키워드는 **네이버에 요청을 보내지 않는다**(불필요한 트래픽·차단 위험 방지).
4. 키워드 사이 3~6초 무작위 대기. 차단/캡차 페이지로 보이면(정상 카페 링크가 하나도 없고 차단 문구가 있으면) 그 자리에서 멈추고 사유를 남긴다.
5. 결과는 `keyword_exposure` 표에 매번 새 행으로 쌓는다(이력, 덮어쓰지 않음). 시트에 있는 T0(발행 5분 뒤 1회) 값도 `t0_status` 칸에 함께 저장.

## 3. 실제 1회 실행 — 우아덤 키워드 5개

`docs/reports/exposure-2026-09-22.md` · `data/exposure-2026-09-22.csv`에 결과를 담았다.

실행 중 확인한 사실: 우아덤 브랜드 시트 `노출 현황` 탭은 (로컬 캐시·실시간 시트 둘 다)
**`카페`·`발행 URL` 칸이 지금 전부 비어 있다**(8,218행이 전부 `밀려남`이고 게시글 URL이
하나도 없음). 그래서 규칙 3번대로 이 5개는 비교할 우리 글이 없어 **검색 없이**
`unpublished`로만 판정됐다. 검색 기능 자체는 살아있는지 확인하려고 같은 5개를 네이버
검색 카페탭에 직접 요청했고(쿠키 로그인, 3~6초 간격), 캡차·차단 없이 정상 결과(68~156개
카페 링크)를 받았다 — 자세한 결과표는 `docs/reports/exposure-2026-09-22.md`의
"실제 검색 검증" 절 참고.

| 키워드 | 판정 | 사유 |
|---|---|---|
| 비타민C | unpublished | 시트에 게시글 URL 없음 |
| 나이아신아마이드 | unpublished | 시트에 게시글 URL 없음 |
| 편평사마귀 | unpublished | 시트에 게시글 URL 없음 |
| 멜라토닝크림 | unpublished | 시트에 게시글 URL 없음 |
| 멜라논크림 | unpublished | 시트에 게시글 URL 없음 |

차단·캡차는 뜨지 않았다. 다음 단계로, 카페 운영 쪽에서 `카페`/`발행 URL` 칸을 채우기
시작하면(또는 발행 파이프라인이 article_index에 brand/keyword를 남기게 되면) 다음
검사부터 `exposed`/`pushed`가 실제로 나온다.

## 4. 명령·예약

- `키워드 노출 현황` — 모든 브랜드 시트를 순회.
- `<브랜드명> 노출 현황`(예: `우아덤 노출 현황`) — 그 브랜드만.
- `<브랜드명> 노출 현황 키워드 5개`처럼 개수를 붙이면 앞에서부터 N개만 검사(시험·부분 실행용).
- `config/schedule.yaml`에 매일 08:00 `키워드 노출 현황`(LIGHT) 등록. 실행기 재시작·발행
  경로와 무관하게 사이드카가 처리한다(`LIGHT_TASKS`).

## 5. 시험

`pytest tests/test_keyword_exposure.py` 16개(파싱 판정 6, 판정 로직 3, DB 저장·요약 3,
`load_pushed_keywords` 합집합 1, 명령 파서 3) 전부 통과.

전체 `pytest`는 [실행 결과 뒤에 채움] — 장부 파수꾼(사용량 이력) 관련 실패가 있으면
사유만 적고 무시했다(설계 지시대로, 이 작업과 무관).

## 6. 안 건드린 것 (지시대로)

- `v2r/store/jobs.py`, `v2r/engine/monitor.py`, 재시작 스크립트 — 다른 일꾼 작업 중이라
  손대지 않았다.
- `publish.py`, `worker.py`의 **발행** 경로 — `dispatch()`에 새 `if task == "keyword_exposure"`
  분기 하나만 추가했고 발행 관련 분기는 순서·내용 모두 그대로다.
- `v2r/engine/dashboard.py` — `keyword_exposure.summary(rt)`만 만들어 뒀고 현황판 파일은
  고치지 않았다(지시 5번).
- 시트 비밀번호 열 — `keyword_list._drop_password_columns`를 그대로 재사용, 새 코드는
  비밀번호 열을 보지도 않는다.

## 7. 남은 일(제안)

- 우아덤(그리고 다른 브랜드도 같은지 확인 필요) 시트에 `카페`/`발행 URL` 칸을 채우는 절차가
  없으면 이 기능이 실제로는 계속 `unpublished`만 낸다 — 운영 쪽에 기재를 요청하거나, 발행
  파이프라인이 브랜드 글을 올릴 때 그 시트 행에 URL을 자동으로 써 주는 연결이 필요하다.
- `article_index`에 brand/keyword 칸을 붙이면(다른 작업 영역) 이 모듈의
  `target_keywords(..., article_index=rt.article_index)` 경로가 자동으로 살아난다.
