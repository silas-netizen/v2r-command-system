# 키워드 노출 확인 교차 검증 — 5개 브랜드 (2026-09-24)

작성 시각: 2026-09-24T07:35 (Asia/Seoul, `date` 실측)

## 0. 방법

- 대상: `data/v2r.sqlite`의 `keyword_exposure` 표에서 브랜드(팥순이·코숨핏·우아덤·장으뜸·뉴더미스)별로 오늘(2026-09-24) 03:00 이후 판정된 키워드 중 최신 상태 기준 노출완 5개 + 밀려남 5개(검색량 큰 순, `data/relevance/<브랜드>.sqlite`의 `keywords.total` 기준)를 뽑았다. 노출완이 5개 미만인 브랜드(코숨핏 4개, 우아덤 1개)는 있는 만큼만 썼다. 선정 결과: [selection-2026-09-24.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/selection-2026-09-24.json)
- 우리 판정 재현: `v2r/knowledge/keyword_exposure.py`의 `resolve_search_query` → `fetch_integrated_search_dom`(헤드리스, `PLAYWRIGHT_BROWSERS_PATH=.pw-browsers`) → `judge_keyword_exposure`(카페 카드 대표 글만 후보 → `confirm_our_article_detail`로 댓글2 계열 식별어 확인)를 그대로 새로 호출했다(오늘 새 조회, DB에는 쓰지 않음). 전체 원시 결과: [reverify-2026-09-24.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/reverify-2026-09-24.json)
- 교차 검증용 보강 증거: 판정에 쓰인 글이 `cached_verdict`(24시간 URL 캐시)로 이미 확정돼 있어 댓글 원문이 안 남는 경우가 있어, 노출완 20건 전부 `fetch_article_html`+`extract_comment_tree`로 다시 열어 댓글 트리·식별어 위치를 새로 뽑았다: [reverify-2026-09-24-enriched.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/reverify-2026-09-24-enriched.json)
- Codex 교차 검증: `codex.exe exec`(모델 `gpt-5.6-sol`, `model_reasoning_effort=medium`, `--sandbox read-only`, 창 없음) 45회 호출. 매번 **우리 판정을 숨기고** 후보 수·대표 글 URL·서브 링크·댓글 트리 발췌만 JSON으로 주고 같은 규칙 문장(대표 글만 후보, 서브 링크는 밀려남, 식별어는 댓글2 계열에서만)으로 독립 판정을 받았다. 원본: [codex-2026-09-24.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/codex-2026-09-24.json)
- 브랜드별 식별어(`config/brands.yaml`): 팥순이 ['팥순이', '팥순추출물', '팥순ㅇㅣ'] / 코숨핏 ['코숨핏'] / 우아덤 ['우아덤', '그린커피 아하바하', '아하바하'] / 장으뜸 ['장으뜸', '장으뜸 장어즙'] / 뉴더미스 ['뉴더미스', '자연방패', '자연방패 항문세정제']
- 계정명은 모두 "작성자"로 가렸다(원문 닉네임 비공개). 증거 JSON 원본은 `data/exposure_audit/<브랜드>/<키워드>.json`.

## 1. 전체 비교표 (러너 DB 판정 vs 내 재확인 vs Codex 판정)

### 팥순이

