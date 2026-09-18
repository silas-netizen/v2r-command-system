# ChatGPT 웹앱 이미지 생성 메모

`v2r/warehouse/gpt_images.py` 운영 노트. **OpenAI API를 쓰지 않는다.** 사용자의
유료 ChatGPT 구독(웹 UI)을 Playwright로 그대로 조작한다.

## 0. 안전 원칙

- **비밀번호를 프로그램이 입력하지도, 저장하지도 않는다.** 코드 어디에도 계정
  정보가 없다. 로그인은 사람이 브라우저 창에서 직접 한다.
- 로그인 후 세션 쿠키는 영속 프로필 `data/browser-profile-gpt/`에 남는다.
  이 폴더는 **절대 커밋하지 않는다** (쿠키 = 계정 접근권).
- 규칙 0: 세탁(`photo_washer`) 안 된 이미지는 발행에 쓰지 않는다.
  `generate_batch`는 생성 직후 `photo_request.collect_new`로 적재 + 세탁까지 끝낸다.

## 1. 흐름

```
open_gpt(headless=False)       # data/browser-profile-gpt → https://chatgpt.com
wait_for_login(page, 900)      # 입력창이 보일 때까지 폴링 (사람이 직접 로그인)
generate_image(page, 프롬프트, out_dir, timeout=240)
generate_batch(브랜드, 키워드, n)   # 프롬프트 n개 → inbox/new/<브랜드>/<키워드>/ → 수거 + 세탁
```

명령: `브랜드 우아덤 키워드 단호박 사진 3개 생성` → 작업 `generate_photos`
(`v2r/command/parser.py`의 `(사진|이미지).*(생성|만들어)`, `request_photos`보다 앞).

## 2. 쓰는 셀렉터 (여기가 제일 잘 깨진다)

모두 `gpt_images.py` 상단 상수. UI가 바뀌면 **이 상수들만** 고치면 된다.

| 용도 | 상수 | 후보 |
| --- | --- | --- |
| 계정 메뉴/아바타 (**로그인 판정**) | `ACCOUNT_SELECTORS` | `[data-testid='profile-button']`, `button[aria-label*='profile' i]`, `button[aria-label*='계정']`, `img[alt*='User' i]`, `[data-testid='accounts-profile-button']` |
| 로그아웃 표시 | `LOGGED_OUT_SELECTORS` | `[data-testid='login-button']`, `button:has-text('Log in')`, `button:has-text('로그인')`, `button:has-text('회원가입')` … |
| 어시스턴트 응답 (`gpt_chat`) | `ASSISTANT_SELECTORS` | `[data-message-author-role='assistant']`, `div[data-testid^='conversation-turn'] .markdown`, `main .agent-turn` |
| 생성 중 표시 (`gpt_chat`) | `STREAMING_SELECTORS` | `button[data-testid='stop-button']`, `button[aria-label*='Stop' i]`, `button[aria-label*='중지']` |
| 프롬프트 입력창 | `COMPOSER_SELECTORS` | `#prompt-textarea`, `div[contenteditable='true']#prompt-textarea`, `textarea[data-id='root']`, `form div[contenteditable='true']`, `main textarea` |
| 전송 | `SEND_SELECTORS` | `button[data-testid='send-button']`, `button[aria-label='Send prompt']`, `button[aria-label='프롬프트 보내기']`, `form button[type='submit']` → 전부 실패하면 `Enter` 키 |
| 결과 이미지 | `IMAGE_SELECTORS` | `img[alt*='Generated' i]`, `div[data-testid^='conversation-turn'] img[src^='http']`, `main img[src*='oaiusercontent']`, `main img[src^='blob:']` |
| 원본 내려받기 | `DOWNLOAD_SELECTORS` | `[data-testid='image-gen-download-button']`, `button[aria-label*='Download' i]`, `button[aria-label*='다운로드']`, `a[download]` |

