# V2R API 연동 사양 (옛 코드 분석 결과, 재구현용)

> 출처: `v2r-auto-posting` 저장소 브랜치 `cursor/internal-command-system-415f` 및 `cursor/api-load-reduction-415f` 분석. 2026-09-19.
> 모든 자료는 가상 테스트 사이트(V2R) 기준이다.

- 베이스: `https://api-v2r.daboja.im`
- 사이트: `https://v2r.daboja.im` (목록 `/nc/board?view=list`, 에디터 `/nc/seone`, 글 상세 `/nc/articleDetail/{source_id}`)

## 0. 실측 정정 (2026-09-19)

아래 문서 본문은 옛 코드 분석 기준이다. 라이브(`https://api-v2r.daboja.im`)를 읽기 전용으로
호출해 확인한 정정 사항은 다음과 같다. 상세 근거는 `docs/reference/live-catalog.md`.

1. **봇 User-Agent 차단** — 기본 httpx UA로는 로그인부터 403 `{"detail":"bot user-agent blocked"}`.
   브라우저 UA(+`Origin`/`Referer`)가 필수다. → `v2r/api/auth.py`의 `BROWSER_HEADERS`.
2. **`/auths/challenge`는 GET** — POST는 405 `Allow: GET`. (프로토콜 문서 §3의 POST는 오기)
3. **`GET /naver_cafe_articles/board_histories`는 404 (경로 없음)** — 대체로
   `GET /naver_cafe_articles/article/written_articles?cafe_id&naver_login_id&page`를 쓴다.
   응답 `{"articles":[…], "total_count":int}`, 행에 `v2r_source_id`가 있어 source_id 확보 가능.
   단 **계정 단위 조회**라 카페 전체 이력을 한 번에 얻을 수 없다.
   `POST /naver_cafe_articles/board_histories/search`는 존재하지만 본문 스키마 미확인(기본 비활성).
4. **래핑 키 이름** — 카페 단건은 `naver_join_cafe`(단수, 계정이 중첩), 게시판 목록은
   `cafe_menus`이며 **행 키가 전부 camelCase**(`cafeId/menuId/menuName/writable`),
   말머리는 `cafe_heads`.
5. **말머리 미사용** — 실측한 카페의 대상 게시판은 모두 `useHead:false`라 말머리 0건이다.
   말머리 행 키(`head_id`/`head_name`)는 아직 실물 샘플로 확인되지 않았다.
6. 오류 본문은 `error{code,reason,extra}`와 FastAPI `{"detail":"…"}`가 혼재한다
   (`parse_error_body`가 둘 다 처리).

## 1. 엔드포인트 목록

공통 헤더: `Authorization: <브라우저에서 캡처한 토큰 문자열>`, `Content-Type: application/json`. 쿠키 미사용. GET은 쿼리스트링, POST/PUT은 JSON 바디.

| # | Method | Path | 쿼리/바디 | 응답에서 읽는 키 |
|---|---|---|---|---|
| 1 | GET | `/navers/accounts` | 없음 | `naver_login_id`(또는 `login_id`/`loginId`), `is_block`, `is_login_fail`, `my_info_v2.is_real_name` |
| 2 | GET | `/naver_cafes/naver_join_cafes` | 없음 | `naver_join_cafes` 또는 `cafes` 리스트. 행: `cafe_id`/`cafeId`, `pc_cafe_name`/`cafe_name`/`cafeName`/`mobile_cafe_name`/`name` |
| 3 | GET | `/naver_cafes/naver_join_cafe` | `cafe_id` | 계정 행: `login_id`/`naver_login_id`, `member_key`, `force_drop`, `stop_cafe_member`, `nick_name`/`nick`/`nickname`, `level_info.{member_level, member_level_name, member_level_icon_url}` |
| 4 | PUT | `/naver_cafes/naver_join_cafe/sync/account` | `{"cafe_id": int, "naver_login_id": str}` | 미사용(재조회로 확인) |
| 5 | GET | `/naver_cafes/menus` | `cafe_id`, `naver_login_id` | `cafe_menus`/`menus`. 행: `menuId`/`menu_id`, `menuName`/`menu_name`, `writable` |
| 6 | GET | `/naver_cafes/heads` | `cafe_id`, `naver_login_id`, `menu_id` | `head_id`/`headId`, `head_name`/`headName` |
| 7 | GET | `/naver_cafe_articles/board_histories` | `cafe_id`, `days_ago`, `include_reserve="true"`, `next_token`(선택) | `{"histories":[...], "next_token"}`. 행: `source_id`, `naver_account_login_id`, `title`, `parent_source_id`, `status`(`RESERVED`/`FAIL`/`DONE`…), `created_at`, `written_at`, `write_executor`. **404 가능** → 건너뛰고 진행 |
| 8 | POST | `/naver_cafe_articles/naver_cafe_article_source` | §4 페이로드 | `response["naver_cafe_article_source"]["source_id"]` |
| 9 | GET | `/naver_cafe_articles/article` | `source_id` | `naver_cafe_article_source`, `naver_cafe_article_destination`, `naver_cafe_article_history`, `naver_cafe_article_source_comments`, `naver_cafe_article_source_detail.body` |
| 10 | POST | `/naver_cafe_articles/article/delete` | `{"source_id": str}` | 미사용 |

