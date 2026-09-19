# V2R 로그인(인증) 프로토콜 — 화면 코드 분석으로 확정 (2026-09-19)

> 출처: `https://v2r.daboja.im/assets/index-*.js`, `loginChallengePow.worker-*.js` 분석. 가상 테스트 사이트 기준.
> 옛 코드가 "브라우저에서 토큰을 훔쳐오던" 미확인 항목을 API로 대체할 수 있게 해준다.

## 1. 공통 HTTP 클라이언트
- baseURL `https://api-v2r.daboja.im`, `Accept: application/json`, `withCredentials: true` (쿠키 동반), timeout 60초.
- 인증된 요청: `Authorization: Bearer <access_token>`.
- 인증 제외 경로(토큰 없이 호출): `/auths/logout, /auths/login, /auths/challenge, /auths/auth_email, /auths/auth_verify_email, /auths/auth_phone_number, /auths/auth_verify_phone_number, /users/user, /users/password/reset_ready, /users/password/reset, /auths/handoff_token/exchange, /auths/handoff_token/preview`.
- POST/PUT/PATCH/DELETE 요청에는 브라우저 신호 헤더가 자동 첨부됨(없을 때).

## 2. 헤더
| 헤더 | 값 |
|---|---|
| `X-Device-Id` | 브라우저 localStorage `v2r_device_id` (UUID 형태의 기기 식별자, 최초 생성 후 재사용) |
| `X-Browser-Signal` | 기기 지문 JSON(§4)을 직렬화한 값 |
| `X-Browser-Signal-Status` | 지문 수집 상태 문자열(성공/실패 신호 목록) |
| `X-Challenge-Proof` | 로그인 재시도 때만. §3의 증명 JSON 문자열 |

## 3. 로그인 절차
```
1) POST /auths/login   (multipart/form-data)
   username=<login_id>  password=<pw>
   headers: X-Device-Id, X-Browser-Signal, X-Browser-Signal-Status
   → 200: { token: { access_token, refresh_token? } }
   → 429: error.extra.retry_after (초), error.extra.scope 있으면 일일 한도
   → 실패 응답의 error.extra 가 "챌린지 필요"를 뜻하면 2)로

2) POST /auths/challenge  → { challenge: { version, nonce, timestamp, difficulty, signature } }

3) 작업 증명(PoW) 계산:
   fp_hash  = SHA-256(정렬된 지문 JSON)   (§4)
   msg      = f"{version}:{path}:{fp_hash}:{nonce}:{timestamp}:{proof_nonce}"   path = "/auths/login"
   proof_nonce = 0,1,2,... 중 SHA-256(msg).hex() 가 "0"*difficulty 로 시작하는 첫 값
   proof = json.dumps({
     "version", "nonce", "timestamp", "difficulty", "signature",
     "path": "/auths/login", "fp_hash": fp_hash, "proof_nonce": proof_nonce })

4) POST /auths/login (같은 폼) + 헤더 X-Challenge-Proof: <proof>
   → { token: { access_token } }
```
- 로그아웃: `POST /auths/logout` 바디 `{loginId}`.
- 토큰 갱신: `refreshToken({access_token, refresh_token: null})` — 경로는 `POST /auths/refresh_token`으로 구현(본문 형태 **미검증**, §7 참고). 갱신 실패 시 `token_error` 필드로 사유 구분.
- 액세스 토큰은 화면에서는 메모리에만 보관. 새 시스템은 로컬 전용 파일(`data/session.json`, git 제외)에 저장하고 **갱신으로 이어 쓴다**. 재로그인은 리프레시 쿠키가 끝날 때(하루 한 번)만 — 자세한 규칙은 **§7**.

## 4. 기기 지문(fp) 페이로드 (version "v1")
snake_case 키를 **알파벳 정렬**해 `JSON.stringify` → SHA-256 hex = `fp_hash`.
```
canvas_hash, color_depth, hardware_concurrency, has_touch, language, platform,
screen_height, screen_width, timezone, version, webdriver, webgl_renderer, webgl_vendor
```
- 새 시스템은 실제 PC 값 기준의 **고정 지문**을 1회 생성해 `data/device.json`에 저장하고 매번 같은 값을 보낸다(기기 일관성). `webdriver`는 `false`, `platform`은 `"Win32"`, `timezone`은 `"Asia/Seoul"`.

## 5. 오류 코드 형태
`error = { code, reason, extra }`. 429 → `extra.retry_after`. 차단 판정은 별도 함수(`blocked`).

## 6. 구현 메모
- 로그인은 하루 20회 한도가 있으므로 **토큰 파일 하나를 모든 프로세스가 공유**하고,
  만료 전에는 갱신으로 잇는다(§7).
