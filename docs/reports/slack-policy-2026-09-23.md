# 슬랙 메시지 정책 재설계 + 자유 대화 명령 (2026-09-23)

작성 시각: 2026-09-23 18:11 (KST, `date` 실측)

## 1. 배경

사용자 지시(2026-09-23 17:45): "슬랙 메시지가 중간 과정까지 잡다하게 와서 혼란스럽다.
진짜 실패했을 때, 진짜 최종 보고만 보내라. 그리고 슬랙에서 나(사용자)와 채팅하듯
자유롭게 명령하고 싶다." 작업 중 추가 지시: "명령을 해석하지 못했습니다" 류 문구를
채널로 절대 보내지 않고, 문법에 안 맞는 말은 전부 자유 대화 해석기로 넘기며
해석기까지 실패하면 되묻기 1건으로 대체한다.

## 2. 새 정책 표

| 종류 | 언제 | 아이콘 |
|---|---|---|
| 🔴 진짜 실패 | (a) 발행 작업 종료 후 **끊긴 작업 점검**까지 거친 뒤에도 남은 확정 실패 1건 이상, (b) 실행기/사이드카가 죽고 자동 재시작도 실패, (c) 예약 발사가 안 됐고 자동 복구(재시도)도 안 됨, (d) 로그인이 백업 복구까지 실패해 정말 풀림, (e) 요금제 한도로 생성 중단, (f) 계정 등록 제한 5건 이상. 사건당 1회 + 해결 시 "✅ 해결" 1회. 자동 진단·재시도·"계속됨 N초째" 문구는 보내지 않는다 |
| 📊 정기 보고 | 09:00 시작 현황판, 12/15/18시 중간 보고, 02:30 일일 보고 |
| 💬 명령 응답 | 채널에서 보낸 명령의 최종 결과 1건. 접수 답장은 없앰. 단, 긴 작업(`config/notify.yaml` `long_job_tasks`)은 "접수 — 약 N분 예상" 1줄 허용 |
| 🟢 자유 대화 답변 | 자유 대화 해석기의 답변/되묻기 |

## 3. 구현

### 3-1. `config/notify.yaml`
- `critical_categories`: 채널로 실제로 나갈 critical 범주 접두어 목록 (a)~(f) 대응.
- `long_job_tasks`: 접수 1줄을 허용할 작업 → 예상 분.
- 기존 `critical_repeat_seconds`, `category_icons`, `push_channels`는 유지.

### 3-2. `v2r/channels/__init__.py`
- `_critical_allowed(category)` 추가: `critical_categories` 접두어 일치만 통과, 그 외는
  `summary`로 강등(로그만, 채널 0건).
- 에스컬레이션("계속됨 N초째") 로직 제거 — `CRITICAL_ESCALATE_SECONDS`, `escalated` 상태 삭제.
- `category`가 `:recovered`로 끝나면 🔴 대신 ✅ 를 붙인다(`_icon_prefix(recovered=True)`).

### 3-3. `v2r/engine/monitor.py`, `v2r/engine/sidecar.py`, `v2r/engine/schedule.py`
- 코드 변경 없음 — `_alert`/watchdog이 쓰는 category(`monitor_alert:*`, `schedule_retry:*`
  등)가 새 허용 목록 밖이라 **자동으로** 강등된다(설계상 허용 목록 방식으로 전수 처리).
- `schedule_recovery_failed`(자동 복구까지 실패)만 허용 목록에 새로 추가.

### 3-4. `v2r/engine/worker.py`
- `poll_channels`: 접수 답장("작업 N 진행")과 `ok:false` 오류 문구를 없애고
  `v2r.channels.freeform.handle_channel_message`로 전량 위임.
- `_finish_run`: `reconcile` 작업에서 `result["failed"] > 0`이면 그제야
  `publish_failed_confirmed` critical 1건을 보낸다(점검 후 확정 실패만).

### 3-5. `v2r/channels/freeform.py` (신규)
- `interpret(rt, text)`: `LLMRouter.complete("freeform_command", ...)` 호출(요금제 길,
  Sonnet, `MODELS["freeform_command"]`, 0원) → JSON 파싱 → `{action, text, confirm}`.
- `handle_channel_message(rt, channel, chat_id, text)`:
  1. 확인 대기(`data/freeform_state.json`, 10분 만료) 응답이면 실행/취소.
  2. 기존 문법(`parse_korean_command`)에 맞으면 모델 호출 없이 바로 실행.
  3. 안 맞으면 모델 해석 → `answer`(🟢 답변) / `ask`(🟢 되묻기) / `command`(문법 재검증
     후 실행, `confirm:true`면 먼저 "실행할까요?" 되묻고 다음 메시지가 "네/응/ㅇㅇ/진행"류
     이면 실행).
  4. 모델 실패·JSON 실패·되물어도 문법에 안 맞으면 "잠깐 못 알아들었어요. 예: …" 1줄로
     대체 — "명령을 해석하지 못했습니다" 문구는 어떤 경로로도 채널에 안 나간다.
  5. `long_job_tasks`에 있는 작업이면 "접수 — 약 N분 예상" 1줄, 아니면 조용히 실행하고
     끝나면 `reply_to_origin`이 결과 1건을 보낸다(기존 메커니즘 그대로 재사용).
