# 노출 캐시 브랜드 분리 수정 (2026-09-24)

작성 시각: 2026-09-24T08:20 (Asia/Seoul, `date` 실측)

## 0. 배경

[exposure-audit-5brands-2026-09-24.md](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/exposure-audit-5brands-2026-09-24.md) 3절에서 발견한 결함 — `v2r/knowledge/keyword_exposure.py`의 `cached_verdict`/`set_cached_verdict`가 글 URL만 캐시 키로 써서, 한 브랜드가 `ours=True`로 확정한 카페 글이 24시간 안에 **다른 브랜드**의 키워드 판정에도 그대로 재사용됐다(팥순이 원고 글이 뉴더미스·장으뜸·우아덤 노출 판정에 오염). 이 보고서는 그 결함을 고치고, 오늘 오염된 실제 판정 건을 찾아 정정한 기록이다.

## 1. 코드 수정 (완료 · 커밋·푸시됨)

- `v2r/knowledge/keyword_exposure.py`: `cached_verdict(rt, url, ...)` / `set_cached_verdict(rt, url, ours, ...)` → `cached_verdict(rt, brand, url, ...)` / `set_cached_verdict(rt, brand, url, ours, ...)`로 시그니처 변경. 캐시 키를 `_cache_key(brand, url)` = `f"{brand}::{_norm_url(url)}"`로 바꿔 브랜드마다 독립된 캐시 항목을 쓴다. 기존 캐시 파일(`data/exposure_article_cache.json`)의 브랜드 없는 옛 키는 새 키 형식과 일치하지 않으므로 조회 시 자동으로 무효(무시) 처리된다(마이그레이션 불필요, 그냥 안 읽힘).
- 호출부(`confirm_our_article_detail`) 두 곳(`cached_verdict`/`set_cached_verdict` 호출)을 브랜드 인자를 넘기도록 수정.
- 단위 시험 추가: `tests/test_keyword_exposure_cycle.py::test_confirm_our_article_캐시는_브랜드별로_분리된다` — 같은 URL을 두 브랜드로 확인하면 캐시를 공유하지 않고 각자 다시 여는지, 같은 브랜드 안에서는 여전히 24시간 캐시를 재사용하는지 확인.
- 시험: 관련 파일(`tests/test_keyword_exposure_cycle.py` + `tests/test_keyword_exposure.py`) 80건 전부 통과. 전체 시험(`pytest tests/`) 1508건 중 1503건 통과, 5건 실패 — 전부 `tests/test_top_reference.py`·`tests/test_parser.py`의 이번 수정과 무관한 기존 실패(코드에 `cached_verdict`/`set_cached_verdict`를 쓰지 않는 파일, `too many values to unpack` 계열 별개 결함으로 보임 — 이번 작업 범위 밖이라 손대지 않았다).
- 커밋: `git log -1`로 확인한 로컬 해시 기준 커밋 메시지 "fix(keyword_exposure): 노출 판정 글 캐시를 브랜드별로 분리", `origin/main`에 푸시 완료(`e0363f4..fdad0e2`).

**러너 재시작 필요**: 지금 돌고 있는 노출 확인 순환 실행기(러너)는 이미 메모리에 올라간 옛 `keyword_exposure` 모듈(브랜드 무관 캐시 키)을 그대로 쓰고 있다. 이번 코드 수정이 실제로 적용되려면 **러너 프로세스 재시작이 필요하다**(이번 작업에서는 지시대로 러너를 직접 재시작하지 않았다).

## 2. 오염 범위 조사

- 방법: `data/v2r.sqlite`의 `keyword_exposure`에서 오늘(2026-09-24) 00:00 이후 `status='exposed'`인 458행을 모두 뽑아, 각 행의 `article_url`에서 카페 별칭+글번호(`cafe.naver.com/<별칭>/<글번호>`)만 추려 같은 글이 **서로 다른 브랜드**로 기록된 경우를 찾았다(정확히 이게 캐시 오염이 생길 수 있는 유일한 조건 — 캐시 키가 URL만이므로, 같은 글이 두 브랜드에서 '노출' 후보로 잡힌 경우에만 한쪽이 다른 쪽 캐시를 재사용할 수 있다).
- 결과: 458행 중 289개 서로 다른 글, 이 중 **3개 글이 2개 이상 브랜드에 걸쳐 있었다**(의심 목록 — [contamination-scan-2026-09-24.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/contamination-scan-2026-09-24.json)):
  - `cafe.naver.com/cantsb/3555269` — 팥순이("드시모네 유산균"), 뉴더미스("드시모네" 2행, "드시모네유산균" 3행)
  - `cafe.naver.com/cantsb/3544901` — 팥순이("비만도", "비만 계산기"), 장으뜸("비만도계산기"), 뉴더미스("비만도계산기")
  - `cafe.naver.com/yangmom/733906` — 뉴더미스("엉덩이 종기" 10행, "엉덩이 종기 연고" 1행), 우아덤("엉덩이종기")
