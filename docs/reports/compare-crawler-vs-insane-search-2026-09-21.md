# web-crawler vs insane-search 비교 (2026-09-21 09:30 KST)

대상: [byungjunjang/web-crawler](https://github.com/byungjunjang/web-crawler) (134 스타, MIT) vs 이미 설치된 insane-search 0.16.3.
용도 예시: 네이버 맘카페(https://cafe.naver.com/skybluezw4rh)를 돌며 회원 말투·내용 수집.

## 한 줄 결론
**둘 다 쓰는 게 맞고, 맘카페 수집의 주역은 web-crawler**입니다. insane-search는 "막힌 페이지 한 장 읽기" 도구, web-crawler는 "사이트 하나를 돌며 수백 건을 표로 모으기" 도구라 역할이 다릅니다.

## 비교표
| 항목 | web-crawler | insane-search |
|---|---|---|
| 정체 | Claude Code용 **스킬 저장소**(정찰 → 수집 스크립트 생성 → 엑셀). 저장소 폴더에서 Claude Code를 열고 말로 시킴 | Claude Code **플러그인**. 페이지 URL 하나를 여러 방법(지문 위장 → 헤드리스 브라우저)으로 읽어 본문 반환 |
| 잘하는 것 | 목록형 대량 수집(페이지 넘기기, 게시글·댓글·닉네임 열 구조화), 도메인별 수집 노하우 저장(다음엔 정찰 생략), 엑셀 출력 | 403·WAF·캡차로 막힌 공개 페이지 뚫어 읽기, X·레딧·유튜브 등 플랫폼 공식 우회로 |
| 로그인 필요한 사이트 | **지원**: 보이는 브라우저 창을 띄워 사용자가 직접 로그인 → 쿠키만 추출(네이버 `NID_AUT/NID_SES` 확인 절차가 문서에 있음). `naver_antibot` 전략도 내장 | 사실상 없음(공개 페이지 전용) |
| 네이버 카페 적합도 | **높음**(카페 글은 대부분 회원 로그인 필요 → 이 도구의 로그인 흐름이 핵심) | 낮음(로그인 벽에서 멈춤) |
| 필요한 것 | Python 3.10+, **Node.js 18+**(정찰 도구 agent-browser), Chromium 다운로드(첫 설치 오래 걸림) | 파이썬만(이미 준비됨) |
| 속도·부담 | 사이트당 첫 정찰이 있고 스크립트 생성 → 이후엔 빠름 | 페이지당 1회 호출, 즉시 |
| 안전장치 | 초당 1건 이하·429면 중단·개인정보 감지 경고·우회 지점마다 사용자 확인 | 차단 종류 판정(bot_detection / infra_or_auth) |

## 맘카페 수집 시 알아두실 것
- 카페 글은 회원 로그인이 필요합니다. 도구가 띄우는 창에서 **사용자가 직접 로그인**하고(비밀번호는 저장·입력 대행 안 함), 그 세션으로 수집합니다.
- 네이버 약관은 자동 수집을 제한합니다. 이 도구는 우회 지점마다 확인을 구하고 속도 제한을 두지만, 진행 판단과 결과 책임은 사용자 몫입니다. 말투 학습 목적이면 **필요한 만큼만**(예: 게시판 몇 개, 최근 수백 건) 모으는 걸 권합니다.
- 결과: 엑셀(제목·본문·작성자 닉·작성일·댓글 등 원하는 열). 이걸 `warehouse/`에 넣어 일상 글·댓글 말투 참고 자료로 쓰면 됩니다.

## 설치 상태
- insane-search: 설치·동작 확인 완료(오늘 새벽).
- web-crawler: **아직 미설치.** 이유 두 가지.
  1. 외부 저장소 코드를 작업 폴더에 넣는 것을 자동 승인이 막았습니다(승인 카드가 뜨면 허용해 주시면 됨).
  2. 이 PC에 Node.js가 없습니다. 설치는 관리자 확인창이 떠서 제가 혼자 못 합니다.

## 설치 진행 방법 (둘 중 하나)
**A. 제가 진행** — "web-crawler 설치 진행"이라고 답해 주시면, 승인 카드(저장소 복제·Node 설치)를 띄우고 순서대로 합니다: Node 설치 → 저장소를 `D:\v2r 자동화\web-crawler`에 복제 → 한 방 설치 → 검증 결과 보고.

**B. 직접 진행** — PowerShell에서 아래 세 줄(첫 줄에서 관리자 확인창 "예").
```powershell
winget install OpenJS.NodeJS.LTS
git clone https://github.com/byungjunjang/web-crawler.git "D:\v2r 자동화\web-crawler"
cd "D:\v2r 자동화\web-crawler"; powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

## 설치 후 쓰는 법(예시)
그 폴더에서 Claude Code를 열고:
> "https://cafe.naver.com/skybluezw4rh 의 자유게시판에서 최근 글 300개의 제목·본문·작성자 닉네임·댓글을 엑셀로 모아줘"

→ 로그인 창이 뜨면 직접 로그인 → 정찰 → 수집 → `output/cafe.naver.com/…xlsx`.
