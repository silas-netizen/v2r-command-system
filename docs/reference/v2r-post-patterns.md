# V2R 발행글 패턴 관찰 (카페별 / 브랜드별)

- 조사일: 2026-09-19
- 대상: `https://v2r.daboja.im/nc/board?view=list` (글목록 리스트 뷰) + `/nc/articleDetail/<source_id>` 상세 12여 건
- 방법: 읽기 전용. 페이지 XHR 응답 후킹(`naver_cafe_articles/board_histories/search`)으로 목록 JSON 수집 + 상세 페이지 DOM 파싱
- 계정: 김성실 (1P), V2R v3.30.1

---

## 1) 목록 화면 구조

### 1-1. 뷰 / 공통 요소
- 상단 탭: **리스트 / 주간 / 월간**
- 안내 문구
  - "게시글은 작성일 기준 최대 6개월 전까지 조회할 수 있습니다."
  - "게시판·닉네임 필터는 **카페 1개 선택 시**에만 사용할 수 있습니다."
  - "글목록에 등록된 글을 N카페에서 직접 수정하신 경우 컨텐츠 충돌… 'URL글 생성'으로 불러와서 작업하세요"
- 액션 버튼: `글쓰기`, `스태프 게시판 글쓰기`, `URL글 생성`
- 페이지 크기 셀렉트: 기본 `15개씩`
- 페이지네이션: 1~10 + `>` (다음 10페이지 창). 전체 6,733건 / 449페이지지만 **약 100페이지(=1,500행) 이후 API가 error를 반환**하여 더 못 감 → 깊은 페이지는 정렬(오름/내림) 전환으로 접근해야 함

### 1-2. 컬럼 (좌→우)
| # | 컬럼 | 비고 |
|---|---|---|
| 1 | 현황 | 상태 아이콘(예약/완료/실패) |
| 2 | 인기 | 인기글 여부 |
| 3 | 지수 | 계정 지수 배지: `일반`, `준최1+`, `준최2+` |
| 4 | 발행일 ↕ | 정렬 가능 |
| 5 | 작성일 ↕ | 정렬 가능 |
| 6 | 카페명 | **필터 있음** |
| 7 | 게시판명 | 필터 있음(카페 1개 선택 시) |
| 8 | 별명 (계정) ↕ | 필터 + 정렬. `닉네임(네이버ID)` 형식 |
| 9 | 제목 | 필터(검색) |
| 10 | 작성 형태 | 대부분 `-` |
| 11 | 작성 IP | 대부분 `-` |
| 12 | 조회수 / 13 댓글수 / 14 좋아요 | |
| 15 | 검색 노출 | `노출` / `미노출` / `-` |
| 16 | 데이터 조회일시 | sync 시각 |

필터 아이콘(깔때기)은 **현황 / 발행일 / 카페명 / (게시판명) / 별명(계정) / 제목** 컬럼에 존재.

### 1-3. 카페 필터 동작 (중요)
- 카페명 헤더의 깔때기 아이콘 클릭 → 팝오버 오픈
- 팝오버 = 검색창 + **체크박스 다중선택 리스트**(카페 아이콘 + 이름) + 하단 `n개 선택 / 초기화 / 적용`
- **체크만 해서는 조회되지 않고 반드시 `적용`을 눌러야 검색 API가 호출됨.** `초기화`는 팝오버를 닫으며 필터를 해제함
- 카페 1개만 선택해야 게시판·닉네임 필터가 활성화됨

### 1-4. 목록 API 응답 필드 (참고)
`POST https://api-v2r.daboja.im/naver_cafe_articles/board_histories/search`
→ `items[] / page_info{page,page_size,total_count,total_pages,has_next,has_prev} / next_cursor`

주요 item 필드:
`source_id, title, naver_account_login_id, naver_account_nick_name, cafe_id, cafe_name, cafe_url, menu_name, written_at(UTC), status, comment_status, like_status, view_count_status, reserved_comment_status, reserved_comment_fail_reason, popular_score, popular_possibility, is_popular, fail_reason, made_by("v2r"), staff_board, parent_id, parent_source_id, child_source_id, del_parent, sync_at, created_at, search_exposure_last_status, search_exposure_checks[{checkpoint:"T0",status,due_at,checked_at}], account_search_exposure, real/target/try_view_count, write/real/target_comment_count, like_count`

