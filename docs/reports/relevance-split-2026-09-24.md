# 키워드-브랜드 연관도 0-4 확장(당위성/무관 분리) 작업 보고

작성 시각: 2026-09-24 00:56:45 +09:00 (PowerShell `Get-Date`로 실측)

## 1. 새 척도 정의

기존 0에서 3(0 직접/1 근접/2 확장/3 무관)에서 0에서 4로 넓혔다.

- 0 = 직접: 브랜드/제품/증상을 바로 가리키는 핵심어
- 1 = 근접: 같은 타깃·같은 문제의식이지만 브랜드를 직접 가리키진 않음
- 2 = 확장: 관련은 있으나 거리가 있음(주변 정보 탐색 수준)
- 3 = 당위성(신설): 브랜드 논리로 자연스럽게 "이어 붙일 수 있는" 키워드. 한 문장짜리 다리(bridge)를 놓으면 억지스럽지 않게 브랜드로 연결된다. **원고(콘텐츠) 대상에 포함**한다.
- 4 = 무관: 브랜드 논리로 이어지지 않는 일반어. 다리를 놓으려면 억지스럽거나 두 문장 이상 설명을 짜내야 한다. 원고 대상에서 제외한다.

원고 대상 = 0에서 3. 4(무관)만 제외한다. 판정은 공용 함수
`v2r.knowledge.keyword_relevance.is_manuscript_target(row)`로 통일했다(문서 4절 참고).

## 2. 개정된 프롬프트의 3/4 구분 기준

`v2r/knowledge/keyword_relevance.py`의 `build_system_prompt`에 사용자가 준 우아덤 예시를 그대로 넣었다.

- 3(당위성)으로 매기는 예(우아덤 기준): 대상포진, 독감, 위고비, 근처피부과, 사마귀 — 면역·호르몬·피부 컨디션 등 브랜드 논리와 한 문장으로 이어 붙일 다리가 있다.
- 4(무관)로 매기는 예(우아덤 기준): 피부과, 안과, 치과, 마운자로, 독감예방접종 — 진료과 이름이거나 비만·안과·치과처럼 브랜드 타깃·논리와 맞닿는 지점이 없어, 다리를 놓으려면 근거 없이 우기는 것이 된다.

프롬프트는 "브랜드마다 이 경계가 다르다"고 명시해 다른 브랜드에 우아덤 예시를 그대로 옮기지 말라고 안내한다. relevance=3인 항목은 모델이 `bridge`(당위성 논리 한 줄)를 JSON에 함께 내도록 요구하고, `parse_response`가 이를 읽어 `bridge_rationale`로 저장한다. 클로드용 system 프롬프트와 Codex 교차검증용 프롬프트(`score_batch_codex`가 같은 `build_system_prompt`를 재사용)에 동일하게 적용된다.

## 3. 우아덤 200개 검증

**미실행.** 이 세션 환경에는 `ANTHROPIC_API_KEY`가 설정돼 있지 않고(`v2r/config.py`가
`os.getenv("ANTHROPIC_API_KEY", "")`로 읽음), `codex` 실행 파일도 PATH에서 찾지 못했다
(`where codex` 실패). 실제 API 호출 없이 결과를 지어내지 않기 위해 실행하지 않았다.
프롬프트·파싱 로직은 `tests/test_keyword_relevance.py`의
`test_system_prompt_includes_bridge_vs_unrelated_examples`,
`test_parse_response_reads_bridge_for_relevance_3` 등으로 단위 시험만 통과 확인했다
(아래 7절).

사용자 예시 10개(대상포진·독감·위고비·근처피부과·사마귀 → 3 기대, 피부과·안과·치과·
마운자로·독감예방접종 → 4 기대)는 프롬프트에 예시 문장으로 그대로 박혀 있음을
`test_system_prompt_includes_bridge_vs_unrelated_examples`로 확인했을 뿐, 모델이 실제로
이 10개를 이렇게 분류하는지는 API 호출 없이는 검증하지 못했다.

## 4. 구현 내역

