# 슬랙 앱 만들기 — 이 화면에서 (2026-09-22 22:58 KST, 실측)

1. 화면 아래 쿠키 창은 **COOKIES SETTINGS → 필수만**(또는 그냥 닫기).
2. 창에서 **From a manifest**(왼쪽 아래, "Upload JSON or YAML config") 선택 → **Continue**.
3. 워크스페이스 `얼리버…` 선택 → Next.
4. YAML 탭에 아래 파일 내용을 **전부 복사해 붙여넣기** → Next → Create.
   [slack-app-manifest.yaml](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reference/slack-app-manifest.yaml) (권한 6개가 이미 들어 있어 따로 고를 필요 없음)
5. 만들어진 앱 화면 왼쪽 **OAuth & Permissions** → **Install to Workspace** → 허용 → **Bot User OAuth Token**(`xoxb-…`) 복사.
6. 슬랙 `카페-클로드` 채널에서 `/invite @V2R 봇` → 채널 이름 클릭 → 맨 아래 **채널 ID** 복사.
7. `D:\v2r 자동화\v2r-command-system\.env` 에 직접 입력:
   ```
   SLACK_BOT_TOKEN=xoxb-…
   SLACK_ALLOWED_CHANNEL_IDS=C…
   ```
8. 저장 후 "슬랙 넣었어"라고 하시면 재시작·점검합니다.

(AI agent·Starter app 템플릿은 필요 없는 기능이 붙어 오니 고르지 마세요. Blank app으로 해도 되지만 권한을 손으로 6개 추가해야 합니다.)
