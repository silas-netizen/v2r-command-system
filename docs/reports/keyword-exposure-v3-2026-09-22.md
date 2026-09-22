# 키워드 노출 검사가 제휴 카페 글도 찾게 (2026-09-22 3차)

작성 2026-09-22 (`date` 실측) · 대상 파일
[v2r/engine/article_sync.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/article_sync.py) ·
[v2r/engine/publish.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/publish.py) ·
[v2r/engine/worker.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py) ·
[config/schedule.yaml](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/config/schedule.yaml) ·
[tests/test_article_sync_scope.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_article_sync_scope.py)

## 1. 문제

브랜드 원고는 **제휴 카페**(씨씨앙·양평맘·쌍둥이맘 모여라 — `config/cafes.yaml`의
`affiliate` 항목)에 올라간다. 그런데 `article_index` 동기화(`sync_all_self_cafes`)는
**자사 카페만** 돌아서, 키워드 노출 검사(`keyword_exposure.py`의 URL (b) 경로 —
"우리 글을 카페+제목으로 찾기")가 브랜드 글이 실제로 올라간 제휴 카페 행을 애초에
찾을 수가 없었다. 색인에 없으면 늘 `unpublished`로만 나온다.

## 2. 무엇을 바꿨나

### 2-1. `sync_all_cafes(rt, scope)` 추가

[`v2r/engine/article_sync.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/article_sync.py)에
`sync_all_self_cafes`는 그대로 두고(호출하는 곳이 있을 수 있어 하위 호환 유지),
새 `sync_all_cafes(rt, scope="self"|"affiliate"|"all")`를 추가했다. `scope`에 따라
자사(`self_cafe_names`)·제휴(새 `publish.affiliate_cafe_names`)·둘 다를 골라 같은
방식(카페별 가입 계정 전부 → `written_articles` → `article_index.upsert`)으로 돈다.

`publish.py`에 `affiliate_cafe_names(rt)`를 새로 추가했다(`self_cafe_names`와 같은
모양, `cafes.yaml: affiliate` 이름 목록).

### 2-2. 새 명령 / 예약

`parser.py`의 기존 정규식(`글\s*목록.*(동기화|색인|갱신)`)이 이미 "자사 카페 글 목록
동기화"·"제휴 카페 글 목록 동기화"·"전체 글 목록 동기화"를 모두 `sync_article_index`
작업으로 잡는다(문구 안 단어는 원래도 정규식이 신경 안 씀). 실제 scope 구분은
`worker.dispatch`에서 원문(`spec.notes`)에 "전체"가 있으면 `all`, "제휴"가 있으면
`affiliate`, 그 외(기존 문구 "글 목록 동기화" / "자사 카페 글 목록 동기화" 포함)는
`self`로 갈라 `sync_all_cafes`를 부르도록 고쳤다. 기존 문구·기존 동작은 그대로다.

`config/schedule.yaml`의 07:30 항목은 원래 계획대로 "전체"로 바꾸려 했으나, 아래
2-3 실측대로 제휴만 돌려도 계정 215개·115초가 걸려 자사(계정이 더 많고 행도
5,000건 넘게 쌓여 있음)와 합치면 부하가 커 보여 **분리**했다:

- `07:30` 글 목록 색인 동기화(자사) — `자사 카페 글 목록 동기화` (기존과 동일)
- `07:45` 글 목록 색인 동기화(제휴) — `제휴 카페 글 목록 동기화` (신규)

### 2-3. 제휴 카페 색인 동기화 실제 1회 실행 (읽기 API만)

`sync_all_cafes(rt, "affiliate")`를 직접 실행했다(발행은 하지 않음, `written_articles`
조회만).

| 카페 | 계정 수 | 새로 쌓은 행 |
|---|---|---|
| 씨씨앙 | 97 | 491 |
| 양평맘 | 87 | 242 |
| 쌍둥이맘 모여라 | 31 | 181 |
| **합계** | **215** | **914** |

- 오류(`errors`) 0건. 경고(`warnings`, 계정 재시도 후에도 실패) 16건 — 특정 계정
  몇 개가 일시적으로 실패했지만 카페별로 계정이 여러 개라 전체 동기화는 `ok: true`.
- 걸린 시간 약 115초. 색인 전체 건수(자사+제휴) 6,376건으로 늘었다.
- 차단·캡차 신호(로그인 실패 폭증, HTTP 403/429 연속 등)는 없었다. 정상 진행.

### 2-4. `find_by_keyword`가 제휴 카페 행도 보는지 확인

[`v2r/store/article_index.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/article_index.py)의
`find_by_keyword`는 `cafe` 조건이 **선택**이고 카페 종류(자사/제휴) 구분 열이 아예
없다 — `article_index` 테이블 전체를 `title_norm LIKE`로 훑는다. 그래서 코드는
고칠 필요가 없었다(지시대로 brand/keyword 열도 추가하지 않았다). 2-3에서 넣은 914건이
바로 이 테이블에 들어갔으니, 이제 씨씨앙·양평맘·쌍둥이맘 카페의 브랜드 글도 검사
대상이 된다. 색인 현재 카페별 건수:

