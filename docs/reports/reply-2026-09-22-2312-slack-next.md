# 슬랙 앱 — 다음 단계 (2026-09-22 23:10 KST, 실측)

이 화면은 권한이 **0개**로 잡혀 있습니다(매니페스트가 안 들어간 상태). 그래도 괜찮습니다 — **Create** 누르고 나서 권한을 넣으면 됩니다.

1. **Create** 클릭.
2. 앱 화면 왼쪽 메뉴 **OAuth & Permissions** 클릭 → 아래로 내려 **Scopes › Bot Token Scopes › Add an OAuth Scope** 에서 6개 추가:
   `chat:write` · `files:write` · `channels:read` · `channels:history` · `groups:read` · `groups:history`
3. 같은 페이지 맨 위 **Install to Workspace**(또는 Install App) → **허용**.
4. 나타나는 **Bot User OAuth Token**(`xoxb-…`) 복사.
5. 슬랙 앱 → `카페-클로드` 채널 → 메시지창에 `/invite @앱이름` 입력(앱 이름은 Basic Information에서 정한 이름).
6. 채널 이름 클릭 → 맨 아래 **채널 ID**(`C…`/`G…`) 복사.
7. `D:\v2r 자동화\v2r-command-system\.env` 에 직접 입력 후 저장:
   ```
   SLACK_BOT_TOKEN=xoxb-…
   SLACK_ALLOWED_CHANNEL_IDS=C…
   ```
8. "슬랙 넣었어"라고 하시면 재시작·`슬랙 점검`으로 확인합니다.
