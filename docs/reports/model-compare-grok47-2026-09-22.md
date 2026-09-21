# Grok 4.7 vs Fable 5.1 vs GPT-6 Astra — 토큰 비용·똑똑함 비교 (2026-09-22 03:09 KST, 실측)

## 1. 토큰 가격 (API, 100만 토큰당)
| 모델 | 입력 | 출력 | 캐시 읽기 | 문맥 창 | 비고 |
|---|---|---|---|---|---|
| **Grok 4.7** (xAI, 09-21 출시) | **$2** | **$6** | 미공개 | 미공개 | Fast 버전 $4/$12(속도 2배) |
| **Fable 5.1** (Anthropic) | $10 | $50 | $0.25 | 100만 / 출력 12.8만 | 캐시 읽기 75% 인하, 같은 일에 토큰 25~45% 덜 씀 |
| **GPT-6 Astra** (OpenAI, 09-04 출시) | $10 | $50 | $1.00 | 105만 / 출력 12.8만 | 첫 토큰 p95 5초 |

→ 표면 단가는 Grok 4.7이 **Fable·Astra의 1/5~1/8**. 단, 우리 원고 작업처럼 긴 지침을 매번 넣는 경우 캐시 읽기 단가(Fable $0.25)가 실제 비용을 좌우해 격차는 줄어듭니다. 우리는 요금제(0원)로 돌리므로 API 단가는 폴백 때만 해당.

## 2. 똑똑함 (공개 벤치마크)
### xAI 발표 표(Grok 페이지 기준)
| 벤치마크 | Grok 4.7 | GPT-5.6 Sol | Fable 5.1 |
|---|---|---|---|
| CursorBench 4.0 (코딩) | 46.3 | 41.7 | **51.8** |
| DeepSWE v1.1 (코딩) | 71.0 | **72.7** | 70.0 |
| Terminal-Bench 4.0 (장시간 터미널) | 38.0 | 37.3 | **57.9** |
| AA Briefcase (장시간 사무) | 1,657 | 1,487 | **1,678** |
| EEBench (전기공학) | **64.0** | 39.4 | 56.4 |
| Harvey 법률 에이전트 | **19.6** | 2.5 | 6.7 |
| HealthBench Pro (임상) | 56.7 | 60.5 | **62.1** |
(xAI 자체 발표라 Fable 수치는 Anthropic 공식 55.8과 소폭 다름)

### Astra(OpenAI) 발표 기준
| 벤치마크 | Astra | Fable 5.1/5 |
|---|---|---|
| Terminal-Bench 4.0 | **57.7** | 55.8 |
| DeepSWE v1.1 | **74.1** | 69.9 |
| FrontierMath Tier 4 | **97.6** | 87.8 |
| Humanity's Last Exam(도구) | 57.2 | **65.0** |
| OSWorld 2.0 (컴퓨터 조작) | **72.6** | 70.2(Opus 5) |
Astra는 Agents' Last Exam에서 Opus 5보다 출력 토큰 약 65% 적게 씀(토큰 효율 강점).

## 3. 한 줄 결론
| 관점 | 순위 |
|---|---|
| 똑똑함(코딩·장시간 에이전트·추론) | **Astra ≈ Fable 5.1 > Grok 4.7** — Grok 4.7은 GPT-5.6 Sol 수준(중상), 최상위 둘과는 격차 |
| 토큰 단가 | **Grok 4.7 ≫ Fable ≈ Astra** (5~8배 저렴) |
| 토큰 효율(같은 일에 쓰는 양) | Astra(출력 절약) ≈ Fable(캐시 $0.25) > Grok(미공개) |
| 우리 용도(한국어 원고·검증) | 한국어 자연스러움 벤치는 공개 없음. Grok는 **교차 검증기 3순위 후보**(GPT 다음)로 붙여 볼 가치는 있으나 xAI 요금제 없이는 유료 |

## 출처
- xAI: https://x.ai/news/grok-4-7
- Fable 5.1: https://llm-stats.com/models/claude-fable-5-1 , https://www.datacamp.com/blog/claude-fable-5-1
- Astra: https://llm-stats.com/models/gpt-6-astra , https://www.datacamp.com/blog/gpt-6-astra
