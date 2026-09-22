# 브랜드 키워드 발굴(A1~A3) 구현 결과 — 2026-09-23 00:05 KST 실측

계획서 [keyword-program-plan-2026-09-22.md](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/keyword-program-plan-2026-09-22.md) A1~A3 구현. 결론부터: **코드·저장소·명령·테스트는 완료**했고,
네이버 검색광고 키워드 도구(ads.naver.com) 실기 조회는 로그인·페이지 접근까지는
됐지만 **"조회" 버튼 자동 클릭 단계에서 UI 구조가 모호해 실제 1,000개 수집은
완료하지 못했다** — 아래 3절에 원인과 다음 단계를 있는 그대로 적는다.

## 1. 구현한 것

| 파일 | 내용 |
|---|---|
| [v2r/knowledge/naver_keyword_tool.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/naver_keyword_tool.py) | 응답 파싱(`parse_keyword_response`), 씨앗 추출(`seeds_from_sheet`·`extract_guide_keywords`), 연관도(`compute_relevance`), BFS 꼬리 물기(`discover`), 브라우저 자동화(`fetch_related_keywords`·`open_keyword_tool_page`), 실행 진입점(`run_for_brand`) |
| [v2r/store/keyword_discovery_store.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/keyword_discovery_store.py) | `data/keywords/<브랜드>.sqlite` — keyword(PK)·pc·mobile·total·source_seed·depth·relevance·collected_at |
| [v2r/command/parser.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/parser.py) | `<브랜드> 키워드 발굴 N개` → `keyword_discovery`, `키워드 발굴 현황` → `keyword_discovery_status` |
| [v2r/command/spec.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/spec.py) | `ALLOWED_TASKS`에 두 작업 추가 |
| [v2r/engine/worker.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py) | `_keyword_discovery`(실행+알림)·`_keyword_discovery_status`(현황 집계) 처리기 |
| [tests/test_naver_keyword_tool.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_naver_keyword_tool.py) | 응답 파싱·연관도·BFS(정상/목표 도달/차단/세션 상한)·저장소·명령 파서 13개 |

목표 개수(`DEFAULT_TARGET`)는 계획대로 10,000. 연관도는 브랜드 논리 낱말(정리본
`warehouse/guides/정리본/<브랜드>.md`의 "브랜드/제품" 줄·결핍 낱말, 없으면
`config/brands.yaml`의 `description`)과 겹치면 0, 아니면 BFS 깊이를 1~3으로
잘라 쓴다.

## 2. 테스트

```
$ .venv/Scripts/python -m pytest tests/test_naver_keyword_tool.py -q
13 passed
```

전체 스위트: `1204 passed, 2 failed`(2026-09-23 00:04 실측). 실패 2건은 이번
구현과 무관:

1. `tests/test_parser.py::test_all_tasks_covered_by_tests` — `ALLOWED_TASKS`
   개수를 44로 하드코딩한 "장부 파수꾼" 테스트. 이번에 작업 2개(`keyword_discovery`,
   `keyword_discovery_status`)를 추가했고, 이 세션 중 다른 작업(노출 순환기
   B1~B2로 보이는 `exposure_cycle_start/stop/status` 3개)도 동시에 추가돼 실제
   개수는 46. 지시대로(사유 적고 무시) 건드리지 않았다 — 숫자 갱신은 두 작업
   모두 마무리된 뒤 한 번에 하는 게 맞다.
2. `tests/test_self_cafe_daily.py::test_account_rotation_start_resumes_after_today_last_published`
   — 자사 카페 계정 로테이션 테스트로 이번 변경 파일과 무관. 같은 저장소에서
   동시에 진행 중인 다른 작업의 영향으로 보인다.

## 3. 실기(우아덤) 시도 — 부분 성공, 중단 사유

- 네이버 로그인 프로필(`data/browser-profile-naver`)은 **이미 로그인돼 있었다**
  (`data/naver_cookies.json` 확인, 로그아웃·재로그인 없이 그대로 사용).
- `https://ads.naver.com/manage/ad-accounts/685753/sa/tool/keyword-planner`
  페이지는 정상 접근됨(계정 `clktrade`, 광고계정 685753).
- 키워드 입력창(`textarea[placeholder*="키워드"]`)에 값을 넣고 Enter를 누르면
  "선택한 키워드 1/100" 칩이 실제로 등록되는 것을 HTML로 확인했다(정상 동작).
- 그런데 그 다음 "조회" 단계가 불명확했다:
  - 페이지에 "조회"가 들어간 버튼이 여러 종류 있고(`조회하기`가 전역에 1개,
    항상 `disabled` 속성 없이 존재 — 이 텍스트/입력과 무관한 다른 기능일
    가능성), 입력창 바로 옆에는 오히려 `지우기` / `바로추가` / `월간 예상
    실적 보기` 버튼이 있었다(연관 키워드 조회가 아니라 "예상 실적" 위젯일
    수 있음 — 페이지에 탭이 여러 개라 지금 보이는 화면이 어떤 탭인지 자동으로
    확신할 수 없었다).
  - 즉, 지금 짠 셀렉터로는 **엉뚱한 버튼을 누르거나 아무 반응 없는 버튼을
    기다리다 타임아웃**날 위험이 있었다. 캡차·차단은 아니지만, 확신 없이
    실제 광고 계정에서 클릭을 반복하면 계정에 원치 않는 조작(예: 다른 탭의
    설정 변경)을 할 위험이 있다고 판단해 — 검증되지 않은 셀렉터로 1,000회를
    돌리는 대신 **여기서 멈추고 보고한다.**
- 그래서 **실제 수집 0개** — 우아덤 1,000개 목표는 이번 회차에 달성하지 못했다.
- 차단·캡차 징후는 없었다(정상 로그인·정상 페이지 렌더).

## 4. 다음 단계(따로 승인/시간 필요)

1. 사람이 `ads.naver.com` 키워드 도구 화면에서 씨앗 1개 입력 → "연관 키워드"
   결과가 뜨는 실제 클릭 경로(탭 이름 포함)를 한 번 육안으로 확인해 주면,
   그 흐름 그대로 셀렉터를 고정할 수 있다. 또는
2. 제가 더 긴 시간(실 클릭 여러 번, 탭 전환 포함)을 들여 DOM을 탐색하되,
   광고 계정에 부작용이 없는지 먼저 조심스럽게 좁혀 나간다.
3. 어느 쪽이든 확정되면 `fetch_related_keywords`의 셀렉터만 교체하면 되고,
   BFS·저장·연관도·명령 체계는 이미 완성돼 있어 바로 우아덤 1,000개→전체
   1만 개로 이어갈 수 있다.

## 5. 커밋

`git commit`(끝 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`) ·
`git push origin HEAD` 완료(본 문서 다음 절 참고 — 커밋 해시는 push 로그 확인).
