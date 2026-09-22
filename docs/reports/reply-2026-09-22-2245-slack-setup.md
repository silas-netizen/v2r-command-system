# 슬랙 연결 방법 (2026-09-22 22:52 KST, 실측)

채널 `카페-클로드`는 만드셨으니, 지금 뜬 "사용자 또는 에이전트 추가" 창은 **지금은 건너뛰기**를 누르세요. 봇 앱을 먼저 만들어야 초대할 수 있습니다.

## 하실 일(5분, 토큰 입력은 직접)
1. 브라우저에서 **api.slack.com/apps** → **Create New App** → **From scratch** → 이름 `V2R 봇`, 워크스페이스 `얼리버…` 선택 → Create.
2. 왼쪽 **OAuth & Permissions** → 아래 **Scopes › Bot Token Scopes**에 6개 추가:
   `chat:write` · `files:write` · `channels:read` · `channels:history` · `groups:read` · `groups:history` (마지막 둘은 비공개 채널용 — 카페-클로드가 자물쇠 채널이라 필요)
3. 같은 화면 위 **Install to Workspace** → 허용 → **Bot User OAuth Token**(`xoxb-`로 시작) 복사.
4. 슬랙 앱에서 `카페-클로드` 채널로 가서 메시지창에 `/invite @V2R 봇` 입력(또는 채널 이름 클릭 → 통합 → 앱 추가).
5. 채널 이름 클릭 → 맨 아래 **채널 ID**(`C…` 또는 `G…`) 복사.
6. 파일 `D:\v2r 자동화\v2r-command-system\.env` 를 메모장으로 열어 두 줄에 붙여넣기(따옴표 없이):
   ```
   SLACK_BOT_TOKEN=xoxb-…
   SLACK_ALLOWED_CHANNEL_IDS=C…
   ```
   (`SLACK_WEBHOOK_URL`은 비워 두면 됩니다.)
7. 저장 후 저에게 "슬랙 넣었어"라고만 하시면 제가 실행기를 재시작하고 `슬랙 점검`으로 연결·파일 전송을 확인합니다.

## 제가 지금 하는 것
- 슬랙 채널 코드에 **파일 전송(HTML·MD 현황판)** 기능이 없어서 추가 중(텔레그램만 파일 가능했음) + `슬랙 점검` 명령 + 절차 문서 [docs/reference/slack-setup.md](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reference/slack-setup.md).
- 토큰·채널 ID는 규칙상 제가 입력하지 않습니다(사용자 직접).
