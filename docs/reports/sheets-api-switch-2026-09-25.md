# 구글 시트 쓰기: 브라우저 조작 → Apps Script API 전환 (2026-09-25)

## 전환 범위

- 신규 `v2r/sources/sheets_api.py`: `config/sheets_api.yaml`을 읽어 배포된 Apps Script
  웹앱(`docs/appsscript/v2r_sheet_api.gs`)에 POST하는 클라이언트. 재시도 3회(지수
  백오프 1s·2s·4s), 타임아웃 120초, 응답 `ok` 검증, 실패 시 `SheetsApiError`.
  action은 `append`/`update_by_key`/`delete_by_key`/`snapshot` 4개만.
- `v2r/sources/sheets_writer.py`의 다음 함수가 `sheets_api.enabled`면 API를 쓰고,
  아니면 기존 브라우저(Playwright) 경로로 간다(설정 파일이 없거나 `enabled: false`
  이면 자동으로 기존 경로 폴백):
  - `append_rows` → API 모드에서 `_append_rows_api` 사용. **행 삽입
    (`_ensure_grid_rows`)은 API 모드에서 절대 호출되지 않는다** — 애초에 그 경로
    (`_write_tsv_at`)를 타지 않는다.
  - `update_by_key`(key_column="H"일 때만 API, 그 외는 기존 경로) — 갱신 열은
    A/G/I/J/K/L/M/N만(H는 별도 `keyword` 필드), B~F(비밀번호 E 포함)는 요청 본문에
    아예 담기지 않는다.
  - `sync_keywords_to_sheet` — API 모드에서는 시트 읽기·기존 키워드 대조·A1 확인을
    전부 `snapshot`으로 한다(공개 CSV export 대신). 브랜드당 상한(1,000행/회)·
    엄격 판정(`keyword_relevance.is_manuscript_target`, 0에서 3)은 그대로.
  - `apply_exposure` — 러너의 A·G·J·K·L 갱신을 `update_by_key` **50개씩 묶어**
    보낸다(`_BrandLock` 유지). I(통합검색)는 스냅샷으로 이미 있는지 확인 후
    비어 있을 때만 채운다.
  - 신규 `delete_rows_by_key`(정리용, 비대상·중복 삭제) — **API 전용**이다. 이
    모듈에는 원래 브라우저로 행을 지우는 함수 자체가 없었으므로(행 삽입만
    있었음) 없어진 기능은 없다. API 비활성이면 오류만 돌려주고 아무 것도 하지
    않는다.
- 안전 가드(요청 3): `append`는 API 모드에서 **쓰기 전 snapshot으로 A1=="카페"
  확인**(아니면 중단·경고), **쓰기 후 snapshot 행 수 증가 == added 확인**(불일치면
  이후 chunk 진행 중단·경고, `mode: csv_only`로 표시). 300행씩 나눠 보낸다.

## 남은 브라우저 경로

- **P2 합계 표(1행 총계 셀)**: API에 해당 작업이 없어 `apply_exposure`는 이
  부분만 항상 기존 `_write_verified`/`set_cell`(Playwright) 경로를 쓴다.
  `try/except`로 감싸 실패해도(예: 실행기 재시작 중 Playwright 문제) 위
  키워드별 A/G/J/K/L 갱신 결과(`written`)는 그대로 돌려준다.
- `copy_row_format`, `_list_tabs`/`_second_tab_gid`, `update_rows`(API 미분기,
  브라우저 경로만 — 호출부가 적어 이번 범위에서 제외), 행 삽입
  (`_ensure_grid_rows`) 자체는 코드는 남아 있으나 API 모드 함수들에서는 호출
  경로가 없다.

## 소량 시험 결과 (실측, 2026-09-25)

각 브랜드 시트에서 키워드 1개의 **J열**(최종 편집 일시)을 테스트 값으로 갱신 →
스냅샷으로 반영 확인 → 원래 값으로 즉시 원복.

| 브랜드 | 키워드 | 갱신 | 검증 | 원복 | 비고 |
|---|---|---|---|---|---|
| 우아덤 | 비타민C | 성공(updated=1) | 일치 | 성공(updated=1) | 원래 J: 2026-09-24T05:50:40.000Z |
| 코숨핏 | 수면테이프 | 성공(updated=1) | 일치 | 성공(updated=1) | 원래 J: 2026-09-24T22:12:54.000Z |
| 장으뜸 | 배란일 계산기 | 성공(updated=1) | 일치 | 성공(updated=1) | 원래 J: 2026-09-23T20:28:49.000Z |
| 뉴더미스 | — | **시도 안 함** | — | — | A1 확인 실패(스냅샷 A1="A1265") — 가드가 작동해 어떤 쓰기도 시도되지 않음 |
| 팥순이 | — | **시도 안 함** | — | — | A1 확인 실패(스냅샷 A1="A9199") — 가드가 작동해 어떤 쓰기도 시도되지 않음 |

뉴더미스·팥순이 두 시트는 A1이 이미 훼손된 상태(기존 코드 주석에 있는
2026-09-25 사고와 같은 증상)였고, 이번 API 클라이언트의 A1 가드가 정상적으로
작동해 그 상태에서 아무 것도 쓰지 않고 멈췄다. 이 두 시트는 이번 작업 범위가
아니므로 복구를 시도하지 않았다 — 복구 필요 여부는 별도 확인 요망.

## 단위 시험

- `tests/test_sheets_api.py`(신규): httpx 가짜 응답으로 설정 로딩·재시도·
  지수 백오프·오류 응답·`config=` 직접 전달 검증 — 9개 전부 통과.
- `tests/test_sheets_writer.py`: API 분기(append/update_by_key/apply_exposure
  배치/A1 가드/행 수 불일치 가드/delete_rows_by_key) 신규 시험 추가, 기존
  브라우저 경로 시험은 API 비활성(임시 폴더 = `config/sheets_api.yaml` 없음)
  상태를 유지하도록 `repo_root` 보강. 59개 중 58개 통과.
  실패한 1개(`test_sync_keywords_to_sheet_picks_only_target_rows`, picked
  2 vs 3)는 이번 변경 **이전부터** 실패하던 무관한 기존 버그(원래 코드로
  되돌려 재현 확인함) — 이번 작업 범위 밖이라 손대지 않음.
- 관련 확장 시험(`-k "sheets or exposure or keyword_exposure or worker"`,
  241개): 240 통과, 실패 1개는 위와 같은 기존 버그.

## 기타

- 실행기(serve)·노출 러너는 **재시작 필요**(코드가 바뀐 `sheets_writer.py`를
  다시 읽어야 새 API 경로가 적용된다).
- git add는 `v2r/sources/sheets_api.py`, `v2r/sources/sheets_writer.py`,
  `tests/test_sheets_api.py`, `tests/test_sheets_writer.py` 4개 파일만.
  커밋 `2b837ef`, `git push origin HEAD` 완료.
