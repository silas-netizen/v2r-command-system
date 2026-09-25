# K열 검색량 무조건 채우기 — 2026-09-24 구현 보고

작성 시각: 2026-09-24 02:41 KST

## 사용자 지시

"검색량을 모를 수가 없어. 검색량은 키워드 도구에 넣으면 다 나와. 무조건 채워 넣어."
— 2026-09-24 01:52, "모르면 K를 건드리지 않음"(12e9c33)을 거부하고, 모르면 네이버
키워드 도구에 직접 넣어 알아내서 채우라는 지시.

## 구현

- 새 함수 `ensure_volumes(rt, brand, keywords, fetch_fn=None)` —
  [v2r/knowledge/keyword_exposure.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_exposure.py)
  - `data/keywords/<브랜드>.sqlite`에 total>0이면 그 값을 그대로 쓴다.
  - 없거나 0이면 5개씩 묶어 네이버 키워드 도구(`naver_keyword_tool.fetch_related_keywords`,
    브랜드 전용 복제 프로필 + `headless=True`)로 조회하고, 결과를 DB에 반영한다(없는
    키워드는 새 행 삽입, 있으면 pc/mobile/total만 갱신 — `relevance` 열은 건드리지 않음, 시험으로 확인).
  - 도구가 씨앗 자신의 행을 안 돌려주면 네이버 "&lt; 10" 표기 관례를 따라 최소값 5로 채운다.
  - 조회 자체가 실패(로그인 풀림·차단 등)하면 예외를 올리지 않고 경고 로그만 남기고
    그 키워드는 결과 dict에서 빠진다(이때만 K를 건드리지 않는 것과 동치).
  - 프로필 잠금은 `naver_session.acquire_profile_lock`을 그대로 타므로 노출 순환
    러너와 채우기 순환이 같은 프로필을 동시에 열지 않는다. "잠금 대기 시간 초과"
    (`ProfileLockTimeout`)면 3초 쉬고 1회 재시도한다.
- `next_cycle_batch`가 배치를 뽑을 때, 검색량이 0/없음인 키워드만 모아
  `ensure_volumes`를 호출해 채운다(같은 파일).
- `_sheet_row_from_result`의 "0보다 크면만 K 기재" 규칙은 그대로 유지 — 이제
  `next_cycle_batch` 단계에서 이미 채워지므로 실제로 volume=0인 채 여기 도달하는
  경우는 조회 자체가 실패한 키워드뿐이다.

**주의**: `v2r/knowledge/keyword_exposure.py`의 이 변경분은 같은 작업 폴더를 쓰는
다른 세션이 커밋 `0fb0d3d`(02:29)에 이미 포함해 먼저 올렸다(내용 동일, git diff
없음 확인). 이번 세션은 그 위에 가짜 `fetch_fn`으로 `ensure_volumes`를 검증하는
단위 시험만 커밋(`a7dfc57`)해 origin/main에 푸시했다.

## 시험

`tests/test_keyword_exposure.py`에 6건 추가(도구 호출은 가짜 함수로):
DB에 이미 있으면 도구 호출 안 함 / 없으면 도구로 조회해 DB 저장 / 기존
`relevance`는 안 건드림 / 도구가 씨앗을 안 돌려주면 최소값 5 / 조회 실패는
예외 없이 경고만(K 미기재와 동치) / `next_cycle_batch`가 volume 0인 항목을
`ensure_volumes`로 채움.

`tests/test_keyword_exposure.py` 33건 전부 통과, 전체 `pytest` 1468건 중
1463건 통과·5건 실패 — 실패 5건은 `tests/test_top_reference.py`(4건)와
`tests/test_parser.py::test_all_tasks_covered_by_tests`(1건)로 이번 변경과
무관한 파일(`git diff HEAD`로 무수정 확인)이며, 같은 저장소를 쓰는 다른
작업(연관도 재산정 등)과 얽힌 기존 결함으로 보인다.

## 시트 K열 실제 채우기(3번 항목)

`config/brands.yaml` 5개 브랜드의 `노출 현황` 탭을 CSV로 스캔해 H열 키워드가
있는데 K열이 비었거나 0인 행을 찾았다(직전 다른 스크립트의 DB 값 복구 이후
남은 것):

| 브랜드 | 대상 행 |
|---|---|
| 우아덤 | 3 |
| 코숨핏 | 0 |
| 뉴더미스 | 4 |
| 장으뜸 | 24 |
| 팥순이 | 11 |
| 합계 | 42 |

쓰기 전 각 브랜드 CSV를 `data/sheet_backups/<브랜드>-<시각>.csv`로 백업한 뒤,
`ensure_volumes`로 검색량을 구해 `sheets_writer._write_verified`로 K열(노출완
행은 L열도 같은 값)을 채우는 스크립트를 실행했다. 5개 브랜드 모두
`keyword_fill_loop`(채우기 순환) 워커가 이미 각 브랜드 복제 프로필을 점유한
채 돌고 있어 첫 배치는 프로필 잠금 대기 시간 초과(3초 후 재시도도 실패)로
직접 조회하지 못했지만, 그 사이 채우기 순환이 같은 키워드들의 검색량을
DB(`data/keywords/<브랜드>.sqlite`)에 이미 채워 넣어 `ensure_volumes`의
1단계(DB 우선 조회)에서 전부 해결됐다. **결과: 42/42행 전부 채움, 실패 0건**
(백업: `data/sheet_backups/우아덤-20260924-020239.csv` 등 5개 브랜드 CSV,
[예시 링크](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/sheet_backups/)).

| 브랜드 | 채움/대상 |
|---|---|
| 우아덤 | 3/3 |
| 코숨핏 | 0/0(대상 없음) |
| 뉴더미스 | 4/4 |
| 장으뜸 | 24/24 |
| 팥순이 | 11/11 |

## 한계

- 이번 42건은 마침 다른 워커가 동시에 채운 DB 값으로 해결돼, `ensure_volumes`의
  실제 키워드 도구 조회 경로(프로필 잠금 재시도 포함)가 실전에서 끝까지
  검증되지는 않았다 — 단위 시험(가짜 `fetch_fn`)으로만 그 경로를 확인했다.
- 네이버 키워드 도구가 "&lt; 10"으로만 보고하는 키워드는 정확한 값이 아니라
  관례값 5로 채워진다(네이버 자체가 정확한 숫자를 안 준다).
- 도구 조회가 로그인 풀림·차단으로 실패하면 그 키워드만 여전히 K 미기재로
  남는다(사용자 지시의 "조회 실패는 예외 아님, K 유지" 조건 그대로).

## 러너 재시작 필요

`exposure_runner`/노출 순환 프로세스가 기동 중이던 코드는 `ensure_volumes`
훅이 없던 버전일 수 있으므로, 이번 변경(이미 `0fb0d3d`로 커밋됨)을 반영하려면
러너 재시작이 필요하다(재시작은 이 세션이 하지 않음 — 다른 일꾼이 관리).