### 응답 파싱 규약
- 서버 응답의 래핑 위치가 불확정 → 목록 응답은 전체 트리를 재귀 순회(`_walk_dicts`)해 원하는 키가 있는 dict만 추출.
- snake_case / camelCase 모두 허용 (`_field(item, "cafe_id", "cafeId")`).

### 글 ID / URL
```python
source_id = response["naver_cafe_article_source"]["source_id"]
url = f"https://v2r.daboja.im/nc/articleDetail/{source_id}"
re.search(r"/nc/articleDetail/([0-9A-Za-z_-]+)", url).group(1)
```

## 2. 로그인 / 인증 토큰

**로그인 API 엔드포인트는 미확인.** 토큰은 브라우저에서 획득:
1. Chrome(CDP)으로 `https://v2r.daboja.im/nc/board?view=list` 로드.
2. `Network.requestWillBeSent` 로그에서 `api-v2r.daboja.im` 요청의 `Authorization` 헤더 값을 그대로 저장.
3. 저장: 메모리만. 디스크 저장 없음 (새 시스템: 로컬 파일에 암호화 없이 저장은 금지, `.env`/로컬 전용 파일에 두고 git 제외).
4. 만료: HTTP 403 + 본문 `TOKEN_ERROR` → 토큰 폐기 후 재캡처 → 1회 재시도.

로그인 폼: `input[type=password]` 존재로 로그인 화면 판정 → email/password 입력 → "로그인" 클릭 → password 입력란 소멸 대기.

**새 시스템 과제:** 로그인 API 리버스(가능하면) 또는 헤드리스 브라우저 1회 실행으로 토큰 캡처 후 전 API 호출.

## 3. 카페 / 게시판 / 말머리 탐색 순서

```
GET /naver_cafes/naver_join_cafes                        → 가입 카페 목록
GET /naver_cafes/naver_join_cafe?cafe_id=…               → 카페별 계정 연결정보(member_key, 등급)
GET /naver_cafes/menus?cafe_id=…&naver_login_id=…        → 계정별 쓸 수 있는 게시판 (writable=False 제외)
GET /naver_cafes/heads?cafe_id=…&naver_login_id=…&menu_id=…  → 말머리
```
- 게시판 권한은 계정마다 다름 → 계정별 조회 후 합집합.
- 메뉴 조회 시 `NOT_LOGIN` / `session not found` / `SAFETY_RELEASE` 오류 계정은 비정상으로 제외.
- 이름 매칭: `re.sub(r"[^0-9a-z가-힣]+","",…).casefold()` 정규화 → 실패 시 한글만 → 중복이면 에러.
- 옛 코드 상수: 테스트 카페 `TEST_CAFE_IDS = {31670254, 31670256}`, 제휴 목적지 `CAFE_DESTINATIONS`, 자사 `SELF_OWNED_CAFE_IDS`, 이름→ID `KNOWN_CAFE_IDS`, 게시판 별칭 `BOARD_ALIASES`.

## 4. 글 등록