### 로그인 판정 (`is_logged_in`) — 중요

**입력창으로 판정하면 안 된다.** chatgpt.com은 **로그아웃 방문자에게도 입력창을
보여 준다.** 예전 판정이 이것만 봐서 로그인 전에도 "로그인 완료"로 오인했고,
`--login` 창이 뜨자마자 닫혀 버렸다(2026-09-19 실사용 증상).

지금은 세 조건을 **모두** 만족해야 로그인으로 본다.

1. `로그인`/`Log in`/`회원가입`/`Sign up` 버튼이 **안 보인다** (`LOGGED_OUT_SELECTORS`)
2. 계정 메뉴/아바타가 **있다** (`ACCOUNT_SELECTORS`)
3. URL이 `/auth/login`·`/auth/signup`·`auth0.openai.com`이 **아니다** (`AUTH_URL_MARKERS`)

`wait_for_login`과 `check_gpt_session` 둘 다 이 함수를 쓴다. 대기 중에는 30초마다
`아직 로그인 전입니다. 창에서 로그인해 주세요`를 찍고, `--login` 창은 **진짜
로그인이 확인될 때까지(최대 15분) 닫지 않는다.**

브라우저는 설치된 크롬 → 엣지 → 내장 크로미움 순으로 시도한다(`open_gpt`).

이미지 획득 순서: ① 내려받기 버튼(원본 화질) → 실패하면 ② `img.currentSrc`를
`page.request.get()` 또는 `blob:`/`data:`면 페이지 안 `fetch`로 직접 읽는다.
그리는 중 잘린 이미지를 잡지 않으려고 `naturalWidth >= 256` 조건 + 2초 뒤 재확인을 둔다.

## 3. 알려진 취약점

- **셀렉터**: ChatGPT UI는 자주 바뀐다. `data-testid`가 가장 오래 버티고,
  `aria-label`은 UI 언어(한/영) 설정에 따라 달라진다. 그래서 둘 다 넣어 뒀다.
- **`#prompt-textarea`는 `<textarea>`가 아니라 contenteditable `<div>`** 인 시기가 있다.
  그래서 `fill()`이 실패할 수 있어 `keyboard.insert_text`로 넣는다.
- **줄바꿈이 전송으로 잡힌다.** 프롬프트의 `\n`을 공백으로 바꿔 한 덩어리로 보낸다.
- **Cloudflare 봇 체크 / 재인증**: 오래 안 쓰면 다시 로그인 화면이 뜬다.
  이때 `wait_for_login`이 다시 사람을 기다린다(예외를 던지지 않고 `False` 반환).
- **사용 한도**: `LIMIT_PATTERNS`에 걸리는 문장이 화면에 뜨면 `GptLimitError(wait_text)`.
  `generate_batch`는 그 시점에 멈추고 `limited: True`, `wait_text`를 결과에 담는다.
  (영/한 둘 다: "usage limit", "limit reached", "이미지 생성 한도", "잠시 후 다시 시도" …)
- **헤드리스 금지에 가깝다.** 로그인·봇 체크 때문에 `headless=False`가 기본.
  실측(2026-09-19): `headless=True`로 chatgpt.com에 가면 Cloudflare 챌린지
  (`?__cf_chl_rt_tk=…`, 제목 `Just a moment...`)에 걸려 입력창이 끝내 안 뜬다.
  반드시 창을 띄운 채(`headless=False`) 써야 한다.
- **창을 띄우려면 실제 데스크톱 세션이 필요하다.** 대화형 데스크톱이 없는
  환경(에이전트/서비스 계정 등)에서 `headless=False`로 띄우면 Playwright가
  `BrowserType.launch_persistent_context: spawn UNKNOWN` 으로 죽는다.
  이 명령은 **사용자가 로그인한 윈도우 세션의 터미널에서** 실행해야 한다.

## 4. AI 티 제거 (2중)

