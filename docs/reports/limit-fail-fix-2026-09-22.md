# 게시글 등록 제한(ID/IP당) 처리 고치기 — 2026-09-22

작성 시각: **2026-09-22 09:40 (KST)** (`date` 실측)

> ⚠️ **적용 시점**: 09:00 일상 글 발행(작업 123)이 도는 중이라 **실행기는 재시작하지 않았습니다**.
> 코드만 고쳐 두었으니 **다음 재시작 때** 적용됩니다.

관련 파일

- [v2r/api/errors.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/api/errors.py)
- [v2r/api/articles.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/api/articles.py)
- [v2r/engine/reconcile.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/reconcile.py)
- [v2r/engine/publish.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/publish.py)
- [v2r/engine/worker.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py)
- [v2r/engine/dashboard.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/dashboard.py)
- [v2r/store/publications.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/publications.py)
- [v2r/store/republish.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/republish.py) (새 파일)
- [v2r/store/db.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/db.py)
- [tests/test_post_limit_20260922.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_post_limit_20260922.py) (새 파일)

---

## 1. 무슨 일이 있었나 (원인)

2026-09-21 18:36~18:38, `peecics` 계정이 그날 **151번째** 글을 올리려다 네이버의
"ID/IP당 게시글 등록 제한을 초과해 신규 게시글 등록이 잠시 제한됩니다"에 걸렸습니다.
(한 계정 하루 상한은 **약 150건**으로 보입니다.)

이때 우리 쪽에서 일어난 일을 순서대로 적으면 이렇습니다.

1. V2R에 글(source)은 만들어졌습니다 → 우리 DB는 `uncertain`(미확정)으로 둡니다.
2. 등록 확인(`wait_written`)이 실패 사유를 받아 오류를 던졌는데, 그 오류가
   **`other`(그 밖의 오류)**로 분류됐습니다. 그래서 장부에는
   `발행 실패(other)`라는 애매한 줄만 남고, 발행 기록은 계속 `uncertain`이었습니다.
3. 오늘 09:00~09:08 끊김 점검(reconcile)이 그 글들을 V2R에서 찾았고,
   **글 번호가 있다는 이유로 `done`(완료)으로 확정**해 버렸습니다.

즉 문제의 뿌리는 **"제한에 걸린 상태"를 알아보는 눈이 없었다**는 것입니다.
`other`로 뭉뚱그려지니 계정을 바꿔 다시 올리지도, 그 계정을 쉬게 하지도 못했습니다.

## 2. V2R API에서 "제한"을 구분하는 자리 (실측)

오늘 09:20~09:35에 실제 API를 조회해 확인했습니다.

| 어디서 | 경로 | 보는 칸 |
|---|---|---|
| 글 상세 | `GET /naver_cafe_articles/article?source_id=…` | `naver_cafe_article_history.status` / **`.fail_reason`**, `naver_cafe_article_destination.status` / **`.fail_reason`** |
| 글 목록(V2R 화면) | `POST /naver_cafe_articles/board_histories/search` | 행의 `status`, **`fail_reason`**, `reserved_comment_fail_reason` |

**핵심**: 상태값(`status`)만으로는 구분할 수 없습니다.

- 목록 상태 열의 **"준비"는 `RESERVED`**인데, 이 값은 **정상 예약 대기**에도 똑같이 쓰입니다.
- 화면의 **경고 아이콘 문구가 곧 `fail_reason`**입니다. 그래서 "제한이냐 아니냐"는
  **`fail_reason` 문구**로 판단해야 맞습니다.

그래서 이렇게 만들었습니다.

- `v2r/api/errors.py` → `POST_LIMIT_PHRASES`, `is_post_limit()`,
  그리고 오류 분류에 새 종류 **`post_limit`** 추가
- `v2r/api/articles.py` → `limit_reason_of(응답)` / `is_limited(응답)`
  (상세 응답이든 목록 행이든 같은 함수로 본다), `wait_written`이 제한을 만나면
  곧바로 `kind="post_limit"`로 던짐

## 3. 4건 정정 결과 — **정정하지 않았습니다 (중요)**

지시는 "4건을 failed로 정정하고 재발행 큐에 넣어라"였습니다만,
실제 API를 조회해 보니 **그 4건은 오늘 아침 V2R이 스스로 다시 올려 이미 성공**했습니다.
그래서 failed로 바꾸고 재발행하면 **같은 글이 두 번 올라갑니다**. 그래서 건드리지 않았습니다.

| 카페 | source_id | 지금 상태 | 네이버 글 번호 | 실제 등록 시각(KST) |
|---|---|---|---|---|
| 글로시 마이 | `01M336SSTK5SRXQH4AXMWJR0MZ` | DONE / SUCCESS | 858 | 09-22 09:03:50 |
| 송도포털 | `01M3371F3AR817SM45NX9D6787` | DONE / SUCCESS | 929 | 09-22 09:08:02 |
| 마이 웨딩 드림 | `01M336M4HBKRRFS16C531VJXV3` | DONE / SUCCESS | 1933 | 09-22 09:00:45 |
| 러브 인썸 | `01M336SATQ3PQEP51CC9QRKZRH` | DONE / SUCCESS | 2065 | 09-22 09:03:34 |