### 요청 `POST /naver_cafe_articles/naver_cafe_article_source`
```json
{
  "tag_list": ["태그1","태그2"],
  "title": "제목",
  "content_json": "<SE-ONE document를 json.dumps한 문자열>",
  "cafe_write_options": {
    "enableComment": true, "externalOpen": true, "enableScrap": true,
    "enableCopy": false, "useAutoSource": true, "useCcl": true,
    "cclTypes": ["ATTRIBUTION","NONCOMMERCIAL","NO_DERIVATIVE"],
    "open": false, "naverOpen": true
  },
  "comments": [ /* §5 */ ],
  "destination": {
    "cafe_id": 25016228, "cafe_name": "…",
    "head_id": null, "head_name": null,
    "menu_id": 328, "menu_name": "자유 수다방",
    "naver_login_id": "작성계정",
    "start_at": "2026-08-31T01:30:00Z",
    "target_view_count": 0,
    "use_comment_ai": true,
    "parent_id": null
  },
  "likes": [],
  "parent_source_id": "…"
}
```
- `start_at: null` = 즉시 발행. 제휴 수정글은 `target_view_count` 80~100 랜덤.
- `parent_source_id`는 수정글일 때만.

### `content_json` — SE-ONE 문서 구조
직렬화: `json.dumps(doc, ensure_ascii=False, separators=(",",":"))`
```json
{"document":{
  "version":"2.9.0","theme":"default","language":"ko-KR",
  "id":"<uuid4().hex.upper()[:26]>",
  "di":{"dif":false,"dio":[{"dis":"N","dia":{"t":0,"p":0,"st":2827,"sk":0}}]},
  "components":[
    {"id":"SE-<uuid4>","layout":"default","@ctype":"text",
     "value":[{"id":"SE-<uuid4>","@ctype":"paragraph",
               "nodes":[{"id":"SE-<uuid4>","value":"한 줄","@ctype":"textNode"}]}]},
    { "...이미지 컴포넌트: SmartEditor가 만든 dict 그대로..." }
  ],
  "documentId":""
}}
```
규칙:
- 본문 한 줄 = paragraph 1개. 빈 줄도 빈 paragraph로 보존.
- 연속 텍스트 줄은 하나의 `text` 컴포넌트, 이미지 위치에서 분리.
- `{사진}` 같은 플레이스홀더 → 해당 순번 이미지 컴포넌트 삽입. 플레이스홀더만 있던 줄은 빈 paragraph 유지.
- 컴포넌트 0개면 빈 텍스트 컴포넌트 1개.

### 이미지 업로드 — 전용 API 미확인. SmartEditor 브라우저 경유
1. `/nc/seone` 열고 카페 → 계정 → 게시판 순서로 선택 (SE-ONE은 계정 선택 전 게시판 비활성).
2. 사진 버튼 클릭 → `input[type=file]`에 절대경로 전달. (파일 입력 요소는 즉시 교체되므로 옛 요소 재접근 금지.)
3. `window.SmartEditor.getEditor('cafepc001').getDocumentData()`로 문서 읽기.
4. `@ctype ∈ {image, imageGroup, imageStrip}` 컴포넌트에 `src`/`path`/`fileName` 비어있지 않고 `fileSize > 0` → 성공 (45초 타임아웃).
5. 컴포넌트 dict를 `content_json`에 삽입.
- 사진 실패 시 사진 없이 등록하지 않고 발행 중단. 재시도 3회는 처음부터 전부 다시 첨부.
- **이미지는 예약 생성 전에 완전히 준비**해야 함 (예약 고아화 방지).

### 등록 확정 체크포인트
- 단계: `ACCOUNT_ASSIGNED` → `DAILY_CREATED`(source_id 저장) → `REVISION_CREATED` → `VERIFIED`. 중간 실패 시 생성 source 삭제 후 되감기.
- 앱 층 상태: 제출 직전 `uncertain` → 확인 후 `registered`. 상태 enum `registered|published|failed|uncertain`.
- POST 실패/타임아웃 복구: `board_histories`를 최대 8회(0.5초) 폴링해 `naver_account_login_id + title + parent_source_id` 일치, `created_at >= 요청시각-15초`, `status` 허용(`DONE` / 예약은 `RESERVED,DONE`)이면 채택, 아니면 삭제 후 실패.
- 즉시 발행 완료 대기: `GET /naver_cafe_articles/article` 폴링. `naver_cafe_article_history.status ∈ {DONE, SUCCESS}` → `written_at`; `FAIL` → `fail_reason`; 예약+30분 초과 → 보류.

