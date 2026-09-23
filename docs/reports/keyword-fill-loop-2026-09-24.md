# 키워드 채우기 순환 — 원고 대상 1만 개까지 (2026-09-24)

작성 시각: 2026-09-24 00:26 (KST, `date` 명령 실측)

## 1. 현재 원고 대상 수 (작업 시작 시점, 발굴 1만 개 기준)

원고 대상 = `relevance_llm` 0–2 AND `relevance_codex` 0–2 AND `needs_review` 아님
(`data/keywords/<브랜드>.sqlite`).

| 브랜드 | 원고 대상 | DB 총 키워드(발굴분) |
|---|---|---|
| 우아덤 | 1,203 | 10,000 |
| 장으뜸 | 283 | 10,000 |
| 팥순이 | 3,447 | 10,000 |
| 뉴더미스 | 474 | 10,000 |
| 코숨핏 | 363 | 10,000 |

## 2. 설계

새 모듈 [file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_fill_loop.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_fill_loop.py)에
한 회차(`run_cycle`)와 반복(`fill_until_target`)을 넣었다.

- **시드 선택** (`select_seed_keywords`): 아직 시드로 안 쓴(`seeded_at` 빈 값) 원고
  대상 키워드 중 검색량(`total`) 상위 50개. `keywords` 표에 `seeded_at` 열을
  새로 더했다(`migrate_fill_columns`).
- **정리본 보충** (`guide_seed_terms`): 원고 대상 시드가 50개에 못 미치면
  `warehouse/guides/정리본/<브랜드>.md`에서 클로드(요금제 길)로 제품·증상·타깃
  용어를 최대 30개 뽑아 채운다. LLM이 실패하면 기존 정규식 추출
  (`naver_keyword_tool.extract_guide_keywords`)로 대체한다(네트워크 없이도 항상
  동작).
- **수집**: 네이버 키워드 도구(`naver_keyword_tool.fetch_related_keywords`)로
  시드 5개씩 묶어 조회(기존 값 그대로, 조회 간 3–6초 대기·세션 200회당 휴식 등
  차단 방지 로직 재사용). 새 키워드만 `INSERT OR IGNORE`로 저장.
- **채점·교차검증**: 새로 들어온 키워드는 `scored_at`이 비어 있어
  `keyword_relevance.score_brand`(클로드, 요금제 길, 0원)와
  `crosscheck_brand`(Codex `gpt-6-astra`, low)가 자동으로 그 키워드들만 집는다
  — 이미 채점된 기존 키워드는 건드리지 않는다.
- **진행 기록** (`data/keywords/fill_progress.json`): 회차마다 브랜드별
  `eligible`(누적 원고 대상)·`new_this_round`(신규 수집)·`adopted_this_round`
  (신규 중 0–2로 채택된 수)·`adoption_rate`(채택률)·`seed_exhausted`·`status`를
  기록.
- **정지 조건**: 3회차 연속 채택률 5% 미만이면 그 브랜드를 `시드고갈`로 표시하고
  멈춘다(`LOW_ADOPTION_STREAK_LIMIT=3`, `LOW_ADOPTION_THRESHOLD=0.05`). 브랜드당
  DB 총 행수 5만 개 상한(`DEFAULT_CAP=50000`)에 닿으면 `용량상한`으로 멈춘다
  (무관 키워드가 늘어나는 것 자체는 허용하되 폭증만 막는다).

실행 진입점 `worker_main`(모듈 안 `--worker` CLI)은 브랜드 전용 복제 프로필
(`keyword_discovery_parallel`이 만든 `data/browser-profile-naver-kw-<브랜드>`)로
키워드 도구를 헤드리스로 열고, `LLMRouter.from_settings`(실행기 메인 DB를 열지
않는 경로 — 실행기 DB에 안 쓰는 규칙 준수)로 `fill_until_target`을 돈다. 로그인이
풀려 있으면 재로그인 없이 즉시 실패로 남긴다.