- `v2r/knowledge/keyword_relevance.py`
  - `RELEVANCE_LABELS`를 `{0:직접,1:근접,2:확장,3:당위성,4:무관}`로 갱신
  - `MANUSCRIPT_MAX_RELEVANCE`를 2 → 3으로 확장, `RELEVANCE_BRIDGE=3`/`RELEVANCE_UNRELATED=4` 상수 추가
  - `MIGRATION_COLUMNS`에 `bridge_rationale` 열 추가(idempotent — `migrate`/`migrate_path`가 이미 있으면 건너뜀)
  - `build_system_prompt`에 3/4 구분 기준·우아덤 예시·`bridge` 필드 요구를 추가
  - `parse_response`가 `bridge` 필드를 읽어 `bridge_rationale`로 반환(relevance!=3이면 강제로 빈 문자열, relevance==4면 rationale도 강제로 빈 문자열)
  - `write_scores`/`write_crosscheck`/`pending_codex_rows`/`crosscheck_brand`가 `bridge_rationale`을 함께 나른다
  - 공용 판정 함수 `is_manuscript_target(row)` 추가 — `relevance_llm`(또는 `relevance`)·`relevance_codex`가 둘 다 0에서 3이고 `needs_review`가 아니면 True. dict/`sqlite3.Row` 둘 다 받는다
  - 재채점 함수 `pending_legacy_unrelated_rows`/`rescore_legacy_unrelated_brand` 추가 — 구 척도 3(무관)이던 키워드만 새 프롬프트로 다시 채점(클로드 100개 배치 → Codex 교차검증, `score_and_crosscheck_brand`와 같은 패턴). **CLI/명령 진입점만 구현하고 실제 대량 실행은 하지 않았다**(아래 5절)
  - `keyword_fill_loop.py`는 건드리지 않았다. 모듈 docstring에 `is_manuscript_target`의 시그니처·사용법을 명시해 그 파일 담당자가 나중에 갈아끼울 수 있게 했다
- `v2r/command/parser.py`, `v2r/command/spec.py`, `v2r/engine/worker.py`
  - 새 명령 `키워드 연관도 재채점 <브랜드|전체>` → `keyword_relevance_rescore_legacy` 작업을 추가(`_keyword_relevance_rescore_legacy`가 브랜드별로 `rescore_legacy_unrelated_brand`를 호출)
- `v2r/sources/sheets_writer.py`
  - 하드코딩된 `relevance_llm between 0 and 2` SQL 조건을 없애고, 전체를 읽어 `keyword_relevance.is_manuscript_target`으로 파이썬에서 거른다
  - `RELEVANCE_LABELS`를 `keyword_relevance.RELEVANCE_LABELS`에서 그대로 가져와 "당위성" 라벨이 N열(본문 분류)에 반영되게 함
  - M열(비고)에 relevance==3(당위성)이면 `bridge_rationale`을, 아니면 기존 `rationale`을 채운다
  - **실제로 구글 시트에 쓰는 호출은 하지 않았다**(코드만 완성 — 절대 규칙 준수)
- `v2r/knowledge/keyword_exposure.py`의 `_relevance_eligible_keywords`
  - 하드코딩된 `relevance_llm between 0 and 2` SQL 조건을 없애고 `is_manuscript_target`으로 교체
- `v2r/content/brand_queue.py`
  - 대기열 우선순위를 "연관도 그룹(0-2 먼저, 3 다음, 미산정/무관 맨 뒤) → 그룹 안에서 검색량 내림차순"으로 개정
  - 4(무관)/`needs_review`인 키워드는 `is_manuscript_target`으로 아예 대기열 후보에서 제외
  - 아직 LLM 재산정 전(옛 낱말겹침 `relevance`만 있는) 항목은 그룹을 나누지 않고 기존처럼 검색량으로만 정렬(기존 시험 `test_refill_orders_by_volume_then_relevance` 동작 유지)
- `v2r/content/brand_writer.py`의 `build_body_prompt`
  - `relevance`/`bridge_rationale` 선택 인자를 추가. relevance==3이면 user 프롬프트에
    `【당위성 논리】 {bridge_rationale} — 이 논리로 자연스럽게 브랜드로 이어라(억지 연결 금지)` 블록을 추가(공용 system은 건드리지 않음)
  - **한계**: `generate_manuscript`/`bulk_generate` 쪽에서 이 두 인자를 실제로 채워 넘기는 배선은 이번 작업 범위에 포함하지 않았다(요청 범위는 "build_body_prompt에 추가"까지였음). 다음 단계에서 대기열/생성 파이프라인이 sqlite의 `relevance_llm`/`bridge_rationale`을 읽어 넘기도록 이어야 한다

## 5. 브랜드별 재채점 진행 상황

