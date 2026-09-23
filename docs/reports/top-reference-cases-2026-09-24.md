# 완전 가까운 키워드 통검 카페1위 형식 대조 (2026-09-24)

현재 5/5 완료.

## A. 사례 (연관도 0 또는 1·검색량 큰 순, 브랜드당 1개, 기존 비교 15개 제외)

비교 항목(형식만, 소재·주제·사례·상품명은 제외): 제목 유형 / 도입 방식 / 단락 수 / 단락 길이 / 마무리 방식 / 목록 사용 / 소제목 사용 / 사진 수 (총 8항목). 임계: 4항목 이상 불일치 = "크게 다름".

| 브랜드 | 키워드 | 검색량 | relevance(claude/gpt) | 1위 글 제목유형 | 도입 | 단락수 | 마무리 | 목록/소제목 | 사진 | 불일치 항목(개수) | 판정 | 1위 글 URL |
|---|---|---:|---|---|---|---:|---|---|---:|---|---|---|
| 우아덤 | 자외선차단마스크 | 21,290 | 1/1 | 정보형 | 상황 서술 | 7 | 정리 | 없음 | 0 | 제목 유형, 단락 수 (2) | 비슷함 | https://cafe.naver.com/mindy7857/5437394 |
| 장으뜸 | 착상혈나오는시기 | 18,260 | 1/1 | 질문형 | 질문 | 3 | 정리 | 없음 | 0 | 단락 길이 (1) | 비슷함 | https://cafe.naver.com/magic26/2326617 |
| 코숨핏 | 입벌림방지밴드 | 5,620 | 1/0 | 후기형 | 상황 서술 | 8 | 정리 | 없음 | 0 | 단락 수, 단락 길이 (2) | 비슷함 | https://cafe.naver.com/bubi2/15987 |
| 뉴더미스 | 장염증상 | 47,290 | 1/1 | 질문형 | 질문 | 3 | 정리 | 없음 | 0 | 단락 길이 (1) | 비슷함 | https://cafe.naver.com/dgmom365/7382932 |
| 팥순이 | 마운자로가격 | 206,900 | 0/1 | 질문형 | 질문 | 11 | 정리 | 없음 | 0 | 단락 수 (1) | 비슷함 | https://cafe.naver.com/magic26/2324133 |

원본(코숨핏 원래 1순위 "코골이"는 1위 카페 글이 모바일 접근 차단(`We're sorry but mobile doesn't work`, 379자만 추출)으로 형식을 못 뽑아 다음 순위 "입벌림방지밴드"로 대체) raw JSON: file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/top_reference/_case5_2026-09-24.json

판정 근거: `OUR_FORMAT` 기준(제목 질문형/후기형, 도입 질문/상황 서술, 단락 3에서 6개, 단락당 100에서 600자, 마무리 정리/요청, 목록·소제목·사진 없음)과 `v2r/content/top_reference.format_gap()`으로 항목별 자동 대조(코드: file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/content/top_reference.py).

## B. 결론

5개 중 8항목 임계(4개) 이상 불일치한 사례는 **없음** (최대 2개, 나머지는 1개 또는 0개). **유지 권고** — 형식을 바꿀 근거 없음. 5번 시도로 마감(사용자 지시대로).

## C. 코드 변경

1. `v2r/content/top_reference.format_brief`는 이미 소재·주제·사례·상품명 없이 형식 항목만 담고 있었음(제목 유형·길이·단락·도입·마무리·목록/소제목만) — 그대로 유지, 회귀 시험 추가로 고정.
2. `config/bulk.yaml`의 `top_reference`에 `mode: "alert"` 추가, 자동 프롬프트 주입은 끔. 대신 `v2r/content/top_reference.evaluate_alert(rt, brand, keyword)`가 원고 생성 직전 1위 카페 글과의 형식 차이 점수(8항목 중 불일치 수)를 계산해 4개 이상이면 `docs/reports/top-reference-alerts-<날짜>.md`에 브랜드·키워드·불일치 항목·1위 URL을 적고 hold=True를 돌려준다. `v2r/content/bulk_generate.py`가 `mode == "alert"`일 때 hold 항목은 원고를 만들지 않고 `brand_queue.mark(..., "hold")`로 대기시킨다(슬랙 전송 없음, 큐 상태만).
3. `v2r/content/top_reference.OUR_FORMAT`/`format_gap`/`GAP_ALERT_THRESHOLD` 신설. 단위 시험 `tests/test_top_reference.py`에 추가.

## D. 시험

`.venv\Scripts\python.exe -m pytest tests/test_top_reference.py tests/test_bulk_generate.py -q` 통과 확인(실행 로그는 커밋 메시지에 요약).
