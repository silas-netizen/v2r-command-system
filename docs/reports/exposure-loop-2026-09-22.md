# 키워드 노출 순환기 재설계(B1~B4) — 2026-09-22

계획서: [`keyword-program-plan-2026-09-22.md`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/keyword-program-plan-2026-09-22.md) B절.
구현 파일: [`v2r/knowledge/keyword_exposure.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_exposure.py)

## 1. 무엇을 바꿨나

### B1 — 판정 기준을 통합검색(통검)으로
- `check_keyword_unified()` 추가 — `https://search.naver.com/search.naver?query=<키워드>` 결과(카페·블로그·VIEW·인플루언서 등 전체)에서 우리 글을 찾으면 `exposed`(노출완), 없으면 `pushed`(밀려남).
- 기존 카페탭 전용 파싱 함수(`parse_cafe_search_rank`/`parse_cafe_search_title_rank`)를 그대로 재사용한다 — 둘 다 HTML 안의 `cafe.naver.com` 링크만 보므로 통검 결과에도 그대로 적용된다. 새 HTML 가져오기 함수(`fetch_integrated_search_html`)만 추가했다.
- 순위 1~5위 이내면 `TOP5_RANK`로 판정해 CSV O열("1~5순위 진입")에 표시한다.
- 기존 카페탭 검사(`check_keyword`/`run_check`, "키워드 노출 현황" 명령)는 손대지 않고 보조로 남겼다.

### B2 — 무한 순환기
- 새 사이드카 슬롯: `v2r/engine/sidecar.py`의 `tick_once()`가 매 틱(5초)마다 `keyword_exposure.cycle_tick(rt)`를 한 번 부른다. 가벼운 작업(`LIGHT_TASKS`) 큐와는 별개의 "long" 루프로, 큐에 쌓이지 않고 꺼져 있으면(`enabled=False`) 즉시 반환한다.
- 대상 키워드 = 시트 둘째 탭(H열, 기존 `target_keywords`) ∪ `data/keywords/<브랜드>.csv`(다른 일꾼이 만드는 키워드 발굴 결과 — 아직 형식이 확정되지 않아 방어적으로 읽는다. 이번 실행 시점엔 파일이 없어 시트 쪽만 썼다).
- 순서: 검색량 큰 순 → 마지막 확인 오래된 순(`next_cycle_batch`). **커서를 따로 두지 않는다** — 확인한 키워드는 DB `checked_at`이 갱신되어 정렬에서 저절로 뒤로 밀리므로, 매번 다시 정렬해서 앞에서 집는 것만으로 끝없이 순환한다(테스트로 확인).
- 명령: `노출 순환 시작` / `노출 순환 중지` / `노출 순환 상태` (`v2r/command/parser.py`에 패턴 추가, `v2r/command/spec.py`의 `ALLOWED_TASKS`에 등록, `v2r/engine/worker.py`에 처리기 추가).
- 간격: 3~6초 무작위. 차단 징후(연속 `unknown` 10회)면 30분 휴식 후 재개(`BLOCK_STREAK_LIMIT`/`BLOCK_REST_SECONDS`).
- `config/schedule.yaml`의 08:00 "키워드 노출 현황" 예약 항목은 삭제(순환기가 대체).

### B3 — 시트 반영 준비
- `write_exposure_csv(rt, brand)`가 매 확인 후 `data/exposure/<브랜드>.csv`(열: 카페·url·발행시간·작성자 아이디·비밀번호(항상 빈칸)·발행 URL·노출 상태·키워드·통합검색 URL·최종 편집 일시·키워드 검색량·노출된 검색량·비고·본문 분류·1~5순위 진입) + `data/exposure/<브랜드>.summary.json`(P1=검색량 합, Q1=노출된 검색량 합, 비율)을 다시 쓴다.
- `v2r/sources/sheets_writer.py` 신설 — `update_rows(spreadsheet_id, sheet, rows)`, `set_cell(...)` 인터페이스만. `data/google-oauth/token.json`이 있으면 `gspread`로 실제 시트에 쓰고, 없으면(지금 상태) `{"written": 0, "mode": "csv_only"}`를 돌려주고 조용히 끝난다 — 자격증명 없이도 그대로 테스트할 수 있다.

### B4 — 현황판 갱신
- `keyword_exposure.summary(rt)`는 기존처럼 노출완/밀려남/미발행/미확인 수·새로 밀려난 키워드를 돌려주고, `data/exposure/<브랜드>.summary.json`이 있으면 `exposed_volume_ratio`(노출 검색량 비율)·`total_volume_p1`·`exposed_volume_q1`을 얹는다. `dashboard.py`는 이 함수만 쓰고 고치지 않았다(기존 설계 §5 유지).

## 2. 실제 시험 — 우아덤 키워드 30개, 통검 판정 1회

실행: `keyword_universe(rt, "우아덤")`로 뽑은 키워드(전체 3,822개, 시트 쪽만 — 발굴 CSV는 아직 없음) 중 앞 30개를 `check_keyword_unified`로 통검 1회씩 확인, 3~6초 간격, `data/naver_cookies.json` 쿠키 사용. 캡차·429·빈 결과 연속 징후 없이 정상 완료(차단 없음).

| # | 키워드 | 상태 | 순위 | 우리 글 URL |
|---|---|---|---|---|
| 1 | 비타민C | 밀려남 | - | https://cafe.naver.com/ca-fe/cafes/15175096/articles/509 |
| 2 | 나이아신아마이드 | 밀려남 | - | https://cafe.naver.com/ca-fe/cafes/15175096/articles/746 |
| 3 | 편평사마귀 | 미발행 | - | - |
| 4 | 멜라토닝크림 | 미발행 | - | - |
| 5 | 멜라논크림 | 미발행 | - | - |
| 6 | 리포좀비타민C | 미발행 | - | - |
| 7 | 글루타치온효과 | 미발행 | - | - |
| 8 | PDRN | 미발행 | - | - |
| 9 | 메가도스비타민C | 미발행 | - | - |
| 10 | 모공각화증 | 미발행 | - | - |
| 11 | 쥐젖제거크림 | 미발행 | - | - |
| 12 | 선크림 | 밀려남 | - | https://cafe.naver.com/ca-fe/cafes/26680163/articles/754 |
| 13 | 트라넥삼산 | 미발행 | - | - |
| 14 | 피코토닝 | 밀려남 | - | https://cafe.naver.com/ca-fe/cafes/16149995/articles/586 |
| 15 | 여드름연고 | 미발행 | - | - |
| 16 | 리포좀글루타치온 | 미발행 | - | - |
| 17 | 화이트태닝 | 미발행 | - | - |
| 18 | PDRN앰플 | 미발행 | - | - |
| 19 | 여드름패치 | 미발행 | - | - |
| 20 | 좁쌀여드름 | 미발행 | - | - |
| 21 | PDRN크림 | 미발행 | - | - |
| 22 | 티타늄리프팅 | 밀려남 | - | https://cafe.naver.com/ca-fe/cafes/26680163/articles/1502 |
| 23 | 기미크림 | 밀려남 | - | https://cafe.naver.com/ca-fe/cafes/10174516/articles/848740 |
| 24 | 피코토닝효과 | 미발행 | - | - |
| 25 | 달바톤업선크림 | 미발행 | - | - |
| 26 | 올타이트리프팅 | 미발행 | - | - |
| 27 | 비타민C효능 | 미발행 | - | - |
| 28 | 무기자차선크림 | 미발행 | - | - |
| 29 | 모공줄이는법 | 미발행 | - | - |
| 30 | PDRN효과 | 미발행 | - | - |

**요약**: 밀려남 6건 · 미발행 24건 · 노출완 0건 · 미확인(차단 의심) 0건. 상위 30개(순번 기준, 검색량 정렬 아님 — 정렬은 DB `checked_at`이 있어야 의미가 생기는데 이번이 첫 실행이라 시트 순서 그대로 뽑혔다) 중 "미발행"이 많은 건 이 브랜드 시트 `노출 현황` 탭·`article_index`에 아직 URL이 채워지지 않은 키워드가 많기 때문이며, 이번 통검 판정 자체의 문제는 아니다. 차단 징후는 한 번도 없었다.

이번 시험은 이 모듈의 `check_keyword_unified`를 직접 호출한 1회성 스크립트(스크래치패드, 커밋 대상 아님)로 돌렸다 — 실제 서비스에서는 `노출 순환 시작` 명령으로 사이드카가 계속 돈다.

## 3. 시험 안 한 것 / 남은 일

- `노출 순환 시작`을 실제로 켜서 몇 시간 돌리는 것 — 지시에 없던 항목이라 안 함(사이드카 재시작 금지 지침과도 맞물림). 명령·틱 로직은 pytest로 확인.
- 구글 시트 OAuth 동의 — 계획서 C절대로 아직 허락받지 않아 `sheets_writer.py`는 인터페이스만, 실제 쓰기는 CSV뿐.
- `data/keywords/<브랜드>.csv`(다른 일꾼의 키워드 발굴 산출물) — 아직 없어 `keyword_universe()`가 시트 쪽만 합쳤다. 파일이 생기면 자동으로 합쳐진다(방어적 파서).

## 4. 테스트

`tests/test_keyword_exposure_cycle.py` 14건 신규 — 통검 파싱(노출완/밀려남/미발행/차단), 순환 순서(검색량 큰 순→확인 오래된 순, 커서 없이 순환 확인), 시작/중지/상태, 틱 1회 진행(DB·CSV·summary 동시 갱신), 연속 차단 시 30분 휴식, 명령어 해석. 기존 `tests/test_keyword_exposure.py`(카페탭 보조 경로)는 손대지 않았다.

전체 `pytest` 실측(2026-09-23 00:11 KST): **1,218 통과 · 2 실패** (10분 57초).

```
FAILED tests/test_parser.py::test_all_tasks_covered_by_tests
FAILED tests/test_self_cafe_daily.py::test_account_rotation_start_resumes_after_today_last_published
2 failed, 1218 passed in 657.38s
```

두 실패 다 이번 작업과 무관해 **사유만 적고 무시**한다(지시대로):

1. `test_all_tasks_covered_by_tests` — "장부 파수꾼" 테스트로, `ALLOWED_TASKS` 개수를 44로 못박아 뒀다. 이번에 내가 3개(`exposure_cycle_start`/`stop`/`status`)를, 다른 일꾼이 키워드 발굴 명령 2개(`keyword_discovery`/`keyword_discovery_status`)를 각자 추가해 지금은 49개 — 장부(숫자 하드코딩)가 실제 작업 목록보다 늦게 갱신되는 게 이 테스트의 원래 목적이라, 새 작업이 늘 때마다 실패하는 게 정상이다. 숫자만 고치는 건 다른 일꾼의 몫과 섞이니 손대지 않았다.
2. `test_account_rotation_start_resumes_after_today_last_published` — 자사 카페 일상 글 계정 순환 로직(`v2r/engine/publish.py`) 테스트로 이번 키워드 노출 작업과 파일이 전혀 겹치지 않는다. 이 세션에서 그 코드를 건드리지 않았으니 내 변경 탓이 아니다.

내가 새로 추가한 `tests/test_keyword_exposure_cycle.py`(14건)와 기존 `tests/test_keyword_exposure.py`는 전부 통과했다.
