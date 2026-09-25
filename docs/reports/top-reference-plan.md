# 통검 1등 글 형식 참고 원고 생성 — 설계 (2026-09-23 11:50)

## 조건 (사용자 지시)

키워드 원고를 만들 때 **그 키워드의 네이버 통합검색 1등 글의 형식**을 참고해, 내용은 우리 브랜드 논리로 쓴다. 5개 브랜드의 **검색량이 상대적으로 높은 키워드**에 적용.

## 설계

| 단계 | 내용 |
|---|---|
| 1등 글 찾기 | 자동완성 1순위 표기로 통검 첫 페이지를 headless로 열고, 광고·파워링크·쇼핑·뉴스를 뺀 **첫 일반 글**(카페·블로그·포스트) 링크를 잡음 |
| 형식 추출 | 그 글을 headless로 열어 제목 유형(질문/후기/정보), 글자수, 단락 수, 도입 방식, 전개 순서, 마무리 유형, 사진 수, 목록·소제목 사용 여부만 계산 |
| 참고 요약 | 300~400자 한국어 "참고 형식" 요약. **문장·표현·사례는 절대 넣지 않음**(형식만). 마지막 줄에 "형식만 참고, 베끼기 금지, 우리 가이드 논리로" 고정 문구 |
| 프롬프트 삽입 | 본문 생성 user 프롬프트 끝에만 삽입(브랜드 공용 system 프롬프트는 그대로 → 캐시·토큰 절약 유지). 건당 +300~400자 |
| 적용 범위 | 브랜드별 키워드 검색량 **상위 50%** 또는 월 100회 이상. 그 외 키워드는 기존 방식 |
| 캐시 | `data/top_reference/<키워드>.json`, 7일 유지. 실패는 1일 캐시 |
| 기록 | 원고 JSON stats에 참고한 1등 글 URL·적용 여부 저장 → 현황판·검수에서 확인 가능 |

## 작업 방식

- 코딩은 Sonnet 일꾼이 지금 진행 중(창 없음, headless, 실행기 재시작 없음).
- 완료 시 15개 키워드(브랜드별 검색량 상위 3개) 실측 표와 원고 1건 시험 결과를 [top-reference-2026-09-23.md](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/top-reference-2026-09-23.md) 로 보고.
- 이후 `대량 원고 …` 명령은 자동으로 이 조건을 따름(설정 `config/bulk.yaml` `top_reference.enabled`).

## 관련 파일

- [bulk_generate.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/content/bulk_generate.py) · [brand_writer.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/content/brand_writer.py) · [keyword_exposure.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_exposure.py)
