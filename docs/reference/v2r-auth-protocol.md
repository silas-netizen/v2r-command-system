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
- 토큰 갱신: `refreshToken({access_token, refresh_token: null})` — 갱신용 경로는 `/auths/*` 하위(정확한 경로는 첫 실행 시 로그로 확정). 갱신 실패 시 `token_error` 필드로 사유 구분.
- 액세스 토큰은 화면에서는 메모리에만 보관. 새 시스템은 로컬 전용 파일(`data/session.json`, git 제외)에 저장하고 만료(403 `TOKEN_ERROR`) 시 재로그인.

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
- 로그인은 하루 한도가 있으므로 **토큰을 재사용**하고 만료 시에만 재로그인한다.
- 비밀번호는 `.env`의 `V2R_PASSWORD`에서만 읽고 로그·DB·git에 남기지 않는다.
