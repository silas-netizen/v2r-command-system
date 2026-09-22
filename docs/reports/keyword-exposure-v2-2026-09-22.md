# 키워드 노출 현황 — 우리 글 URL 원천 보강 (2026-09-22 2차)

작성 2026-09-22 (`date` 실측) · 대상 파일 [v2r/knowledge/keyword_exposure.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_exposure.py)

## 1. 문제

앞 일꾼이 만든 `keyword_exposure.py`는 "우리 글 URL"을 브랜드 시트 `노출 현황` 탭에서만
가져왔는데, 그 탭엔 실제로 URL이 없어(뒤에서 보듯 있는 칸도 상태 문구가 섞여 있음)
전부 `unpublished`로 나왔다. 이번 작업은 URL 원천을 세 갈래로 늘리고, 검사 대상 키워드
범위·상한·현황판 연동·실제 1회 실행까지 마쳤다.

## 2. 무엇을 바꿨나

### 2-1. 우리 글 URL 세 갈래 원천 (`target_keywords`)

1. **(a) 시트에 이미 있으면 그대로.** 단, 실측해 보니 일부 브랜드 시트의 `url` 칸에는
   실제 URL이 아니라 `노출완`/`밀려남` 같은 **상태 문구**가 들어 있었다
   (`data/brand_sheet_뉴더미스.xlsx` 328행 확인). `http(s)://`로 시작하지 않으면
   버리도록 방어를 넣었다.
2. **(b) `article_index`에서 카페+제목으로 찾기.** 같은 카페에서 제목에 그 키워드가
   들어간 `article_index` 행(`ArticleIndexStore.find_by_keyword`, 새로 추가)을 찾아,
   `cafe_id`+`article_id`가 있으면
   `https://cafe.naver.com/ca-fe/cafes/<카페번호>/articles/<글번호>`를 만든다
   (`_naver_article_url`). 이 형태는 카페 별칭(alias)을 몰라도 카페 번호·글 번호만으로
   만들 수 있는 표준 형태다(참고: `docs/reports/limit-fail-fix-2026-09-22.md`).
3. **(c) 그래도 없으면** `article_index`에 제목은 있는데 글 번호가 비어 있는 경우
   `candidate_title_norm`(정규화 제목)만 들고 가서, 검사 시점에 검색 결과 카페 이름+
   제목 일치로 다시 찾는다(`parse_cafe_search_title_rank`, 새로 추가).
4. **어떤 원천도 없으면** `unpublished`(그대로 유지, 네트워크 호출도 하지 않는다 —
   불필요한 네이버 요청을 줄이는 기존 설계를 지켰다).

### 2-2. 검사 대상 키워드 범위·상한

- `target_keywords`가 시트 키워드 ∪ **우리가 발행한 브랜드 글 키워드**를 합친다.
  후자는 시트에 등장하는 카페들의 `article_index` 글 제목에서 **맨 앞 키워드 토큰**
  (`_title_lead_keyword`)을 뽑아, 시트에 없는 것만 추가한다.
- `run_check`가 하루 브랜드당 **최대 60개**(`DEFAULT_DAILY_CAP`)로 자른다. 넘치면
  `_prioritize`가 "밀려남"이었던 키워드·우리 글이 확인된 키워드를 앞으로 보낸 뒤
  자른다(안정 정렬이라 나머지는 원래 순서 유지).
- 요청 간격은 기존 그대로 3~6초(`MIN_DELAY_SEC`~`MAX_DELAY_SEC`) 유지.

### 2-3. 매칭 로직 버그 하나 발견·수정

`_naver_article_url`로 만든 `ca-fe/cafes/<번호>/articles/<번호>` 꼴 URL을 검색 결과의
별칭(alias) 꼴 링크(`cafe.naver.com/parisienlook/123`)와 견줄 때, 기존 `_article_id()`
정규식이 새 URL 형태에서 글 번호를 못 뽑아 **글 번호 매칭이 조용히 실패**하는 문제를
실제 실행 중 발견했다. `_article_id()`가 두 형태(별칭 꼴 / `ca-fe/cafes/.../articles/...`
꼴) 모두에서 글 번호를 뽑도록 고쳤다(테스트로 고정).

### 2-4. 현황판 연동

[v2r/engine/dashboard.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/dashboard.py)
일일·중간·현황판 보고 모두에 **"브랜드 키워드 노출"** 섹션을 추가했다(HTML 6번,
MD 5번 — 기존 "오늘 진행·결정 대기"는 한 칸씩 밀림). `keyword_exposure.summary(rt)`
결과만 쓰고 `keyword_exposure.py`는 고치지 않는다는 설계(§5)를 지켰다. 브랜드별
노출/밀려남/미발행/미확인 수 + 새로 밀려난 키워드 목록을 보여준다. 손으로 만든
`docs/reports/dashboard-2026-09-21.html/.md`는 건드리지 않았다.

## 3. 실제 1회 실행 (2026-09-22 12:28 KST)

### 3-1. 연결·파싱 확인 (실제 네이버 검색)

