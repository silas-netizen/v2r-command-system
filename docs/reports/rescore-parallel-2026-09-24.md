# 키워드 연관도 재채점 — 브랜드별 병렬 실행 (2026-09-24)

## 구조

- 진입점: `python -m v2r.knowledge.keyword_relevance --rescore-worker <브랜드>` — 브랜드 하나를 처음부터 끝까지 돈다.
  1. 구 척도 3(무관)이던 키워드를 새 척도(3=당위성/4=무관)로 재채점(클로드 50개 묶음 → Codex 교차검증, `bridge_rationale` 저장).
  2. 끝난 뒤 이 브랜드의 GPT(Codex) 미검증분 전체를 이어서 교차검증.
  3. 진행: `data/keywords/rescore_progress_<브랜드>.json`(scored/pending/codex_checked/updated_at/status), 로그: `logs/rescore-<브랜드>.log`.
- LLM 라우터는 실행기·채우기 순환과 같은 방식(`LLMRouter.from_settings`)으로 얻고, `_PatientRouter`로 감싸 요금제 한도(plan_quota) 오류가 나면 10분 쉬고 자동 재개한다(예약 신뢰성 원칙).
- 중복 실행 방지: `data/locks/rescore-<브랜드>.lock`. 신선한(10분 이내) 잠금이 있으면 그 브랜드 프로세스는 곧바로 끝난다. 잠금은 실행 중 2분마다 스스로 touch해 신선하게 유지하고, 10분 넘게 안 만져지면 죽은 잠금으로 보고 무시한다.
- `scripts/rescore.cmd` + `scripts/rescore-hidden.vbs`가 5개 브랜드를 창 없이 동시에 띄운다(`scripts/keyword-fill-hidden.vbs` 쌍과 같은 구조).
- 실행기 작업 `키워드 연관도 재채점`(`v2r.engine.worker._keyword_relevance_rescore_legacy`)은 이제 5개 브랜드를 프로세스 안에서 순차로 몇 시간 돌리지 않고, `rescore-hidden.vbs`를 띄우기만 하고 곧바로 끝난다(메인 줄을 막지 않음).
- 시험: [file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_keyword_relevance.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_keyword_relevance.py), [file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_engine.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_engine.py) — 관련 시험(재채점 400여건, 사이드카·엔진 포함) 통과. 무관한 기존 실패 1건(`test_긴_작업_중_중지_명령이_즉시_먹힌다`, 타이밍 플레이키 — stash로 대조해 이번 변경과 무관함 확인).
- 커밋: `93cd24c`, `git push origin HEAD` 완료.

## 실행 및 첫 5분 처리 현황

- 시작: 2026-09-24 10:54:29 (`cscript //nologo scripts/rescore-hidden.vbs`)
- 첫 관측(10:57분에서 10:59분 사이, 브랜드별 갱신 시각 다름): 5개 브랜드 모두 `status: running`, 각 100개 채점(첫 묶음) 완료, 실패 묶음 0.

| 브랜드 | 대상 시작 수 | 첫 관측까지 채점 | 관측 시각 | 경과(초) | 채점 속도(개/분, 클로드 단계) | 남은 개수 | 예상 소요(클로드 단계만, 시) |
|---|---|---|---|---|---|---|---|
| 뉴더미스 | 8,495 | 100 | 10:57:51 | 202 | 29.7 | 8,395 | 4.7 |
| 팥순이 | 27,046 | 100 | 10:58:54 | 265 | 22.6 | 26,946 | 19.8 |
| 코숨핏 | 9,218 | 100 | 10:59:04 | 275 | 21.8 | 9,118 | 7.0 |
| 장으뜸 | 28,840 | 100 | 10:59:16 | 287 | 20.9 | 28,740 | 22.9 |
| 우아덤 | 9,583 | 100 | 10:59:19 | 290 | 20.7 | 9,483 | 7.6 |

- 예상 완료 시각(클로드 채점 단계만, 10:54:29 기준 위 소요 더함): 뉴더미스 약 15:36, 코숨핏 약 17:57, 우아덤 약 18:30, 팥순이 약 06:43(9/25), 장으뜸 약 09:47(9/25).
- 주의: 이 추정은 **관측 시점까지의 첫 묶음 속도**를 단순 연장한 값이고, 뒤이은 Codex 교차검증 단계(클로드와 비슷한 처리량 추가 소요)와 요금제 한도 대기(걸리면 10분 단위 지연)는 포함하지 않았다. 실제 완료는 이보다 늦을 수 있다. 진행 파일 갱신 여부는 `data/keywords/rescore_progress_<브랜드>.json`로 계속 확인할 수 있다.