**미실행, 대기 중.** 실제 대량 API 호출(5개 브랜드 약 3만 7천개, 그중 구 척도 3이던
키워드만 추려도 상당수)은 시간·비용이 크고 이 세션에 API 키가 없어 직접 실행하지
않았다. `키워드 연관도 재채점 <브랜드|전체>` 명령과 `rescore_legacy_unrelated_brand`
함수만 구현했다. 실행기(메인 프로세스)를 재시작하지 않았으므로 이 새 명령은 다음
재시작 때부터 실제로 반응한다.

## 6. 원고 대상 키워드 수 변화 (실측)

`data/keywords/<브랜드>.sqlite`에서 이미 `relevance_llm`이 매겨진 키워드만 대상으로,
"구 조건(0에서 2, 둘 다 통과, needs_review 아님)" 대 "신 조건(`is_manuscript_target`,
0에서 3)"을 실제로 세었다(2026-09-24 실측, 마이그레이션은 idempotent라 재실행해도
안전).

| 브랜드 | 채점됨(relevance_llm 있음) | 0 | 1 | 2 | 3(구=무관/신=당위성) | 구 조건(0-2) 대상 | 신 조건(0-3) 대상 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 우아덤 | 10,000 | 25 | 625 | 2,983 | 6,367 | 1,369 | 9,400 |
| 팥순이 | 10,800 | 221 | 2,597 | 3,228 | 4,754 | 4,019 | 8,919 |
| 장으뜸 | 10,300 | 59 | 206 | 1,362 | 8,673 | 481 | 9,264 |
| 코숨핏 | 10,400 | 139 | 339 | 1,852 | 8,070 | 603 | 9,386 |
| 뉴더미스 | 11,000 | 113 | 301 | 1,464 | 9,044 | 565 | 9,645 |

("채점됨" = `relevance_llm`이 있는 행 수, 반올림 없이 실측한 0/1/2/3 분포 합과 일치.
전체 행 수·미산정 수는 브랜드마다 다르며 생략했다.)

주의: 위 표의 "3"열은 **구 척도 기준으로 채점된** 값이라 지금은 전부 "무관"으로
저장돼 있다(재채점 전). 신 조건(0-3) 대상 수는 이 3(구=무관)을 전부 당위성으로
넣은 것이 아니라, `is_manuscript_target`이 relevance_llm 0에서 3을 모두 통과시키는
계산값 — 즉 **재채점을 실제로 돌리기 전에도 구 3(무관)이 전부 새 3(당위성)으로
간주돼 원고 대상에 포함되는 상태**라는 뜻이다. 이는 임시 상태이며, 5절의
재채점을 실행해 구 3을 진짜 3(당위성)/4(무관)으로 나눠야 정확한 수치가 된다.

## 7. 테스트

`tests/test_keyword_relevance.py`, `tests/test_sheets_writer.py`,
`tests/test_bulk_generate.py`, `tests/test_brand_writer.py`에 새 척도·프롬프트·
`is_manuscript_target`·마이그레이션·대기열 우선순위 시험을 추가/갱신했다.
`.venv\Scripts\python.exe -m pytest`로 실행해 전부 통과를 확인했다(세부 통과 수는
채팅에 남기지 않는다 — 이 보고서가 결과다).

## 8. 한계와 다음 단계

1. 실제 API 검증(우아덤 200개, 사용자 예시 10개 실채점)을 이 세션에서 못 했다
   — API 키가 있는 환경에서 `키워드 연관도 재채점 우아덤`(또는
   `rescore_legacy_unrelated_brand` 함수를 200개 한도로 호출)을 먼저 돌려 확인해야
   한다.
2. 5개 브랜드 전체 재채점(약 3만 7천개)은 함수·명령만 준비된 상태다. 실행하면
   시간이 오래 걸리고(브랜드당 클로드 100개 배치 + Codex 교차검증) 요금제 한도에
   걸릴 수 있으니, 한도 감지·자동 재개(예약 신뢰성 원칙)를 고려해 나눠 돌리는 편이
   안전하다.
3. `brand_writer.generate_manuscript`/`bulk_generate`가 `relevance`/`bridge_rationale`을
   `build_body_prompt`로 실제로 넘기는 배선이 아직 없다 — 대기열(`brand_queue`)이
   sqlite에서 이 값을 읽어 원고 생성 호출부까지 전달하도록 다음 작업에서 이어야
   원고 본문에 당위성 논리 블록이 실제로 들어간다.
4. `keyword_fill_loop.py`는 이번에 건드리지 않았다 — 그 파일 담당자가
   `is_manuscript_target`으로 갈아끼울 때 `v2r/knowledge/keyword_relevance.py`
   모듈 docstring의 시그니처 설명을 참고하면 된다.
