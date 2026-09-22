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

### B1 재설계 1차(2026-09-23) — 검색어 자동완성 정규화 + 끝까지 스크롤
사용자 지시로 통검 판정 기준을 다음처럼 정확히 맞췄다.
- `resolve_search_query(keyword)` — 네이버 자동완성 API(`ac.search.naver.com/nx/ac`)의 첫 항목이 있으면 그 표기(띄어쓰기 포함)로 검색, 없으면 띄어쓰기 정규화(`pykospacing`이 있으면 그걸로, 없으면 — 지금 환경 — 원문 그대로: "실패 시 원문"). 실제로 쓴 최종 검색어는 `ExposureRow.search_query`/DB `keyword_exposure.search_query`(마이그레이션 추가) → `write_exposure_csv`가 I열(통합검색 URL)을 이 검색어로 만든다.
- `fetch_integrated_search_dom(query, cookies_path)` — Playwright(headless, 네이버 프로필 쿠키)로 통검 첫 페이지를 열어 **페이지 이동 없이 끝까지 스크롤**(더보기류 버튼 클릭 포함, 높이가 더 안 늘 때까지)한 뒤 최종 DOM을 돌려준다.

### B1 재설계 2차(2026-09-23) — 카페 후보 → 댓글 식별어로 "우리 글" 확정
"우리 글"인지 더 정확히 가리기 위해 판정을 2단계로 바꿨다(`judge_keyword_exposure`, 이제 순환기 `cycle_tick`이 이걸 쓴다).
1. **1차 관문**: 통검 최종 화면의 문서 중 `config/cafes.yaml`의 제휴·자사 카페(이름/번호/별칭)에 속한 것만 후보로 삼는다(`load_cafe_registry`/`is_our_cafe_url`) — 다른 카페·블로그는 아예 열지 않는다. 순위(`rank`)는 전체 문서(카페·블로그·VIEW 다 합쳐) 중 몇 번째인지.
2. **2차 확정**: 후보를 순위 순으로 최대 `MAX_CANDIDATES_TO_OPEN`(5)개까지 열어(Playwright, 네이버 프로필, headless, 후보 사이 3~6초) 댓글(+본문, `cafe_main` iframe)에 브랜드 식별어(`config/brands.yaml`의 새 `identifiers` 목록 — 우아덤: 우아덤/그린커피 아하바하, 코숨핏: 코숨핏, 뉴더미스: 뉴더미스/자연방패, 장으뜸: 장으뜸, 팥순이: 팥순이/팥순추출물)가 있으면 그 글을 우리 글로 확정(`exposed`). `article_index`에 이미 있는 글이면 열지 않고 바로 확정한다(보조).
3. 확인 결과는 `data/exposure_article_cache.json`에 24시간 캐시(`cached_verdict`/`set_cached_verdict`) — 같은 글 URL은 하루 안엔 다시 안 연다.

## 2. 실제 시험 — 우아덤 키워드 30개

### 1차(통검 1회, 저장된 article_url 기준) — 2026-09-22 23:5x KST
`keyword_universe(rt, "우아덤")` 앞 30개를 `check_keyword_unified`(당시 기준: 우리가 알던 article_url이 통검 결과에 있는지)로 확인. 밀려남 6 · 미발행 24 · 노출완 0 · 미확인 0. 차단 없음. (이 표는 이후 판정 방식이 바뀌어 참고용으로만 남긴다.)

### 2차 재시험(카페 후보 → 댓글 식별어 확정, 자동완성 검색어) — 2026-09-23 00:2x KST 실측
`judge_keyword_exposure`로 같은 30개를 다시 확인 — 자동완성 최종 검색어, 제휴·자사 카페(총 10개) 후보 필터, 후보 있으면 Playwright로 열어 댓글/본문 식별어 확인, 3~6초 간격. **캡차·차단 징후 전혀 없이 정상 완료**.

