# 키워드 연관도 재채점 강건화 — 2026-09-24

## 실패 유형별 건수 (logs/serve.log 실측, 2026-09-24 04시 기준 집계)

| 유형 | 건수 |
|---|---|
| `번째 keyword 불일치`(모델이 오타를 교정해 돌려줌) | 24건 |
| `모델 응답의 JSON이 끝나지 않았습니다`(100개 묶음 + bridge 문장으로 max_tokens 초과) | 7건 |
| `묶음 포기`(3회 재시도 후 포기, 100개 통째로 버려짐) | 5건 |
| `codex 묶음 포기` | 0건 |

keyword 불일치 예시(로그 원문): 기대 '클랜징폼' vs 응답 '클렌징폼', 기대 '리포즘글루타치온' vs 응답
'리포솜글루타치온', 기대 '때타월' vs 응답 '때타올', 기대 '마그네스정' vs 응답 '마그네슈정', 기대
'마몽드리퀴드마스크' vs 응답 '마몬드리퀴드마스크', 기대 '바디샴푸' vs 응답 '바디샤워' — 전부 모델이
철자를 "교정"해 돌려준 사례다.

## 수정 내용 (v2r/knowledge/keyword_relevance.py)

1. **`parse_response` 완화**: 응답 항목을 keyword 문자열 완전일치가 아니라 **위치(index)로
   기대 키워드에 대응**시킨다. 정규화(공백 제거·소문자) 후 같거나, 다르더라도 편집 거리
   (레벤슈타인, `_levenshtein`)가 기대 키워드 길이의 30% 이하(최소 2)면 오타로 보고 **기대
   키워드 값을 그대로 저장**한다(응답의 철자는 버린다). 응답에 `keyword` 필드가 없어도 위치로만
   대응한다. 응답 개수가 다르거나 편집 거리가 크게 벗어나면 기존처럼 `RelevanceParseError`.
2. **묶음 크기·토큰 한도**: `DEFAULT_BATCH_SIZE` 100 → 50, 새 상수 `DEFAULT_MAX_TOKENS`(8000,
   기존 4000)를 신설해 `score_batch`가 사용한다. `score_brand`·`crosscheck_brand`·
   `rescore_legacy_unrelated_brand`·`score_and_crosscheck_brand`는 모두 `DEFAULT_BATCH_SIZE`를
   기본값으로 쓰므로 자동으로 50개로 줄어든다. Codex 교차검증(`score_batch_codex`)도 같은
   `batch_size`로 호출되는 묶음을 받으므로 동일하게 완화된다.
3. **포기된 묶음 재대상화 확인**: `score_brand`/`rescore_legacy_unrelated_brand`는 실패한 묶음에
   대해 `write_scores`를 호출하지 않으므로 `scored_at`이 갱신되지 않는다. `pending_keywords`는
   `scored_at = ''`인 키워드만 뽑으므로 포기된 묶음은 다음 회차에 자동으로 다시 대상이 됨을
   `test_score_brand_leaves_failed_batch_keywords_pending`으로 확인했다. 실패 원인은 기존
   `log.error("묶음 포기(...): %s", exc)` 한 줄로 남는다(변경 없음, 요구사항 충족 확인).

## 시험 결과

`tests/test_keyword_relevance.py` 만 대상 실행(전체 스위트는 다른 작업자와 동시 실행 중이라
장시간 대기 상태로 보여 중단함 — 사용자 지시):

```
.............................                                            [100%]
29 passed in 54.24s
```

새로 추가한 시험: 오타 교정 응답 수용(`test_parse_response_accepts_typo_corrected_keyword`),
keyword 필드 없이 위치로만 대응(`test_parse_response_no_keyword_field_matches_by_position`),
편집 거리가 큰 경우는 여전히 실패(`test_parse_response_rejects_wildly_different_keyword`),
개수 불일치는 계속 실패(`test_parse_response_count_mismatch`, 기존), 기본 묶음 50개
(`test_default_batch_size_is_50`), max_tokens 8000 사용(`test_score_batch_uses_higher_max_tokens`),
포기된 묶음이 다음 회차 대상으로 남음(`test_score_brand_leaves_failed_batch_keywords_pending`).

전체 스위트는 미실행(관련 시험만 통과).

## 실행기 재시작 필요

이 변경이 반영되려면 실행기(serve) 재시작이 필요합니다. 직접 재시작하지 않았습니다.