브랜드 시트 5개(`우아덤·뉴더미스·장으뜸·코숨핏·팥순이`)를 실측해 보니, `노출 현황` 탭의
카페는 전부 **제휴 카페**(씨씨앙·양평맘·쌍둥이맘 모여라·러브인썸·마이웨딩드림)였고,
`article_index`(자사 카페 글 목록 동기화)는 **자사(self_owned) 카페만** 갖고 있다
(`고요한 아침·글로시 마이·송도포털·러브인썸·마이웨딩드림` — 5,444행, 2026-09-22 기준).
두 집합이 겹치는 카페가 거의 없어(러브인썸·마이웨딩드림만 양쪽에 있으나 그 브랜드
글에는 해당 키워드 매칭이 없었음), **지금 데이터로는 (a)/(b) 원천이 실제로 찾아내는
브랜드 글이 0건**이었다(`뉴더미스` 374개 키워드 전부 확인, `candidate` 0건).

그래서 파이프라인이 실제로 네이버에 붙어 정상 동작하는지는 별도로 확인했다:

```
fetch_cafe_search_html("다이어트", cookies=data/naver_cookies.json 쿠키 15개)
→ HTML 1,507,870자, 카페 글 링크 102개 파싱됨(차단 아님)
```

캡차·차단 문구는 없었고(`_looks_blocked` 오탐 가능성 확인 — 실제 코드 경로는
`links`가 비었을 때만 그 함수를 쓰므로 이번 결과엔 영향 없음), 실제 네이버 검색
카페탭에서 글 링크를 정상적으로 받아 파싱했다.

### 3-2. `뉴더미스` 실제 10개 키워드 검사 (실측 DB에 저장됨)

우리 발행 글과 연결되는 키워드가 없어(위 3-1) 전부 `unpublished`로 판정됐고, 설계대로
URL/후보가 없는 키워드는 **네트워크 호출을 하지 않았다**(불필요한 네이버 요청 방지).

| 카페 | 키워드 | 상태 | 순위 | 검사시각(KST) |
|---|---|---|---|---|
| 양평맘 | 변비에 좋은 음식 | unpublished | - | 2026-09-22T12:28:31+09:00 |
| 양평맘 | 치질 | unpublished | - | 2026-09-22T12:28:31+09:00 |
| 양평맘 | 엉덩이 종기 | unpublished | - | 2026-09-22T12:28:31+09:00 |
| 양평맘 | 치핵 | unpublished | - | 2026-09-22T12:28:31+09:00 |
| (빈칸) | 항문 가려움 | unpublished | - | 2026-09-22T12:28:31+09:00 |
| 양평맘 | 치질증세 | unpublished | - | 2026-09-22T12:28:31+09:00 |
| (빈칸) | 혈변 원인 | unpublished | - | 2026-09-22T12:28:31+09:00 |
| (빈칸) | 치루 | unpublished | - | 2026-09-22T12:28:31+09:00 |
| 양평맘 | 혈변 | unpublished | - | 2026-09-22T12:28:31+09:00 |
| 양평맘 | 엉덩이 뾰루지 | unpublished | - | 2026-09-22T12:28:31+09:00 |

`data/v2r.sqlite`의 `keyword_exposure` 표에 실제로 쌓였다(조회로 확인).

### 3-3. 정직한 한계

- 지금 구조상 (a)/(b) 원천은 **자사 카페 동기화 목록**(`article_index`)에만
  기대는데, 브랜드 키워드 캠페인은 **제휴 카페**에 올라간다. 두 목록이 지금은
  거의 겹치지 않아 실전 효과가 제한적이다. 제휴 카페 글도 `article_index`에 함께
  동기화되게 하거나(`sync_all_self_cafes`류 작업을 제휴 카페까지 넓히기), 발행
  시점에 (source_key, row_number) ↔ 키워드를 남기는 열을 `publications`에 추가하는
  쪽이 근본 해법이다 — 이번 작업 범위(§ "publish.py 수정 금지") 밖이라 손대지
  않았다.
- `_naver_article_url`이 만드는 `ca-fe/cafes/<번호>/articles/<번호>` 링크 형식 자체가
  실제로 브라우저에서 열리는지는 이번에 브라우저로 직접 확인하지 않았다(로그인
  우회 금지 규칙상 브라우저 자동화를 쓰지 않았음). 글 번호 매칭(§2-3)은 검색 결과
  HTML 파싱 단계에서만 쓰이므로 이 URL을 사람이 열어 보는 용도로는 별도 검증이
  필요하다.

## 4. 테스트

`tests/test_keyword_exposure.py`(26개, +10) · `tests/test_article_index.py`(+3) ·
`tests/test_dashboard.py`(+1). 전체 `pytest` 통과(사용량 장부 파수꾼은 사유상 무관 —
`tests/conftest.py`의 감시는 실제 사용량 파일 변경 여부만 본다).

## 5. 바뀐 파일

- [v2r/knowledge/keyword_exposure.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_exposure.py)
- [v2r/store/article_index.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/article_index.py)
- [v2r/engine/dashboard.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/dashboard.py)
- [tests/test_keyword_exposure.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_keyword_exposure.py)
- [tests/test_article_index.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_article_index.py)
- [tests/test_dashboard.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_dashboard.py)