숨김 실행 스크립트:
[file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/keyword-fill-hidden.vbs](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/keyword-fill-hidden.vbs) →
[file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/keyword-fill-hidden.cmd](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/keyword-fill-hidden.cmd) —
브랜드 5개를 각각 숨김 파이썬 프로세스로 띄운다(CRLF·`chcp 65001`·상대경로).
로그: `logs/keyword-fill-<브랜드>.log`.

명령 배선(`키워드 채우기 시작|중지|현황`)은
[file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/parser.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/parser.py),
[file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/spec.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/spec.py),
[file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py)에
코드로만 넣었다(실행기 재시작 금지 규칙에 따라 지금 도는 실행기에는 반영 안 됨).
**다음 `v2r serve` 재시작부터 명령으로 쓸 수 있다.** 지금 실행은 위 2번 스크립트로
직접 띄웠다.

## 3. 테스트

`.venv\Scripts\python.exe -m pytest`로 실행, 전부 통과.

- 새 테스트
  [file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_keyword_fill_loop.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_keyword_fill_loop.py)
  7건 — 네트워크 없이 가짜 도구·가짜 LLM으로 시드 선택·시드 소진 제외·채택률
  계산·용량상한·진행 파일 갱신·3연속 저채택 고갈 판정을 검증. 전부 통과.
- 기존 회귀: `test_keyword_relevance.py`(32건) · `test_keyword_discovery_parallel.py`
  · `test_naver_keyword_tool.py` 합계 49건, `test_parser.py` 75건(새 명령 3개 추가로
  `ALLOWED_TASKS` 개수 검증 57→60 갱신 포함) 모두 통과.

## 4. 첫 회차 실측 (실행 시작 00:12:40 → 이 보고서 작성 00:26)

`scripts\keyword-fill-hidden.vbs`로 5개 브랜드를 동시에 헤드리스로 띄웠다.
약 14분 경과 시점 DB 총 행수(신규 수집 누적, 아직 회차 완주 전 중간 값):

| 브랜드 | 발굴 시작 총량 | 현재 총량 | 신규 수집(이번 순환) | 채점 대기(진행 중) |
|---|---|---|---|---|
| 우아덤 | 10,000 | 13,787 | 3,787 | 3,887 |
| 장으뜸 | 10,000 | 12,699 | 2,699 | 2,399 |
| 팥순이 | 10,000 | 13,938 | 3,938 | 3,438 |
| 뉴더미스 | 10,000 | 13,500 | 3,500 | 3,100 |
| 코숨핏 | 10,000 | 11,192 | 1,192 | 892 |

(채점 대기 수가 신규 수집보다 적은 것은 그사이 일부가 이미 채점·교차검증까지
끝났기 때문 — 정확한 "채택 수·채택률"은 회차가 완주돼야 나온다. 로그에서 클로드
채점 호출이 실제로 돌고 있음을 확인했다: 예) `뉴더미스` 00:15, `코숨핏` 00:16,
`우아덤` 00:21에 채점 요청·재시도가 찍혔다. 재시도 사유는 응답 한글 자모가
살짝 달라진 것(예: "보탬알페이스"→"보탐알페이스") — 기존 재시도 로직(최대 2회)이
정상 작동 중이다.)

**회차 하나가 완주하는 데 시간이 오래 걸린다**: 브랜드당 신규 1,200–3,900개를
100개씩 묶어 클로드로 채점한 뒤 Codex(`gpt-6-astra`, low, 최대 300초/묶음)로
다시 교차검증해야 해서, 5개 브랜드 동시 실행이라도 1회차가 수십 분 이상 걸릴
것으로 보인다. 지시대로 "최소 1회차 확인 후 백그라운드로 계속 돌게 둔다" — 이
보고서 작성 시점에도 5개 프로세스가 살아서 계속 돌고 있고, `data/keywords/fill_progress.json`은
회차가 완주되는 대로(각 브랜드 독립적으로) 채워진다.

