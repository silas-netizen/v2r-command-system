# 쌍둥이맘 모여라 — 최근 글 작성 계정 등급 실측 (2026-09-25)

- 대상: V2R(가상 테스트 사이트) `https://v2r.daboja.im`, 카페 `쌍둥이맘 모여라`(cafe_id 10174516), 게시판 `⭐가족업체 자유게시판`(menu_id 664)
- 조회 시각(KST): 2026-09-25 12시경 (실행 중 실측, 시스템 시각 기준)
- 방법: 우리 DB(publications/article_index)에 의존하지 않고 V2R **라이브 API**를 카페 전체 단위로 직접 호출해 확인. 화면(헤드리스 브라우저)으로도 열어 확인을 시도했으나 §5의 사유로 실패해 API 응답(JSON)을 증거로 남김.

## 1. 이 카페에 가입된 계정과 쓰기 권한

`GET /naver_cafes/naver_join_cafe?cafe_id=10174516` → 가입 계정 **31개**(탈퇴/활동정지 제외).
`GET /naver_cafes/menus` → 게시판 `⭐가족업체 자유게시판`(menu_id 664, `writelevel=120`)에 **실제로 쓸 수 있는 계정은 15개**뿐(나머지 16개는 등급 미달 또는 조회 실패로 제외).

## 2. 최근 작성 순 — 쓰기 가능 15개 계정 (사이트 API `written_articles`로 계정별 최근 글 확인)

| 순번 | login_id | 최근 글 작성 시각(UTC) | 최근 글 URL | 계정 등급(가입정보 API) | 글 작성자 표시 등급(글 상세 API) | 시트(아이디 리스트) 작업 구분 | account_state 제한 |
|---|---|---|---|---|---|---|---|
| 1 | chaeyaah | 2026-09-19T01:48:04Z | https://v2r.daboja.im/nc/articleDetail/01M2VN6S6R70XCCDY362BMDMZ0 | 카페 스탭(888) | 카페스탭(888) | 시트 없음 | 없음 |
| 2 | azqpale | 2026-09-19T00:09:47Z | https://v2r.daboja.im/nc/articleDetail/01M2TTVQ71PGR5JTV0A4K5H0M1 | 카페 스탭(888) | 카페스탭(888) | 시트 없음 | 없음 |
| 3 | lverland | 2026-09-19T00:05:56Z | https://v2r.daboja.im/nc/articleDetail/01M2TT27AWX84TR1TE79R4JEGW | 씨앗둥이(1) | 카페스탭(888) — §5 불일치 | 시트 없음 | 없음 |
| 4 | xalageas | 2026-09-17T13:25:42Z | https://v2r.daboja.im/nc/articleDetail/01M2QQ29QS6EZA955C4BSYNK4A | 카페 스탭(888) | 카페스탭(888) | 시트 없음 | 조회 안 됨(기록 없음) |
| 5 | walseibb | 2026-09-16T09:31:20Z | https://v2r.daboja.im/nc/articleDetail/01M2MQT333KRREY6CVDK3X0DK1 | 카페 스탭(888) | 카페스탭(888) | 시트 없음 | 조회 안 됨(기록 없음) |
| 6 | timusataro | 2026-09-16T09:24:21Z | https://v2r.daboja.im/nc/articleDetail/01M2MQRT5YWGYZCVXFDGC84EPF | 씨앗둥이(1) | 카페스탭(888) — §5 불일치 | 시트 없음 | 조회 안 됨(기록 없음) |
| 7 | kilstyau | 2026-09-16T08:42:04Z | https://v2r.daboja.im/nc/articleDetail/01M2MHRCGSKHNXT3GG21JZTY9G | 카페 스탭(888) | 카페스탭(888) | 시트 없음 | 조회 안 됨(기록 없음) |
| 8 | hushnane | 2026-09-16T08:14:36Z | https://v2r.daboja.im/nc/articleDetail/01M2MHQ5ENMND6MGWKR7ANZEGW | 카페 스탭(888) | 카페스탭(888) | 제휴 댓글 | 조회 안 됨(기록 없음) |
| 9 | huladoe | 2026-09-16T08:06:19Z | https://v2r.daboja.im/nc/articleDetail/01M2MHPHXBSBKT9TKCTQS7CZGZ | 카페 스탭(888) | 카페스탭(888) | 시트 없음 | 조회 안 됨(기록 없음) |
| 10 | granao | 2026-09-16T07:54:44Z | https://v2r.daboja.im/nc/articleDetail/01M2MHNYCTFAF8BKHCJ53KY73Z | 카페 스탭(888) | 카페스탭(888) | 시트 없음 | 조회 안 됨(기록 없음) |
| 11 | creudnb | 2026-09-16T07:45:19Z | https://v2r.daboja.im/nc/articleDetail/01M2MHNAVAPGFX6FCXDY9Z0SDD | 카페 스탭(888) | 카페스탭(888) | 시트 없음 | 조회 안 됨(기록 없음) |
| 12 | oaxastera | 2026-09-14T07:33:48Z | https://v2r.daboja.im/nc/articleDetail/01M2F96S9HAQSSRBHMQ4XST2GG | 카페 스탭(888) | 카페스탭(888) | 시트 없음 | 조회 안 됨(기록 없음) |
| 13 | tmolha | 2026-09-14T07:26:40Z | https://v2r.daboja.im/nc/articleDetail/01M2F965RMJPS1KQFED86SBGQ2 | 카페 스탭(888) | 카페스탭(888) | 시트 없음 | 조회 안 됨(기록 없음) |
| 14 | olleepah | 2026-09-14T07:11:25Z | https://v2r.daboja.im/nc/articleDetail/01M2F94YPHQA85WVPEV9W1SGKS | 카페 스탭(888) | 카페스탭(888) | 시트 없음 | 조회 안 됨(기록 없음) |
| 15 | poosivksy | 2026-09-14T06:41:45Z | https://v2r.daboja.im/nc/articleDetail/01M2F92GGK0GEMKFK9SKWEBZA2 | 카페 스탭(888) | 카페스탭(888) | 제휴 작업 | 조회 안 됨(기록 없음) |