- `status`: 관찰값 `DONE`, `FAIL` (예약 대기 건은 이번 수집분엔 없음)
- **수정글 판별**: `parent_source_id != null` (원글은 `child_source_id`로 자식을 가리킴)
- **말머리(head)는 목록 API/화면 어디에도 노출되지 않음** — `menu_name`(게시판)만 있음

---

## 2) 카페별 표

필터에 노출되는 카페 12개 (UI 표기 그대로):

| # | 카페명(UI) | cafe_id | cafe_url | 총 글수 |
|---|---|---|---|---|
| 0 | 쌍둥이맘 모여라 - 쌍둥이 대표 카페 | 10174516 | getamped2 | 228 |
| 1 | 고요한 아침 | – | – | 686 |
| 2 | 글로시 마이 | – | – | 337 |
| 3 | 웨딩 노트 | – | – | 35 |
| 4 | 송도포털 | – | – | 383 |
| 5 | 양평 맘\`s 전원 Story | 22788814 | yangmom | 397 |
| 6 | 헬씨 트리 | – | – | 27 |
| 7 | 국내1위 다이어트 커뮤니티 씨씨앙(식단,운동,후기,헬스,체험단) | 25016228 | cantsb | 1,528 |
| 8 | 러브 인썸 (Love in Some) | 26616683 | fik50kjkc | 1,415 |
| 9 | 마이 웨딩 드림 | 26680163 | fik505050 | 1,325 |
| 10 | 태극마케팅센터 | 31670254 | kih8h8g | 357 |
| 11 | 소나무마케팅센터 | 31670256 | iuywe91 | 15 |

(– 는 이번 수집에서 id/url을 확보하지 못한 카페)

### 2-1. 카페별 상세

| 카페 | 주 게시판(menu_name) | 작성 계정(닉네임/ID) | 글 유형 | 발행 시간대(KST) | 이미지 | 댓글 |
|---|---|---|---|---|---|---|
| **씨씨앙** | `자유 수다방` 단일 | 끌레오(croeues), 데벨갓(zzoonylee), 즈즈에스(zzshop7876), 답방무조건가요(aitigkq539), 뉴칼레도니아(qlvjticcid11350), 가능dji(dplkubclxs75486) | **원글(미끼)+수정글(본 홍보) 쌍**. 원글 0댓글, 수정글 12댓글 | 평일 07:50~12:50 집중 | 0~1 (본문 중단 이미지 슬롯) | 12 (루트 6 + 대댓 6) |
| **양평 맘\`s 전원 Story** | `이모저모 이야기💕` 단일 | ueki97(ueki97), 바나나민트초코(ppo9011), 워니이w4B(choppaiy) | 원글+수정글 쌍 (동일 시각) | 07:45~09:40 | 1 (본문 2단락 뒤) | 12 (6+6), 실패 시 0/12 `FAIL` |
| **쌍둥이맘 모여라** | `⭐가족업체 자유게시판` 단일 | 쌀별86(xalageas), 왈라비00(walseibb), 팀버라22(timusataro), 킼키00(kilstyau), 바다그림자됴65(hushnane), 그래아95(granao), 헐래아89(huladoe) | 원글+수정글 쌍 | 08:10~13:30 | 1 | 12~13 (6+6, 가끔 +1 좋아요) |
| **고요한 아침** | 생활 쇼핑 정보 / 함께 사는 사람 / 마음 증상 / 몸 증상 / 반말일기 / 지갑을 열다 / 극복후기 / 거울 속 나 / 창 너머의 시간 / 오래된 벗, 오늘의 벗 | 두아이의엄마9gb(onerarain), 저스틴이버z6S(polbbo), 일투성(doansli), 구간천사swO(lerelabs), 쏴랑훼oZu(tedriing), 짬뽕맘uw5(somor4212), 아리아리송송(qbneb) | **카페 활성화용 단문 원글만** | 07:00~17:45, 1~10분 간격 연속 | 0 | 0 |
| **글로시 마이** | 스킨 톡 / 메이크 톡 / 다이어트·운동 톡 / 사랑·이별 톡 / 직장 톡 / 유부 톡 / 자유 톡 / 이벤트 / 신상 아카이브 / 운영자에게 문의 | 위와 동일 계정 풀(닉네임 접미사 다름) | 활성화용 단문 원글 | 07:00~17:45 | 0 | 0 |
| **송도포털** | 어린이집·유치원 / 무엇이든 자유 Q&A / 고민·위로 수다 / 미용실·네일·쇼핑 / 병원·소아과·조리원 / 가입 인사 & 닉네임 변경 / 피트니스·필테 / 인테리어·만들기 / 우리는 어디서 만났을까 / 10문 10답 자기소개 | 동일 계정 풀 | 활성화용 단문 원글 | 07:00~17:45 | 0 | 0 |
| **러브 인썸** | 🌸신혼 가전 후기 / 촬영·스냅 후기 / 헤어·메이크업 후기 / 웨딩홀 투어 후기 / 예물·예단·예복 / 준비 질문 있어요 / 🪻가입인사 / 🌸신혼 일기장 / 🌸신혼집 인테리어 / 💐인썸 수다방 / 🌸살림 노하우 / 커플 이야기 | 푸린푸크린(peecics), 로진무지개(cambrude), 일투성(doansli), 빨간머리(middoom), 두아이의엄마(onerarain), 쏴랑훼(tedriing), 짬뽕맘(somor4212), 아리아리송송(qbneb) + 운영자 러브인썸M(redsagua01) | 업체/지역 실명 들어간 **후기형 단문 원글** | 07:00~17:45 | 0 | 0 |
| **마이 웨딩 드림** | 🚩혜택·이벤트 / 신혼집 꾸미기 / 준비 Q&A / 헤어메이크업 리뷰 / 💍고민 나누기 / 💍톡톡 수다방 / 가전 사용기 / 뷰티&다이어트 / 예물·예단·예복 정보 / 맛집·데이트 코스 / 🚩공지 / 💍마웨드 수다방 | 러브 인썸과 동일 계정 풀 + 마웨드M(redsagua01) | 후기형 단문 원글 (🚩혜택·이벤트에는 건기식 홍보 혼입) | 07:00~17:45 | 0 | 0 |
| **웨딩 노트** | 부부 일상 / 살림 노하우 / 신혼 일기 / 허니문 다녀온 후기 / 견적 노트 / 뷰티·다이어트 / 함께 고민 / 헤어·메이크업 후기 / 웨딩홀 탐방 후기 | 호갱구조대(arc0108), 순둥WfW(dds6118), 로진무지개(cambrude) 3계정 **로테이션** | 활성화용 단문 원글 | 2026-09-03 05:47~06:50, **정확히 7분 간격** | 0 | 0 |
| **헬씨 트리** | 마음 충전소 / 맛있는 건강 식단 공유 / 외부 전문가 칼럼 모음 / 처음 오셨나요? / 새 회원 가입 인사 / 운영진의 건강 레터 / 나의 건강 변화 일지 | 웨딩 노트와 **동일 3계정** 로테이션 | 활성화용 단문 원글 | 2026-09-03 05:50~06:53, **7분 간격** | 0 | 0 |
| **태극마케팅센터** | `자유게시판` 단일 | 대량 계정 풀(닉네임 3자 축약형 xal/lke/sep/hul + 랜덤 ID 계정 다수) | 테스트/워밍업 더미 (제목=본문="김천kb보험 그라래NNN") | 03:36~04:55 | 0 | 0 |
| **소나무마케팅센터** | `자유게시판` 단일 | 랜덤 ID 계정 다수(zwqjffouqv38671 등) | 더미 ("김천kb보험 그리기NNN") | 09:24~16:12 | 0 | 0~1 |

> 말머리(말머리/head)는 UI·API 어디에도 없었음. 카페 구분은 `menu_name`(게시판)만으로 이뤄짐.

---

## 3) 브랜드별 관찰 (제품명 기반 추정)

| 브랜드 | 관찰 키워드 | 주 카페 | 게시판 | 전형 제목 |
|---|---|---|---|---|
| **팥순이** (다이어트/체지방) | 팥순추출물, 유기농레몬즙, 농촌진흥청 체지방감소 검증, 6주 -11.2kg, 정체기 | 씨씨앙 | 자유 수다방 | "유기농레몬즙먹다가이거추가했더니대박", "단기간다이어트 정체기 이렇게 뚫었네요", "바나바잎 효능 혈당 잡으려다 뱃살까지" |
| **코숨핏** (코/호흡/수면) | 코골이치료, 호흡기치료기, 양압기, 코세척기계, 수면 입막음 테이프, 비염 | 씨씨앙 | 자유 수다방 | "코골이치료 병원 가면 뭘 해주는 건가요ㅠㅠ", "코세척기계 써보신 분들 진짜 효과 있나요ㅠㅠ" |
| **자연방패 / 뉴더미스** (항문·피부 가려움) | 치질, 치루 초기증상, 외치핵, 항문곤지름, 똥꼬 가려움, 사타구니 가려움, 약국 치질연고, 치질방석, 항문 피 | 양평맘, 쌍둥이맘 | 이모저모 이야기💕 / ⭐가족업체 자유게시판 | "치루 초기증상 이거 맞나요ㅠㅠ", "약국 치질연고 아무거나 사면 안되요ㅠㅠ?" |
| **장으뜸** (장/혈당) | 신바이오틱스, 장건강, 토끼똥 원인, 바나바잎/혈당 | 양평맘, 마이 웨딩 드림(🚩혜택·이벤트) | | "장건강 챙기려고 산 신바이오틱스 특가", "토끼똥 원인이 뭘까요ㅠㅠ" |
| **우아덤** (여성 건강) | 엽산, 여성종합영양제, 산부인과, 난임검사, 임신준비, 테니스엘보치료 | 쌍둥이맘 | ⭐가족업체 자유게시판 | "엽산 추천 받고싶은데 뭐가 좋을까요ㅠㅠ", "울산 산부인과 추천 좀요 ㅠㅠ 난임검사…" |

브랜드 공통 제목 규칙
- **"~ㅠㅠ" / "~인가요" / "~아시는 분" 질문형**이 홍보 수정글의 기본형 (쌍둥이맘·양평맘·씨씨앙 코숨핏/자연방패/우아덤)
- 팥순이는 질문형 대신 **성과 자랑형/띄어쓰기 제거형** ("유기농레몬즙먹다가이거추가했더니대박")
- 쌍으로 붙는 **원글(미끼)** 제목은 브랜드와 무관한 순수 일상글 ("세탁소 맡긴 옷이 감쪽같이 사라졌습니다", "옷걸이 통일했더니 옷장이 달라 보입니다")

---

## 4) 본문 형식 관찰

### 4-A. 홍보형 수정글 (씨씨앙 / 양평맘 / 쌍둥이맘)
- 총 길이 **230~280자**
- 구조: **4~6개 문단(스탠자)**, 문단 사이 빈 줄 1개
- 한 문단 = **2~4줄**, 각 줄 **7~20자**(한 줄이 20자를 넘는 경우 거의 없음)
- 이미지: **0~1장**, 위치는 **1~2번째 문단 직후**(본문 30~40% 지점). 이미지 자리에는 빈 블록 2~3개가 함께 들어감
- 어투: 구어체 + `ㅠㅠ`, `ㅎㅎ`, `ㅋㅋ` 빈번, 마지막 문단은 **질문으로 마무리**("혹시 써보신 분 계시면…", "어떻게 관리하셨는지 알려주세요ㅠㅠ")
- 씨씨앙 팥순이 계열은 **공백 완전 제거**(`유기농레몬즙아침마다물에타서`) — 다른 카페/브랜드는 정상 띄어쓰기

예 (양평맘, 243자, 이미지 1):
```
T13 T19 / B / T13 T17 T5 / B B IMG B B / T17 T14 T13 / B / T17 T6 T15 T6 / B / T15 T9 T13 T9
```

예 (씨씨앙 팥순이, 257자, 이미지 0):
```
T14 T9 / B / T10 T12 T10 T15 / B / T8 T11 T15 / B / T11 T18 T7 / B / T12 T11 T16 / B / T11 T10 T13
```

### 4-B. 원글(미끼) — 홍보글과 쌍으로 발행
- **1문단 / 1~2줄 / 40~60자**, 이미지 0, 댓글 0
- 예: "점심 먹고 잠깐 걸었더니 머리가 좀 맑아지네요. 오후에도 가볍게 움직여보려고요." (44자)

### 4-C. 카페 활성화용 단문 (고요한 아침 / 글로시 마이 / 송도포털 / 러브 인썸 / 마이 웨딩 드림 / 웨딩 노트 / 헬씨 트리)
- 총 길이 **50~170자**, 문단 **1~4개**, 줄당 **18~56자**(홍보글보다 훨씬 김)
- 이미지 0, 댓글 0, 태그 없음
- 러브 인썸 / 마이 웨딩 드림은 **업체·지점·담당자 실명**이 들어간 후기체
  - "롯데본점이 새단장하면서 열린 그랜드오픈 행사에 맞춰 방문했어요 / 이순호 매니저님이…" (108자, 3문단)
  - "촬영 앞두고 지니킴에서 헤어변형 4시간을 이용했어요 …" (165자, 5문단)
- 고요한 아침/글로시 마이는 더 짧음 (51~83자, 2~3문단)

### 4-D. 더미 (태극 / 소나무)
- 제목과 동일한 문장 2줄 반복, 28자. 예: `김천kb보험 그라래759 \n\n 김천kb보험 그라래759`

### 4-E. 태그
- 상세 화면에 **태그 입력/표시 UI가 없었음**. 목록 API에도 태그 필드 없음 → 이번 조사에서 확인 불가

---

## 5) 댓글 타이밍 관찰

댓글 트리는 **깊이 2단계**(루트 댓글 → 답글). `v2rComment_<id>_<parentId>` 클래스로 부모를 식별.

전형 구성 = **루트 6개 + 작성자 답글 6개 = 12개** (목표 `target_comment_count = 12`).

| 사례 | 루트 간격 | 작성자 답글 오프셋 | 추가 스레드 |
|---|---|---|---|
| 씨씨앙(팥순이) `01M2PQRKWDKC…` | 16:52,16:53,16:54,16:55,16:56 → **1분** | 루트 +10분 정확히 (17:02~17:06) | 3자 답글 17:13 → 작성자 17:23 (+10분씩) |
| 씨씨앙(코숨핏) `01M2MJKA…` | 21:21~21:25 → **1분** | +10분 (21:31~21:35) | 한 스레드에 3자 21:42, 21:52 (+10분 체인) |
| 양평맘(자연방패) `01M2MR79…` | 14:42~14:46 → **1분** | +10분 (14:52~14:56) | 한 스레드 15:03(3자), 15:13(3자) |
| 쌍둥이맘 `01M2QQBXPK…` | 20:31,20:37,20:49,20:55,21:01 → **3~6분** | 루트 +3분 (20:34,20:40,20:52,20:58,21:04) | 20:43, 20:46 (+3분 체인) |

정리
- **루트 댓글: 1분 간격(씨씨앙·양평맘) 또는 3분 간격(쌍둥이맘)**
- **작성자 답글: 직전 루트 대비 +10분(씨씨앙·양평맘) / +3분(쌍둥이맘)**
- 3자 참여 스레드는 같은 간격으로 2~3단 체인
- 댓글 계정 풀은 **카페 간 공유**: 봄같은하루(chenallo), 여름냐옹(hunnede), 범수맘(chocobbn), 너는러브(quilliant), 다사요이/다사고시포(prtchht), 러블리데이지님(colpith) — 닉네임 접미사만 카페별로 다름
- 본문 발행 시각과 댓글 시각의 관계는 **일관되지 않음**(씨씨앙 +246분, 양평맘 −236분, 쌍둥이맘 −114분). 수정글 상세의 "발행" 시각은 수정 예약 시각이고 댓글은 원글 기준으로 보임 → 오프셋 기준을 **원글 발행 시각**으로 잡아야 함 (미검증)

---

## 6) 새 시스템 설정에 반영할 항목

### cafes.yaml
```yaml
cafes:
  ssiang:
    name: "국내1위 다이어트 커뮤니티 씨씨앙(식단,운동,후기,헬스,체험단)"
    cafe_id: 25016228
    cafe_url: cantsb
    boards: ["자유 수다방"]
    post_style: bait_pair          # 원글(미끼)+수정글(홍보)
    accounts: [croeues, zzoonylee, zzshop7876, aitigkq539, qlvjticcid11350, dplkubclxs75486]
    publish_window: "07:50-12:50"
    comments: { total: 12, roots: 6, root_interval_min: 1, reply_offset_min: 10, max_depth: 2 }
    body: { chars: [230, 280], stanzas: [4, 6], lines_per_stanza: [2, 4], line_chars: [7, 20], images: [0, 1], image_after_stanza: 1 }
    brands: [patsooni, kosumfit]

  yangmom:
    name: "양평 맘`s 전원 Story"
    cafe_id: 22788814
    cafe_url: yangmom
    boards: ["이모저모 이야기💕"]
    post_style: bait_pair
    accounts: [ueki97, ppo9011, choppaiy]
    publish_window: "07:45-09:40"
    comments: { total: 12, roots: 6, root_interval_min: 1, reply_offset_min: 10, max_depth: 2 }
    body: { chars: [230, 260], stanzas: [5, 6], images: [1, 1], image_after_stanza: 2 }
    brands: [jayeonbangpae, jangeutteum]

  ssangdungimom:
    name: "쌍둥이맘 모여라 - 쌍둥이 대표 카페"
    cafe_id: 10174516
    cafe_url: getamped2
    boards: ["⭐가족업체 자유게시판"]
    post_style: bait_pair
    accounts: [xalageas, walseibb, timusataro, kilstyau, hushnane, granao, huladoe]
    publish_window: "08:10-13:30"
    comments: { total: 12, roots: 6, root_interval_min: 3, reply_offset_min: 3, max_depth: 2 }
    body: { chars: [230, 250], stanzas: [5, 5], images: [1, 1], image_after_stanza: 2 }
    brands: [uahdeom, jayeonbangpae]

  goyohan_achim:
    name: "고요한 아침"
    post_style: activation          # 활성화 단문, 댓글 없음
    boards: ["생활 쇼핑 정보","함께 사는 사람","마음 증상","몸 증상","반말일기","지갑을 열다","극복후기","거울 속 나","창 너머의 시간 ","오래된 벗, 오늘의 벗"]
    accounts: [onerarain, polbbo, doansli, lerelabs, tedriing, somor4212, qbneb]
    publish_window: "07:00-17:45"
    post_interval_min: [1, 10]
    body: { chars: [50, 90], stanzas: [2, 3], line_chars: [20, 30], images: 0 }
    comments: { total: 0 }

  glossy_my:
    name: "글로시 마이"
    post_style: activation
    boards: ["스킨 톡","메이크 톡","다이어트 · 운동 톡","사랑 · 이별 톡","직장 톡","유부 톡","자유 톡","이벤트","신상 아카이브","운영자에게 문의"]
    accounts: [tedriing, peecics, lerelabs, middoom, somor4212, polbbo, doansli, cambrude]
    publish_window: "07:00-17:45"
    body: { chars: [50, 120], images: 0 }
    comments: { total: 0 }

  songdo_portal:
    name: "송도포털"
    post_style: activation
    boards: ["어린이집 · 유치원 ","무엇이든 자유 Q&A","고민 · 위로 수다","미용실 · 네일 · 쇼핑","병원 · 소아과 · 조리원","가입 인사 & 닉네임 변경","피트니스 · 필테 ","인테리어 · 만들기","우리는 어디서 만났을까","10문 10답 자기소개"]
    accounts: [doansli, somor4212, qbneb, polbbo, cambrude, lerelabs, peecics, tedriing]
    publish_window: "07:00-17:45"
    body: { chars: [50, 120], images: 0 }
    comments: { total: 0 }

  love_in_some:
    name: "러브 인썸 (Love in Some)"
    cafe_id: 26616683
    cafe_url: fik50kjkc
    post_style: review              # 업체명/지점/담당자 실명 후기
    boards: ["🌸신혼 가전 후기","촬영·스냅 후기","헤어·메이크업 후기","웨딩홀 투어 후기","예물·예단·예복","준비 질문 있어요","🪻가입인사","🌸신혼 일기장","🌸신혼집 인테리어","💐인썸 수다방","🌸살림 노하우","커플 이야기"]
    accounts: [peecics, cambrude, doansli, middoom, onerarain, tedriing, somor4212, qbneb]
    staff_account: redsagua01
    publish_window: "07:00-17:45"
    body: { chars: [100, 180], stanzas: [2, 5], line_chars: [18, 56], images: 0 }
    comments: { total: 0 }

  my_wedding_dream:
    name: "마이 웨딩 드림"
    cafe_id: 26680163
    cafe_url: fik505050
    post_style: review
    boards: ["🚩혜택·이벤트","신혼집 꾸미기","준비 Q&A","헤어메이크업 리뷰","💍고민 나누기","💍톡톡 수다방","가전 사용기","뷰티&다이어트","예물·예단·예복 정보","맛집·데이트 코스","🚩공지","💍마웨드 수다방"]
    accounts: [middoom, qbneb, tedriing, cambrude, doansli, peecics, polbbo, lerelabs]
    staff_account: redsagua01
    publish_window: "07:00-17:45"
    body: { chars: [100, 180], images: 0 }
    comments: { total: 0 }

  wedding_note:
    name: "웨딩 노트"
    post_style: activation
    boards: ["부부 일상","살림 노하우","신혼 일기","허니문 다녀온 후기","견적 노트","뷰티 · 다이어트","함께 고민","헤어 · 메이크업 후기","웨딩홀 탐방 후기"]
    accounts: [arc0108, dds6118, cambrude]     # 3계정 순환
    post_interval_min: 7
    body: { chars: [100, 140], stanzas: [3, 4], images: 0 }
    comments: { total: 0 }

  healthy_tree:
    name: "헬씨 트리"
    post_style: activation
    boards: ["마음 충전소 ","맛있는 건강 식단 공유","외부 전문가 칼럼 모음","처음 오셨나요?","새 회원 가입 인사 ","운영진의 건강 레터 ","나의 건강 변화 일지"]
    accounts: [dds6118, cambrude, arc0108]     # 3계정 순환
    post_interval_min: 7
    body: { chars: [60, 100], stanzas: [3, 3], images: 0 }
    comments: { total: 0 }

  taegeuk:
    name: "태극마케팅센터"
    cafe_id: 31670254
    cafe_url: kih8h8g
    post_style: dummy
    boards: ["자유게시판"]
  sonamu:
    name: "소나무마케팅센터"
    cafe_id: 31670256
    cafe_url: iuywe91
    post_style: dummy
    boards: ["자유게시판"]