- 비밀번호는 `.env`의 `V2R_PASSWORD`에서만 읽고 로그·DB·git에 남기지 않는다.

## 7. 로그인 횟수 제한(20/일/지문)과 대응
2026-09-19 11:16 KST에 실제로 맞은 응답:
```
429 {"error":{"code":160,"reason":"RATE_LIMIT_LOGIN",
     "extra":{"scope":"daily_fingerprint","retry_after":43459,"max_attempts":20}}}
```
**기기 지문 하나당 하루 20회 로그인**까지다. 지문은 `data/device.json`에 고정돼 있으므로
실행기·점검 스크립트·테스트가 각자 로그인하면 반나절 만에 소진된다.

### 목표
- 세션은 **24시간 이상** 끊기지 않는다.
- 로그인은 **하루 한 번**. 나머지는 전부 토큰 갱신으로 잇는다.

### 토큰 수명
| 값 | 수명 | 보관 |
|---|---|---|
| `access_token` | 약 1시간 (JWT `exp`) | `data/session.json` |
| `refresh_token` 쿠키 | 로그인 후 약 24시간 (도메인 `api-v2r.daboja.im`, 경로 `/auths`) | `data/session.json` |

`exp`는 서명 검증 없이 payload만 base64 디코딩해 읽는다(`auth.decode_exp`).

### 저장 파일 `data/session.json` (git 제외)
```json
{"access_token": "...", "refresh_token": "...", "refresh_expires_at": 0,
 "obtained_at": 0, "expires_at": 0}
```
- 같은 폴더의 임시 파일에 쓰고 `os.replace`로 바꿔 끼운다(원자적 쓰기, 동시 프로세스 안전).
- **모든 프로세스가 이 파일 하나를 공유한다.** 토큰을 쓰기 전에 파일을 다시 읽어,
  다른 프로세스가 방금 갱신해 둔 값을 그대로 쓴다.

### 갱신 (`POST /auths/refresh_token`) — 본문 형태는 **미검증**
화면 번들의 `refreshToken({access_token, refresh_token: null})`에서 읽은 추정값이다.
```
POST /auths/refresh_token
Cookie: refresh_token=<저장된 쿠키 값>
{"access_token": "<옛 액세스 토큰>", "refresh_token": null}
→ {"token": {"access_token": "<새 토큰>"}}
```
- 첫 실제 응답의 **형태만**(HTTP 상태, 최상위 키 목록, Set-Cookie 유무) INFO 로그로 한 번 남긴다.
  토큰·쿠키 값은 어떤 경로로도 로그에 남기지 않는다.
- 4xx거나 본문에 `access_token`이 없으면 로그인으로 폴백한다.

### 결정 순서
- **요청 직전**(`AuthSession.ensure_token`): 만료 5분 이상 남았으면 그대로 사용 →
  파일 재적재 → 그래도 임박하면 갱신 → 갱신 실패면 로그인.
- **주기 점검**(`AuthSession.maintain`, serve가 30분마다): 리프레시 쿠키가 1시간 안에
  죽으면 **지금 로그인**(하루 한 번 예정된 로그인) → 아니면 다음 주기 전에 죽을 토큰만 갱신 →
  그 외에는 아무것도 안 함.

### 로그인 예산 (`data/login_log.json`)
최근 24시간의 로그인 **시도** 시각을 기록한다. 15회(`LOGIN_BUDGET_PER_DAY`)에 닿으면
서버를 부르지 않고 `V2RApiError(kind="login_budget")`로 막는다 — 서버 한도 20회 전에
우리가 먼저 멈춘다.

### 429 처리
- `reason == RATE_LIMIT_LOGIN`이거나 `retry_after > 900초`인 429는 `kind="rate_limited_long"`으로
  **즉시** 올린다. 그 자리에서 10초·30초 재시도를 돌리지 않는다(`V2RClient.request`).
- 발행 일꾼(`worker._run_publish`)은 `rate_limited` / `rate_limited_long` / `login_budget`을 만나면
  남은 슬롯을 줄줄이 실패시키지 않는다. 채널에 **한 번만** 알리고
  (`V2R 로그인 제한: HH:MM까지 대기 후 이어갑니다`), 리스를 30초마다 연장하며
  `retry_after`까지(최대 6시간) 기다린 뒤 **같은 슬롯**부터 다시 한다.
- 그 슬롯의 발행 행은 `uncertain`이 아니라 재시도 가능한 `failed`로 남긴다 —
  `uncertain`으로 두면 다음 시도에서 그 원고를 건너뛰어 원고가 소모된다.

### 확인 방법
```
python -m v2r session
```
액세스 토큰 만료, 갱신 쿠키 만료, 최근 24시간 로그인 횟수를 보여준다(비밀값 없음).