### 등록 후 검증 (POST 후 반드시 GET)
`title`/`tag_list`, `menu_id`/`head_id`, `enableComment`, 본문 문단 원문 일치·중괄호 잔존 없음, 이미지 개수와 `src/path/fileName/fileSize`, `start_at`, `target_view_count`, 댓글 개수·순서.

## 5. 댓글 / 답글 / 수정글 / 삭제

- 댓글 전용 API 없음. 글 등록 POST의 `comments` 배열로 예약.
```json
{
  "contents": "댓글 내용", "naver_login_id": "댓글계정",
  "start_at": "2026-08-31T01:35:00Z", "repeat_count": 0, "interval_seconds": 0,
  "root_start_at": "2026-08-31T01:30:00Z",
  "reply_member": {"member_key":"…","naver_login_id":"…","nick":"…"},
  "comments": [ /* 자식 답글, 1단계 중첩 */ ]
}
```
- 옛 구조: 루트 5 + 답글 7 = 12개. 더 깊은 계층은 `reply_member`로 표현.
- 같은 계정이 같은 분(minute)에 두 댓글 불가 → 1분 밀기 또는 번들 전체 시프트(최대 24h).
- 고정 오프셋(분): 댓글1~5 = 5~9, 대댓글 = 15~19, 대대댓글2 = 26, 대대대댓글2 = 36.
- **수정글** = 같은 POST에 `parent_source_id` 포함. 카페별 지연 예: 4h / 20h / 22h.
- **삭제** = `POST /naver_cafe_articles/article/delete`. 찌꺼기 정리: `FAIL` 또는 30분 넘게 `RESERVED` + `parent_source_id is None` + `write_executor is None`.

## 6. 호출량 제한과 캐시 (api-load-reduction 브랜치 기준)

| 항목 | 내용 |
|---|---|
| 전역 레이트 게이트 | 0.25초 간격, 프로세스 전역 Lock, 모든 워커 공유 |
| GET 캐시 | TTL 300초, key = 완성 URL. 대상 `/navers/accounts`, `/naver_cafes/naver_join_cafes`, `/naver_cafes/menus`, `/naver_cafes/heads` |
| 재시도 | 429/5xx만. 최대 3회, 대기 (10, 30)초. `Retry-After` 헤더 우선(최대 900초) |
| 멤버 상태 캐시 | `cafe_id`별 `naver_join_cafe` 응답 재사용 |
| 이력 페이지 | 5페이지, `days_ago=30` |
| 폴링 완화 | 예약 15초 전까지 폴링 안 함, 이후 2→5초 간격, 429면 10초 |

## 7. 오류 코드 매핑
| 신호 | 의미 |
|---|---|
| `NOT_FOUND_MODEL` + `NaverJoinCafeAccoun` | 가입 연결정보 없음 |
| `33007` / 등급 | 등급 미달 |
| `NOT_LOGIN` / `session not found` | 세션 없음 |
| `NAVER_LOGIN_FAIL` / `NID_SES` | 로그인 실패 |
| `27000` | 계정 활동 제한 → 일정 기간 제외 |
| `20004` / `연속으로 등록` | 연속 등록 제한 |
| `TOKEN_ERROR` (403) | 토큰 만료 → 재캡처 |
| `DELETED_NAVER_CAFE_ARTICLE_SOURCE` | 글 삭제됨 |

## 8. 새 시스템에서 해결해야 할 미확인 항목
1. 로그인 API (토큰 발급) 엔드포인트
2. 이미지 업로드 엔드포인트
→ 브라우저 CDP 캡처 도구로 1회 탐색해 확정하는 작업을 첫 스프린트에 넣는다.

## 9. 댓글 취소·추가 실측 (프런트 번들 2026-09-19)