| 키워드 | 검색량 | DB 최근 판정(시각) | 내 재확인 | Codex 판정(확신도) | 3자 일치 |
|---|---|---|---|---|---|
| 애플사이다비니거 | 13,100 | 노출완 (2026-09-24T03:12:46+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 비만도 | 10,940 | 노출완 (2026-09-24T03:44:55+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 냉동도시락 | 10,840 | 노출완 (2026-09-24T03:45:01+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 곤약밥 | 10,670 | 노출완 (2026-09-24T03:09:46+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 클렌즈주스 | 7,420 | 노출완 (2026-09-24T03:58:12+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 테이크핏 | 10,390 | 밀려남 (2026-09-24T03:50:51+09:00) | 밀려남 | 밀려남 (1) | 일치 |
| 에어스텝퍼 | 9,270 | 밀려남 (2026-09-24T03:42:24+09:00) | 밀려남 | 밀려남 (1.0) | 일치 |
| 운동매트 | 7,750 | 밀려남 (2026-09-24T03:47:55+09:00) | 밀려남 | 밀려남 (1.0) | 일치 |
| 프로틴바 | 7,270 | 밀려남 (2026-09-24T04:10:35+09:00) | 밀려남 | 밀려남 (1) | 일치 |
| 러닝머신 | 6,810 | 밀려남 (2026-09-24T04:03:52+09:00) | 밀려남 | 밀려남 (1) | 일치 |

### 코숨핏

| 키워드 | 검색량 | DB 최근 판정(시각) | 내 재확인 | Codex 판정(확신도) | 3자 일치 |
|---|---|---|---|---|---|
| 코골이양압기 | 8,750 | 노출완 (2026-09-24T05:55:25+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 코골이치료 | 1,020 | 노출완 (2026-09-24T05:55:33+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 코세척기계 | 200 | 노출완 (2026-09-24T05:55:19+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 호흡기치료기 | 0 | 노출완 (2026-09-24T05:55:30+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 소아과 | 139,670 | 밀려남 (2026-09-24T03:02:02+09:00) | 밀려남 | 밀려남 (1.0) | 일치 |
| 대상포진 | 131,100 | 밀려남 (2026-09-24T03:02:14+09:00) | 밀려남 | 밀려남 (1) | 일치 |
| 매트리스 | 123,300 | 밀려남 (2026-09-24T03:02:26+09:00) | 밀려남 | 밀려남 (1.0) | 일치 |
| 쌀국수 | 120,600 | 밀려남 (2026-09-24T07:06:37+09:00) | 밀려남 | 밀려남 (1.0) | 일치 |
| 독감 | 107,500 | 밀려남 (2026-09-24T03:05:37+09:00) | 밀려남 | 밀려남 (1) | 일치 |

### 우아덤

| 키워드 | 검색량 | DB 최근 판정(시각) | 내 재확인 | Codex 판정(확신도) | 3자 일치 |
|---|---|---|---|---|---|
| 엉덩이종기 | 18,160 | 노출완 (2026-09-24T05:54:19+09:00) | 노출완 | 밀려남 (0.98) | **불일치** |
| 미녹시딜 | 70,700 | 밀려남 (2026-09-24T03:01:01+09:00) | 밀려남 | 밀려남 (0.99) | 일치 |
| 리쥬란 | 70,000 | 밀려남 (2026-09-24T03:01:01+09:00) | 밀려남 | 밀려남 (1) | 일치 |
| 편평사마귀 | 64,790 | 밀려남 (2026-09-24T03:01:16+09:00) | 밀려남 | 밀려남 (1) | 일치 |
| 위고비가격 | 58,230 | 밀려남 (2026-09-24T03:04:14+09:00) | 밀려남 | 밀려남 (0.99) | 일치 |
| 콜라겐 | 57,790 | 밀려남 (2026-09-24T03:04:29+09:00) | 밀려남 | 밀려남 (0.99) | 일치 |

### 장으뜸

| 키워드 | 검색량 | DB 최근 판정(시각) | 내 재확인 | Codex 판정(확신도) | 3자 일치 |
|---|---|---|---|---|---|
| 비만도계산기 | 114,940 | 노출완 (2026-09-24T05:25:21+09:00) | 노출완 | 노출완 (0.98) | 일치 |
| 등이아픈이유 | 6,400 | 노출완 (2026-09-24T06:06:30+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 얼리임신테스트기 | 1,170 | 노출완 (2026-09-24T03:41:28+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 다낭성난소증후군임신 | 1,100 | 노출완 (2026-09-24T03:44:04+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 부산정형외과추천 | 1,010 | 노출완 (2026-09-24T03:44:03+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 정형외과 | 458,200 | 밀려남 (2026-09-24T05:08:24+09:00) | 밀려남 | 밀려남 (1.0) | 일치 |
| 내과 | 399,100 | 밀려남 (2026-09-24T05:19:59+09:00) | 밀려남 | 밀려남 (1) | 일치 |
| 안과 | 323,300 | 밀려남 (2026-09-24T05:20:00+09:00) | 밀려남 | 밀려남 (1) | 일치 |
| 오메가3 | 207,700 | 밀려남 (2026-09-24T05:20:04+09:00) | 밀려남 | 밀려남 (1) | 일치 |
| 산부인과 | 202,520 | 밀려남 (2026-09-24T06:59:34+09:00) | 밀려남 | 밀려남 (1) | 일치 |

### 뉴더미스

| 키워드 | 검색량 | DB 최근 판정(시각) | 내 재확인 | Codex 판정(확신도) | 3자 일치 |
|---|---|---|---|---|---|
| 비만도계산기 | 114,040 | 노출완 (2026-09-24T05:27:35+09:00) | 노출완 | 밀려남 (0.99) | **불일치** |
| 드시모네 | 51,000 | 노출완 (2026-09-24T05:40:50+09:00) | 노출완 | 밀려남 (0.99) | **불일치** |
| 드시모네유산균 | 37,970 | 노출완 (2026-09-24T05:46:37+09:00) | 노출완 | 밀려남 (0.99) | **불일치** |
| 치핵 | 18,060 | 노출완 (2026-09-24T05:47:00+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 디오스민 | 7,110 | 노출완 (2026-09-24T05:52:07+09:00) | 노출완 | 노출완 (0.99) | 일치 |
| 약국 | 596,700 | 밀려남 (2026-09-24T06:58:48+09:00) | 밀려남 | 밀려남 (1) | 일치 |
| 이비인후과 | 503,500 | 밀려남 (2026-09-24T05:21:21+09:00) | 밀려남 | 밀려남 (1) | 일치 |
| 피부과 | 458,900 | 밀려남 (2026-09-24T05:21:28+09:00) | 밀려남 | 밀려남 (1) | 일치 |
| 정형외과 | 458,200 | 밀려남 (2026-09-24T05:21:31+09:00) | 밀려남 | 밀려남 (1) | 일치 |
| 내과 | 399,100 | 밀려남 (2026-09-24T05:21:31+09:00) | 밀려남 | 밀려남 (1) | 일치 |

## 2. 종합

- 총 45개 키워드(브랜드 5개 × 최대 10개) 중 **러너 DB 판정 · 내 재확인 · Codex 판정 3자 전부 일치 41개**, 불일치 4개.
- 내 재확인은 러너 DB 최신 판정과는 45개 전부 일치했다(순서만 다를 뿐 상태값 자체는 어긋난 것이 없었다).
- 불일치는 전부 "러너 DB=노출완, 내 재확인=노출완"인데 "Codex=밀려남"인 경우였다 — 아래 3절에서 원인을 밝힌다.

## 3. 불일치 원인 분석 — 크로스 브랜드 캐시 오염 (발견한 결함)

불일치 4건:

| 브랜드 | 키워드 | DB/내 재확인 | Codex 판정 | Codex 사유 |
|---|---|---|---|---|
| 우아덤 | 엉덩이종기 | 노출완 | 밀려남 | 대표 글 후보는 있으나 브랜드 식별어가 댓글2 계열에서 발견됐다는 확정 증거가 없습니다. |
| 뉴더미스 | 비만도계산기 | 노출완 | 밀려남 | 대표 글 후보는 확인됐지만 댓글2 계열에서 브랜드 식별어가 발견되지 않아 노출로 확정할 수 없다. |
| 뉴더미스 | 드시모네 | 노출완 | 밀려남 | 대표 글은 후보로 확인됐지만 브랜드 식별어가 댓글2 계열에서 발견되지 않아 노출 확정 조건을 충족하지 못했다. |
| 뉴더미스 | 드시모네유산균 | 노출완 | 밀려남 | 대표 글 후보는 확인됐지만 브랜드 식별어가 댓글2 계열에서 발견되지 않아 노출로 확정할 수 없습니다. |

**근본 원인**: `judge_keyword_exposure` → `confirm_our_article_detail`이 글이 "우리 글"인지 확정할 때 먼저 `cached_verdict(rt, url)`(24시간 URL 캐시, `data/…article_judgment_cache…json`)를 본다. 이 캐시는 **URL만 키로 쓰고 브랜드를 구분하지 않는다**(`v2r/knowledge/keyword_exposure.py:1827` `cached_verdict`, `_norm_url(url)`만 키). 그 결과, 예를 들어 뉴더미스 "비만도계산기" 키워드에서 후보로 잡힌 글 `https://cafe.naver.com/cantsb/3544901`은 실제로는 **팥순이** 원고(댓글에 "팥순추출물", "팥순ㅇㅣ"만 나오고 뉴더미스 식별어는 전혀 없음 — 어제 팥순이 감사 보고서의 "비만도 계산기" 사례와 **URL이 완전히 동일**)인데, 팥순이 판정 때 `ours=True`로 캐시된 값이 그대로 재사용되어 뉴더미스 판정에서도 "노출완"으로 나왔다. 같은 방식으로 확인한 4건 전부(우아덤 엉덩이종기 → `cafe.naver.com/yangmom/733906`, 뉴더미스 드시모네/드시모네유산균 → `cafe.naver.com/cantsb/3555269`)에서 댓글 원문에 해당 브랜드 식별어가 없고 대신 팥순이 식별어("팥순추출물", "팥순ㅇㅣ")만 확인됐다. Codex는 매번 신선한 댓글 증거만 보고 판정했으므로 이 오염의 영향을 받지 않아 정확했다.

**영향 범위**: 오늘 03:00 이후 판정에서 이 경로로 확정된 "노출완" 항목이 이 4건 외에도 더 있을 수 있다(캐시가 브랜드 무관 URL 키라서, 한 브랜드가 먼저 확인한 카페 글 URL을 다른 브랜드 키워드 후보가 우연히 같은 URL로 잡으면 전부 이 문제에 노출된다 — 다만 카페 글 URL 자체가 브랜드별로 다른 것이 보통이라 흔하지는 않다).

**수정안(적용 안 함 — 판정 로직 변경은 별도 확인 필요)**: `cached_verdict`/`set_cached_verdict`의 캐시 키를 `f"{brand}:{_norm_url(url)}"`로 바꿔 브랜드별로 분리한다. `data/…article_judgment_cache…json`의 기존 캐시는 브랜드 구분이 없으므로 마이그레이션 없이 그냥 비우고 다시 쌓는 편이 안전하다.

## 4. 브랜드별 노출완 근거 상세 (식별어 댓글 위치)

### 팥순이

- **애플사이다비니거** — 대표 글: https://cafe.naver.com/cantsb/3428491?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzQyODQ5MSwiaXNzdWVkQXQiOjE3OTAyMDE0NjEzODB9.jv2xF9yWaIB-MCfME2yLQaDXfgROBrr880tw8zYXiwA
  - 식별어 "팥순추출물" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: False, 위치 이탈: False)
  - 증거 원본: [애플사이다비니거.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%ED%8C%A5%EC%88%9C%EC%9D%B4/%EC%95%A0%ED%94%8C%EC%82%AC%EC%9D%B4%EB%8B%A4%EB%B9%84%EB%8B%88%EA%B1%B0.json)
- **비만도** — 대표 글: https://cafe.naver.com/cantsb/3544901?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzU0NDkwMSwiaXNzdWVkQXQiOjE3OTAyMDE0Njg2MTl9.4i7rl7juVYA8T_CcX0Uaa6xPkUTyow1eb2y5xXZBM4g
  - 식별어 "팥순추출물" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: False, 위치 이탈: False)
  - 증거 원본: [비만도.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%ED%8C%A5%EC%88%9C%EC%9D%B4/%EB%B9%84%EB%A7%8C%EB%8F%84.json)
- **냉동도시락** — 대표 글: https://cafe.naver.com/cantsb/3555298?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzU1NTI5OCwiaXNzdWVkQXQiOjE3OTAyMDE0NzQ5Njl9.6ITIXUk55D0fE-Sv4kmM4HdqH_bUO-rLNeVozdmD3ek
  - 식별어 "팥순추출물" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: False, 위치 이탈: False)
  - 증거 원본: [냉동도시락.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%ED%8C%A5%EC%88%9C%EC%9D%B4/%EB%83%89%EB%8F%99%EB%8F%84%EC%8B%9C%EB%9D%BD.json)
- **곤약밥** — 대표 글: https://cafe.naver.com/cantsb/3560030?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzU2MDAzMCwiaXNzdWVkQXQiOjE3OTAyMDE0ODI0ODN9.Z6zDkf4Bg-VOTJZ4AZr8QTez9NUpe74W0f1JPf3eKPE
  - 식별어 "팥순추출물" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: False, 위치 이탈: False)
  - 증거 원본: [곤약밥.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%ED%8C%A5%EC%88%9C%EC%9D%B4/%EA%B3%A4%EC%95%BD%EB%B0%A5.json)
- **클렌즈주스** — 대표 글: https://cafe.naver.com/cantsb/3510032?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzUxMDAzMiwiaXNzdWVkQXQiOjE3OTAyMDE0OTA1NjF9.gKXoVw0d1k-JwvKLk4iIbxlJ-RhtPx19N4lDQyjORsE
  - 식별어 "팥순추출물" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: False, 위치 이탈: False)
  - 증거 원본: [클렌즈주스.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%ED%8C%A5%EC%88%9C%EC%9D%B4/%ED%81%B4%EB%A0%8C%EC%A6%88%EC%A3%BC%EC%8A%A4.json)

### 코숨핏

- **코골이양압기** — 대표 글: https://cafe.naver.com/cantsb/3537134?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzUzNzEzNCwiaXNzdWVkQXQiOjE3OTAyMDE1MzY4MzN9.1LWaN7HiMIoXBipmFuSZQpaE76ynpGsa5_Kr--bSmNQ
  - 식별어 "코숨핏" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: True, 위치 이탈: False)
  - 증거 원본: [코골이양압기.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EC%BD%94%EC%88%A8%ED%95%8F/%EC%BD%94%EA%B3%A8%EC%9D%B4%EC%96%91%EC%95%95%EA%B8%B0.json)
- **코골이치료** — 대표 글: https://cafe.naver.com/cantsb/3562808?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzU2MjgwOCwiaXNzdWVkQXQiOjE3OTAyMDE1NDcxODB9.uUIT9kIHEiJjC9HlAymO-byR4RMgePuMFRnEkFt5NJo
  - 식별어 "코숨핏" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: True, 위치 이탈: False)
  - 증거 원본: [코골이치료.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EC%BD%94%EC%88%A8%ED%95%8F/%EC%BD%94%EA%B3%A8%EC%9D%B4%EC%B9%98%EB%A3%8C.json)
- **코세척기계** — 대표 글: https://cafe.naver.com/cantsb/3562791?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzU2Mjc5MSwiaXNzdWVkQXQiOjE3OTAyMDE1NjA3NzZ9.41GBpnNtEepba3N29ZObireEVqy2PiMpeH-w0YCuUB8
  - 식별어 "코숨핏" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: True, 위치 이탈: False)
  - 증거 원본: [코세척기계.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EC%BD%94%EC%88%A8%ED%95%8F/%EC%BD%94%EC%84%B8%EC%B2%99%EA%B8%B0%EA%B3%84.json)
- **호흡기치료기** — 대표 글: https://cafe.naver.com/cantsb/3562858?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzU2Mjg1OCwiaXNzdWVkQXQiOjE3OTAyMDE1Njc4NDh9.SKweOYlGGWwdHExS4s9YIIszKyIGINYGQdirDMz4e1s
  - 식별어 "코숨핏" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: True, 위치 이탈: False)
  - 증거 원본: [호흡기치료기.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EC%BD%94%EC%88%A8%ED%95%8F/%ED%98%B8%ED%9D%A1%EA%B8%B0%EC%B9%98%EB%A3%8C%EA%B8%B0.json)

### 우아덤

- **엉덩이종기** — 대표 글: https://cafe.naver.com/yangmom/733906?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6Inlhbmdtb20iLCJhcnRpY2xlSWQiOjczMzkwNiwiaXNzdWVkQXQiOjE3OTAyMDE2MTYyNjN9.btJjy9U3IcTXPSrAM0hT3h1j3glQnNMGL-D8YqiiltI
  - 이번 재확인 시 댓글 트리에서 해당 브랜드 식별어를 새로 확인하지 못함(위 3절 캐시 오염 사례 참고 — 재조사 필요)
  - 증거 원본: [엉덩이종기.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EC%9A%B0%EC%95%84%EB%8D%A4/%EC%97%89%EB%8D%A9%EC%9D%B4%EC%A2%85%EA%B8%B0.json)

### 장으뜸

- **비만도계산기** — 대표 글: https://cafe.naver.com/cantsb/3544901?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzU0NDkwMSwiaXNzdWVkQXQiOjE3OTAyMDE0Njg2MTl9.4i7rl7juVYA8T_CcX0Uaa6xPkUTyow1eb2y5xXZBM4g
  - 이번 재확인 시 댓글 트리에서 해당 브랜드 식별어를 새로 확인하지 못함(위 3절 캐시 오염 사례 참고 — 재조사 필요)
  - 증거 원본: [비만도계산기.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EC%9E%A5%EC%9C%BC%EB%9C%B8/%EB%B9%84%EB%A7%8C%EB%8F%84%EA%B3%84%EC%82%B0%EA%B8%B0.json)
- **등이아픈이유** — 대표 글: https://cafe.naver.com/yangmom/734479?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6Inlhbmdtb20iLCJhcnRpY2xlSWQiOjczNDQ3OSwiaXNzdWVkQXQiOjE3OTAyMDE2NzQzNDR9.N-h-Q9EYYERqfC7EOdzyYkCMiT1J6neKLLF7t2B9N58
  - 식별어 "장으뜸" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: True, 위치 이탈: False)
  - 증거 원본: [등이아픈이유.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EC%9E%A5%EC%9C%BC%EB%9C%B8/%EB%93%B1%EC%9D%B4%EC%95%84%ED%94%88%EC%9D%B4%EC%9C%A0.json)
- **얼리임신테스트기** — 대표 글: https://cafe.naver.com/yangmom/728780?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6Inlhbmdtb20iLCJhcnRpY2xlSWQiOjcyODc4MCwiaXNzdWVkQXQiOjE3OTAyMDE2ODI1Mjl9.YWuduPSF2Igv4CUPwi97-4Q8VoWIbyxvan4kuYveCD0
  - 식별어 "장으뜸" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: True, 위치 이탈: False)
  - 증거 원본: [얼리임신테스트기.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EC%9E%A5%EC%9C%BC%EB%9C%B8/%EC%96%BC%EB%A6%AC%EC%9E%84%EC%8B%A0%ED%85%8C%EC%8A%A4%ED%8A%B8%EA%B8%B0.json)
- **다낭성난소증후군임신** — 대표 글: https://cafe.naver.com/cantsb/3525106?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzUyNTEwNiwiaXNzdWVkQXQiOjE3OTAyMDE2OTA3NjZ9.FZBLc68PbZt_PkH2Ko5d3-ZELyHcNqJfpCYosRyBUGg
  - 식별어 "장으뜸" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: True, 위치 이탈: False)
  - 증거 원본: [다낭성난소증후군임신.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EC%9E%A5%EC%9C%BC%EB%9C%B8/%EB%8B%A4%EB%82%AD%EC%84%B1%EB%82%9C%EC%86%8C%EC%A6%9D%ED%9B%84%EA%B5%B0%EC%9E%84%EC%8B%A0.json)
- **부산정형외과추천** — 대표 글: https://cafe.naver.com/yangmom/734105?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6Inlhbmdtb20iLCJhcnRpY2xlSWQiOjczNDEwNSwiaXNzdWVkQXQiOjE3OTAyMDE2OTg0Mjd9.56Adq7kabxZVd-h74zj_mOTjw6wEljAFZS7IAMOYjIQ
  - 식별어 "장으뜸" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: True, 위치 이탈: False)
  - 증거 원본: [부산정형외과추천.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EC%9E%A5%EC%9C%BC%EB%9C%B8/%EB%B6%80%EC%82%B0%EC%A0%95%ED%98%95%EC%99%B8%EA%B3%BC%EC%B6%94%EC%B2%9C.json)

### 뉴더미스

- **비만도계산기** — 대표 글: https://cafe.naver.com/cantsb/3544901?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzU0NDkwMSwiaXNzdWVkQXQiOjE3OTAyMDE0Njg2MTl9.4i7rl7juVYA8T_CcX0Uaa6xPkUTyow1eb2y5xXZBM4g
  - 이번 재확인 시 댓글 트리에서 해당 브랜드 식별어를 새로 확인하지 못함(위 3절 캐시 오염 사례 참고 — 재조사 필요)
  - 증거 원본: [비만도계산기.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EB%89%B4%EB%8D%94%EB%AF%B8%EC%8A%A4/%EB%B9%84%EB%A7%8C%EB%8F%84%EA%B3%84%EC%82%B0%EA%B8%B0.json)
- **드시모네** — 대표 글: https://cafe.naver.com/cantsb/3555269?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzU1NTI2OSwiaXNzdWVkQXQiOjE3OTAyMDE3NDk3MjF9.0dWFuOHCtQyBR0cK2c2fM1VD_Pah-bYmB9SOkBfPBmM
  - 이번 재확인 시 댓글 트리에서 해당 브랜드 식별어를 새로 확인하지 못함(위 3절 캐시 오염 사례 참고 — 재조사 필요)
  - 증거 원본: [드시모네.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EB%89%B4%EB%8D%94%EB%AF%B8%EC%8A%A4/%EB%93%9C%EC%8B%9C%EB%AA%A8%EB%84%A4.json)
- **드시모네유산균** — 대표 글: https://cafe.naver.com/cantsb/3555269?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6ImNhbnRzYiIsImFydGljbGVJZCI6MzU1NTI2OSwiaXNzdWVkQXQiOjE3OTAyMDE3NDk3MjF9.0dWFuOHCtQyBR0cK2c2fM1VD_Pah-bYmB9SOkBfPBmM
  - 이번 재확인 시 댓글 트리에서 해당 브랜드 식별어를 새로 확인하지 못함(위 3절 캐시 오염 사례 참고 — 재조사 필요)
  - 증거 원본: [드시모네유산균.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EB%89%B4%EB%8D%94%EB%AF%B8%EC%8A%A4/%EB%93%9C%EC%8B%9C%EB%AA%A8%EB%84%A4%EC%9C%A0%EC%82%B0%EA%B7%A0.json)
- **치핵** — 대표 글: https://cafe.naver.com/yangmom/733366?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6Inlhbmdtb20iLCJhcnRpY2xlSWQiOjczMzM2NiwiaXNzdWVkQXQiOjE3OTAyMDE3NjYzMjh9.iLjezM1KwgURuZ_tTuq0Cse3vTk6zmXIkiaU-pZ8uDI
  - 식별어 "자연방패" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: True, 위치 이탈: False)
  - 증거 원본: [치핵.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EB%89%B4%EB%8D%94%EB%AF%B8%EC%8A%A4/%EC%B9%98%ED%95%B5.json)
- **디오스민** — 대표 글: https://cafe.naver.com/yangmom/733189?art=ZXh0ZXJuYWwtc2VydmljZS1uYXZlci1zZWFyY2gtY2FmZS1wcg.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYWZlVHlwZSI6IkNBRkVfVVJMIiwiY2FmZVVybCI6Inlhbmdtb20iLCJhcnRpY2xlSWQiOjczMzE4OSwiaXNzdWVkQXQiOjE3OTAyMDE3NzI5NzJ9.rXxFvQImcMrvOw3BgZxJ4TrUnHEry-UGxcN9ukePrDs
  - 식별어 "자연방패" 댓글2 계열 2번째 스레드에서 발견(대댓글 여부: True, 위치 이탈: False)
  - 증거 원본: [디오스민.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/%EB%89%B4%EB%8D%94%EB%AF%B8%EC%8A%A4/%EB%94%94%EC%98%A4%EC%8A%A4%EB%AF%BC.json)
