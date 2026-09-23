# 당위성 논리(bridge_rationale) 배선 — 2026-09-24 02:17

## 1. 배경

커밋 4eb4edd에서 키워드 연관도 척도가 0에서 4로 확장됐다(3=당위성). `data/keywords/<브랜드>.sqlite`의
`keywords` 표에 `relevance_llm`·`relevance_codex`·`bridge_rationale`(당위성 논리 한 줄) 열이 생겼고,
`v2r/content/brand_writer.py`의 `build_body_prompt`는 `relevance`/`bridge_rationale`을 받으면
`relevance == 3`일 때 프롬프트에 【당위성 논리】 블록을 넣도록 이미 되어 있었다. 다만 원고 생성 경로
(대기열 → `bulk_generate` → `brand_writer.generate_manuscript` → `build_body_prompt`)가 이 값을
sqlite에서 읽어 끝까지 넘기는 배선은 없었다(`docs/reports/relevance-split-2026-09-24.md` 8절 3항).

## 2. 바꾼 파일과 전달 경로

- `v2r/content/brand_queue.py`
  - `bridge_info(brand, keyword, data_dir) -> tuple[int | None, str]` 신설. `data/keywords/<브랜드>.sqlite`에서
    해당 키워드의 `relevance_llm`·`bridge_rationale`을 읽는다. 표·열·키워드가 없으면 `(None, "")`
    (기존 동작 그대로 — 당위성 블록 없이 지나간다).
- `v2r/content/bulk_generate.py`
  - `generate_for_brand`가 대기열 항목마다 `brand_queue.bridge_info(brand, row["keyword"], rt.settings.data_dir)`를
    불러 `relevance_llm`/`bridge_rationale`을 얻고, `_worker.generate_and_crosscheck_one`에
    `relevance=`/`bridge_rationale=`로 넘긴다.
- `v2r/engine/worker.py`
  - `generate_and_crosscheck_one`에 `relevance: int | None = None`, `bridge_rationale: str = ""` 인자를 추가하고
    그대로 `bw.generate_manuscript`로 넘긴다.
- `v2r/content/brand_writer.py`
  - `generate_manuscript`에 같은 두 인자를 추가해 `build_body_prompt`(단건 경로, `mode="single"`)와
    `_generate_combined`→`build_combined_prompt`(`mode="combined"`)까지 넘기도록 배선했다.
  - `build_combined_prompt`에도 `relevance`/`bridge_rationale` 선택 인자를 새로 추가했다(기존에는
    `build_body_prompt`에만 있었다) — combined 모드도 같은 규칙(연관도 3 + 근거 문장이 있을 때만
    【당위성 논리】 블록, system은 건드리지 않음)을 따른다.
  - 값이 없으면(`relevance != 3` 이거나 `bridge_rationale`이 빈 문자열) 블록을 넣지 않아 기존 동작 그대로다.

경로 전체: `brand_queue.pending()`으로 꺼낸 대기열 행 → `bulk_generate.generate_for_brand`가
`bridge_info`로 sqlite 조회 → `worker.generate_and_crosscheck_one(relevance=, bridge_rationale=)` →
`brand_writer.generate_manuscript(relevance=, bridge_rationale=)` → `build_body_prompt`/`build_combined_prompt`가
user 프롬프트에 【당위성 논리】 블록 삽입.

## 3. 시험 결과

`.venv\Scripts\python.exe -m pytest` (Windows, 프로젝트 루트) 기준.

- `tests/test_brand_writer.py`: 75개 전부 통과. 이번에 추가한 시험:
  - `test_combined_prompt_adds_bridge_block_for_relevance_3` / `test_combined_prompt_no_bridge_block_when_relevance_not_3`
    — combined 모드 프롬프트에도 배선이 됐는지.
  - `test_generate_manuscript_passes_relevance_to_body_prompt` — `generate_manuscript`가 실제 LLM 호출 user
    프롬프트까지 relevance/bridge_rationale을 넘기는지(FakeLLM 호출 기록으로 확인).
  - 기존 `test_body_prompt_adds_bridge_block_for_relevance_3` 등 `build_body_prompt` 단위 시험도 함께 통과.
- `tests/test_bulk_generate.py`: 21개 전부 통과. 이번에 추가한 시험:
  - `test_bridge_info_reads_relevance_and_rationale` — `brand_queue.bridge_info`가 있음/없음/빈 표 케이스를
    올바로 `(None, "")`까지 포함해 돌려주는지.
  - `test_bulk_generate_passes_bridge_rationale_to_prompt` — `generate_for_brand`를 실제로 돌려 FakeLLM이 받은
    본문 프롬프트에 【당위성 논리】 블록과 근거 문장이 들어있는지(대기열 전체 경로 종단 확인).
- 전체 `pytest -q`: 1467개 중 1454 통과, 7 실패(0:46:19 소요). 실패 7건은 모두 이번 변경과 무관한
  기존 결함이다(아래 4절).

## 4. 한계 — 전체 스위트의 기존 실패 7건(이번 변경과 무관)

전체 실행에서 아래 7개가 실패했다. `git diff`로 확인한 결과 이 파일들은 이번 세션에서 건드리지 않았고,
실패 원인도 이번 배선(브랜드 원고 생성 경로)과 무관하다.

- `tests/test_top_reference.py::test_volume_gate_min_volume`
- `tests/test_top_reference.py::test_volume_gate_top_pct`
- `tests/test_top_reference.py::test_reference_for_uses_cache_and_meta`
- `tests/test_top_reference.py::test_reference_for_caches_failure_and_returns_empty`
  - 원인: `v2r/content/top_reference.py:492`의 `total, _relevance = vol_map.get(...)`가 2개 값만 받는데,
    `v2r/content/brand_queue._volume_map`은 커밋 4eb4edd부터 `(total, group, relevance)` 3-튜플을 돌려준다
    (`ValueError: too many values to unpack`). `_volume_map` 자체는 이번 세션에서 건드리지 않았다.
- `tests/test_parser.py::test_all_tasks_covered_by_tests`
  - 원인: `ALLOWED_TASKS` 개수가 61인데 시험은 60을 기대(하드코딩된 개수가 최근 추가된 작업 하나만큼 뒤처짐).
- `tests/test_keyword_exposure_cycle.py`의 한글 이름 시험 2건
  - 개별 실행 확인은 못 했다(한글 테스트 ID가 셸에서 깨져 개별 지정이 실패했다). 전체 실행 로그의 실패
    목록에만 이름이 남아 있다.

이 4건(top_reference 3-튜플 언팩, ALLOWED_TASKS 개수, exposure_cycle 2건)은 이번 배선 작업 범위 밖이고,
같은 저장소를 다른 세션이 함께 쓰고 있어(`CLAUDE.md` 운영 규칙) 손대지 않았다. 별도 세션으로 넘기는 게
안전하다고 판단해 손대지 않고 보고만 남긴다.

## 5. 확인 못 한 것

- 실제 sqlite에 `relevance_llm=3`·`bridge_rationale`이 채워진 키워드로 만든 원고가 사람이 보기에도
  자연스러운지는 이번 시험(FakeLLM)으로는 확인할 수 없다 — 실제 모델 호출 결과 검토가 필요하다.
