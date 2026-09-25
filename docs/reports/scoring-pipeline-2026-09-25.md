# 연관도 채점 파이프라인 분리 — 2026-09-25

## 1. 무엇을 바꿨나

재채점 워커(`rescore_worker`)는 브랜드당 프로세스 1개 안에서 클로드 채점 → Codex
교차검증을 **순차로** 돌렸다. 클로드 채점이 훨씬 빠른데(브랜드당 시간당
1,300~1,600개), Codex 교차검증이 그보다 느려서 같은 프로세스 안에서 막히면
클로드 채점도 같이 멈춘다 — 2026-09-25 08:30 실측으로 팥순이 GPT 미검증
16,204건·장으뜸 8,331건이 쌓여 있던 원인이다.

`v2r/knowledge/keyword_relevance.py`에 두 진입점을 새로 만들어 분리했다.

- `--score-worker <브랜드>`: `relevance_llm`이 없는 행만 검색량(`total`) 큰
  순으로 50개씩 묶어 클로드로 채점한다(브랜드당 1개 프로세스).
- `--codex-worker <브랜드> <번호>`: `relevance_llm`은 있는데 `relevance_codex`가
  없는 행을, **당위성(3) 후보 우선 + 검색량 큰 순**으로 50개씩 묶어 Codex로
  교차검증한다. 같은 브랜드에 번호 1·2 두 개까지 동시에 돌 수 있고, 같은 행을
  두 워커가 중복 처리하지 않도록 `claimed_at` 열을 sqlite 트랜잭션(`BEGIN
  IMMEDIATE`)으로 선점한다(15분 지나면 죽은 선점으로 보고 재배정).

브랜드 5개 × (score 1 + codex 2) = **15개 프로세스**를
`scripts/rescore.cmd`/`scripts/rescore-hidden.vbs`가 한 번에 띄운다. 정지는
`data/keywords/rescore_STOP` 파일(모든 워커가 다음 묶음 전에 멈춘다). 처리량은
`data/keywords/score_progress_<브랜드>.json`/`codex_progress_<브랜드>_<번호>.json`에
시간당 처리 수(`per_hour`)로 남는다.

채우기 순환(`keyword_fill_loop`)은 새로 수집한 키워드를 자체적으로 채점하던
로직을 껐다(`AUTO_SCORE_IN_FILL_LOOP = False`) — 이제 수집·저장만 하고, 채점은
위 두 워커가 전담한다.

## 2. 배포 중 발견한 사고와 수정 (08:44–09:24)

15개 프로세스를 처음 띄우자(08:44) codex-worker 10개가 전부 첫 묶음에서
`sqlite3.OperationalError: database is locked`로 죽었다. 같은 브랜드 안에서
score-worker 1개 + codex-worker 2개(+ 이미 돌고 있던 옛 `rescore_worker` 2개
— 장으뜸·팥순이는 이전 구조가 아직 안 끝나 있었다)가 같은 sqlite 파일을
동시에 쓰다가 기본 5초 대기(`sqlite3.connect` 기본 timeout)를 넘긴 것이다.

`_connect_concurrent()`를 추가해 `timeout=30`·`PRAGMA journal_mode=WAL`·
`PRAGMA busy_timeout=30000`으로 바꾸고, `claim_codex_batch`의 `BEGIN
IMMEDIATE`에도 잠김 시 5회 재시도(1~5초 백오프)를 더해 09:11 커밋
(`9ab6d4f`)에 포함해 재배포했다. `scripts/codex-restart.cmd`로 codex-worker
10개만 09:23 다시 띄웠고, 이후 로그에는 `database is locked`가 더 나오지
않았다(수정 확인됨).

옛 `rescore_worker`(장으뜸 PID 32072, 팥순이 PID 41016)는 이 정지 파일을
모른다(코드에 STOP 검사가 없었다) — `rescore_STOP`을 08:42 잠깐 만들었지만
이 두 프로세스는 반응하지 않았고, 강제 종료는 금지 규칙이라 그대로 뒀다.
사실만 기록한다.

## 3. 30분 실측 (08:45 시작 → 09:30 기준, 위 사고로 codex 구간은 09:24 재시작 후 약 6분)

### 클로드 score-worker (사고 영향 없음, 처음부터 정상)