(참고: `choppaiy` 계정은 가입은 돼 있으나 `written_articles` 조회 자체가 API 오류로 실패해 최근 글을 확인 못 함. 해당 게시판 쓰기 권한 목록(15개)에도 없어 순위에서 제외.)

## 3. 글 작성 가능 등급 계정 추천 10개

게시판 `writelevel=120` 기준 **실제로 글을 쓸 수 있는 15개 계정** 중 최근 작성 순 **상위 10개**를 추천한다(순위 1–10, 위 표와 동일):
`chaeyaah, azqpale, lverland, xalageas, walseibb, timusataro, kilstyau, hushnane, huladoe, granao`

10개 확보 완료 — 부족하지 않음. (여유분 5개: creudnb, oaxastera, tmolha, olleepah, poosivksy)

등급 요건: 게시판 쓰기 권한 API(`GET /naver_cafes/menus`)가 직접 내려주는 `writable=true` 15개 계정 기준이며, 가입정보 API 등급은 대부분 `카페 스탭(888)`, 예외로 `lverland`·`timusataro`는 `씨앗둥이(1)`인데도 쓰기 권한이 있다 — 즉 이 게시판은 등급 숫자 하나만으로 걸리지 않고 계정별 개별 권한(스탭 또는 특례)으로 열려 있다.

## 4. 시트·제한 상태 대조

- 계정 시트: `config/accounts_sources.yaml`(spreadsheet `1UgcAvHFCpC5N9joC9T5WCATK834F3XAtRrepFv6XbEs`, gid `218285244`, ID열 B) — 총 **31행**뿐이며, 이 카페의 쓰기 가능 15개 계정 중 시트에서 찾은 건 **`hushnane`(제휴 댓글)·`poosivksy`(제휴 작업)** 2개뿐이다. 비밀번호 열(C)은 읽지 않았다.
  - 나머지 13개 계정(chaeyaah, azqpale, lverland, xalageas, walseibb, timusataro, kilstyau, huladoe, granao, creudnb, oaxastera, tmolha, olleepah)은 이 시트에 **없다** — 이 시트가 이 V2R 카페의 계정 전체를 담고 있지 않거나(다른 용도의 ID 목록), V2R 테스트 계정 ID 체계가 시트의 실계정 ID와 별개일 가능성이 있다. 회사(@prekoko.co.kr) 계정 여부를 이 시트만으로는 가릴 수 없었다 — 추가 확인 필요.
- `data/v2r.sqlite`의 `account_state`: 15개 계정 중 **4개만 기록 있음**(azqpale, chaeyaah, lkeaub, lverland) — 전부 `restricted_until` 없음(제한 없음). 기록 없는 나머지 11개는 우리 시스템이 이 계정으로 발행한 이력이 없다는 뜻이며, 제한 여부는 "기록 없음(=제한 걸린 적 없음)"으로 본다.

## 5. 한계·불일치 — 그대로 보고

- **헤드리스 화면 로그인 실패**: `v2r/browser/session.py`의 API 세션 주입(`inject_api_session`)이 쿠키를 넣어도 SPA가 로그인 화면(`/login`)에 머물렀다(600초 대기 후 타임아웃). 화면을 실제로 띄워 보는 방식은 이번에 실패했고, 사용자 PC에 창은 띄우지 않았다(규칙 준수). 대신 **글 상세 API**(`GET /naver_cafe_articles/article?source_id=`)가 반환하는 `naver_cafe_article_history.writer.memberLevel/memberLevelName`을 "글 작성자 정보에 표시되는 등급"으로 사용했다 — 이 값은 사이트가 실제로 글에 붙여 내려주는 값이라 화면에 뜨는 값과 같다고 판단했다.
- **등급 값 불일치 발견**: `lverland`·`timusataro`는 가입정보 API(`naver_join_cafe`)에서는 등급이 `씨앗둥이(1)`인데, 이 두 계정의 최근 글 상세 API에서는 작성자 등급이 `카페스탭(888)`로 나왔다(나머지 13개는 두 값이 일치). 원인 미확인 — 가능성: ①가입정보 쪽 등급이 최신이 아니거나, ②V2R이 가상 테스트 사이트라 글 이력의 `writer` 스냅샷이 테스트용 고정값(전부 888)일 수 있음(15개 전부 글 상세에서는 동일하게 888로 나와 후자 쪽 의심이 더 크다). 표 3열·6열에 둘 다 적어 숨기지 않았다.
- 계정 시트 매칭 저조(2/15)로 "회사 계정만 남기기" 조건을 시트 기준으로는 완전히 적용하지 못했다. 이 시트가 맞는 문서인지(다른 탭·다른 시트 존재 여부) 다음 확인이 필요하다.

## 6. 증거 파일

- `data/twins_accounts/_capture_index.json` — 15개 계정 요약(등급·URL·시각)
- `data/twins_accounts/<login_id>_article_evidence.json` — 계정별 글 상세 API 원문(작성자 등급 필드 포함)
