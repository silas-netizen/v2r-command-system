# 라이브 댓글 순서·역할 기준 (제휴 수정글)

2026-09-19에 라이브를 **읽기만** 해서 모은 기록이다. 계정은 앞 3글자, `comment_id`는
뒤 6글자만 남겼다. 대상은 제휴 3개 카페의 **수정글**(`parent_source_id`가 있는 글).

- 씨씨앙 `cafe_id=25016228` / 양평맘 `22788814` / 쌍둥이맘 `10174516`
- 수정글은 `written_articles`에 직접 잡히지 않는다. 계정별 `written_articles`로
  **일상 글**(`child_source_id` 보유)을 찾고 그 `child_source_id`를 `get_article`한다.
- `naver_cafe_article_source_comments`는 **평탄한 배열**이고, 서버가 돌려주는
  배열 순서가 곧 **화면에서 읽히는 순서**다. 모든 노드의 `comments`는 빈 배열.

---

## 1. 서버 배열 순서 (참고 글 12건 전부 동일)

| idx | 라벨 | parent_comment_id | 작성 계정 | reply_member | 오프셋 |
|---|---|---|---|---|---|
| 0 | 댓글1 | None | 댓글풀 A1 | None | 기준+5 |
| 1 | 대댓글1 | 댓글1 | **작성자** | None | 기준+15 |
| 2 | 댓글2 | None | 댓글풀 A2 | None | 기준+6 |
| 3 | 대댓글2 | **댓글2** | **작성자** | None | 기준+16 |
| 4 | 대대댓글2 | **댓글2** | (아래 2-1/2-2) | 대댓글2 작성자 | 기준+26 |
| 5 | 대대대댓글2 | **댓글2** | (아래 2-1/2-2) | 대대댓글2 작성자 | 기준+36 |
| 6 | 댓글3 | None | 댓글풀 A3 | None | 기준+7 |
| 7 | 대댓글3 | 댓글3 | 작성자 | None | 기준+17 |
| 8 | 댓글4 | None | 댓글풀 A4 | None | 기준+8 |
| 9 | 대댓글4 | 댓글4 | 작성자 | None | 기준+18 |
| 10 | 댓글5 | None | 댓글풀 A5 | None | 기준+9 |
| 11 | 대댓글5 | 댓글5 | 작성자 | None | 기준+19 |

핵심:

- **정렬은 시간순이 아니라 "읽는 순서"다.** 댓글2 스레드(idx 2~5)가 댓글3(+7분)보다
  앞에 온다. 즉 `start_at`이 배열 안에서 단조 증가하지 **않는다**.
- 네이버 댓글은 **2단계까지만** 존재한다. 대대댓글2·대대대댓글2도
  `parent_comment_id`는 **루트(댓글2)** 를 가리키고, 더 깊은 계층은
  `reply_member`(= 내가 답하는 사람)로만 표현된다.
- `reply_member`는 **depth 2 이상에서만** 채워지고, 값은 **바로 위 노드의 작성 계정**이다.
- 오프셋 기준(0분)은 수정글의 `destination.start_at`이 아니라 **댓글1 시각 − 5분**으로
  보면 된다. 루트는 1분 간격, 작성자 답글은 해당 루트 +10분, 대대/대대대댓글2는
  다시 +10분씩이다.

## 2. 대대댓글2 / 대대대댓글2 의 작성 계정 (원고유형에 따라 갈린다)

### 2-1. 질문형 (관측 12건 중 10건)

작성자가 본문에서 묻고, 제품명은 **댓글 계정**이 처음 꺼낸다.

- 대댓글2 = 작성자 (되묻기) → 대대댓글2 = **댓글2를 쓴 바로 그 댓글풀 계정 A2**
  (`reply_member` = 작성자), 제품명 최초 언급
- 대대대댓글2 = **루트로 쓰이지 않은 여분 댓글풀 계정 A6**
  (`reply_member` = A2), "222 저도 효과 봤어요" 여론

### 2-2. 후기형 (관측 12건 중 2건, 씨씨앙 팥순이 후기형)

작성자가 이미 써본 사람이라 **작성자가** 대댓글2에서 제품명을 꺼낸다.

- 대대댓글2 = **여분 댓글풀 계정 A6** (`reply_member` = 작성자), "저도 이거 먹는중"
- 대대대댓글2 = **작성자** (`reply_member` = A6), 마무리 맞장구

두 유형 모두 **루트 5개는 서로 다른 댓글풀 계정**이고, 6개짜리 공용 풀
(chenallo / hunnede / chocobbn / quilliant / prtchht / colpith) 중 **루트에 안 쓰인
1개**가 A6로 남는다.

---

## 3. 마스킹 덤프 (카페별 대표 1건씩)

### 3-1. 씨씨앙 · 후기형 · `01M1XBAH9R4BAY58ZQ0AVRZWGA` (작성자 `lgh…`, 수정글 예약 13:59)

```
idx cid    parent login start  reply_member  라벨
 0  WEJA9W -      col   14:04  -             댓글1
 1  VGF6ZS WEJA9W lgh   14:14  -             대댓글1(작성자)
 2  CE8Y3K -      prt   14:05  -             댓글2
 3  37R218 CE8Y3K lgh   14:15  -             대댓글2(작성자, 제품명 언급)
 4  6GEJSP CE8Y3K cho   14:25  lgh           대대댓글2(여분 댓글계정)
 5  ATBZ6Q CE8Y3K lgh   14:35  cho           대대대댓글2(작성자)
 6  MG19A6 -      qui   14:06  -             댓글3
 7  JZGX8A MG19A6 lgh   14:16  -             대댓글3
 8  X5FPTT -      hun   14:07  -             댓글4
 9  0MEG9J X5FPTT lgh   14:17  -             대댓글4
10  GJFY27 -      che   14:08  -             댓글5
11  VEGQ6T GJFY27 lgh   14:18  -             대댓글5
```