| 브랜드 | 채점 수(45분) | 시간당(`per_hour`) | 상태 |
|---|---|---|---|
| 우아덤 | 1,050 | 1,402 | running |
| 코숨핏 | 1,200 | 1,626 | running |
| 뉴더미스 | 350 | 1,294 | running(09:02 이후 갱신 없음 — 배치 처리 중 추정) |
| 장으뜸 | 50 | 1,488 | running(08:47 이후 갱신 없음 — 확인 필요) |
| 팥순이 | 500 | 1,495 | running(09:06 이후 갱신 없음 — 확인 필요) |

뉴더미스·장으뜸·팥순이가 중간에 진행 파일 갱신이 멈춘 것으로 보이는데,
같은 시각 옛 `rescore_worker`(장으뜸·팥순이)나 exposure-runner 등 다른
프로세스가 같은 요금제 길을 나눠 쓰며 대기가 길어졌을 가능성이 크다 —
후속 점검 필요.

### Codex codex-worker (09:24 재기동 이후 약 6분)

10개 워커 전부 `codex_checked = 0`. `database is locked` 크래시는 재발하지
않았지만, 각 묶음이 `gpt-6-astra` → `gpt-5.6-sol` 순으로 모델을 돌려가며
실패해(`codex 묶음 포기`) 아직 확정된 교차검증이 없다. `data/codex_pause_until.json`
파일이 없어 한도 감지 로직(`_detect_usage_limit`)이 이 실패를 "한도"로
인식하지는 않았다 — 문제 설명에 있던 Codex 계정(clktrade07@gmail.com) 한도
소모와 같은 현상으로 보이나, 6분 관측만으로 단정하기는 이르다. 계속 지켜봐야
할 대상으로 남긴다.

### 확정 원고 대상(`is_manuscript_target`, 09:30 실측)

| 브랜드 | 총 키워드 | 미채점 | GPT 미검증 | 확정 원고대상 |
|---|---|---|---|---|
| 우아덤 | 25,069 | 3,544 | 7,171 | 4,563 |
| 코숨핏 | 19,203 | 1,460 | 8,328 | 1,810 |
| 뉴더미스 | 19,744 | 3,416 | 10,460 | 1,206 |
| 장으뜸 | 39,776 | 4,178 | 34,027 | 309 |
| 팥순이 | 45,267 | 4,710 | 35,083 | 3,447 |

(수치는 채우기 순환이 계속 새 키워드를 넣고 있어 08:30 문제 설명 시점보다
총량·미검증 수 모두 늘어 있다 — 같은 방향의 병목이라는 점은 그대로다.)

## 4. 1만 개 도달 예상 재계산

클로드 적체(미채점)는 시간당 1,300~1,600개 속도면 브랜드당 1~3시간 안에
소진된다(가장 큰 팥순이 4,710건 기준 약 3시간). 문제는 GPT 교차검증 단계다
— codex-worker가 09:30 현재 실질 처리량 0으로 관측돼, **이 단계가 정상
가동되기 전까지는 확정 원고대상(둘 다 채점 완료)이 늘어나는 속도를 계산할
근거가 없다**. Codex가 정상 응답을 시작한 뒤 시간당 처리량이 확인되면(진행
파일의 `per_hour`로 자동 기록됨) 아래 방식으로 재계산할 수 있다:

- 남은 확정 필요 수 = 10,000 − 확정 원고대상(브랜드별)
- 예상 소요 시간 = 남은 필요 수 ÷ (min(클로드 시간당, GPT 시간당) × 당위성
  비율 추정치)
- Codex가 계속 0이면 ETA는 무한대 — 이 경우 계정 한도(clktrade07@gmail.com)
  회복 여부를 `data/codex_pause_until.json` 생성 여부로 먼저 확인해야 한다.

## 5. 남은 일

- codex-worker가 계속 0건이면 원인(모델 실패 문구 원문, 한도 여부)을 다음
  점검에서 다시 봐야 한다.
- codex-worker에는 아직 하트비트(잠금 갱신) 스레드가 없다 — 한 묶음이 15분을
  넘기면 다른 프로세스가 같은 번호를 중복 실행할 수 있다(현재는 발생 안
  했지만 잠재 위험으로 남긴다).
- 뉴더미스·장으뜸·팥순이 score-worker 진행 정체는 원인 미확인 — 다음 점검
  대상.

## 6. 시험·배포

`tests/test_keyword_relevance.py`(50개)·`tests/test_keyword_fill_loop.py`(25개)
전부 통과. 변경 파일만 커밋(`9ab6d4f`, "연관도 채점: 클로드 score-worker /
Codex codex-worker 분리")해 `origin/main`에 푸시했다.