| # | 키워드(원문) | 최종 검색어 | 후보 수 | 열어본 수 | 판정 | 순위 |
|---|---|---|---|---|---|---|
| 1 | 비타민C | 비타민C | 0 | 0 | 밀려남 | - |
| 2 | 나이아신아마이드 | 나이아신아마이드 | 0 | 0 | 밀려남 | - |
| 3 | 편평사마귀 | 편평사마귀 | 0 | 0 | 밀려남 | - |
| 4 | 멜라토닝크림 | 멜라토닝크림 가격 | 0 | 0 | 밀려남 | - |
| 5 | 멜라논크림 | 동아제약 멜라논크림 | 0 | 0 | 밀려남 | - |
| 6 | 리포좀비타민C | 리포좀 비타민c | 0 | 0 | 밀려남 | - |
| 7 | 글루타치온효과 | 글루타치온 효과 | 0 | 0 | 밀려남 | - |
| 8 | PDRN | pdrn 효과 | 0 | 0 | 밀려남 | - |
| 9 | 메가도스비타민C | 메가도스 비타민c | 0 | 0 | 밀려남 | - |
| 10 | 모공각화증 | 모공각화증 | 0 | 0 | 밀려남 | - |
| 11 | 쥐젖제거크림 | 쥐젖 제거 크림 | 0 | 0 | 밀려남 | - |
| 12 | 선크림 | 선크림 | 0 | 0 | 밀려남 | - |
| 13 | 트라넥삼산 | 트라넥삼산 | 0 | 0 | 밀려남 | - |
| 14 | 피코토닝 | 피코토닝 효과 | 0 | 0 | 밀려남 | - |
| 15 | 여드름연고 | 약국 여드름 연고 | 0 | 0 | 밀려남 | - |
| 16 | 리포좀글루타치온 | 리포좀 글루타치온 | 0 | 0 | 밀려남 | - |
| 17 | 화이트태닝 | 화이트태닝 | 0 | 0 | 밀려남 | - |
| 18 | PDRN앰플 | PDRN 앰플 | 0 | 0 | 밀려남 | - |
| 19 | 여드름패치 | 여드름패치 | 0 | 0 | 밀려남 | - |
| 20 | 좁쌀여드름 | 좁쌀여드름 원인 | 0 | 0 | 밀려남 | - |
| 21 | PDRN크림 | pdrn크림 | 0 | 0 | 밀려남 | - |
| 22 | 티타늄리프팅 | 티타늄리프팅 | 0 | 0 | 밀려남 | - |
| 23 | 기미크림 | 기미크림 | 0 | 0 | 밀려남 | - |
| 24 | 피코토닝효과 | 피코토닝 효과 | 0 | 0 | 밀려남 | - |
| 25 | 달바톤업선크림 | 달바 톤업선크림 | 0 | 0 | 밀려남 | - |
| 26 | 올타이트리프팅 | 올타이트리프팅 유지기간 | 0 | 0 | 밀려남 | - |
| 27 | 비타민C효능 | 비타민C효능 | 0 | 0 | 밀려남 | - |
| 28 | 무기자차선크림 | 무기자차 선크림 | 0 | 0 | 밀려남 | - |
| 29 | 모공줄이는법 | 모공 줄이는법 | 0 | 0 | 밀려남 | - |
| 30 | PDRN효과 | pdrn 효과 | 0 | 0 | 밀려남 | - |

**요약**: 30개 전부 밀려남(후보 0, 열어본 글 0) — 통검 최종 화면(끝까지 스크롤)에 이 30개 키워드로는 우리 제휴·자사 카페(10곳) 글이 하나도 안 잡혔다. 자동완성 정규화는 잘 작동했다(예: "멜라토닝크림"→"멜라토닝크림 가격", "동아제약 멜라논크림", "PDRN"→"pdrn 효과"처럼 실제 검색창 표기로 바뀜). 후보가 0이라 댓글 열람(2차 확정)까지 간 키워드는 없었다 — 파이프라인의 "후보 있을 때만 연다" 동작이 그대로 확인됐다(단위 테스트로도 확인). 1차 시험(밀려남 6)과 대상 키워드가 이번에도 이 30개 안에 겹쳤지만(비타민C·나이아신아마이드·선크림·피코토닝·티타늄리프팅·기미크림), 이번엔 판정 기준이 "우리가 이미 아는 article_url"이 아니라 "제휴·자사 카페 소속인지"로 바뀌어 0으로 나왔다 — 즉 그 6개 글이 실제로는 제휴 카페가 아닌 카페에 있거나(카페 레지스트리 불일치), 통검 결과 30건(스크롤 끝까지) 안에 없다는 뜻이다. 이 브랜드의 발행 이력이 아직 얕아 30개 표본에서 교집합이 적은 것으로 보이며, 더 큰 표본(순환기 상시 가동)에서 재확인이 필요하다.