- 네 건 모두 `fail_reason`이 비어 있고, 검색 노출 점검도 `EXPOSED`(노출됨)입니다.
- V2R이 어제 실패한 글을 **자기 대기줄에 두었다가 오늘 아침 다시 등록**한 것으로 보입니다
  (등록 이력 `history`가 오늘 09:00~09:08에 새로 생겼습니다).
- 어제 본 경고 아이콘은 **그때의 상태**였고, 지금 목록을 다시 보시면 "준비"가 아니라
  완료로 바뀌어 있을 것입니다.
- 우리 DB의 `done` 확정은 **결과적으로 맞았습니다**. 다만 확정한 **근거가 틀렸습니다**
  (제한 상태를 볼 줄 몰랐을 뿐입니다). 그래서 로직은 그대로 고쳤습니다.

> 📌 **확인 부탁**: 그래도 4건을 실패로 돌리고 다시 올리길 원하시면 말씀해 주세요.
> 지금은 **중복을 막으려고 그대로 두었습니다.**
>
> ⚠️ 부수 효과 하나: 이 4건이 **오늘 09:00~09:08에 peecics로 올라갔기** 때문에,
> 오늘 peecics 몫에서 4건이 이미 나갔습니다(아래 상한 계산에 자동 반영됩니다).

## 4. 앞으로 어떻게 되나 (새 규칙)

### 4-1. 제한에 걸리면 완료로 확정하지 않는다

- 점검(reconcile)이 `fail_reason`에서 제한 문구를 보면 `done`이 아니라
  **`failed` + 사유 `제한`**으로 기록합니다.
- 동시에 **재발행 대기 줄**(`republish_queue` 표)에 **같은 시트 행**을 넣습니다.
  실제로 다시 올리는 일은 하지 않고 **대기만** 합니다(사람이 확인하고 돌립니다).
- 점검 결과에 `limited`(제한 건수)가 따로 나옵니다.

### 4-2. 계정별 하루 상한 100건

- `ACCOUNT_DAILY_LIMIT = 100` (실측 상한 약 150보다 넉넉히 낮게).
- 오늘 이미 100건을 채운 계정은 **그날 계정 후보에서 빠집니다**
  (하루 10개 묶음 `daily_self_accounts`와 일반 계정 배정 양쪽 모두).
- 발행 중에 "등록 제한" 응답이 오면 **그 자리에서** 그 계정을 오늘 하루 제외하고
  (`account_state`에 오늘 23:59:59까지 제한, 코드 `POST_LIMIT`),
  **같은 행을 다른 계정으로 1회 다시 시도**합니다(기존 재시도 장치를 그대로 씁니다).
- 그 한 번마저 실패하면 실패로 남고 재발행 대기 줄에 들어갑니다.

### 4-3. 현황판·일일 보고

- 현황판 오늘 요약에 **"제한 걸린 글"** 카드 추가.
- 현황판 카페별 표에 **"제한 걸린 글"** 열 추가.
- 점검 요약 줄이 `실패 N건(그중 제한 M건)`으로 나옵니다.
- 일일 보고(`docs/reports/self-daily-<날짜>.md`) 표에 **"제한 걸린 글"** 열 추가,
  머리말에 `그중 제한 걸린 글 N건 — 다른 계정으로 재발행 대기` 한 줄 추가.
- 제한 건은 **실패로 집계**합니다(따로 세되 성공에는 넣지 않습니다).

## 5. 재발행 대기 목록

현재 대기 줄: **0건**입니다.

- 위 4건은 이미 올라갔으므로 넣지 않았습니다(3절 참고).
- 다음 재시작 이후부터는 제한에 걸린 글이 자동으로 이 줄에 쌓입니다.
- 줄 확인: `republish_queue` 표 (`status = 'pending'`).

## 6. 시험

- 새 시험 16개: [tests/test_post_limit_20260922.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_post_limit_20260922.py)
  - 문구 알아보기 / 오류 분류(`post_limit`) / 연속등록 제한과 헷갈리지 않기
  - 상세 응답·목록 행에서 제한 찾아내기, "준비"만으로는 제한이 아님
  - 점검이 제한 건을 `done`으로 확정하지 않고 `failed`(제한) + 대기 줄에 넣기
  - `wait_written`이 제한을 `post_limit`으로 던지기
  - 하루 상한 100건 계산·계정 제외·하루 제외 처리
  - 현황판에 "제한 걸린 글"이 나오는지, 대기 줄 중복 방지
- 기존 시험 `test_engine.py`의 점검 결과 비교를 새 칸(`limited`)에 맞춰 고쳤습니다.
- 전체 결과: **1120개 통과, 실패 0개** (11분 20초).
  - 뒷정리 단계 오류 5개가 같이 떴는데, 이건 **지금 돌고 있는 09:00 발행 작업**이
    `data/llm_usage-2026-09.jsonl`(모델 사용량 장부)에 줄을 쌓고 있어서
    "시험이 진짜 장부를 건드렸나" 감시 장치가 오해한 것입니다.
    **이번 수정과는 무관**하며, 실행기가 쉬는 때 돌리면 나지 않습니다.