## 5. 브랜드별 1만 도달 예상 시간(현재 관측치 기준 거친 추정)

첫 회차가 아직 안 끝나 정확한 채택률을 못 구했다. 대신 **이전 발굴(BFS)** 단계의
전체 채택률(발굴 1만 개 중 원고 대상 비율)로 하한을 가늠하면:

| 브랜드 | 발굴 채택률(원고대상/발굴1만) | 남은 목표(1만-현재) | 거친 추정(같은 비율 유지 시 필요 신규 수집) |
|---|---|---|---|
| 우아덤 | 12.0% | 8,797 | 약 73,000개 |
| 장으뜸 | 2.8% | 9,717 | 약 347,000개 |
| 팥순이 | 34.5% | 6,553 | 약 19,000개 |
| 뉴더미스 | 4.7% | 9,526 | 약 203,000개 |
| 코숨핏 | 3.6% | 9,637 | 약 268,000개 |

이 추정은 **BFS 깊이가 늘수록 채택률이 떨어질 가능성**을 반영하지 못한 낙관적
하한이다(주변 낱말로 갈수록 무관해질 개연성이 큼). 장으뜸·뉴더미스·코숨핏은
5만 개 상한(`DEFAULT_CAP`)에 먼저 닿아 원고 대상 1만 개에 못 미친 채 멈출 가능성이
높다 — 정리본 논리 자체를 넓히거나(신규 제품/증상 반영) 상한을 올리는 사용자
결정이 필요할 수 있다. 팥순이는 가장 유력하게 1만 개에 도달할 것으로 보인다.

## 6. 한계·차단 리스크·대책

- **속도 병목**: 클로드 채점(100개/묶음)·Codex 교차검증(묶음당 최대 300초)이
  순차라 회차당 수십 분이 걸린다. 5개 브랜드가 각자 프로세스라 서로 막지는
  않지만, 요금제(Claude Code CLI) 세션 하나를 공유하면 한도에 먼저 닿는 브랜드가
  대기할 수 있다(`LLMRouter`가 한도 재시도를 자동 처리하므로 멈추지 않고 늦어질
  뿐이다).
- **네이버 차단**: 기존 값(조회 간 3–6초, 세션 200회당 휴식) 그대로 재사용했다 —
  새 위험을 추가하지 않았다. 다만 신규 시드가 많아질수록(정리본 보충분 포함)
  누적 조회 횟수가 늘어 장기적으로 차단 위험이 커질 수 있다.
- **5만 상한**: 일부 브랜드(장으뜸·뉴더미스·코숨핏)는 채택률이 낮아 상한에 먼저
  닿을 것으로 보인다 — 이 경우 "시드 고갈"이 아니라 "용량상한"으로 멈추므로
  구분해 살펴야 한다.
- **정리본 시드 품질**: LLM으로 뽑은 보충 시드가 부정확하면(정리본이 짧거나
  모호하면) 채택률이 더 떨어질 수 있다 — 실패 시 정규식 추출로 자동 대체하지만,
  둘 다 품질이 낮으면 회차가 반복돼도 효과가 작다.
- **실행기 명령 미반영**: `키워드 채우기 시작|중지|현황` 명령은 코드만 배선했고
  지금 도는 실행기에는 반영되지 않았다(재시작 금지 규칙) — 다음 실행기 재시작부터
  사용 가능하다. 그전까지 채우기는
  [file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/keyword-fill-hidden.vbs](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/keyword-fill-hidden.vbs)로
  수동 실행/추적한다(`data/keywords/fill_progress.json`, `logs/keyword-fill-<브랜드>.log`).
- **git 상태**: 이 저장소는 `.git`이 없어(git init 안 됨) 커밋을 시도하지 않고
  파일 작성만으로 마쳤다.