공개 프런트 번들 `https://v2r.daboja.im/assets/index-BTDlw7VX.js`를 내려받아
`naverCafeArticle` 스토어의 정적 메서드를 그대로 뽑았다(`JM = /naver_cafe_articles`).
**경로는 실측이고, 요청 바디 모양은 미확인**이다(바디는 지연 로드되는 화면 청크에
들어 있고 번들 안에서는 `e`로 그대로 전달만 한다).

| 화면 메서드 | HTTP | 경로 |
|---|---|---|
| `getArticleDetail` | GET | `/naver_cafe_articles/article` |
| `getArticleDetailComments` | GET | `/naver_cafe_articles/comments` (params: `cafe_id, article_id, naver_login_id, page`, v2는 `order_by, cafe_url` 추가) |
| `addArticle` / `addSeoneArticle` | POST | `/naver_cafe_articles/naver_cafe_article_source` |
| `updateArticle` / `updateSeoneArticle` | **PUT** | `/naver_cafe_articles/article` |
| `deleteArticle` | POST | `/naver_cafe_articles/article/delete` |
| `syncArticle` | POST | `/naver_cafe_articles/article/sync` |
| `syncComments` | POST | `/naver_cafe_articles/comments/sync` |
| `createInstantComment` | POST | `/naver_cafe_articles/comment` |
| `updateInstantComment` | PUT | `/naver_cafe_articles/comment` |
| `createInstantReplyComment` | POST | `/naver_cafe_articles/comment/reply` |
| `deleteInstantComment` | POST | `/naver_cafe_articles/comment/delete` |
| `deleteStaffArticleDetailComment` | POST | `/naver_cafe_articles/staff_board/comment/delete` |
| `createCommentByAi` | POST | `/naver_cafe_articles/comments/by_ai` |
| `recoverArticle` | POST | `/naver_cafe_articles/article/recover` |
| `deleteNaverWrittenArticle` | POST | `/naver_cafe_articles/article/delete_in_direct` |

읽어낸 것:

- **댓글 단위 엔드포인트는 존재한다.** 다만 `Instant*` 계열은 이름·파라미터
  (`article_id`, `cafe_id`, `naver_login_id`)로 보아 **이미 네이버에 올라간 글의
  실시간 댓글**을 다루는 쪽이다. 우리가 고쳐야 하는 건 아직 `RESERVED` 상태인
  **source 댓글**이라 같은 경로인지 확인되지 않았다.
- **예약 상태 글·댓글의 수정 경로는 `PUT /naver_cafe_articles/article`** 로 보인다
  (등록 화면의 "수정"이 이 메서드를 쓴다). 등록(POST)과 같은 바디에
  `source_id`를 얹는 형태일 가능성이 높으나 **미검증**이다.
- 번들의 오류 문구로 미루어 서버는 예약 댓글 취소를 인지한다:
  `"원본 댓글이 삭제되어 댓글 등록에 실패했습니다"`(cancelCommentReserve),
  `"원글이 삭제되어 댓글/답글 발행이 취소되었습니다"`(notFountArticle)
  → **원글을 지우면 딸린 예약 댓글도 함께 취소**된다.
- `POST /naver_cafe_articles/article/recover`(복구)가 있어 삭제가 완전 파괴는
  아닐 수 있으나, 이 역시 바디 미확인.

→ 결론: 예약 댓글만 골라 취소/추가하는 경로는 **확정하지 못했다**. 실제 수리는
바디 모양을 한 번 캡처(브라우저 CDP)해 확정한 뒤에 하는 것이 안전하다.


## 10. 글 수정 (실측 확정 2026-09-19)

`PUT /naver_cafe_articles/article` — 바디는 **평면**(POST의 `destination` 묶음과 다름):

```
{ source_id, title, content_json, tag_list, cafe_write_options,
  cafe_id, menu_id, head_id, naver_login_id, start_at,
  target_view_count, target_comment_count, comments: [], likes: [], parent_source_id }
```

- 빠진 필드는 422 `{"detail":[{"type":"missing","loc":["body","menu_id"]...}]}` 로 알려준다.
- 이미 발행(SUCCESS)된 글도 제목·본문 교체가 통과했다 (씨씨앙 01M2VDPPA8P01A73GMYPFY9Y29). 재조회로 반영 확인.
- 코드: `v2r/api/articles.py::update_article`.
