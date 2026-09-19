# 브랜드 원고 생성 비용 줄이기 (2026-09-19)

> 단가는 **2026-09 기준 추정이며 실제 요금표를 확인해야 합니다.**
> 표는 `v2r/llm/router.py`의 `PRICES_USD_PER_MTOK` 한 곳에만 적어 두었습니다.

## 1. 줄이기 전 (측정값)

| 항목 | 값 |
|---|---|
| 원고 | 25건 |
| 모델 호출 | 190번 (원고당 7.6번) |
| 입력 토큰 | 889,604 (호출당 평균 4.7천) |
| 출력 토큰 | 100,877 |
| 어림 비용 (Sonnet 5, 입력 $3 / 출력 $15 per 1M) | **약 $4.18** |

왜 이렇게 많았나:

1. **본문과 댓글을 따로 불렀다.** 원고 1건에 기본 2번.
2. **재시도가 매번 프롬프트를 통째로 다시 보냈다.** 검증에 한 줄만 걸려도
   브랜드 규칙 + 지침 원문 + 본문 전체를 다시 올렸다 (최대 6번).
3. **캐시를 쓰지 않았다.** 25건 내내 똑같은 규칙·지침 글이 매 호출마다
   새 입력 토큰으로 값이 매겨졌다.

## 2. 바꾼 것

### (1) 프롬프트 캐싱 — 고정부와 변하는 부분을 나눴다

`v2r/llm/anthropic.py`의 `create_message`가 이제 `system`을 블록 목록으로 보내고
`cache_control: {"type": "ephemeral"}`을 붙입니다.

그에 맞춰 프롬프트를 갈랐습니다 (`v2r/content/brand_writer.py`).

| 어디 | 무엇이 들어가나 | 캐시 |
|---|---|---|
| `system` (고정부) | 말투 규칙, 브랜드 규칙표, 글자 수, 금칙어, 자리표시자 예시, 12개 구조·역할, 지침 원문 | 걸린다 |
| `user` (변하는 부분) | 작성 키워드, 카페, 페르소나 씨앗, 이번 본문(댓글용), 어긴 규칙 | 안 걸린다 |

- `build_body_prompt` / `build_comments_prompt`는 그대로 `(system, user)`를 주지만
  내용 비중이 바뀌었습니다: 본문은 system 1,570자 / user 247자,
  댓글은 system 2,208자 / user 186자 (지침 원문을 빼고 잰 값).
- 캐시 읽기·쓰기 토큰(`cache_read_input_tokens` / `cache_creation_input_tokens`)을
  `usage`에 쌓고 원고 통계(`stats`)에도 적습니다.
- **캐시가 깨지지 않게** 키워드 같은 변하는 값은 system에 한 글자도 넣지 않습니다
  (테스트가 이걸 지킵니다).

### (2) 재시도를 얇게 — 걸린 자리만 다시 받는다

- `failing_comment_labels(checks)` — 검증 결과에서 문제가 있는 댓글 라벨만 골라냅니다.
- `build_partial_retry_prompt(...)` — 그 자리 이름과 구체적인 위반만 담은 user를 만듭니다
  (직전 댓글 12개도, 본문도 다시 보내지 않습니다).
- `merge_comments(current, payload)` — 부분 응답 `{"댓글2": "...", "대댓글4": "..."}`를
  라벨로 갈아 끼우고, **합친 12개를 다시 검증**합니다.

재시도 1번의 user가 수천 자에서 **약 120자**로 줄었습니다.

### (3) 한 번에 모드 (`mode="combined"`)

- `generate_manuscript(..., mode="combined")` — 제목 + 본문 + 댓글 12개를
  JSON 하나로 **한 번에** 받고 둘 다 검증합니다.
- 댓글만 어긋나면 (2)의 부분 재시도로 이어 붙입니다. 본문이 어긋나면 통째로 다시.
- 기본값은 그대로 `single` (`brand_writer.DEFAULT_MODE`).
- 명령에서 `한번에` 라고 적으면 `combined`로 돕니다
  (`TaskSpec.generate_mode`, 예: `우아덤 원고 3개 한번에 만들어줘`).

### (4) 비용 표시

- `v2r/llm/router.py`의 `estimate_cost(usage, model)` — 백만 토큰당 단가표로
  달러를 어림합니다. 모델별 누계(`usage["by_model"]`)가 있으면 모델마다 제 단가를 씁니다.
- 원고 통계(`stats["estimated_usd"]`)와 작업 결과(`_generate_brand`의 `estimated_usd`)에
  같이 담깁니다.

## 3. 줄인 뒤 (어림)

호출 한 번의 입력 4.7천 토큰 가운데 고정부를 약 4.3천으로 잡고 계산한 **추정치**입니다.

| | 호출 | 입력(새) | 캐시 쓰기 | 캐시 읽기 | 출력 | 어림 비용 |
|---|---|---|---|---|---|---|
| 전 | 190 | 889,604 | 0 | 0 | 100,877 | **$4.18** |
| 후 (`single` + 캐싱 + 부분 재시도) | 190 | 약 60,000 | 약 43,000 | 약 774,000 | 100,877 | **약 $2.1** |
| 후 (`combined`) | 약 75 | 약 30,000 | 약 50,000 | 약 325,000 | 약 65,000 | **약 $1.4** |

- 25건 기준 **$4.2 → 약 $2.1 (절반)**, `한번에` 모드까지 쓰면 **약 $1.4 (1/3)**.
- 캐시는 5분 동안만 살아 있습니다. 원고를 띄엄띄엄 만들면 캐시 쓰기가 늘어
  절감폭이 줄어듭니다 (연달아 돌릴수록 유리).
- 실제 값은 결과의 `estimated_usd`와 `cache_read_input_tokens`로 확인하세요.
  캐시 읽기가 계속 0이면 고정부에 변하는 값이 섞인 것입니다.

## 4. 만진 파일

| 파일 | 무엇 |
|---|---|
| `v2r/llm/anthropic.py` | `system_blocks`, `create_message`의 캐시 블록·캐시 토큰 집계 |
| `v2r/llm/router.py` | `PRICES_USD_PER_MTOK`, `estimate_cost`, `LLMRouter.estimated_cost` |
| `v2r/llm/prompts.py` | `BRAND_COMBINED_SYSTEM`, `BRAND_PARTIAL_RETRY_SYSTEM` |
| `v2r/content/brand_writer.py` | 프롬프트 고정/변동 분리, `build_combined_prompt`, `build_partial_retry_prompt`, `failing_comment_labels`, `merge_comments`, `DEFAULT_MODE`, `mode` 인자, 토큰·비용 통계 |
| `v2r/command/spec.py` · `parser.py` | `generate_mode` 슬롯과 `한번에` 해석 |
| `v2r/engine/worker.py` | `_generate_brand`가 `mode`를 넘기고 `estimated_usd`를 답에 싣는다 |
| `docs/USAGE.md` | 비용 표시와 `한번에` 안내 |
