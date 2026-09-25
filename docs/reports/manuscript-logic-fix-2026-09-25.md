# 브랜드 원고 논리 일관성 수정 (2026-09-25)

## 지적

장으뜸 "장어" 원고: "장어를 먹었는데 부족해서 방법을 바꿨더니 장으뜸 장어즙"
— 실패한 원물과 대안으로 고른 제품이 같은 원물이라 앞뒤가 안 맞는다는
사용자 지적(13:30).

## 원인

`v2r/content/brand_writer.py`의 본문·댓글 프롬프트가 키워드를 항상
"겪는 문제·해 본 것 → 실패"로 두고 브랜드를 "다른 해결책"으로 잇는
**단일 서사 틀**만 썼다. 키워드가 브랜드 제품과 **같은 범주·원물**(장어 →
장으뜸 장어즙, 팥순이의 팥 → 팥순추출물 등, 연관도 0 직접)일 때는 이 틀이
"같은 원물을 실패했다가 같은 원물 제품으로 되돌아온" 꼴이 되어 부자연스러웠다.

## 고친 서사 틀 3종

`BrandRule`에 `core_material`(브랜드 제품과 같은 원물로 볼 낱말)을 추가하고,
`is_same_product_keyword(rule, keyword)`로 키워드를 판정해 본문·댓글
프롬프트(user 쪽, 캐시 대상 system은 그대로 둔다)에 다른 지시를 넣는다.

| 유형 | 판정 | 서사 틀 |
|---|---|---|
| (a) 동일 제품·원물 키워드 | `product`/`banned_in_body`/`core_material` 낱말과 키워드가 겹침 (예: 장어, 팥) | "실패→대안" 금지. **고르는 법·먹는 방식·형태 차이**를 묻는 틀 (`SAME_PRODUCT_BODY_NOTES`/`SAME_PRODUCT_COMMENT_NOTE`) |
| (b) 근접·확장(연관도 1–2) | 그 외 일반 키워드 | 기존 "문제→관리법" 틀 그대로 |
| (c) 당위성(연관도 3) | `relevance == 3` | 저장된 당위성 논리(`bridge_rationale`) 그대로 주입 (기존 동작 유지) |

`core_material`은 두 브랜드에 등록: 장으뜸 `("장어",)`, 팥순이 `("팥",)`
(질문형·후기형 둘 다).

## 검증 — 논리 일관성 항목

`check_logic_consistency()`가 본문+댓글의 브랜드 연결부를 요금제 길
Opus(`brand_logic_check` 용도, `v2r/llm/router.py`)에게 예/아니오+이유로
묻는다. 아니오면 `_retry_logic_consistency()`가 지적 이유를 프롬프트에 넣어
**최대 2회** 본문을 다시 받고(댓글도 같이 갱신), 최종 판정은
`stats["logic_check"] = {"ok", "이유", "시도들"}`에 남는다. 판정 API가
실패하면(요금제 한도 등) 원고 생성을 막지 않고 통과로 본다.

## 시험

`tests/test_brand_writer.py`에 추가:
- `test_is_same_product_keyword_*` 3건 — 같은 원물 판정
- `test_body_prompt_uses_comparison_frame_for_same_product_keyword` 외 3건 — 본문/댓글/combined 프롬프트가 틀을 바꿔 넣는지
- `test_logic_check_passes_records_ok_true` / `_retries_body_when_judge_says_no` / `_gives_up_after_max_retries` — 검증·재시도·포기 경로

기존 호출 횟수를 정확히 세던 시험 3건(`test_generate_manuscript_with_fake_llm`,
`test_combined_mode_makes_one_call_and_validates_both`,
`test_combined_mode_falls_back_to_partial_retry`, `test_stats_carry_tokens_and_cost`)은
`brand_logic_check` 호출 1회가 늘어난 만큼 기대값을 갱신했다.

`.venv/Scripts/python.exe -m pytest tests/test_brand_writer.py tests/test_brand_writer_redesign.py -q`
→ **117 passed**.

## 재생성 결과 — 20건 전체 논리 검증

`check_logic_consistency`를 기존 20건(`warehouse/manuscripts/twins-2026-09-25/`)에
그대로 돌렸다(요금제 길, 0원). 문제 있던 2건을 새 서사 틀로 재생성했다
(기존 파일은 `.bak`으로 보존).

| 브랜드 | 키워드 | 동일 원물 | 1차 판정 | 조치 |
|---|---|---|---|---|
| 뉴더미스 | 그릭요거트 / 덴마크유산균 / 비데 / 유산균 / 장염증상 | 아니오 | 예 (5/5) | 유지 |
| 우아덤 | 나이아신아마이드 / 리쥬란 / 멜라닌 / 비타민C / 주근깨 | 아니오 | 예 (5/5) | 유지 |
| 장으뜸 | 이노시톨 / 임신 / 자궁근종 | 아니오 | 예 (3/3) | 유지 |
| **장으뜸** | **장어** | **예** | **아니오** — "장어를 구워 먹어도 효과가 없었다고 해놓고 결국 같은 원물인 장어즙 제품으로 해결됐다" | **재생성** → 고르는 법·형태 비교 틀, 1차 통과 |
| **장으뜸** | **오메가3** | 아니오 | **아니오** — "오메가3를 실패로 규정한 뒤 오메가3 함유 식품인 장어즙으로 되돌아오는 구조" | **재생성**(일반 재시도 경로) → 2차 시도에서 통과 |
| 코숨핏 | 비염 / 비염치료 / 양압기 / 이비인후과 / 항히스타민제 | 아니오 | 예 (5/5) | 유지 |

18/20건은 처음부터 논리가 자연스러웠다(먹는 것 vs 바르는 것, 시술 vs 관리,
약물 vs 근육 강화처럼 카테고리가 달라 "실패→대안" 틀이 문제가 안 됐다).
"장어"는 동일 원물 판정대로 새 틀을 써서 1차에 통과했고, "오메가3"는
`core_material` 판정 대상은 아니었지만(오메가3는 성분명이라 낱말 자체가
안 겹침) 내용상 같은 성분(EPA·DHA)로 되돌아오는 문제가 있어 **논리
검증(항목 2)이 별도로 잡아낸 사례**다 — 검증이 규칙 기반 판정을 빠져나간
경우도 걸러 준다는 걸 확인했다.

## 갱신한 파일

- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/warehouse/manuscripts/twins-2026-09-25/%EC%9E%A5%EC%9C%BC%EB%9C%B8-%EC%9E%A5%EC%96%B4.json` (교체, `.bak` 보존)
- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/warehouse/manuscripts/twins-2026-09-25/%EC%9E%A5%EC%9C%BC%EB%9C%B8-%EC%98%A4%EB%A9%94%EA%B0%803.json` (교체, `.bak` 보존)
- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/manuscripts-%EC%9E%A5%EC%9C%BC%EB%9C%B8-2026-09-25.md` (장어·오메가3 절 갱신)
- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/content/brand_writer.py`
- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/llm/router.py`
- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_brand_writer.py`

## 커밋

1. `브랜드 원고 동일 원물 키워드 서사 틀 분리 + 논리 일관성 검증 추가` — 코드·시험 3파일, 푸시 완료
2. 원고·보고서 갱신 — 이 커밋에서 함께 푸시