1. **프롬프트** — `photo_request.ANTI_AI_RULES`가 모든 프롬프트 끝에 붙는다:
   평범한 폰 스냅샷, 자연광, 살짝 어긋난 화이트밸런스, 미세 모션 블러 + 그레인,
   가장자리 생활 잡동사니, 중앙 정렬/완벽 대칭 금지, 1~2도 기울기 허용,
   글자·로고·워터마크 금지, 3D 렌더·CG·HDR 과보정·매끈한 플라스틱 질감 금지,
   낮은 채도.
2. **후처리** — `postprocess()`:
   긴 변 1024px 축소 → ≤1도 랜덤 회전 → 가장자리 1~2.5% 랜덤 크롭 →
   가우시안 그레인(σ 2.5~5.0, Pillow `effect_noise`, numpy 불필요) →
   JPEG 품질 86~93 재압축(약한 세대 손실).
3. **세탁** — 이어지는 `photo_washer.wash`가 랜덤 EXIF(카메라/렌즈/촬영일시/시리얼)를 심는다.

## 4-1. 폭 400px 규칙 (발행에 붙는 사진)

생성물은 긴 변 1024px로 만들지만, **세탁본(= 실제로 글에 붙는 파일)은 폭 400px
이하**다. 비율은 유지하고 **작은 건 절대 늘리지 않는다**.

- `photo_washer.MAX_VARIANT_WIDTH = 400`
- `wash()` — 크롭 직후 `shrink_to_width()`를 거쳐 저장한다 → `make_variants()`도 자동 적용
- `Warehouse.pick_variant()` — 옛 세탁본이 더 넓으면 돌려주기 전에 자리에서 줄인다(방어선)
- `resize_all_variants(washed_dir)` — 기존 세탁본 일괄 정리(자리 수정, EXIF 보존)

일괄 정리 실측(2026-09-19): 세탁본 3,240장 중 **336장이 400px 초과 → 전부 축소**,
2,904장은 이미 규격(건드리지 않음), 오류 0건. 정리 후 최대 폭 400px.
표본 400장 EXIF 재확인 결과 카메라 메타 유실 0건.

```powershell
.\.venv\Scripts\python.exe -c "from v2r.warehouse.photo_washer import resize_all_variants; print(resize_all_variants('warehouse/images/washed'))"
```

## 4-2. 세션 점검 / 킵얼라이브

- `check_gpt_session()` — 비밀번호 없이 로그인 상태만 본다.
  1차 헤드리스로 입력창 확인 → Cloudflare 등으로 막히면 2차로 프로필 쿠키의
  **만료 시각만** 읽어 판정한다. **쿠키 값은 읽지도 찍지도 않는다.**
  반환: `logged_in`, `method`(`headless`/`cookie-expiry`), `expires_in_days`, `note`.
- 명령 `gpt 세션 점검` / `지피티 세션 유지` → 작업 `gpt_keepalive`
  (파서 `(gpt|지피티).*(유지|점검)`). 로그인이 풀려 있으면 텔레그램으로
  `ChatGPT 로그인이 풀렸습니다. PC에서 scripts\gpt-login.cmd 를 실행해 다시 로그인해 주세요` 발송.

## 4-3. 제휴 카페 일상 글 생성 (`gpt_chat`)

제휴 카페(씨씨앙·양평맘·쌍둥이맘) 일상 글은 **자사 카페 xlsx 일상 글과 완전히
별개**이고, ChatGPT 웹 세션으로만 만든다(**API 토큰 0**).

- `gpt_chat.ask(page, prompt, timeout=120)` — 프롬프트를 보내고 스트리밍이 끝날
  때까지 기다렸다가 마지막 어시스턴트 메시지를 돌려준다. 종료 판정은
  "중지 버튼이 사라졌고 + 텍스트가 2회 연속 동일". 한도 문구면 `GptLimitError`.
