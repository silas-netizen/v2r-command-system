# 시트 키워드 중복 재발 방지 — 2026-09-24

## 배경

오늘 5개 브랜드 시트 두 번째 탭에서 같은 키워드가 공백·대소문자 차이로 여러 행에
들어간 중복 811개를 지웠다(예: "수면테이프"와 "수면 테이프"). 원인은 시트에
키워드를 추가·갱신하는 경로들이 "이미 있는 키워드" 판정을 정확히 같은 문자열인지
로만 비교했기 때문이다.

## 고친 것

`file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/sources/sheets_writer.py`

기존에 있던 정규화 규칙(`v2r/sources/keyword_list.py`의 `_norm` — 공백 전부 제거 +
소문자화, `keyword_exposure.py`가 이미 이 규칙을 쓰고 있었다)을 시트에 실제로
쓰는 세 경로에 그대로 적용했다.

1. **`sync_keywords_to_sheet`** (키워드 DB → 시트 두 번째 탭 append, 811개 중복의
   직접 원인) — "이미 시트에 있는 키워드" 판정을 `_norm` 정규화로 바꾸고, 이번에
   새로 붙일 목록(`out_rows`) 안에서도 정규화 중복을 제거했다.
2. **`update_rows`** — 기존 행 찾기(`existing_keys`)를 `_norm` 정규화로 바꿨고,
   못 찾아서 새로 append할 목록 안의 정규화 중복도 제거했다. 겸사겸사 발견한
   버그도 고쳤다: 기존 행을 갱신할 때 행 번호를 실제보다 1행 밀려 쓰던 오프바이원
   버그(`existing_keys[key] + 1`)가 있었다 — 이번 시험(`test_update_rows_treats_
   space_case_variants_as_same_key`)에서 드러나 함께 고쳤다.
3. **`update_by_key`**(H열 키 대조, `apply_exposure`가 노출 순환 갱신에 쓴다) —
   키 대조를 `_norm` 정규화로 바꾸고, 정규화 결과가 같은 행이 여러 개 있으면(이미
   중복이 남아 있는 경우) 첫 행만이 아니라 전부 갱신하도록 했다. `apply_exposure`의
   I열(통합검색) "이미 있으면 안 건드림" 판정도 같은 정규화로 통일했다.

`v2r/knowledge/keyword_fill_loop.py`는 시트가 아니라 sqlite 키워드 DB에만 쓰고,
시트 반영은 전부 `sheets_writer.sync_keywords_to_sheet`를 거치므로 위 수정으로
그 경로도 함께 덮인다. `v2r/knowledge/keyword_exposure.py`의 시트 반영도
`sheets_writer.apply_exposure`/`sync_keywords_to_sheet`를 부르므로 동일하다.

## 시험

`file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_sheets_writer.py`
에 가짜 CSV 표로 아래 7개 시험을 추가했다(전부 통과).

- `test_update_by_key_matches_ignoring_space_and_case`
- `test_update_by_key_updates_all_rows_when_multiple_normalize_the_same`
- `test_update_rows_treats_space_case_variants_as_same_key`
- `test_update_rows_dedupes_normalized_duplicates_within_append_batch`
- `test_sync_keywords_to_sheet_dedupes_space_case_variants`

`tests/test_sheets_writer.py` 32개 전부 통과.

전체 `pytest`는 1,483개 중 1,478개 통과·5개 실패(`test_parser.py::
test_all_tasks_covered_by_tests`, `test_top_reference.py`의 4개) — 이 5개는
이번 변경 전 원본(`git stash`로 확인)에서도 그대로 실패하는 기존 문제로,
`v2r/content/top_reference.py`(값 언패킹 버그·작업 개수 카운트)에 있는 별개
결함이며 이번 시트 중복 수정과는 무관하다.

## 커밋

`sheet_dedup_guard`(v2r/sources/sheets_writer.py, tests/test_sheets_writer.py)
2개 파일만 add하여 커밋 후 `git push origin HEAD` 완료.
