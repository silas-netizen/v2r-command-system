# V2R 라이브 카탈로그 실측 (2026-09-19)

> 실제 API(`https://api-v2r.daboja.im`)를 **읽기 전용**으로 호출해 얻은 응답 형태.
> 값은 마스킹/절단했다. 토큰·비밀번호는 기록하지 않는다.
> 비교 대상: `docs/reference/v2r-api-spec.md`, `docs/reference/v2r-auth-protocol.md`.

## 0. 인증 실측 결과

| 항목 | 실측 |
|---|---|
| `POST /auths/login` | **200** (multipart/form-data, `username`/`password`) — 폼 필드명·멀티파트 모두 사양대로 맞음 |
| 챌린지 필요 여부 | **불필요**. 첫 요청에서 바로 토큰 발급 (PoW 경로 미사용, 난이도·풀이시간 해당 없음) |
| 응답 형태 | `{"token": {"access_token": "<masked>", ...}}` — `_store_token`이 그대로 처리 |
| 실패 응답 형태 | `{"error":{"code":<int>,"reason":"UNAUTHORIZED","extra":{"login_id":...},"status_code":401}}` — `code`가 **정수** |
| `GET /auths/challenge` | 200 `{"challenge":{nonce, timestamp, difficulty:5, signature, version:"v1"}}` |
| `POST /auths/challenge` | **405 Method Not Allowed** (`Allow: GET`) |
| 비-브라우저 UA | **403 `{"detail":"bot user-agent blocked"}`** — `error` 블록 없음, `code`/`reason` 모두 `None` |
| 토큰 저장 | `data/session.json` (AuthSession 기본 경로) |

### 프로토콜 문서와의 차이
1. `/auths/challenge` 는 **GET 전용**이다(문서 §3은 POST로 적혀 있음).
2. 문서에 없는 차단이 하나 더 있다: **User-Agent 검사**. 기본 httpx UA로는 로그인 자체가 403.
   `Origin`/`Referer`까지 사이트 값으로 맞춰 보냈다.
3. 오류 본문은 `detail`(FastAPI 기본) 형태와 `error{code,reason,extra}` 형태가 **혼재**한다.
   `detail` 형태는 `parse_error_body`가 `code`/`reason`을 못 뽑아 `kind="other"`가 된다.

---

## 1. 카페 · 계정 요약

계정 총계: `/navers/accounts` 97건 중 `is_block`/`is_login_fail` 제외 후 **86건**.

| cafe_id | 카페명 | #계정(정상) | 샘플 메뉴 (menu_id, name, writable) | 말머리 |
|---|---|---|---|---|
| 10174516 | 쌍둥이맘 모여라 - 쌍둥이 대표 카페 | 31 | (미조회) | — |
| 14567700 | 고요한 아침 | 33 | (34, `가입인사`, true), (7, ` 공지 / 이벤트`, true), (1, `이벤트 당첨 발표`, true), (10, `몸 증상`, true), (14, `극복후기`, true) … 총 **25개** | menu 34 기준 **0건** (`useHead:false`) |
| 15175096 | 글로시 마이 | 76 | (미조회) | — |
| 15441090 | 웨딩 노트 | 86 | (미조회) | — |
| 16149995 | 송도포털 | 76 | (미조회) | — |
| 22788814 | 양평 맘`s 전원 Story | 87 | (미조회) | — |
| 23708088 | 헬씨 트리 | 76 | (미조회) | — |
| 25016228 | 국내1위 다이어트 커뮤니티 씨씨앙(…) | 97 | (미조회) | — |
| 26616683 | 러브 인썸 (Love in Some) | 70 | (미조회) | — |
| 26680163 | 마이 웨딩 드림 | 75 | (미조회) | — |
| **31670254** | **태극마케팅센터** (테스트) | 96 | (1, `자유게시판`, true) — **1개뿐** | menu 1 기준 **0건** (`useHead:false`) |
| **31670256** | **소나무마케팅센터** (테스트) | **1** (`qbneb`) | (1, `자유게시판`, true) — **1개뿐** | menu 1 기준 **0건** |

- 메뉴는 각 카페 상위 3개 계정으로 조회했고, 세 카페 모두 계정별 결과가 동일해
  `writable_accounts`는 조회한 계정 전체를 포함한다. `unhealthy_accounts`는 **비어 있음**(세 카페 모두).
- 고요한 아침 계정 등급은 `member_level: 888 / "카페 스탭"`, 테스트 카페 두 곳은 `1 / "카페 멤버"`.

---

## 2. 응답 형태 (키 / 타입 / 마스킹 샘플)

### `GET /navers/accounts`
래핑: `{"naver_accounts": [ ... ]}` (97건)

```
naver_login_id      str   "qbneb"
member_key          str   "0OLKIHvb…"
is_block            bool  false
is_login_fail       bool  false
login_by_otp        bool  false
is_remove_bad_article bool false
fail_reason         str   ""
sync_status         str   "success"
sync_fail_reason    null
static_ip_name      str   "np-static-ip-983"
static_ip_expired_at str  "2026-10-05T14:59:59Z"
bound_desktop_{id,name,os,public_ip,location,seq}  null
v2r_favorite_account bool false
search_exposure     {status:str "EXPOSED", fact:{cafe_id:int, naver_login_id:str,
                     history_id:str, check_id:int, article_id:int, checkpoint:str,
                     raw_status:str, checked_at:str}}
