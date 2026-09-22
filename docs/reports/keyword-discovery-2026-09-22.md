# 브랜드 키워드 발굴(A1~A3) 구현 결과 — 2026-09-23 00:26 KST 갱신(실측)

계획서 [keyword-program-plan-2026-09-22.md](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/keyword-program-plan-2026-09-22.md) A1~A3 구현. **우아덤 1,000개 실제 수집 완료**(사람이 직접 결과 확인). 코드·저장소·명령·테스트 모두 완료했고, 남은 과제(헤드리스 다운로드 불안정, 연관도 분포)는 4절에 있는 그대로 적는다.

## 1. 구현한 것

| 파일 | 내용 |
|---|---|
| [v2r/knowledge/naver_keyword_tool.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/naver_keyword_tool.py) | 씨앗 최대 5개 배치 조회(`fetch_related_keywords`, 실제 페이지 구조에 맞춤), "전체 다운로드" xlsx 파싱(`parse_keyword_download_rows/_file`), 씨앗 추출(`seeds_from_sheet`·`extract_guide_keywords`), 연관도(`compute_relevance`, 0~3), BFS 꼬리 물기(`discover`), 화면 밖 창 실행(`open_keyword_tool_page`), 실행 진입점(`run_for_brand`), 저장된 연관도 재계산(`recompute_relevance`) |
| [v2r/store/keyword_discovery_store.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/keyword_discovery_store.py) | `data/keywords/<브랜드>.sqlite` — keyword(PK)·pc·mobile·total·source_seed·depth·relevance·collected_at, `update_relevance`·`all_rows` 추가 |
| [v2r/command/parser.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/parser.py) / [spec.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/command/spec.py) | `<브랜드> 키워드 발굴 N개` → `keyword_discovery`, `키워드 발굴 현황` → `keyword_discovery_status` |
| [v2r/engine/worker.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py) | `_keyword_discovery`(실행+알림)·`_keyword_discovery_status`(현황 집계) |
| [tests/test_naver_keyword_tool.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_naver_keyword_tool.py) | 19건: 다운로드 파싱·연관도·BFS(정상/배치5개/깊이안섞임/목표도달/차단/세션상한)·저장소·명령 |

## 2. 우아덤 실기 수집 — 완료

- 로그인은 이미 돼 있던 세션 그대로 썼다(로그아웃·재로그인 없음).
- 실제 페이지 구조(사람이 직접 확인해 알려준 값 그대로 반영):
  씨앗 입력창 placeholder `"한줄에 하나씩 입력하세요.\n(최대 5개까지)"`(줄바꿈
  구분, 최대 5개), 조회 버튼 정확히 `"조회하기"`, 결과는 `"전체 다운로드"`
  xlsx로 받아 파싱(표를 직접 긁지 않음). `광고 만들기`·`전체추가`·`바로추가`·
  `월간 예상 실적 보기`는 코드에서 아예 건드리지 않는다.
- 씨앗 5개(예: 비타민C·나이아신아마이드·편평사마귀·멜라토닝크림·멜라논크림)
  한 번 조회에 **관련 키워드 600~700개**가 나왔다 — 브랜드가 이미 키워드
  시트에 3,823개를 갖고 있어 씨앗은 차고 넘친다.
- **최종: 1,000개 수집 확인**(`data/keywords/우아덤.sqlite`), 조회 2회로
  달성(첫 조회 710개 + 두 번째 조회로 1,000개 도달). 검색량 합계
  1,687,080(PC+모바일). 상위 검색량: 더마팩토리(131,300), 사마귀(57,080),
  비타민B(47,740) 등.
- 차단·캡차 징후는 전 과정에서 없었다.

## 3. 헤드리스/화면 밖 실행 — 확인 결과

지시대로 창을 화면에 띄우지 않는 쪽으로 바꾸는 걸 시도했다:

- **완전 헤드리스(`headless=True`)**: 로그인·페이지 접근·씨앗 입력·조회까지는
  되는데, **"전체 다운로드" 저장 직전에 브라우저가 통째로 닫히는 문제**가
  재현됐다(`Download.save_as: Target page, context or browser has been
  closed`). 재시도해도 같은 지점에서 실패.
- **화면 밖 창(`--window-position=-32000,-32000`, `offscreen=True`,
  `headless=False`)**: 코드는 추가했지만 마지막 확인에서 **똑같은 지점에서
  같은 오류**가 났다. 다만 이전에 화면에 보이는 창(`headless=False`,
  `offscreen=False`)으로는 첫 조회가 확실히 성공했었다(710개를 그렇게
  받았다) — 그러다 두 번째 조회에서도 같은 오류로 한 번 끊겼었다.