- `v2r/llm/router.py`에 `MODELS["freeform_command"] = "claude-sonnet-5"` 추가.

## 4. `notify_all` 호출 전수표

grep으로 `v2r/**/*.py`의 모든 `notify_all(` 호출을 확인했다(`v2r/channels/__init__.py`의
정의부 제외, 총 47곳). 새 허용 목록(`critical_categories`) 도입 이전과 이후 실제
동작 변화만 표로 남긴다 — `level` 인자가 없거나 `info`/`summary`인 호출(약 30곳,
작업 시작/진행/마무리 한 줄 등)은 원래도 채널로 안 나가 변화 없음.

| 파일:줄 | 지금 등급/범주 | 새 등급/범주 | 이유 |
|---|---|---|---|
| `v2r/engine/monitor.py:189` (`_alert`, 시작 지연/정체/실패 진단) | critical, `monitor_alert:*` | **강등**(summary, 로그만) | 정책표 (a)~(f) 밖의 일반 자동 진단 — "자동 진단·재등록 안내는 채널 금지" |
| `v2r/engine/sidecar.py:200` (심장박동 낡음) | critical, `sidecar_heartbeat_stale` | 유지 | (b) 실행기/사이드카 죽음 |
| `v2r/engine/sidecar.py:220` (심장박동 복구) | critical, `sidecar_heartbeat_stale:recovered` | 유지 + ✅ 아이콘 | 사건당 복구 1회 |
| `v2r/engine/schedule.py:305` (`fire`, expect_task 불일치) | critical, `schedule_mismatch:*` | 유지 | (c) 예약이 다른 작업으로 잘못 해석돼 발사를 막음 |
| `v2r/engine/schedule.py:371` (watchdog 첫 재시도) | critical, `schedule_retry:*` | **강등**(로그만) | 중간 경과("자동 재시도") — 아직 실패 확정 아님 |
| `v2r/engine/schedule.py:396` (watchdog 최종 실패) | critical, `schedule_recovery_failed:*` | 유지(신규로 허용 목록에 추가) | (c) 자동 복구까지 실패 |
| `v2r/engine/worker.py:352,955` (ChatGPT 로그인 필요) | critical, `gpt_login_pending` | 유지 | (d) 로그인 풀림 |
| `v2r/engine/worker.py:957` (ChatGPT 이미지 한도) | critical, `gpt_image_quota` | 유지 | (e) 요금제 한도 |
| `v2r/engine/worker.py:1012,1024,1042` (네이버/웹 재로그인 필요) | critical, `relogin_needed*` | 유지 | (d) 로그인 풀림(백업 복구 실패 확인 후 — 세션 점검 함수가 이미 내부에서 백업 복구를 먼저 시도) |
| `v2r/engine/worker.py:1029` (네이버 세션 "경고") | critical, `naver_session_warning` | **강등**(로그만) | 완전히 풀린 게 아니라 경고 수준 — (d)에 안 듦 |
| `v2r/engine/worker.py:1692` (사진 부족) | critical, `photo_missing` | **강등**(로그만) | (a)~(f) 밖 — 사진 부족은 `publish.py:1116`에서 `always`/사용자 승인 요청으로 별도 안내 |
| `v2r/engine/worker.py:2904` (사이드카 재시작 한계) | critical, `sidecar_restart_exhausted` | 유지 | (b) 자동 재시작도 실패 |
| `v2r/engine/worker.py:2941` (사이드카 스레드 재시작 성공) | critical, `sidecar_died` | **강등**(로그만) | 자동 복구에 성공한 중간 경과 — (b)는 "재시작도 실패"만 해당 |
| `v2r/engine/reconcile.py:321` (등록 제한 다발) | critical, `register_limit_spike` | 유지 | (f) 계정 등록 제한 5건 이상 |
| `v2r/engine/worker.py:2599` (`_finish_run`, 개별 작업 완료/실패) | summary, `publish` | 유지 | 정기 보고에만 (기존대로) |
| `v2r/engine/worker.py:2603`(신규) (`reconcile` 확정 실패) | 없음(신규) | critical, `publish_failed_confirmed` | (a) 발행 실패 — 점검 뒤 확정 실패만 |
| `v2r/engine/worker.py:220,1121,1139,1178,1188,1221,1708,983,1001,917,945` 등 (직접 명령 응답류) | always | 유지 | 사용자 직접 명령 결과 — 등급 분류 밖 |
| `v2r/engine/worker.py:poll_channels` 접수 답장 | always(구두 문구 "작업 N 진행") | **제거** | 접수 답장 정책 폐지, 긴 작업만 `freeform.py`의 "접수 — 약 N분" 1줄로 대체 |
| 그 외 `level` 생략/`info`/`summary` (진행 중 한 줄, 예약 발사, 실행기 시작 등 약 30곳) | info/summary | 변화 없음 | 원래도 채널 억제 대상 |

