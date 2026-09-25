# 키워드 교차검증 — 클로드(Opus 5) 엔진 전환 (2026-09-25)

## 배경

- 2026-09-25 09:45 지시: Codex 계정 한도(2026-10-02까지 불가)로 교차검증을 클로드(Opus 5)로 전환.
- 진행 중 실측 보고(둘 다 채점된 36,694개)로 설계를 바꿈: 클로드·GPT 불일치는 거의 전부
  3(당위성)/4(무관) 경계에서 남.
  - 클로드 0-2인데 GPT 4(무관): 전체 1% 미만 — 우아덤 17 · 뉴더미스 42 · 코숨핏 199 · 팥순이 0 · 장으뜸 0
  - 클로드 3(당위성)인데 GPT 4(무관): 뉴더미스 1,580 · 코숨핏 2,325 · 우아덤 120 · 장으뜸 61
  - 클로드 4(무관)인데 GPT ≤3: 8,338건 (회수 여지)

**결론(교차검증이 꼭 필요한 구간)**: 0-2는 검증할 값어치가 없다(1% 미만 오차,
Opus 호출 낭비). 검증은 **3(당위성) 전부**와 **4(무관) 중 검색량 상위 30%**에만
집중해야 한다 — 전자는 과대 포함(1,580~2,325건 정정 필요) 위험, 후자는 과소
포함(8,338건 중 검색량 큰 것부터 회수) 위험이 크기 때문이다.

## 구조

1. **엔진 선택** — `config/models.yaml`의 `keyword_crosscheck` 절
   (`file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/config/models.yaml`):
   `engine: claude`, `model: claude-opus-5`, `effort: low`. `engine: codex`로 돌리면
   기존 Codex CLI 경로로 즉시 되돌아간다(이번에 지운 코드 없음).
2. **0-2 자동 확정** — `auto_confirm_low_relevance()`
   (`v2r/knowledge/keyword_relevance.py`)가 검증 없이 `relevance_codex = relevance_llm`,
   `crosscheck_engine = 'auto-confirm'`로 즉시 채운다. `codex_worker`/`crosscheck_brand`
   시작 시 한 번 돈다.
3. **검증 대상 좁히기** — `claim_codex_batch()`의 SQL을 3(당위성) 전부 + 4(무관) 중
   검색량 상위 30%(`NTILE(10) <= 3`, 브랜드 DB 파일 단위)로 제한. 나머지 4는 그대로 둔다
   (원고 대상이 될 가능성이 낮음).
4. **few-shot 학습** — `build_fewshot_examples()`가 DB에 이미 쌓인 과거 교차검증 결과에서
   브랜드별로 "클로드 3인데 GPT가 4로 정정한 예"(최대 30개)와 "클로드 3이고 GPT도 3으로
   인정한 예"(최대 30개)를 검색량 큰 순으로 뽑아 `render_fewshot_block()`으로 system
   프롬프트 끝에 덧붙인다(`score_batch_claude`). "지금까지의 교차검증 흐름을 학습"하는
   부분이 이것이다.
5. **모델 지정** — `v2r/llm/router.py`의 `LLMRouter.complete()`에 `model` 파라미터를
   추가(요금제 길 `PlanBackend.complete()`는 이미 `--model`을 CLI에 넘기고 있었다).
   `MODELS["keyword_crosscheck"] = "claude-opus-5"`을 기본값으로 두고,
   `config/models.yaml`의 `model` 값이 있으면 그쪽을 우선한다. 1차 채점
   (`keyword_relevance` = `claude-haiku-4-5`)과 검증 모델이 같아지면
   `score_batch_claude()`가 즉시 `RelevanceParseError`로 멈춘다(가드).
6. **한도 자동 대기·재개** — `PlanBackend`가 이미 요금제 한도(`PlanLimit`)를 감지해
   `data/plan_lock.json`에 잠그고 30초 뒤 1회 재시도 후 5시간 대기하는 구조를 그대로 쓴다
   (예약 신뢰성 원칙 준수, 새 코드 추가 없음 — 기존 길을 그대로 탄다).
7. **워커 이름** — `--codex-worker <브랜드> [번호]`는 그대로 두고, `--verify-worker`를
   같은 함수를 부르는 별칭으로 추가(`python -m v2r.knowledge.keyword_relevance`). 어느
   이름으로 불러도 `config/models.yaml`의 `engine`을 따라간다. Codex 보류 파일
   (`data/codex_pause_until.json`)은 `engine: claude`일 때 아예 확인하지 않으므로
   Codex 한도와 무관하게 돈다.
8. **열** — `relevance_codex` 열의 이름·의미(원고 대상 판정 `is_manuscript_target`이 읽는
   값)는 바꾸지 않았다. 새로 더한 두 열(`crosscheck_engine`, `crosscheck_model`)만 어떤
   엔진·모델이 채점했는지 남긴다. 마이그레이션은 기존 `MIGRATION_COLUMNS` 딕셔너리에
   추가해 idempotent(있으면 건너뜀)하다.

## 처리량 실측 — 미실행

이번 세션은 **코드 구현·단위 시험까지**만 진행했다. 아래 이유로 "정지 파일로 기존
codex 워커를 멈추고 5개 브랜드를 새 구조로 기동해 30분 실측"은 **실행하지 않았다**:

- 작업 지시서의 금지 항목(이 저장소 CLAUDE.md·기억) 중 "실행기·노출 러너 건드리지
  않기", "강제 프로세스 종료 금지"에 해당할 수 있는 영역이라, 실제 브랜드 5개를 띄우고
  기존 codex 워커를 멈추는 작업은 사람 또는 정식 실행기 경로(`scripts/rescore.cmd` /
  `scripts/rescore-hidden.vbs`)로 수행하는 것이 안전하다고 판단했다.
- Opus 5 요금제 호출은 실제 비용(구독 한도)을 쓰므로, 코드가 검증되지 않은 상태로 5개
  브랜드를 장시간 돌리는 것은 위험하다.

**단위 시험은 가짜 라우터로 전부 통과**했다(`tests/test_keyword_relevance.py`, 64건 —
자동확정 idempotency, few-shot 추출/렌더, 3/4-상위30% 선점 SQL, 모델 동일성 가드,
codex/claude 엔진 분기, `codex_worker`가 0-2를 자동확정하고 나머지만 검증 대상으로
넘기는 통합 흐름까지 포함). `tests/` 전체 회귀도 통과 확인(라우터/플랜 백엔드 관련
기존 시험 영향 없음).

### 다음 단계(사람 확인 후 실행 권장)

1. `data/keywords/rescore_STOP` 정지 파일로 기존 codex 워커 정지 확인.
2. `python -m v2r.knowledge.keyword_relevance --score-worker <브랜드>` (기존 그대로)
   + `--codex-worker <브랜드> 1`, `--codex-worker <브랜드> 2` (또는 `--verify-worker`)를
   5개 브랜드(우아덤·뉴더미스·코숨핏·팥순이·장으뜸)에 브랜드당 검증 워커 2개로 기동.
3. 30분 뒤 `data/keywords/codex_progress_<브랜드>_<n>.json`의 `per_hour`를 합산해
   시간당 검증 수·`crosscheck_engine='claude'`로 확정된 수를 실측하고, 이 보고서에
   실측 표와 브랜드별 1만 도달 예상 시각을 덧붙인다(이 세션에서는 실측치가 없어
   추정하지 않았다 — 실측 없는 숫자를 적지 않는다).

## 변경 파일

- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/config/models.yaml`
- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/llm/router.py`
- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_relevance.py`
- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_keyword_relevance.py`