- 정리하면: **세 가지 모드(완전 표시·헤드리스·화면 밖) 모두에서 같은
  "브라우저가 닫힘" 오류가 간헐적으로 났다.** 실패 시점이 항상 "다운로드
  저장 직전"으로 동일하고, 모드를 바꿔도 패턴이 똑같이 재현되는 걸 보면
  **모드(헤드리스 여부) 자체의 문제가 아니라, 같은 네이버 로그인 프로필을
  다른 프로세스(예: 예약된 "네이버 세션 점검"·다른 브라우저 작업)가 동시에
  건드릴 때 프로필 잠금이 충돌해 먼저 연 쪽이 닫히는 것으로 보인다.**
  (`data/browser-profile-naver`는 Chromium 특성상 한 번에 한 프로세스만
  쓸 수 있다.)
- **현재 기본값**: `run_for_brand(headless=False, offscreen=True)` — 화면
  밖 창으로 시도하되, 실패 시(예: 위 충돌) 재시도하면 대개 다음 시도에서는
  된다(실측: 여러 번 재시도해 1,000개를 다 받았다). 완전 헤드리스는 코드로는
  가능하지만 기본값으로 쓰지 않는다(같은 실패가 나고 화면 밖 모드보다 나은
  점이 확인되지 않았다).
- **다음 단계 제안**: 이 키워드 발굴을 돌릴 때는 같은 프로필을 쓰는 다른
  예약(네이버 세션 점검 등)이 겹치지 않게 시간을 피하거나, 재시도 로직을
  `run_for_brand`에 내장(지금은 호출 쪽에서 반복 호출)하는 걸 다음에
  추가하면 좋겠다.

## 4. 연관도(0~3) 분포 — 보강했지만 여전히 치우침

- 정리본 핵심어 추출 로직을 다시 짰다(`extract_guide_keywords`): "결핍:
  A, B, C", "카페에 A, B, C 결핍을 가진 타겟", "목록 상세(A, B, C)" 세
  형태를 모두 찾고, AI-티 제거 규칙 같은 무관한 줄(화살표·물결 포함 줄)은
  걸러낸다. 우아덤 결과: `그린커피 아하바하, 색소침착, 미백, 착색, 기미,
  흑자, 검버섯, 주근깨, 여드름 흔적, 겨드랑이, y존, 팔꿈치, 목뒤 색소침착`
  (4개 → 13개로 보강).
- `compute_relevance`도 다시 짰다: 0=그대로 포함, 1=논리 낱말과 2글자 이상
  겹침(부분 일치), 2~3=텍스트로 안 겹치고 BFS 깊이만 남음.
- 보강한 뒤 재계산한 실제 분포: **{0: 12, 1: 5, 2: 983}** — 여전히 2에
  압도적으로 몰린다. **원인**: 목표(1,000개)를 조회 2회 만에 다 채워서
  BFS가 깊이 1에서 멈췄다 — 나온 키워드를 다시 씨앗으로 넣어 깊이 2~3까지
  파고드는 단계까지 못 갔다. 텍스트 부분 일치 규칙만으로는 한계가 있고,
  진짜 0~3이 고르게 나오려면 **더 깊이 도는(깊이 2~3) 조회가 실제로
  있어야** 한다(지금 1,000개는 전부 씨앗 5×2=10개에서 1단계만 내려간
  결과). 다음 회차(2,000개~1만개로 늘릴 때)에는 목표를 여유 있게 잡아
  자연히 깊이가 깊어지게 두면 해소될 걸로 본다 — 코드는 이미 그렇게
  동작한다(`discover`가 큐에 남은 깊이 1 키워드를 다시 씨앗 삼아 계속
  돈다).

## 5. 테스트

```
$ .venv/Scripts/python -m pytest tests/test_naver_keyword_tool.py tests/test_parser.py tests/test_self_cafe_daily.py tests/test_keyword_exposure.py -q
177 passed
```

이전에 보고했던 pytest 실패 2건도 원인을 찾아 고쳤다:

1. `tests/test_parser.py::test_all_tasks_covered_by_tests` — `ALLOWED_TASKS`
   개수 하드코딩(44). 이번에 2개(`keyword_discovery`·`keyword_discovery_status`)
   + 동시 진행 중이던 노출 순환기 작업 3개(`exposure_cycle_*`)가 늘어
   실제 49개. 주석·숫자를 49로 갱신.
2. `tests/test_self_cafe_daily.py::test_account_rotation_start_resumes_after_today_last_published`
   — **진짜 버그였다.** `v2r/engine/publish.py`의
   `_last_published_account_today`가 `date(created_at) = date('now',
   'localtime')`로 "오늘 발행"을 찾는데, `created_at`은 `now_iso()`가 만든
   KST 오프셋 문자열(`...+09:00`)이고 SQLite `date()`는 오프셋을 무시하고
   UTC로 계산한다. 자정~오전 9시 KST 사이(정확히 이 세션이 그 시간대를
   지나며 재현됨)에는 UTC 날짜가 아직 어제라 "오늘 발행한 계정"을 못 찾아
   계정 순번이 처음부터 다시 도는 버그였다. `substr(created_at, 1, 10)`로
   KST 날짜 문자열을 직접 비교하도록 고쳤다(SQLite 시간대 함수에 의존하지
   않음).

## 6. 커밋·push

이 갱신을 포함해 커밋(`Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`)
후 `git push origin HEAD` 완료.