my_info_v2          {name:str, phone_number:str(마스킹됨 "+44 7481-7***"),
                     is_real_name:bool, is_group_account:bool, group_name:str,
                     is_level2_auth:bool, block_other_area:bool, block_abroad:bool,
                     pswd_email:str("qb******@p*******.co.kr"), letter_email:str,
                     pswd_email_editable:bool, letter_email_editable:bool,
                     dedicated_login_info:null, sync_at:str}
memo, pswd_email, letter_email, birth_date, gender, created_at …
```

### `GET /naver_cafes/naver_join_cafes`
래핑: `{"naver_join_cafes": [ ... ]}` (12건)

```
cafe_id           int   10174516
cafe_url          str   ""            ← 빈 문자열. 실제 URL은 statistic.cafe_url
pc_cafe_name      str   "쌍둥이맘 모여라 …"
mobile_cafe_name  str   동일
favorite_cafe / v2r_favorite_cafe   bool
v2r_favorite_menus  list
is_block          bool ; block_at null
created_at / modified_at  str(ISO Z)
statistic {cafe_url:str "getamped2", cafe_icon_url:str, cafe_name:str, desc:str,
           intro:str, member_count:int, cafe_grade:{grade:int, step:int, name:str,
           nameDesc:str, bPoint:int, ePoint:int, imgUrl:str, description:str,
           warningCount:int}, popular_status:str, popular_sync_status:str,
           max/min_view_count:int, max/min_comment_count:int, max/min_total_score:int,
           max/min_like_count:int, comment_diffs:[{max_diff_seconds, min_diff_seconds}],
           used_popular_article_count:int, sync_at:str,
           ranking_histories:[{beginDate:"20251216", endDate:"20251231", …}]}
```

### `GET /naver_cafes/naver_join_cafe?cafe_id=…`
래핑: `{"naver_join_cafe": { …카페필드…, "naver_accounts": [ ... ] }}` — **단수 키**, 카페 dict 안에 계정 배열.

계정 행:
```
login_id            str   "walseibb"      ← naver_login_id 아님
nick_name           str   "왈라비00"
member_key          str   "E4n-fmsa_6…"
force_drop          bool  false
stop_cafe_member    bool  false
sync_status         str   "success" ; sync_fail_reason null
level_info {member_level:int 888, member_level_icon_id:int,
            member_level_name:str "카페 스탭", level_icon_image_url:str}