### 3-2. 양평맘 · 질문형 · `01M1GPJ3W7A6KS6T387YGS3RJ9` (작성자 `tab…`, 수정글 예약 06:54)

```
 0  V8R664 -      che   06:59  -    댓글1
 1  GJGFBS V8R664 tab   07:09  -    대댓글1(작성자)
 2  R6F0JF -      col   07:00  -    댓글2
 3  KHZRDQ R6F0JF tab   07:10  -    대댓글2(작성자, 되묻기)
 4  ZJDNBW R6F0JF col   07:20  tab  대대댓글2(= 댓글2 계정, 제품명 최초 언급)
 5  9V294A R6F0JF qui   07:30  col  대대대댓글2(여분 댓글계정, "222 저도")
 6  H5MTRS -      prt   07:01  -    댓글3
 7  9GWF4N H5MTRS tab   07:11  -    대댓글3
 8  8D1GC6 -      hun   07:02  -    댓글4
 9  YFB0GR 8D1GC6 tab   07:12  -    대댓글4
10  5FVFJK -      cho   07:03  -    댓글5
11  HRA628 5FVFJK tab   07:13  -    대댓글5
```

### 3-3. 쌍둥이맘 · 질문형 · `01M2MQT74GE0ZR0H9KXVV7QWPF` (작성자 `wal…`, 수정글 예약 07:30)

```
 0  0NKJN2 -      che   07:35  -    댓글1
 1  GHW1MJ 0NKJN2 wal   07:45  -    대댓글1(작성자)
 2  X33PC4 -      hun   07:36  -    댓글2
 3  TK4MPC X33PC4 wal   07:46  -    대댓글2(작성자)
 4  65EGA1 X33PC4 hun   07:56  wal  대대댓글2(= 댓글2 계정)
 5  PG796B X33PC4 col   08:06  hun  대대대댓글2(여분 계정)
 6  4TA7XE -      cho   07:37  -    댓글3
 7  17FV0Y 4TA7XE wal   07:47  -    대댓글3
 8  RY7GDC -      qui   07:38  -    댓글4
 9  9FZESF RY7GDC wal   07:48  -    대댓글4
10  EFR1AN -      prt   07:39  -    댓글5
11  FXM96A EFR1AN wal   07:49  -    대댓글5
```

관측 12건 요약(작성자 / 루트 계정 / idx4 / idx5):

| 카페 | 작성자 | 루트 5개 | idx4(대대댓글2) | idx5(대대대댓글2) | 유형 |
|---|---|---|---|---|---|
| 씨씨앙 | lgh | col prt qui hun che | cho (rm=lgh) | lgh (rm=cho) | 후기형 |
| 씨씨앙 | oll | col hun prt qui che | hun (rm=oll) | cho (rm=hun) | 질문형 |
| 씨씨앙 | azq | hun cho qui col che | prt (rm=azq) | azq (rm=prt) | 후기형 |
| 씨씨앙 | gra | cho prt col qui che | prt (rm=gra) | hun (rm=prt) | 질문형 |
| 양평맘 | tab | che col prt hun cho | col (rm=tab) | qui (rm=col) | 질문형 |
| 양평맘 | kkp | cho che hun qui col | che (rm=kkp) | prt (rm=che) | 질문형 |
| 양평맘 | urf | hun qui che cho prt | qui (rm=urf) | col (rm=qui) | 질문형 |
| 양평맘 | gra | che prt hun col qui | prt (rm=gra) | cho (rm=prt) | 질문형 |
| 쌍둥이맘 | wal | che hun cho qui prt | hun (rm=wal) | col (rm=hun) | 질문형 |
| 쌍둥이맘 | cha | che col cho qui prt | col (rm=cha) | hun (rm=col) | 질문형 |
| 쌍둥이맘 | lve | hun prt qui col cho | prt (rm=lve) | che (rm=prt) | 질문형 |
| 쌍둥이맘 | tim | hun qui cho col prt | qui (rm=tim) | che (rm=qui) | 질문형 |

---

## 4. 우리가 오늘 만든 글 3건 (수리 전)

| source_id | 카페 | 작성자 | idx4 (대대댓글2) | idx5 (대대대댓글2) |
|---|---|---|---|---|
| `01M2TT7K8ECC6T7WV856PRVJGY` | 씨씨앙 | mol | **mol (작성자!)** rm=mol | **mol (작성자!)** rm=mol |
| `01M2TTV44G9WK334DCPSGM32DH` | 양평맘 | mol | **mol** rm=mol | **mol** rm=mol |
| `01M2TTVTW6SDKFYZKR1BM43MM1` | 쌍둥이맘 | azq | **azq** rm=azq | **azq** rm=azq |

배열 순서·`parent_comment_id`·시각 오프셋은 기준과 같았다. 어긋난 것은
**작성 계정 역할**이다. 댓글2 스레드 4개 노드(대댓글2·대대댓글2·대대대댓글2)가
전부 본문 작성자 한 계정이라, 작성자가 혼자 묻고 스스로 제품명을 대답하고
스스로 "222 저도 효과 봤어요"를 다는 모양이 됐다. `reply_member`도 전부
자기 자신(작성자)을 가리켰다.

→ 수정: `v2r/content/comments.py::assign_comment_accounts`가 `manuscript_type`을
받아 질문형/후기형별로 대대댓글2·대대대댓글2 계정을 배정하고, 여분 댓글풀 계정을
한 개 남겨둔다. 순서는 `DEFAULT_TREE`를 읽는 순서로 고정한다.