이번 시험도 이 모듈 함수(`judge_keyword_exposure`)를 직접 호출한 1회성 스크립트(스크래치패드, 커밋 대상 아님)로 돌렸다 — 실제 서비스에서는 `노출 순환 시작` 명령으로 사이드카가 계속 돈다.

## 3. 시험 안 한 것 / 남은 일

- `노출 순환 시작`을 실제로 켜서 몇 시간 돌리는 것 — 지시에 없던 항목이라 안 함(사이드카 재시작 금지 지침과도 맞물림). 명령·틱 로직은 pytest로 확인.
- 구글 시트 OAuth 동의 — 계획서 C절대로 아직 허락받지 않아 `sheets_writer.py`는 인터페이스만, 실제 쓰기는 CSV뿐.
- `data/keywords/<브랜드>.csv`(다른 일꾼의 키워드 발굴 산출물) — 아직 없어 `keyword_universe()`가 시트 쪽만 합쳤다. 파일이 생기면 자동으로 합쳐진다(방어적 파서).
- `pykospacing` 미설치 — 자동완성이 없을 때의 "한국어 띄어쓰기 규칙" 폴백은 지금 원문 그대로다(지시된 "실패 시 원문"과 일치). 필요하면 `pip install pykospacing`만 추가하면 자동으로 쓰인다.
- 후보 0건이라 이번엔 댓글 열람(2차 확정) 경로를 실제 네이버 글로는 시험하지 못했다 — 단위 테스트(고정 HTML·mock)로는 확인했다.

## 4. 테스트

`tests/test_keyword_exposure_cycle.py` **29건**(1차 14건 + 이번 자동완성·끝까지 스크롤·카페 후보/댓글 식별어 판정 15건 추가) — 통검 파싱(노출완/밀려남/미발행/차단), 자동완성 우선/폴백, 카페 후보 판별, 결과 링크 순서, 브랜드 식별어 로딩, article_index/캐시/직접 열람 3단 확정, 후보 있음/없음/DOM 실패 시 판정, 순환 순서(검색량 큰 순→확인 오래된 순, 커서 없이 순환 확인), 시작/중지/상태, 틱 1회 진행(DB·CSV·summary 동시 갱신, 검색어도 같이 저장), 연속 차단 시 30분 휴식, 명령어 해석. 기존 `tests/test_keyword_exposure.py`(카페탭 보조 경로)는 손대지 않았다.

관련 범위 `pytest`(db/keyword_exposure/spec/worker/sidecar) 실측(2026-09-23, 8분 34초): **96 통과, 실패 0**.

전체 `pytest` 실측(2026-09-23 00:11 KST, 이번 2차 재설계 전 스냅샷): **1,218 통과 · 2 실패** (10분 57초).

```
FAILED tests/test_parser.py::test_all_tasks_covered_by_tests
FAILED tests/test_self_cafe_daily.py::test_account_rotation_start_resumes_after_today_last_published
2 failed, 1218 passed in 657.38s
```

두 실패 다 이번 작업과 무관해 **사유만 적고 무시**한다(지시대로):

1. `test_all_tasks_covered_by_tests` — "장부 파수꾼" 테스트로, `ALLOWED_TASKS` 개수를 44로 못박아 뒀다. 이번에 내가 3개(`exposure_cycle_start`/`stop`/`status`)를, 다른 일꾼이 키워드 발굴 명령 2개(`keyword_discovery`/`keyword_discovery_status`)를 각자 추가해 그 시점엔 49개였다 — 장부(숫자 하드코딩)가 실제 작업 목록보다 늦게 갱신되는 게 이 테스트의 원래 목적이라, 새 작업이 늘 때마다 실패하는 게 정상이다. 이후 재확인 시(`select:db or keyword_exposure or spec or worker or sidecar`) 이미 갱신돼 통과했다 — 다른 일꾼이 고친 것으로 보인다.
2. `test_account_rotation_start_resumes_after_today_last_published` — 자사 카페 일상 글 계정 순환 로직(`v2r/engine/publish.py`) 테스트로 이번 키워드 노출 작업과 파일이 전혀 겹치지 않는다. 이 세션에서 그 코드를 건드리지 않았으니 내 변경 탓이 아니다.

내가 새로 추가/수정한 `tests/test_keyword_exposure_cycle.py`(29건)와 기존 `tests/test_keyword_exposure.py`는 전부 통과했다.