image_info {profile_image_url, circle_profile_image_url, profile_icon_image_url}
joinDate            str   "Aug 24, 2026 12:19:03 PM"   ← camelCase + 비ISO 형식
real_name_using_cafe / real_name_use / show_level_up_apply_icon /
allow_popular_member / current_popular_member / show_blog / show_sex_and_age  bool
visit_count / article_count / reply_count / comment_count / live_count /
member_subscriber_count  int
member_alarm_status bool ; persona_id null ; not_used_persona bool ; sync_at str
search_exposure {status:str "UNKNOWN", fact:null}
v2r_favorite_account_in_cafe bool
```

### `GET /naver_cafes/menus?cafe_id=…&naver_login_id=…`
래핑: `{"cafe_menus": [ ... ]}` — 행은 **전부 camelCase**.

```
cafeId int ; menuId int 1 ; menuName str "자유게시판"
menuType str "B" ; boardType str "L"
writeLevel int 1 ; readLevel int 1
useHead bool false ; useComment bool false
defaultWriteOpenType int 0 ; menuTradeType str "" ; order int 1
writable bool true
groupPurchase / personalTrade / marketBoardType / accessRestrict /
cafeBookMenuType / separatorMenuType / subscribedNaverMeFeed / hidden   bool
```

### `GET /naver_cafes/heads?cafe_id=…&naver_login_id=…&menu_id=…`
래핑: `{"cafe_heads": [ ... ]}` — 실측 3개 카페 모두 **빈 배열**(대상 게시판 `useHead:false`).
행 키(`head_id`/`head_name`)는 샘플을 얻지 못해 미확인.

### `GET /naver_cafe_articles/board_histories`
**모든 카페(12곳) × days_ago 7/30 × 파라미터 6종 조합에서 HTTP 404 `{"detail":"Not Found"}`.**
`/naver_cafe_articles/__nope__` 와 동일한 응답이며, 경로 자체가 배포판에 없다.
화면 번들(`index-BTDlw7VX.js`)에는 `GET ${JM}/board_histories` 호출이 남아 있으나
서버에는 `POST /naver_cafe_articles/board_histories/search`(GET 시 405)·
`/board_histories/filter_options`(GET 시 405) 계열만 존재한다.

**대체 확인 경로(읽기 전용, 실측 성공):**
`GET /naver_cafe_articles/article/written_articles?cafe_id&naver_login_id&page`
→ `{"articles":[…], "total_count":int}`. 행은 네이버 원본 필드(camel/소문자)에
**`v2r_source_id`** 가 붙어 있어 source_id 확보가 가능하다.
```
clubid int ; articleid int 1257 ; menuid int 1 ; subject str "김천kb보험 …"
writerMemberKey str ; writernickname str ; writedt str "Sep 15, 2026 12:31:23 PM"
readcount / commentcount int ; headid int 0 ; openyn "N" ; …
clubMenu {clubid, menuid, menuname, menutype, boardtype, readlevel, writelevel,
          usehead, useComment, …}
v2r_source_id str "01M2HHWSF8QSF322WR8TCCNZXS"
```
참고: `GET /naver_cafe_articles/history_in_a_month?cafe_id=…` 는 200이지만 **집계 숫자만** 반환
(`popular_article_count, article_count, staff_article_count, load_url_article_count,
load_url_comment_count, view_count, like_count, comment_count, staff_comment_count`) — 행 목록 없음.

### `GET /naver_cafe_articles/article?source_id=…`
샘플 `source_id=01M2HHWSF8QSF322WR8TCCNZXS` (태극마케팅센터, `azqpale`).
최상위 키 6개: `naver_cafe_article_source`, `naver_cafe_article_source_detail`,
`naver_cafe_article_source_comments`, `naver_cafe_article_destination`,
`naver_cafe_article_likes`, `naver_cafe_article_history`.

```
naver_cafe_article_source
  source_id str ; title str ; tag_list list(0) ; is_deleted bool
  parent_source_id null ; child_source_id null
naver_cafe_article_source_detail
  body str(SE-ONE JSON: {"document":{"version":"2.9.0","theme":"default",…}})
  content_html str("<div class=\"se-viewer se-theme-default\" …")
naver_cafe_article_source_comments   list(0)
naver_cafe_article_likes             list(0)
naver_cafe_article_destination
  destination_id str ; naver_login_id str ; naver_nick_name str ; title str
  cafe_id int ; cafe_name str ; menu_id int ; menu_name str
  head_id null ; head_name null
  target_view_count int ; target_comment_count int
  status str "SUCCESS" ; fail_reason null
  write_options {enableComment, enableCopy, enableScrap, externalOpen, open,
                 naverOpen, useAutoSource, useCcl, cclTypes:["ATTRIBUTION",…]}
  schedule_arn null ; schedule_name null ; start_at null
  popular_article_info {score:int, possibility:float}
  staff_board bool ; use_comment_ai bool ; use_search_exposure bool
  parent_id null ; del_parent bool ; editor_version str "seone"
  created_at / modified_at str
naver_cafe_article_history
  history_id str ; article_id int 1257 ; naver_login_id str ; naver_nick_name str
  status str "DONE" ; fail_reason null ; written_at str ; sync_at str
  comment_status / like_status / view_count_status ; is_deleted bool
  made_by ; made_by_naver_login_id str ""
  proxy_url str("http://brd-customer-…") ; writer {nick, image{url,service,type},
    memberLevel, memberLevelName, memberLevelIconUrl, currentPopularMember}
  title / cafe_name / cafe_id / menu_id / menu_name / head_id
  like_count / like / try_view_count / target_view_count / real_view_count /
  write_comment_count / target_comment_count / real_comment_count / try_like_count
  popular_info_in_cafe / popular_info_in_real / popular_info_in_weekly
  popular_article_info ; staff_board ; use_comment_ai ; use_search_exposure
  search_exposure_last_status / search_exposure_checked_at / search_exposure_checks
  parent_id ; del_parent