- 의심 (브랜드, 키워드) 쌍 10개를 코드 수정 이후의 `confirm_our_article_detail`(브랜드별 캐시라 캐시 오염 없이 새로 검사)로 각각 다시 열어 그 브랜드 식별어가 댓글2 계열에 실제로 있는지 재판정했다(헤드리스, `PLAYWRIGHT_BROWSERS_PATH=.pw-browsers`, 창 없음). 원본: [contamination-verdicts-2026-09-24.json](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_audit/contamination-verdicts-2026-09-24.json)

| 글 | 브랜드/키워드 | 재판정 | 비고 |
|---|---|---|---|
| cantsb/3555269 | 팥순이 / 드시모네 유산균 | 노출완(진짜 주인) | 팥순이 자체 키워드로 별도 정상 판정 |
| cantsb/3555269 | 뉴더미스 / 드시모네 | **밀려남(정정)** | 팥순이 캐시 재사용으로 오염됐던 건 |
| cantsb/3555269 | 뉴더미스 / 드시모네유산균 | **밀려남(정정)** | 팥순이 캐시 재사용으로 오염됐던 건 |
| cantsb/3544901 | 팥순이 / 비만도, 비만 계산기 | 노출완(진짜 주인) | 팥순이 자체 키워드로 별도 정상 판정 |
| cantsb/3544901 | 장으뜸 / 비만도계산기 | **밀려남(정정)** | 팥순이 캐시 재사용으로 오염됐던 건 |
| cantsb/3544901 | 뉴더미스 / 비만도계산기 | **밀려남(정정)** | 팥순이 캐시 재사용으로 오염됐던 건 |
| yangmom/733906 | 뉴더미스 / 엉덩이 종기, 엉덩이 종기 연고 | 노출완(진짜 주인) | 뉴더미스 자체 키워드로 정상 판정(식별어 '자연방패' 계열 확인) |
| yangmom/733906 | 우아덤 / 엉덩이종기 | **밀려남(정정)** | 뉴더미스 캐시 재사용으로 오염됐던 건 |

**정정 대상 5건**: 뉴더미스(드시모네, 드시모네유산균, 비만도계산기), 장으뜸(비만도계산기), 우아덤(엉덩이종기).

## 3. DB·시트 정정 — 차단됨(사용자 승인 필요)

위 5건에 대해 DB에 `status=pushed`(오염 정정 표시, `t0_status="contamination_fix_2026-09-24"`)로 새 판정 행을 넣고, 시트 CSV 백업 후 `sheets_writer.apply_exposure`로 G열=밀려남·L열=0을 쓰는 스크립트를 준비했다(B~F열은 읽지도 쓰지도 않음). 실행하자 Claude Code 자동 모드 분류기가 "Modify Shared Resources"(공유 자원 변경)로 이 작업을 **거부**했다 — 운영 중인 DB에 새 행을 쓰고 실서비스 구글 시트를 갱신하는 행위라 명시적 승인이 필요하다는 판단으로 보인다.

**실행되지 않음(DB·시트 모두 그대로다)**. 사용자가 승인하면 아래 스크립트를 그대로 다시 돌리면 된다(같은 내용을 여기 그대로 옮겨 적었다 — 5건의 브랜드·키워드·오염 URL·검색량):

```
(뉴더미스, 드시모네, https://cafe.naver.com/cantsb/3555269, 검색량 51000)
(뉴더미스, 드시모네유산균, https://cafe.naver.com/cantsb/3555269, 검색량 37970)
(장으뜸, 비만도계산기, https://cafe.naver.com/cantsb/3544901, 검색량 114940)
(뉴더미스, 비만도계산기, https://cafe.naver.com/cantsb/3544901, 검색량 114040)
(우아덤, 엉덩이종기, https://cafe.naver.com/yangmom/733906, 검색량 18160)
```

## 4. 요약

- 코드 결함: **수정 완료**, 시험 통과, 커밋·푸시 완료.
- 오염 의심 건: 3개 글, 10개 (브랜드,키워드) 쌍 전수 재판정 완료.
- 정정 필요 건: **5건**(뉴더미스 3·장으뜸 1·우아덤 1), DB·시트 반영은 **권한 차단으로 미실행** — 사용자 승인 필요.
- **러너 재시작 필요**: 코드 수정을 실제 순환에 반영하려면 실행 중인 노출 확인 러너를 재시작해야 한다(이번 작업에서는 러너를 건드리지 않았다).
