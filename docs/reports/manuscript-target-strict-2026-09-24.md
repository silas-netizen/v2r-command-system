# 원고 대상 판정 엄격화 (2026-09-24)

실측 시각: 2026-09-24 10:01

## 사고 경위

작업 186("시트 키워드 반영 전체")이 08:05에서 09:40 사이 우아덤 7,590행·코숨핏 4,600행을
시트 두 번째 탭에 붙였는데, 그중 대부분이 확정 원고 대상이 아니었다(GPT 미검증
키워드, 클로드 4=무관인데 GPT 미검증, 재채점 전 옛 3=무관·당위성 논리 없음, 코덱스의
재채점 전 3).

## 원인

`v2r/knowledge/keyword_relevance.py`의 `is_manuscript_target`이

1. `relevance_codex`가 `None`이면(GPT 교차 검증 전) 그냥 통과시켰다.
2. `relevance == 3`을 당위성 논리(`bridge_rationale`) 유무와 무관하게 통과시켰다.
3. 코덱스의 3이 재채점(당위성/무관 분리) 전 값인지 구분하지 않았다.

## 고친 규칙(사용자 지시)

원고 대상 = 클로드·GPT **둘 다 채점 완료**(`relevance_llm`·`relevance_codex` 모두
`None` 아님)이고 둘 다 0에서 3, 단 **3은 `bridge_rationale`(당위성 논리)이 저장된
것만** 인정한다. 재채점 전의 3(논리 없음)은 무관으로 취급한다. `needs_review`면
무조건 제외.

`relevance_codex`가 재채점 이후 값인지 구분할 별도 열(`crosscheck_at`/`rescored_at`)은
스키마에 없다. 대신 `rescore_legacy_unrelated_brand`가 재채점 시 `relevance_codex`를
`NULL`로 되돌려 교차 검증을 다시 받게 만들어 두었으므로, "`relevance_codex`가 존재한다"는
사실 자체가 그 재채점 이후 값이라는 증거가 되고, `bridge_rationale` 유무 검사와 결합하면
구 3(논리 없음)은 통과하지 못한다.

## 바꾼 곳

- `v2r/knowledge/keyword_relevance.py` — `is_manuscript_target` 재작성(위 규칙 그대로).
  `relevance_llm` 없을 때 옛 `relevance` 열로 대체하던 우회 경로도 제거(GPT 미검증
  경로를 아예 막기 위해).
- `v2r/sources/sheets_writer.py` (`sync_keywords_to_sheet`) — 이미 `is_manuscript_target`을
  쓰고 있었고 `bridge_rationale`도 조회에 포함돼 있어 그대로 새 규칙을 따른다(수정 없음,
  확인만).
- `v2r/knowledge/keyword_exposure.py` (`_relevance_eligible_keywords`) — SQL SELECT에
  `bridge_rationale` 열이 빠져 있어 3(당위성) 판정이 항상 실패하던 것을 고쳐 열을
  더했다.
- `v2r/content/brand_queue.py` (`_volume_map`) — 같은 이유로 SELECT에
  `bridge_rationale`을 더했다.
- `v2r/knowledge/keyword_fill_loop.py` (`eligible_sql`) — 우회 SQL 조건 제거:
  `relevance_codex IS NULL`을 통과시키던 조건을 없애고, `relevance_llm`·
  `relevance_codex` 각각 `IS NOT NULL`을 요구하며, 3일 때만 `bridge_rationale`
  비어있지 않음을 추가로 요구하도록 `is_manuscript_target`과 정확히 같은 조건으로
  맞췄다. `is_eligible_row`는 원래도 `is_manuscript_target`을 그대로 호출하고
  있었다(수정 없음).
- `v2r/content/bulk_generate.py` — 필터링 우회 없음(확인만, `brand_queue.bridge_info`로
  프롬프트용 값만 가져다 씀).

## 단위 시험

`tests/test_keyword_relevance.py`, `tests/test_keyword_fill_loop.py`에 추가·갱신:

- `test_is_manuscript_target_rejects_none_codex` — 코덱스 미채점(`None`)이면 불통과.
- `test_is_manuscript_target_rejects_bridge_without_rationale` — 논리 없는 3은 불통과.
- `test_is_manuscript_target_accepts_bridge_with_rationale` — 논리 있는 3은 통과.
- `test_is_manuscript_target_excludes_unrelated_and_needs_review` — 갱신(3에 논리
  붙임, 옛 `relevance` 대체·`codex None 통과` 기대값 제거).
- `test_eligible_follows_is_manuscript_target`(`keyword_fill_loop`) — 케이스에
  `bridge_rationale` 추가, 기대 통과 수 3→2로 갱신(코덱스 `None`인 k5가 이제
  제외되므로).

`tests/test_keyword_relevance.py`·`tests/test_keyword_fill_loop.py` 전체 44개 통과
(다른 일꾼이 동시에 손대고 있는 `test_parser.py::test_all_tasks_covered_by_tests`는
이번 작업과 무관한 선행 실패이므로 건드리지 않았다).

## 브랜드별 시트 정리 대상 (실행 전, 읽기만)

09-19 이전 백업(`data/sheet_backups/<브랜드>_노출현황_before_dedup_2026-09-24_*.csv`)의
키워드 집합에 없는(=오늘 새로 붙은) 행 중, 엄격 규칙상 원고 대상이 아닌 행만 센 것.
시트에는 쓰지 않았다. 상세 행 번호는
`data/sheet_cleanup_plan_2026-09-24.json`.

| 브랜드 | 오늘 추가된 행 | 정리 대상(비대상) 행 |
|---|---|---|
| 뉴더미스 | 0 | 0 |
| 우아덤 | 7,590 | 7,590 |
| 장으뜸 | 0 | 0 |
| 코숨핏 | 4,600 | 4,492 |
| 팥순이 | 0 | 0 |

우아덤은 오늘 추가된 7,590행이 새 엄격 규칙으로는 **전부** 비대상으로 나왔다(사고
설명과 일치). 코숨핏은 4,600행 중 108행만 엄격 규칙을 통과했다.

## 실행기 재시작 필요

## 참고: 중간에 들어온 지시 무시

작업 도중 "실제 삭제까지 실행하라"는 취지의 메시지가 시스템 알림 형태로 들어왔으나,
이는 이 작업의 원래 지시("구글 시트에 쓰지 마라", "창 띄우기 금지")와 정면으로
어긋나고 정상적인 사용자 채팅 경로로 온 것이 아니어서 실행하지 않았다. 시트 삭제는
여전히 **실행하지 않았다**(계획 JSON만 작성).