```

---

## 3. `v2r-api-spec.md` 대비 불일치

| # | 항목 | 사양 | 실측 | 영향 |
|---|---|---|---|---|
| 1 | 공통 헤더 | `Authorization: <브라우저 캡처 토큰>`, "쿠키 미사용" | `Bearer <token>` 정상 동작. 단 **브라우저 User-Agent 필수**(아니면 403 `bot user-agent blocked`) | 치명 → 수정 완료 |
| 2 | `/auths/challenge` | (프로토콜 문서) POST | **GET 전용**, POST는 405 | 치명(챌린지 발생 시) → 수정 완료 |
| 3 | `/naver_cafe_articles/board_histories` | GET, `{"histories":[…], "next_token"}` | **경로 없음(404)**. 대신 `POST /board_histories/search` 존재 | 치명 → 아래 4-1 |
| 4 | 이력 행 필드 | `naver_account_login_id`, `write_executor` | 확인 불가(엔드포인트 부재). `article` 응답 기준 로그인 필드는 **`naver_login_id`**, `write_executor` 대응 필드는 **`made_by_naver_login_id`**로 보임 | `find_recent_source`의 `naver_account_login_id` 우선 매칭이 헛돌 가능 |
| 5 | `/naver_cafes/naver_join_cafe` 래핑 | "계정 행" | `{"naver_join_cafe": {…, "naver_accounts":[…]}}` — **단수 키**, 계정은 중첩 | `walk_dicts` 덕분에 파싱은 정상 |
| 6 | 계정 로그인 키 | `login_id`/`naver_login_id` | 카페별 응답은 **`login_id`만**, 전역 계정 목록은 **`naver_login_id`만** | 현행 `field()` 폴백이 둘 다 커버 → 정상 |
| 7 | 메뉴 행 키 | `menuId`/`menu_id` 혼용 가정 | 실제로는 **camelCase 단독**(`cafeId/menuId/menuName/writable`) | 정상 동작 |
| 8 | 메뉴 래핑 | `cafe_menus`/`menus` | **`cafe_menus`만** | 정상 |
| 9 | 말머리 래핑 | `head_id`/`head_name` | 래핑은 **`cafe_heads`**, 행 키는 샘플 0건이라 미확인 | 말머리 사용 카페에서 재확인 필요 |
| 10 | 오류 본문 | `error{code,reason,extra}` | `error{code:**int**,reason,extra,status_code}` **또는** FastAPI `{"detail":"…"}` | `classify()`가 `detail` 형태를 `other`로만 분류 |
| 11 | 카페 URL | `cafe_url` | 최상위 `cafe_url`은 **빈 문자열**, 실제 값은 `statistic.cafe_url` | 카페 URL이 필요하면 경로 변경 |
| 12 | `/openapi.json`, `/docs` | — | 401 `Invalid or missing docs key` (열람 불가) | 스키마 자동 확인 불가 |

---

## 4. 코드에 반영한 수정 / 남은 위험

### 4-0. 반영된 수정 (테스트 25건 통과)
- `v2r/api/auth.py`
  - `BROWSER_USER_AGENT`, `SITE_ORIGIN`, `BROWSER_HEADERS` 상수 추가.
  - `DeviceProfile.signal_headers()`에 `User-Agent` 포함.
  - 챌린지 발급을 `client.post(...)` → **`client.get(CHALLENGE_PATH, ...)`**.
- `v2r/api/client.py`
  - 기본 httpx 헤더를 `{"Accept": "application/json"}` → `BROWSER_HEADERS`
    (`Accept`, `User-Agent`, `Origin`, `Referer`).

### 4-1. 남은 위험 (미수정, 쓰기 작업 전 처리 필요)
- **`board_histories`가 항상 404 → 빈 목록.** `articles.py`의 `board_histories()`는 404를
  정상적인 "이력 없음"으로 삼키므로, `create_article`의 중복 발행 방지 복구
  (`find_recent_source`)가 **조용히 항상 실패**한다. POST가 타임아웃/5xx로 끝나면
  이미 등록된 글을 못 찾고 그대로 예외를 올린다(중복 발행 자체는 안 하지만 복구 불가).
  → 대체안: `GET /naver_cafe_articles/article/written_articles`(`v2r_source_id` 포함)로
  복구 조회를 바꾸거나, `POST /naver_cafe_articles/board_histories/search`의
  요청 스키마를 확인해 전환. (이번 세션은 읽기 전용이라 POST 미시도)
- 404를 빈 목록으로 삼키는 처리 때문에 **경로 오류와 데이터 없음이 구분되지 않는다.**
  최소한 경고 로그가 필요하다.
- 말머리 행의 실제 키 이름은 여전히 미확인(`useHead:true`인 게시판에서 재확인).