## 5. 자유 대화 사용 예 (5개)

1. **입력**: "지금 뭐 하고 있어?" → **해석 JSON**: `{"action":"command","text":"현황","confirm":false}`
   → **동작**: 문법 재검증 통과 → `handle_text`로 `현황` 실행, 결과는 작업 종료 시
   💬 1건으로 답장.
2. **입력**: "오늘 몇 개 나갔어?" → **해석 JSON**: `{"action":"answer","text":"오늘 12건
   나갔습니다"}` → **동작**: 즉시 "🟢 오늘 12건 나갔습니다" 1줄 답장(작업 큐에 안 들어감).
3. **입력**: "우아덤 원고 많이 좀 만들어줘" → **해석 JSON**: `{"action":"command",
   "text":"우아덤 대량 원고 3건","confirm":false}` → **동작**: `long_job_tasks`에 없으면
   조용히 실행, 끝나면 결과 1건.
4. **입력**: "이제 그만해 줘" → **해석 JSON**: `{"action":"command","text":"중지",
   "confirm":true}` → **동작**: "'중지' — 실행할까요? '네'라고 답하면 진행합니다."
   되묻기 → 사용자가 "네" → 그제야 중지 실행(10분 안에 응답 없으면 상태 만료, 다음
   메시지는 새로 해석).
5. **입력**: 모델이 오류를 내거나 애매한 말("음... 그거 있잖아") → **해석 JSON**: 없음/파싱
   실패 또는 `{"action":"ask", ...}` → **동작**: "잠깐 못 알아들었어요. 예: 현황 / 우아덤
   대량 원고 3건 / 오늘 글 몇 개 나갔어?" 되묻기 1건(🟢). "명령을 해석하지 못했습니다"
   문구는 어떤 경로로도 나가지 않는다(테스트로 확인).

## 6. 한계

- 모델(`freeform_command`)이 실제로 허용 명령 밖의 동작을 만들어낼 가능성은 시스템
  프롬프트로만 막는다(코드 차원에서는 `parse_korean_command` 재검증이 최종 방어선 —
  문법에 안 맞으면 실행하지 않고 되묻기로 대체한다).
- `long_job_tasks`의 예상 분(N)은 고정값(설정 파일)이라 실측과 다를 수 있다.
- 확인 대기 상태(`data/freeform_state.json`)는 파일 기반이라 여러 실행기 프로세스가
  동시에 도는 경우를 상정하지 않았다(현재 구조상 실행기는 1개).
- `naver_session_warning`/`photo_missing`/`sidecar_died`(재시작 성공)는 이번에
  critical에서 강등했다 — 운영 중 이 판단이 틀렸다면 `config/notify.yaml`
  `critical_categories`에 추가하면 코드 수정 없이 바로 반영된다.

## 7. 테스트 결과

```
tests/test_channels.py ..........................  26 passed
tests/test_monitor.py + tests/test_schedule.py .....................................................................  67 passed
tests/test_freeform.py (신규) ...............  15 passed
tests/test_notify_policy.py (신규) .......  7 passed
tests/test_engine.py, tests/test_parser.py  전부 통과
```

전체 `pytest tests/`(1397개)를 끝까지 돌렸다(약 30분). 처음 통과분 이후
접수 답장 제거로 어긋난 기존 테스트 3개를 추가로 찾아 새 정책에 맞게 고쳤다:

- `tests/test_cleanup_orphans.py::test_serve는_명령을_보낸_방에_답한다` — "접수+완료"
  2건을 기대하던 것을 "완료 1건"으로.
- `tests/test_cleanup_orphans.py::test_serve는_해석_실패를_그_방에_알린다` — "해석"
  문구 기대를 "못 알아들었어요"(자유 대화 되묻기)로, "해석하지 못했습니다"가
  안 나가는지도 같이 확인.
- `tests/test_sidecar.py::test_틱이_예약과_감시와_수신과_가벼운_작업을_모두_돌린다` —
  "접수+결과" 2건 기대를 "결과 1건"으로.

최종 결과: `1397 passed`(제외/스킵 없음).

## 8. 적용 시점

이 코드는 저장소에 커밋만 하고 실행기(`serve`)는 재시작하지 않았다(규칙: 실행기
재시작 금지). **다음 실행기 재시작 때부터** 새 정책이 적용된다.
