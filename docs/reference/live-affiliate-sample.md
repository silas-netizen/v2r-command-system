# 라이브 제휴 체인 샘플 (팥순이 / 씨씨앙, 잡 40·41)

2026-09-19 실행한 `publish_brand`(브랜드 팥순이, 카페 씨씨앙) 두 건을 라이브에서
**읽기만** 해서 정리한 기록이다. 값은 필요한 만큼만 남기고 본문·계정은 줄였다.

## 1. DB 상태 (수리 전)

`publications` (source_key = `팥순이`)

| row | status | stage | source_id | url | account | cafe | menu_id | scheduled_at |
|---|---|---|---|---|---|---|---|---|
| 2 | uncertain | `revision_submitting` | `01M2TT5DQ56NP8SSJ64AHBDV84` (= 일상 글/부모) | (없음) | molitan | 씨씨앙 | 328 | 2026-09-19T09:13:00+09:00 |
| 3 | uncertain | `revision_created` | `01M2TT7K8ECC6T7WV856PRVJGY` (= 수정글/본 글) | (없음) | molitan | 씨씨앙 | 328 | 2026-09-19T09:12:00+09:00 |

`jobs` / `events`

| job | status | 결론 |
|---|---|---|
| 40 | failed | `단호박 샐러드 다이어트 식단으로 괜찮나요: 발행 실패: 사진 수가 부족합니다: 자리 1, 사진 0` / 미확정 `팥순이#2` |
| 41 | failed | `공복혈당수치 다이어트랑 관련있나요: 등록 검증 실패: 댓글 개수 불일치: 12 != 5` / 미확정 `팥순이#3`, 건너뜀 `팥순이#2 (이미 발행됨)` |

- 잡 40: 일상 글은 등록됐고, **수정글은 `create_article` 이전**(`seone.content_json`의
  자리표시자 검사)에서 끊겼다. 그런데도 행이 `uncertain`으로 남아 잡 41이
  `이미 발행됨`으로 건너뛰었다 → 등록 전 실패는 `failed`여야 한다.
- 잡 41: 글·댓글은 전부 서버에 올라갔는데 검증 쪽 계산이 틀려 실패로 처리됐다.

## 2. 글 3건의 실제 모양

`get_article` 응답의 최상위 키는 세 건 모두 동일하다:

```
naver_cafe_article_destination
naver_cafe_article_history
naver_cafe_article_likes
naver_cafe_article_source
naver_cafe_article_source_comments
naver_cafe_article_source_detail
```

`naver_cafe_article_source` 키: `source_id, parent_source_id, child_source_id,
title, tag_list, is_deleted`.

| source_id | 역할 | title | parent_source_id | child_source_id | dest.status | dest.start_at | dest.parent_id | 댓글 |
|---|---|---|---|---|---|---|---|---|
| `01M2TT5DQ56NP8SSJ64AHBDV84` | 잡 40 일상 글 | 동남아 신혼여행 견적 여쭤봐요 | None | **None** | RESERVED | 2026-09-19T00:13:00Z | None | 0 |
| `01M2TT7F4KEGX3K9VPF1G6VQF9` | 잡 41 일상 글 | 부평벨라루체 본식메이크업 후기 남겨요 | None | `01M2TT7K8ECC6T7WV856PRVJGY` | RESERVED | 2026-09-19T00:12:00Z | None | 0 |
| `01M2TT7K8ECC6T7WV856PRVJGY` | 잡 41 수정글 | 공복혈당수치 다이어트랑 관련있나요 | `01M2TT7F4KEGX3K9VPF1G6VQF9` | None | RESERVED | 2026-09-19T04:12:00Z | None | 12 |

정리:

- **일상 글 ↔ 수정글은 `parent_source_id`/`child_source_id` 양방향으로 이어진다.**
  잡 41은 두 글이 모두 있고 제대로 연결돼 있다. 잡 40은 일상 글만 있고
  `child_source_id`가 `None`이다(수정글이 아예 안 만들어졌다).
