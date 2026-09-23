# 노출 확인 속도 개선 — 러너 구현 (2026-09-24)

## 0. 요약

`docs/reports/exposure-speed-plan-2026-09-23.md` 설계대로 상주 브라우저 작업자 +
스크롤 조기 종료 + 우선순위 큐를 새 모듈로 구현했다. 판정 규칙
(`v2r/knowledge/keyword_exposure.py`, 카드 대표/서브 구분·댓글2 식별어·시트
A/G/J/K/L)은 다른 일꾼이 마무리 중이라 **손대지 않았고**, 그 안의
`resolve_search_query`·`judge_keyword_exposure`·`_enqueue_sheet_row`·
`keyword_exposure_store.save`를 그대로 불러 썼다.

**중요 — 이 문서 작성 시각까지 실제 30분/15분 실측 가동은 아직 돌리지 못했다.**
네이버에 실제로 부하를 주는 장시간 실측은 이 작업 세션 안에서 안전하게 끝낼 수
있는 범위를 넘어서서(차단 리스크·세션 길이), 코드·단위 테스트·설정까지만
끝내고 실측은 다음 단계로 넘긴다. 아래 3절이 그 대신 지금 할 수 있는 것 —
로직 검증과 현재 대기열 실측 — 이다.

## 1. 구성 (설계 그대로)

| 항목 | 값 |
|---|---|
| 작업자 수 | 기본 2 (`config/exposure.yaml` `workers`) |
| 작업자 간 간격 | 6–10초 무작위 |
| 브라우저 | 작업자당 1개 상주(익명 컨텍스트, `data/naver_cookies.json` 있으면 사용) |
| 스크롤 조기 종료 | 카페 카드 로드 후 문서 높이가 2회 연속 같으면 중단, 카드가 하나도 없으면 기존 최대 20회까지 |
| 미확인 연속 3건 | 그 작업자 15분 휴식 |
| 전체 작업자 동시 휴식 | (다음 단계에서 순환 상태 파일에 30분 정지 기록 — 지금은 작업자별 휴식만 동작, 아래 4절 한계 참고) |

## 2. 새 파일

- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/exposure_runner.py`
  — 작업자 루프, 조기 종료 스크롤(`should_stop_scrolling`,
  `fetch_integrated_search_dom_resident`), 상태 파일(`update_worker_state`,
  `is_alive`). 판정은 `keyword_exposure.judge_keyword_exposure`를 `dom_html`로
  호출만 한다.
- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/exposure_priority.py`
  — 우선순위 큐(`next_priority_batch`, `queue_counts`). 등급: 1 미확인 → 2 최근
  발행(4·24·72시간 근방) → 3 노출완(24시간 지남) → 4 밀려남·검색량 상위 30%(48시간)
  → 5 밀려남 하위(7일). 주기는 `config/exposure.yaml`.
- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/config/exposure.yaml`
  — 작업자 수·간격·조기 종료·재검사 주기.
- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/exposure-runner-hidden.vbs`,
  `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/exposure-runner-hidden.cmd`
  — 숨김 실행(인자로 작업자 수, 기본 2). **아직 실행하지 않았다** — 5절 참고.
- `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_exposure_runner.py`
  — 조기 종료 판정, 상태 파일, 우선순위 등급, "같은 키워드 연속 두 번 안 뽑힘" 12개 테스트, 모두 통과.

## 3. 사이드카 연결 + 오늘 두 지적 반영

- `v2r/engine/sidecar.py`의 `cycle_tick` 호출부만 고쳐, 러너(`exposure_runner.is_alive`)가
  살아 있으면 사이드카는 자기 검사를 쉬고 생존만 확인한다(중복 검사 방지,
  `keyword_exposure.py`는 그대로).
