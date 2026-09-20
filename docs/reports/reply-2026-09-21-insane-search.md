# insane-search 설치 확인 (2026-09-21 03:40 KST)

## 결론
**설치돼 있고, 방금 실제로 돌려 동작까지 확인했습니다.**

| 항목 | 상태 |
|---|---|
| 마켓 등록 `gptaku-plugins` | 등록됨 (`~/.claude/plugins/known_marketplaces.json`) |
| 플러그인 `insane-search@gptaku-plugins` | 설치됨 09-18 15:37, 버전 0.16.3, 사용자 범위, 활성화(`enabledPlugins: true`) |
| 파이썬 의존성(curl_cffi 0.15+, bs4, pyyaml, pypdf, markdownify) | 오늘 새벽 설치함(그 전엔 없었음 → 첫 호출 때 자동 설치되는 구조지만 미리 넣음) |
| 실행 테스트 `engine https://example.com` | `ok: true` (curl_cffi 사파리 지문으로 200) |

## 한 가지 손볼 점
- 이 PC의 `python3` 명령이 마이크로소프트 스토어 안내 스텁이라(실제 파이썬은 `C:\Users\user\AppData\Local\Programs\Python\Python312`), 플러그인 문서의 `python3 -m engine …` 그대로는 안 먹습니다. 제가 쓸 때는 실제 파이썬 전체 경로로 호출하니 문제 없고, 기억 파일에 적어 둡니다.
- 완전히 깔끔하게 하려면 윈도우 설정 → 앱 → 고급 앱 설정 → "앱 실행 별칭"에서 `python3.exe` 스토어 별칭을 끄면 됩니다(사용자 설정이라 제가 건드리지 않음).

## 인수인계 규칙(검색) 적용 상태
- "찾으라" 하면 GitHub·Hugging Face·X·Threads·Reddit을 서브 에이전트 여러 개로 훑고, 막히면 insane-search로 뚫는다 — 규칙은 [인수인계 문서](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reference/handoff.md) 검색 규칙 절에 있고, 이제 도구도 준비됨.
- 지금까지 V2R 작업에서는 외부 검색이 필요한 상황이 없어 아직 실전 사용 0회.