- `naver_cafe_article_destination.parent_id`는 **`None`**이다(말머리/부모 게시판
  개념이지 제휴 부모 글이 아니다). 제휴 연결은 `parent_source_id`만 쓴다.
- `naver_cafe_article_destination.start_at`은 UTC(`...Z`). 수정글은 일상 글보다
  4시간 뒤(00:12Z → 04:12Z)로 예약돼 있다.
- `naver_cafe_article_destination.target_comment_count`는 **12**(= 전체 댓글 수).
- **`naver_cafe_article_history`는 세 건 모두 `None`**이다. `reconcile._status_of`가
  `walk_dicts` 폴백으로 `destination.status`(= `RESERVED`)를 집는다. 예약 건이므로
  `_verdict(RESERVED, scheduled=True) → done`.

## 3. 댓글 모양 (핵심)

`naver_cafe_article_source_comments`는 **평탄한 배열**이다.

- 길이 **12** (루트 5 + 답글 7). 중첩은 없다 — 모든 노드가 depth 0에 있다.
- 각 노드에 `comments` 키가 있지만 **항상 빈 배열**이다.
- 답글은 `parent_comment_id`가 루트의 `comment_id`를 가리킨다.

루트별 답글 수: 5 / 2 / 1 / 1 / 1 → 루트 5개, 답글 7개, 합계 12.

댓글 1건의 필드:

```
cafe_id, comment_id, comments, contents, created_at, error, interval_seconds,
modified_at, naver_login_id, naver_parent_comment_page, parent_comment_id,
photo_url, repeat_count, repeat_finish_reason, repeat_written_comment_ids,
reply_member, schedule_arn, schedule_name, start_at, status, sticker_id,
sticker_is_animation, written_at, written_comment_id, written_ref_comment_id
```

예시(내용 축약, 계정만 표기):

| depth 표기 | comment_id | parent_comment_id | naver_login_id | start_at | status | reply_member |
|---|---|---|---|---|---|---|
| 루트1 | `…K8Q09…` | None | chenallo | 2026-09-19T04:17:00Z | RESERVED | None |
| └답글 | `…K9N9C…` | `…K8Q09…` | molitan | 04:27:00Z | RESERVED | None |
| 루트2 | `…KAGKE…` | None | hunnede | 04:18:00Z | RESERVED | None |
| └답글 | `…KB0PP…` | `…KAGKE…` | molitan | 04:28:00Z | RESERVED | None |
| └답글 | `…KBKKT…` | `…KAGKE…` | molitan | 04:38:00Z | RESERVED | 있음(molitan) |
| └답글 | `…KC1DZ…` | `…KAGKE…` | molitan | 04:48:00Z | RESERVED | 있음(molitan) |
| 루트3 | `…KCHPB…` | None | quilliant | 04:19:00Z | RESERVED | None |
| └답글 | `…KD7QN…` | `…KCHPB…` | molitan | 04:29:00Z | RESERVED | None |
| 루트4 | `…KDNKS…` | None | prtchht | 04:20:00Z | RESERVED | None |
| └답글 | `…KE41H…` | `…KDNKS…` | molitan | 04:30:00Z | RESERVED | None |
| 루트5 | `…KEGQY…` | None | chocobbn | 04:21:00Z | RESERVED | None |
| └답글 | `…KEXP3…` | `…KEGQY…` | molitan | 04:31:00Z | RESERVED | None |

`reply_member`는 2단계 이상 답글에서만 채워진다(`member_key`·`naver_login_id`·`nick`).

### `12 != 5`가 난 이유

- **요청 페이로드**(`content/comments.py::to_api_payload`)는 답글을 루트의
  `comments`에 **중첩**해 보낸다 → 최상위 길이 **5**.
- **응답**은 답글까지 **평탄**하게 준다 → 길이 **12**.
- `verify_article`은 응답을 `len()`으로 세고(12), 기대값으로는 페이로드
  `len()`(5)을 받았다 → 항상 불일치.

→ 양쪽 모두 **전체 노드 수**로 세면 12 == 12로 맞는다.
(`v2r/api/articles.py::count_comment_nodes`)