```
러브 인썸(자사)        1,614
마이 웨딩 드림(자사)    1,539
고요한 아침(자사)         996
송도포털(자사)            693
글로시 마이(자사)          648
씨씨앙(제휴, 신규)        491
양평맘(제휴, 신규)         242
쌍둥이맘 모여라(제휴, 신규) 181
```

## 3. 실제 노출 현황 실행 — 뉴더미스 / 우아덤 (문제 발생, 중단)

명령 `뉴더미스 노출 현황`(작업 134) · `우아덤 노출 현황`(작업 135)을 실제 큐에
넣어(발행 중인 실행기가 그대로 집어 가도록) 돌렸다. 두 작업 모두 **네이버 검색
요청까지 못 가고 약 30분 동안 진행이 없어 실행기 자체 감시(스톨 감지)가 자동으로
정리**했다(`jobs.error`: `"감시: 30분 멈춤 — 자동 정리"`, 결과 없음).

- `keyword_exposure` 테이블에 이번 실행으로 새로 쌓인 행은 **0건**(남아 있는 15행은
  이전(12:28) 실행분).
- 캡차 페이지·403/429 같은 **차단 신호가 로그에 남지 않았다** — 즉 어디서 멈췄는지
  이번 조사로는 특정하지 못했다(네이버 검색 호출 자체엔 10초 타임아웃이 있어, 거기서
  막혔다면 재시도로 넘어갔을 것). 지침("차단·캡차면 즉시 중단·기록")에 따라 **더
  재시도하지 않고 여기서 멈춘다.**
- 그래서 **뉴더미스·우아덤의 매칭 건수·순위 표는 이번 보고서에 담지 못했다.** 원인
  후보: 브랜드 시트(구글시트 gviz) 응답 지연, 또는 검사 대상 키워드 산출
  (`target_keywords`/`_prioritize`) 쪽에서 실행기가 다른 무거운 작업(발행 큐)과
  자원을 다투며 멈췄을 가능성. 별도 조사가 필요하다.

## 4. 테스트

새 파일 `tests/test_article_sync_scope.py`(13건) — `affiliate_cafe_names`, scope별
`sync_all_cafes` 라우팅(자사/제휴/전체/잘못된 값), 파서가 세 문구 모두
`sync_article_index`로 잡는지, `worker.dispatch`가 문구에서 scope를 올바로 골라
그 카페들만 도는지(대역 API로 실제 네트워크 호출 없이 검증).

전체 스위트: `.venv/Scripts/python -m pytest -q` → **1185 passed, 7 errors**
(11분 52초). 7건은 모두 `tests/conftest.py`의 "장부 파수꾼"(실행 중 실제
`data/llm_usage-2026-09.jsonl` 크기가 바뀌면 실패시키는 안전장치) 테어다운
오류로, `test_brand_writer.py`·`test_plan_command.py`·`test_warehouse_daily.py`의
기존 테스트에서 났다 — 이번 스위트 실행 중에도 실제 실행기(pid 27880)가 살아서
발행·색인 작업을 돌리고 있어 그 원장 파일이 실제로 갱신됐기 때문이다(이번 코드
변경과 무관, 지시대로 사유만 적고 무시). 내가 새로 추가한 테스트 13건은 모두 통과.

## 5. 남은 일 / 제안

- 07:45 제휴 색인 동기화가 자리 잡으면(내일부터) 브랜드 키워드 노출 검사가 실제로
  씨씨앙·양평맘·쌍둥이맘 글을 찾는지 다음 정기 실행(08:00 "키워드 노출 현황")
  결과로 다시 확인할 것.
- 3장의 30분 스톨은 재현·원인 규명이 필요하다. 발행이 진행 중이지 않은 시간대에
  다시 한번 `뉴더미스 노출 현황`만 단독으로 돌려 재현되는지 보는 것을 제안한다.