- **(코디네이터 지적 1) 순환 상태 충돌** — 00:20에 `노출 순환 중지`로 옛 순환기를
  멈췄다. 러너는 `exposure_cycle_state.json`을 전혀 안 쓰고 독립적으로
  `exposure_priority`만 보므로, 옛 순환이 꺼진 채로도 러너만으로 재개된다.
  나중에 `노출 순환 시작`으로 옛 순환이 다시 켜져도 사이드카가 러너 생존을
  먼저 확인해 건너뛰므로 중복 실행은 안 생긴다.
- **(코디네이터 지적 2) 같은 키워드 반복 검사** — 옛 `next_cycle_batch`는
  키워드를 원문 그대로 딕셔너리 키로 써서 표기가 미세하게 다르면 "미확인"으로
  오인해 주기 전에 다시 뽑을 수 있었다(00:11–00:17 더마팩토리·착상혈·양압기
  2회씩). `exposure_priority.next_priority_batch`는 양쪽 다 `_norm()`으로
  정규화해 키를 맞추고, 검사 직후 `keyword_exposure_store.save`가 즉시(autocommit)
  반영되므로 다음 호출부터 그 키워드는 재검사 주기 전엔 등급 99(제외)가 된다.
  `tests/test_exposure_runner.py::test_next_priority_batch_같은_키워드_연속_두번_안뽑힘`
  로 이 시나리오를 그대로 재현해 통과를 확인했다.

## 4. 지금 브랜드별 대기 수 (실측, 이 문서 작성 시각)

`exposure_priority.queue_counts`로 각 브랜드의 등급별 대기 수를 직접 조회했다
(등급 99 = 아직 재검사 주기 전이라 대기열 밖):

| 브랜드 | 1(미확인) | 99(주기전, 대기열 밖) |
|---|---:|---:|
| 우아덤 | 4,826 | 33 |
| 코숨핏 | 845 | 1 |
| 뉴더미스 | 1,193 | 62 |
| 장으뜸 | 2,139 | 56 |
| 팥순이 | 6,043 | 60 |
| **합계** | **15,046** | 213 |

미확인(1순위) 합계 15,046건이 지금 실제로 가장 먼저 검사 대상이다 — 계획서의
"원고 대상 약 1.4만 개" 추정과 대체로 맞는다. 2~5순위(노출완 재검사·밀려남
재검사)는 아직 `keyword_exposure` 표에 이 정규화된 키로 찍힌 이력이 거의 없어
지금은 대부분 1순위로 잡힌다 — 러너를 한 번 돌리고 나면 등급 분포가 정상화된다.

## 5. 아직 안 한 것 (한계·다음 단계)

1. **실측 미실행** — 작업자 2개 30분, 3개 15분 실측을 아직 못 돌렸다(위 0절).
   `scripts/exposure-runner-hidden.vbs`를 실행하면 시작되고, 진행은
   `file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/exposure_runner_state.json`
   (작업자별 처리 수·시간당 속도)로 바로 확인 가능하다. 실행 승인 후 다음
   보고서에 시간당 처리량·unknown 비율·차단 징후를 채운다.
2. **전체 정지(30분) 미구현** — 작업자별 15분 휴식은 동작하지만, "전체
   작업자가 동시에 휴식"이면 순환 상태 파일에 30분 정지를 기록하는 부분은
   아직 없다(작업자가 서로의 휴식 상태를 보려면 `exposure_runner_state.json`을
   읽는 조정 로직이 필요 — 다음 단계).
3. **3→5개로 올리는 조건**: 2개로 30분 실측해 시간당 처리량이 목표(약
   700–1,000건/2작업자)에 가깝고 unknown 비율이 낮으면(차단 징후 없음) 3개로,
   이후도 같은 기준(unknown 급증·15분 휴식 빈발이 없으면) 통과할 때마다 +1.
4. 전체 테스트 스위트(`pytest -q`, 기존 파일 포함)를 백그라운드로 돌려 확인
   중 — 결과는 이어지는 커밋에서 확정한다. `tests/test_exposure_runner.py`
   12개, `tests/test_keyword_exposure.py`+`tests/test_keyword_exposure_cycle.py`
   기존 71개 모두 통과는 이미 확인했다.