```

### brands.yaml
```yaml
brands:
  patsooni:
    label: 팥순이
    category: 다이어트/체지방
    keywords: [팥순추출물, 유기농레몬즙, 농촌진흥청, 체지방감소, 정체기, 요요]
    cafes: [ssiang]
    title_style: boast_nospace      # 띄어쓰기 제거 + 성과 자랑형
    proof_points: ["농촌진흥청 체지방감소 검증", "6주 -11.2kg", "정부기관 실험 체지방 25% 감소"]
  kosumfit:
    label: 코숨핏
    category: 코/호흡/수면
    keywords: [코골이치료, 호흡기치료기, 양압기, 코세척기계, 수면 입막음 테이프, 비염, 무호흡]
    cafes: [ssiang]
    title_style: question_tt        # "~인가요ㅠㅠ" 질문형
  jayeonbangpae:
    label: 자연방패(뉴더미스)
    category: 항문/피부 가려움
    keywords: [치질, 치루 초기증상, 외치핵, 항문곤지름, 똥꼬 가려움, 사타구니 가려움, 치질연고, 치질방석]
    cafes: [yangmom, ssangdungimom]
    title_style: question_tt
  jangeutteum:
    label: 장으뜸
    category: 장건강/혈당
    keywords: [신바이오틱스, 장건강, 토끼똥, 변비, 바나바잎, 혈당]
    cafes: [yangmom, my_wedding_dream]
    title_style: question_tt
  uahdeom:
    label: 우아덤
    category: 여성건강/임신준비
    keywords: [엽산, 여성종합영양제, 산부인과, 난임검사, 임신준비]
    cafes: [ssangdungimom]
    title_style: question_tt