- `daily_generator.generate_affiliate_pool_via_gpt(cafes, per_cafe)` →
  `warehouse/manuscripts/affiliate_daily_pool.jsonl` (append, `content_hash` 중복 제거)
- 규칙: 제목 1줄 ≤20자 + 본문 1줄 ≤40자, 쉼표·마침표·말줄임표 금지,
  `ㅋㅋ`/`ㅠㅠ` 허용(이모지 금지), 브랜드명 금지, 상황 중심 반말 수다체.
  (`AFFILIATE_RULES`, 지침 `제휴 게시판(소재 포함) 일상 글 작성.md` 요약)
- 응답은 `제목: …` / `본문: …` 형식으로 받아 `parse_affiliate_reply`가
  길이·문장부호를 검증하고 **통과한 것만** 담는다.
- 명령: `제휴 일상 글 20개 만들어줘` → 작업 `generate_affiliate_daily`
  (파서 `제휴.*일상\s*글.*(생성|만들어)`, 기존 `generate_daily`보다 앞).
- 발행 쪽 `publish._daily_pool`은 이제 **이 풀만** 읽는다. 풀이 비었을 때만
  옛 `랜덤일상` 시트로 되돌아가며 그때 경고 로그를 남긴다.

## 5. 다시 로그인하는 법

가장 쉬운 방법: 바탕화면에서 **`scripts\gpt-login.cmd` 를 더블클릭**한다.
(.venv 활성화 → `python -m v2r.warehouse.gpt_images --login` → 창이 뜨면 직접 로그인,
최대 15분 대기. 반드시 본인이 로그인한 윈도우 세션에서 실행할 것.)

수동 절차:

1. 세션이 풀리면 `generate_photos` 실행 시 창이 열리고 콘솔에
   `브라우저 창에서 ChatGPT에 직접 로그인해 주세요` 가 뜬다.
2. 열린 창에서 **직접** 로그인한다. 최대 15분(`LOGIN_TIMEOUT=900`) 기다린다.
3. 로그인되면 자동으로 이어서 생성한다. 시간 안에 못 하면 조용히 멈추고
   `로그인 대기 — 내일 재시도` 를 보고한다 (실패로 치지 않는다).
4. 프로필이 깨졌다 싶으면 `data/browser-profile-gpt/` 폴더를 통째로 지우고
   1번부터 다시 한다. (지우면 로그인도 같이 날아간다)

세션이 살아 있는지만 보려면 (창 안 뜸):

```powershell
.\.venv\Scripts\python.exe -m v2r.warehouse.gpt_images --check
```

## 5-1. 매일 09:00 세션 점검 (작업 스케줄러)

등록 명령 — **사용자가 직접 한 번 실행**한다 (관리자 권한 명령 프롬프트):

```cmd
schtasks /Create /TN "V2R-GptKeepalive" /SC DAILY /ST 09:00 ^
  /TR "\"D:\v2r 자동화\v2r-command-system\.venv\Scripts\python.exe\" -m v2r \"gpt 세션 점검\"" ^
  /RL LIMITED /F
```

확인 / 수동 실행 / 삭제:

```cmd
schtasks /Query  /TN "V2R-GptKeepalive" /V /FO LIST
schtasks /Run    /TN "V2R-GptKeepalive"
schtasks /Delete /TN "V2R-GptKeepalive" /F
```

로그인이 풀려 있으면 텔레그램으로 안내가 오고, `scripts\gpt-login.cmd`를
실행해 다시 로그인하면 된다.

## 6. 결과 파일

- 생성 직후: `warehouse/inbox/new/<브랜드>/<키워드>/gpt_<epoch>_<n>.jpg` (긴 변 1024px)
- 수거 후 원본: `warehouse/images/originals/<브랜드>/<키워드>/`
- 세탁본: `warehouse/images/washed/<sha256>/0.jpg, 1.jpg, …`
- 인박스 처리분은 `inbox/new/_done/` 으로 옮겨져 다음 실행에서 다시 읽지 않는다.