```

### 발행/작성 옵션 기본값 (상세 화면에서 확인한 값)
```yaml
write_options:
  공개설정: 멤버공개
  검색_네이버서비스_공개: true
  댓글_허용: true
  블로그_카페_공유_허용: true
  외부_공유_허용: true
  자동출처_사용: true
  CCL_사용: true
  CCL: { 저작자표시: 필수, 영리적이용: 허용안함, 콘텐츠변경: 허용안함 }
  댓글_AI: 미사용
  카페탭_검색노출검사: 미사용
reserve:
  발행형태: [단건발행, 반복발행]
  발행시간: [즉시, 예약]
search_exposure:
  checkpoints: [T0]           # 발행 ~5분 뒤 1차 검사, 결과 EXPOSED/미노출
```

---

## 7) 읽지 못한 것 / 확인 실패

1. **말머리(head)** — 목록 UI·목록 API·상세 화면 어디에도 말머리 필드가 없었음. V2R이 말머리를 다루지 않는 것인지, 다른 화면(글쓰기 폼)에만 있는지 미확인
2. **태그(tag)** — 상세 화면에 태그 표시/입력 UI 없음, API 응답에도 태그 필드 없음. `글쓰기` 화면은 열지 않음(쓰기 화면 진입은 생성 리스크가 있어 회피)
3. **cafe_id / cafe_url** — 고요한 아침, 글로시 마이, 웨딩 노트, 송도포털, 헬씨 트리 5개 카페는 이번 수집 샘플에 상세 JSON이 안 잡혀 미확보
4. **예약 대기(RESERVED) 상태 글** — 수집분은 전부 `DONE` 또는 `FAIL`. 예약 대기 중인 글의 표시 방식 미확인. `FAIL` 사례는 양평맘 2건(수정글, 댓글 0/12)
5. **이미지 원본** — 본문 이미지가 있는 글에서도 이미지는 1장뿐이었고, 씨씨앙 코숨핏 글은 이미지 자리(빈 블록 3개)만 있고 실제 `<img>`가 없었음. 이미지 업로드 방식/개수 규칙은 추가 확인 필요
6. **댓글 오프셋 기준 시각** — 수정글 상세의 "발행" 시각과 댓글 시각이 카페마다 ±4시간까지 어긋남. 원글 발행 시각 기준일 가능성이 높으나 원글-수정글 시각 대조는 미수행
7. **주간 / 월간 뷰** — 리스트 뷰만 조사, 열지 않음
8. **깊은 페이지** — 목록 API가 약 1,500행(≈100페이지) 이후 error를 반환해 전수 조사는 불가. 카페 필터 + 정렬로 우회함
9. **카페별 "10개"** — 씨씨앙/양평맘/쌍둥이맘 등은 원글+수정글이 쌍으로 세어져 실질 홍보 건수는 표기의 절반
