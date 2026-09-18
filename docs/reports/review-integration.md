# 통합 묶음(engine) 교차 검증 보고 (읽기 전용 리뷰)

작성 2026-09-19. 대상: `v2r/engine/context.py`, `v2r/engine/publish.py`, `v2r/engine/reconcile.py`, `v2r/engine/status.py`, `v2r/engine/worker.py`, `v2r/__main__.py`, `tests/test_engine.py`.
기준 문서: `docs/DESIGN.md` §5–§6, `docs/reference/v2r-api-spec.md` §4–§5, `docs/reference/legacy-business-logic.md` §2, §5.
비(非)엔진 모듈(client/articles/sheets/parser/comments 등)은 다른 에이전트가 동시 수정 중이므로, 엔진 코드가 **사양에 맞게 호출·처리하는지**만 보았다.

검증 방법:
- 전 파일 정독 + 관련 저장소·API 모듈 시그니처 대조.
- `.venv/Scripts/python.exe -m pytest tests/test_engine.py -q` → **15 passed** (7.5s).
- 스크래치 프로브(`socket.connect`/`getaddrinfo` 차단, 임시 DB `V2R_DB_PATH`, 채널·LLM 키 제거)로 CLI 6건 실행: `상태`, `일상 글 2개 올려줘`, `상태`(재실행), `끊긴 작업 이어가`, `브랜드 글 1개 올려`, `카페 목록`. 모두 dry_run(기본값).
  - `일상 글 2개 올려줘`, `브랜드 글 1개 올려` → **Google Sheets 가져오기 시도**(DNS 차단에 걸림) → 모의 실행에서 네트워크 호출 발생 (M-1).
  - 두 번째 `상태` → "작업 1 등록됨" 후 **"실행할 작업이 없습니다."** (M-3).
  - 원본 전부 적재 실패인데 작업이 **완료**로 종료, rc=0 (M-4).
  - `카페 목록` → V2R API 호출 시도(조회 작업이므로 허용 범위이나 dry_run 플래그와 무관함을 기록).

집계: **치명 2 / 중요 9 / 경미 10 / 확인됨 11**.

---

## 치명 (반드시 수정)

### C-1. 제휴 체인 중간 실패 후 reconcile이 **일상 글을 원고의 완료로 오판**
- 위치: `v2r/engine/publish.py:625` (`mark(*key, "uncertain", "daily_done", source_id=daily_id)`), `publish.py:629-644` (수정글 등록·검증), `v2r/engine/reconcile.py:80-102` (source_id만 보고 done/failed 판정, `stage` 무시), `reconcile.py:15` (`DONE_STATUSES`에 `RESERVED` 포함).
- 문제: 제휴 흐름은 원고 1건(key)에 대해 일상 글(daily) → 수정글(revision) 두 개의 V2R source를 만든다. daily 성공 후 revision `create_article`/`verify_article`이 실패하면 publications 행은 `uncertain / stage=revision_submitting / source_id=<daily_id>`로 남는다. 이후 `reconcile()`은 `source_id`만 보고 `GET article`을 호출하고, 예약 상태 `RESERVED`를 `DONE_STATUSES`로 취급하여 **원고를 `done`으로 확정하고 URL도 일상 글 URL로 기록**한다. 실제로는 홍보 원고(수정글)는 올라가지 않았고(또는 검증 실패한 채로 서버에 고아 상태), 시트 행은 영구히 "발행됨"으로 잠긴다. legacy §5 "결과 URL = 수정글", api-spec §4 "REVISION_CREATED → VERIFIED … 중간 실패 시 생성 source 삭제 후 되감기"에 어긋난다.
- 수정:
  1. `reconcile()`에서 `pub["stage"]`를 분기: `stage in {"daily_done", "revision_submitting"}`이면 `source_id`는 **부모**로 취급. `board_histories(cafe_id, days_ago=30)`에서 `parent_source_id == daily_id`인 행을 찾아 그 상태로 판정하고, 없으면 `unresolved`(자동 재발행 금지 유지).
  2. `publish.py`에서 daily와 revision의 source_id를 분리 저장: `publications`에 `parent_source_id` 열을 추가(`store/publications.py _FIELDS`)하거나, 최소한 `mark(..., stage="revision_submitting", source_id=None)` 전에 `daily_id`를 stage 문자열이 아닌 별도 열로 남긴다.
  3. 즉시 발행이 아닌 예약 건(`scheduled_at` 있음)은 `RESERVED`를 done으로 보되, 즉시 발행 건(`scheduled_at` 없음)은 DESIGN §6 "즉시 발행이면 `status ∈ {DONE,SUCCESS}`"에 따라 `RESERVED`를 미확정으로 남긴다.
  4. 테스트: `stage=revision_submitting, source_id=daily` 행 + `GET article` → `RESERVED` 응답일 때 `result["unresolved"]`에 들어가고 `status`가 `uncertain`으로 유지되는지.

### C-2. 등록 직후 `source_id`를 보존하지 않아, 검증 실패 건이 **제목 없는 계정만 매칭**으로 잘못 확정될 수 있음
- 위치: `v2r/engine/publish.py:541-566` (`_create_and_verify`: create → get → verify 후에야 반환, 중간에 `publications.mark` 없음), `publish.py:657-669` (자사 흐름은 `done` 표시 전까지 source_id 저장 지점이 전무), `v2r/engine/reconcile.py:116-125` (`title = _title_for(...)`; `if title and field(row,"title") != title: continue` → **title이 빈 문자열이면 계정만 같으면 첫 행 채택**), `reconcile.py:52-67` (`_title_for`는 `sources_cache`에 의존 → `명령첨부`(spec.manuscripts) 원고는 항상 `""`).
- 문제: `verify_article` 불일치, `wait_written` 타임아웃/`FAIL`, `PendingError` 등 **글은 이미 서버에 생겼는데** 실패로 빠지는 모든 경로에서 publications 행은 `uncertain`이고 `source_id=NULL`이다(테스트 `test_검증_불일치면_uncertain으로_남는다`가 이 상태를 정상으로 고정). reconcile은 `board_histories` 폴백으로 넘어가는데, 캐시에 없는 원고(명령에 첨부한 원고, 캐시 만료/갱신으로 행 번호가 밀린 시트)는 title이 `""`가 되어 **같은 계정이 최근 7일 안에 올린 아무 글**을 그 원고의 결과로 `done` 처리한다. 잘못된 URL이 기록되고 진짜 글은 고아가 된다.
- 수정:
  1. `_create_and_verify`에 `on_created: Callable[[str], None]` 인자를 추가하고 `create_article` 반환 직후 `rt.publications.mark(*key, "uncertain", "<stage>_created", source_id=source_id)`를 호출. 자사/제휴 두 호출 지점(`publish.py:612`, `:630`, `:657`) 모두 전달.
  2. `reconcile.py:116-125`: `title`이 비면 **매칭을 시도하지 말고** `unresolved`로 넘긴다(`if not title: result["unresolved"].append(label); continue`). 추가로 `created_at >= pub["created_at"] - 15s` 조건을 넣어 api-spec §4 복구 규칙과 맞춘다.
  3. `publications` 행에 `title` 열을 추가하여 캐시 없이도 매칭 가능하게 하는 것이 근본 해결.
  4. 테스트: 검증 실패 후 `by_source_id("SRC-2")`가 `uncertain` 행을 반환하는지; 제목 미상 + 계정 일치 행이 있을 때 reconcile이 done으로 바꾸지 않는지.

---

## 중요

### M-1. 모의 실행(dry_run)이 Google Sheets·계정 시트를 **네트워크로 가져옴**
- 위치: `v2r/engine/publish.py:136-141` (`load_manuscripts` → `sheets.load_source`), `publish.py:224-245` (`load_accounts` → `sheets.load_source`), `worker.py:179,189` (dry_run 분기 없이 호출). `sheets.load_source`(`v2r/sources/sheets.py:212-231`)는 **항상 먼저 fetch**하고 실패 시에만 캐시를 쓴다.
- 재현: 프로브 `일상 글 2개 올려줘`(dry_run) → `시트 가져오기 실패: DNS BLOCKED`. 즉 정상 환경이면 매 모의 실행마다 시트 2~3건 HTTP GET.
- 문제: 요구사항 "dry_run은 네트워크/브라우저 호출 0건" 위반. 시트 읽기는 부작용이 없어 위험도는 낮지만, 사무실 밖·오프라인·시트 권한 문제에서 모의 실행이 통째로 실패하며(M-4와 결합하면 `완료`로 보고됨), `sources_cache`가 있어도 쓰지 않는다.
- 수정: `Runtime`에 `offline: bool` 또는 `prefer_cache: bool`을 두고, `spec.dry_run`이면 `_cache_hooks`에서 `cache_get` 결과가 있을 때 `sheets.load_source`를 건너뛰는 래퍼(`load_manuscripts(rt, entry, prefer_cache=spec.dry_run)`)를 쓴다. 캐시가 없으면 "캐시 없음 — `시트 동기화` 먼저 실행" 메시지로 skipped 처리. `load_accounts`도 동일.

### M-2. 작업 상태 판정이 **DB 전체의 uncertain 건**을 이번 작업 결과로 취급
- 위치: `v2r/engine/worker.py:224-226` (`left_uncertain = ... rt.publications.list_uncertain()` — job 무관 전건), `worker.py:306-308` (`if result.get("uncertain"): status = "uncertain"` 이 `ok is False`보다 우선).
- 문제: 과거 `uncertain` 1건만 남아 있어도 이후 **모든 발행 작업(모의 실행 포함)** 이 `불확실`로 종료·보고된다. 반대로 실제 실패(`failures` 있음)도 `uncertain`으로 덮여 채널에 "작업 N 불확실: … (수동 확인 필요)"만 가고 실패 사유가 사라진다(`finish(..., error=None)`).
- 수정: `_run_publish`에서 이번 작업이 표시한 key 집합을 모아(`touched: set[tuple]`) 그 중 아직 `uncertain`인 것만 `result["uncertain"]`에 넣는다. `run_once`는 `failures`와 `uncertain`이 함께 있으면 `failed`(error에 두 목록 모두)로, `uncertain`만 있으면 `uncertain`으로 판정. 기존 전체 uncertain 건수는 `status_report`가 이미 보여준다.

### M-3. 멱등 키가 **하루 단위로 같은 문장을 영구 차단** → `상태`를 하루 두 번 못 봄
- 위치: `v2r/engine/worker.py:27-29` (`idem_key = sha256(text + today)`), `worker.py:57` (`jobs.enqueue` → 기존 id 반환), `v2r/store/jobs.py:31-37` (상태 불문 조회), `v2r/__main__.py:52-57`.
- 재현: 프로브 `상태` 2회 → 두 번째는 "작업 1 등록됨" 후 "실행할 작업이 없습니다." 조회·점검·중지 명령 모두 동일. 채널(`serve`)에서도 같은 문장은 하루 1회만 동작.
- 문제: DESIGN §5-1의 idem_key는 **중복 발행 방지**가 목적이다. 조회성 작업(`status`, `inspect_failures`, `reconcile`, `catalog`, `stop`)과 이미 종료된 작업에까지 적용되면 사용자가 "고장"으로 인식한다. `stop`이 하루 1회만 먹는 것은 안전 문제.
- 수정: (a) `enqueue`가 `status IN ('queued','running')`인 동일 키만 재사용하고 종료된 키는 새로 등록하도록 하되(`jobs.idem_key UNIQUE` 제약은 `(idem_key, id)` 또는 해시에 시퀀스 포함으로 완화), (b) 발행 작업(`PUBLISH_TASKS`)에 한해서만 날짜 키를 적용하고 그 외 작업은 `idem_key = sha(text + now_iso())`로 항상 새 작업. 최소한 `stop`/`status`는 즉시 실행되어야 한다.

### M-4. 원본 적재가 **전부 실패**해도 작업이 `완료`(ok=True)
- 위치: `v2r/engine/worker.py:180-187` (`if not manuscripts: return {"ok": True, ..., "message": "발행할 원고가 없습니다"}`), `publish.py:194-197` (적재 예외를 skipped로 흡수).
- 재현: 프로브 `일상 글 2개 올려줘` → 시트 실패 → "작업 2 완료 … 발행할 원고가 없습니다", rc=0. 채널에는 "작업 2 완료: 일상 글 발행…"으로 간다.
- 문제: 네트워크·권한 장애가 성공으로 보고된다(요구 (10) "성공으로 보고되는 실패 경로").
- 수정: `prepare_manuscripts`가 시도한 원본 수와 실패 수를 돌려주거나, `skipped`에 `reason.startswith("원본 적재 실패")` 항목이 있고 선택된 원고가 0이면 `ok=False, failures=[...]`로 반환. 시트가 정상인데 정말 남은 행이 없을 때만 `ok=True`.

### M-5. 실행기 리스 900초 vs. 발행 작업 수십 분 — **heartbeat 미호출, 고아 running 미복구**
- 위치: `v2r/store/jobs.py:75,97` (`acquire(lease_seconds=900)`, `heartbeat()` 존재), `v2r/engine/worker.py:201-217` (`_run_publish` 슬롯 루프에서 heartbeat 없음), `worker.py:287-319` (`run_once`에 owner/heartbeat 배선 없음), `publish.py:564-565` (`wait_written` 기본 `timeout_after_sched=1800s`), `jobs.py:79-81` (`queued`만 집음 → 프로세스가 죽은 `running` 작업은 영구 잔류).
- 문제: 즉시 발행 1건이 최대 30분 대기하므로 슬롯 몇 개면 리스 만료. 다른 PC의 `serve`가 리스를 가져가 **두 실행기가 동시에 발행**(전역 단일 실행기 원칙, legacy §2 위반). 또 크래시 후 재시작하면 `running` 작업은 다시 실행되지도 정리되지도 않고 `status`에 "실행중"으로 영원히 남는다.
- 수정: `_run_publish(rt, job_id, spec, owner)`에 owner를 넘겨 각 슬롯 시작 전 `rt.jobs.heartbeat(job_id, owner)`; 실패(False)면 리스 상실로 간주하고 남은 슬롯을 중단(`PublishError("리스 상실")`). `run_once` 진입 시 `lease_until < now`인 `running` 작업을 `uncertain`(결과 없음, error="실행기 중단")으로 정리하는 `jobs.reap_stale()` 추가. `wait_written` 호출에 `max_wait_s`를 리스보다 짧게 두고, `PendingError`는 실패가 아닌 "예약 확인 보류"로 처리(M-7).

### M-6. 계정 제한(27000)·확정 실패 건이 `uncertain`으로 남아 **다른 계정 재시도가 영구 불가**
- 위치: `v2r/engine/publish.py:671-679` (`account_restricted` → `RetryWithOtherAccount`만 던짐, publications는 `uncertain/daily_submitting` 유지), `worker.py:205-206` (예외를 `failures`에 넣고 끝), `publications.py:10,28-35` (`exists`가 `uncertain`을 발행됨으로 간주).
- 문제: 27000·33007(등급)·no_membership·consecutive_limit 등은 서버가 **요청을 거부**한 것으로 글이 생기지 않는다(`create_article`은 ambiguous kind에서만 이력 복구). 그런데 행은 `uncertain`으로 남아 `exists()`가 참 → 다음 실행에서 "이미 발행됨"으로 건너뛴다. reconcile도 source_id 없음 + 이력 없음 → `unresolved`. 결과적으로 DESIGN §5-5 "계정 제한은 다른 계정으로"가 불가능하고 사람이 DB를 고쳐야 한다. 무한 루프·중복 발행은 없다(확인됨 V-5).
- 수정: `classify()` 결과가 **확정 거부**(`account_restricted, grade, no_membership, consecutive_limit, not_login`)이고 create 응답 전이면 `rt.publications.mark(*key, "failed", stage)`로 내린 뒤 예외를 올린다(제휴 흐름에서 daily는 성공했고 revision이 거부된 경우는 C-1 처리와 함께 `uncertain` 유지). `_run_publish`는 `RetryWithOtherAccount`를 받으면 `restricted` 집합을 갱신해 그 슬롯만 `plan()`으로 재배정하고 **최대 1회** 재시도(`retried: set[int]`로 가드).

### M-7. `wait_written`의 `PendingError`/타임아웃을 실패로 처리
- 위치: `v2r/engine/publish.py:564-565` (`wait_written(rt.client, source_id)` — `scheduled_at` 미전달), `publish.py:682-684` (`except Exception` → `PublishError`), `v2r/api/articles.py:280-337`.
- 문제: 즉시 발행 건에서 `wait_written`은 최대 30분 폴링 후 `V2RApiError("등록 확인 시간 초과")`를 던지고, 엔진은 이를 "발행 실패"로 채널에 보고한다. 글은 서버에 존재하며 곧 DONE이 될 수 있다. 또 `scheduled_at`을 넘기지 않아 예약 건에는 호출 자체가 없어 정상이지만, `start_at is None` 판정만으로 즉시/예약을 나누므로 테스트 카페 즉시 발행 다수 건이 직렬로 30분씩 대기할 수 있다(M-5와 결합).
- 수정: `wait_written(..., max_wait_s=120)`을 명시하고 `PendingError`·시간 초과는 `PublishError`가 아닌 **"확정 보류"** 로 분류하여 행을 `uncertain/stage=written_pending, source_id=...`로 두고 결과 `status="pending"`으로 돌려준다. 채널 보고는 "예약 확인 대기(N건)"으로.

### M-8. reconcile이 **stage/source_id 없는 행을 계정+제목만으로 확정**하며 이력 조회 기간이 사양과 다름
- 위치: `v2r/engine/reconcile.py:110` (`days_ago=7`; api-spec §6은 30일·5페이지), `reconcile.py:118-125` (created_at·parent_source_id 조건 없음), `reconcile.py:130-143`.
- 문제: 같은 계정이 같은 제목으로 예전에 올린 글(재발행·유사 원고)이 있으면 그 글을 이번 원고의 결과로 채택한다. 제휴 흐름은 daily 제목이 아니라 원고 제목으로 찾으므로 `stage=daily_submitting` 건은 절대 못 찾고 `unresolved`(안전하지만 사람이 매번 확인).
- 수정: `created_at >= pub.created_at - 15s` 조건, `parent_source_id`(제휴 수정글) 조건, `days_ago=30`을 적용. 후보가 2건 이상이면 `unresolved`.

### M-9. `stop`이 큐에 들어가 **앞 작업이 끝나야 실행**됨
- 위치: `v2r/engine/worker.py:275-277` (`stop`도 일반 작업으로 dispatch), `worker.py:333-360` (`serve`: poll → 전부 enqueue → `drain`), `__main__.py:98`.
- 문제: 발행 작업이 진행 중일 때 채널에서 "중지"를 보내면 `stop`은 뒤에 큐잉되고, 현재 발행이 모두 끝난 뒤에야 실행되어 이미 끝난 작업들을 "취소"한다. 실질적으로 진행 중 발행을 멈출 수 없다.
- 수정: `handle_text`에서 `spec.task == "stop"`이면 큐에 넣지 않고 즉시 `rt.jobs.cancel_open(owner)` + `rt.scratch["stop_requested"]=True`를 세팅. `_run_publish` 슬롯 루프는 매 슬롯 전에 `rt.jobs.get(job_id)["status"] == "cancelled"`(또는 scratch 플래그)를 확인하고 남은 슬롯을 건너뛴다. `serve`는 poll 결과 중 `stop`을 먼저 처리한다.

---

## 경미

### m-1. `plan()`이 첫 원고의 카페로만 `work_type`을 정함
- 위치: `v2r/engine/publish.py:373-375` (`work_type_for(spec.task, cafes[0], ...)`).
- 문제: `publish_batch`처럼 자사·제휴 원고가 섞이면 제휴 카페 원고에 자사 계정이 배정될 수 있다(legacy §3 "카페가 제휴면 무조건 제휴").
- 수정: 카페별로 pool을 나눠 배정(`{cafe: assign(...)}`)하거나, 섞인 배치는 카페 단위로 `plan()`을 나눠 호출.

### m-2. 수동 배정에서 계정 풀이 비면 **지정 계정을 검증 없이 통과**
- 위치: `v2r/engine/publish.py:377-379` (`pool_ids = [...] or list(spec.accounts)`).
- 문제: 계정 시트 로드 실패(`load_accounts`는 예외를 삼켜 `[]`) 시 제한 중/댓글 전용/회색 음영 계정도 지정만 하면 쓰인다.
- 수정: 풀이 비면 `AssignError("계정 시트를 읽지 못해 지정 계정을 검증할 수 없습니다")`. `restricted_accounts`만은 별도로 걸러 최소 안전선 유지.

### m-3. 채널·CLI 실패 문구에 예외 문자열을 **그대로** 전달
- 위치: `v2r/engine/worker.py:300-302` (`notify_all(rt.channels, f"작업 {job_id} 실패: {exc}")`), `worker.py:206-208`, `__main__.py:20-21`, `status.py:91,117`(events.message).
- 확인 결과: 현재 경로에서 토큰·비밀번호가 문자열에 들어가는 지점은 찾지 못했다(`Settings.__repr__` 마스킹, `V2RApiError.__str__`은 message/reason만). 다만 `httpx` 예외는 요청 URL(쿼리 포함)을 담고, 향후 `AuthSession` 예외가 이메일을 담을 여지가 있다.
- 수정: `channels.format_report` 앞단에 `sanitize(text)`(길이 500자 제한, `token=`, `Bearer `, `password`, 이메일 패턴 마스킹)를 두고 `notify_all`이 항상 거친다.

### m-4. `status` 작업이 자기 자신을 "실행중"으로 표시
- 위치: `v2r/engine/status.py:36-42`, `worker.py:246-247`.
- 재현: 프로브 `상태` → "최근 작업: 1 실행중 status".
- 수정: `status_report(rt, exclude_job_id=job_id)` 또는 `dispatch`에서 status 작업은 `finish` 후 보고문을 만든다.

### m-5. 비발행 작업의 실패 사유가 `finish(error=None)`로 유실
- 위치: `v2r/engine/worker.py:311-316` (`error = "; ".join(result.get("failures") or [])`).
- 문제: `_collect_daily`는 `{"ok": False, "error": ...}`, `_sync_entries`/`reconcile`은 `errors: [...]`를 돌려주는데 모두 무시된다. `_sync_entries`, `reconcile`은 오류가 있어도 `ok: True`.
- 수정: `error = result.get("error") or "; ".join(result.get("failures") or result.get("errors") or [])`; `_sync_entries`는 `ok = not errors`.

### m-6. 자사 흐름 stage 이름이 `daily_submitting`
- 위치: `v2r/engine/publish.py:648-656`.
- 수정: `"submitting"`/`"created"`로 바꾸고 제휴 흐름만 `daily_*`/`revision_*`을 쓴다(C-1의 stage 분기 전제).

### m-7. `Runtime.client`의 무의미한 분기, `close()`가 일부 자원 미해제
- 위치: `v2r/engine/context.py:87-89` (`if ...: pass`), `context.py:66-76` (`_channels`, `_warehouse` 미초기화, `_llm_ready` 유지).
- 수정: 죽은 분기 삭제; `close()`에서 `_channels=None`, `_llm=None`, `_llm_ready=False`.

### m-8. 모의 실행에서도 DB에 `skipped` 행이 기록됨
- 위치: `v2r/engine/publish.py:205-211` (`duplicate` 판정 시 `mark(..., "skipped")`).
- 문제: dry_run 결과가 DB를 바꾼다(부작용 0 원칙). 이후 실제 실행에는 영향 없음(`exists`는 uncertain/done만 봄).
- 수정: `spec.dry_run`이면 `mark` 생략.

### m-9. `__main__._cmd_command`가 방금 등록한 작업이 아닌 **가장 오래된 queued**를 실행
- 위치: `v2r/__main__.py:57` (`worker.run_once(rt)`), `jobs.py:79-81`.
- 문제: 크래시로 남은 queued 작업이 있으면 사용자가 입력한 명령 대신 그 작업이 실행되고 결과가 뒤바뀌어 출력된다.
- 수정: `run_once(rt, job_id=accepted["job_id"])`를 지원하거나, 출력 시 `out["job_id"] != accepted["job_id"]`면 "이전에 남은 작업 N을 먼저 실행했습니다" 안내.

### m-10. 테스트 공백
- 위치: `tests/test_engine.py`.
- 누락: (a) 제휴 체인 순서(이미지 → daily create → daily_done(source_id) → revision create with `parent_source_id`, `target_view_count 80~100`, 댓글 payload) 검증, (b) `_run_publish` 수준에서 `RetryWithOtherAccount`·`uncertain` 집계, (c) worker 리스/heartbeat, (d) reconcile `stage` 분기(C-1), (e) `stop` 즉시성. `datetime` import 미사용(`tests/test_engine.py:5`).
- 수정: 위 항목별 테스트 추가. C-1/C-2 수정과 함께 `test_검증_불일치면_uncertain으로_남는다`에 `source_id` 보존 assert 추가.

---

## 확인됨 (사양대로 동작)

- **V-1. `uncertain` 선기록** — `publish.py:603-611`, `:629`, `:648-656`에서 `create_article` 호출 전에 `mark(..., "uncertain", stage, account, cafe, menu_id, scheduled_at)`. `done`은 `_create_and_verify`가 `verify_article` 통과 후 반환한 뒤에만(`publish.py:686-687`). 테스트 `test_등록_전에_uncertain으로_먼저_표시한다`가 순서를 고정.
- **V-2. 제휴 체인 순서** — `publish.py:598-644`: `catalog.resolve` → `_attach_images`(브라우저, 예약 전) → `_take_daily` → daily 등록·검증 → `revision_at` 시각으로 `build_comments` → 수정글 등록(`parent_source_id=daily_id`, `target_view_count=randint(80,100)`, `comments=payload`). api-spec §4 "이미지는 예약 생성 전에 완전히 준비", §5 "수정글 = 같은 POST에 parent_source_id" 충족. 사진 부족은 `plan()`(`pick_images`, `publish.py:324-350`)에서 예약 전 `PublishError`.
- **V-3. 모의 실행은 V2R API·브라우저 호출 0건** — `run_slot`은 `spec.dry_run`이면 `catalog.resolve` 이전에 반환(`publish.py:591-592`); `_run_publish`는 `need_browser = (not dry_run) and ...`(`worker.py:190`), 슬롯별 채널 보고도 dry_run이면 생략(`worker.py:209`). 프로브에서 V2R/브라우저 호출 없음 확인(시트 GET은 M-1).
- **V-4. 안전 기본값** — `TaskSpec.dry_run=True` 기본, 파서는 `(실제|바로)(발행|등록)` 문구만 실제로 전환, JSON 명령도 `notes`에 문구 없으면 강제 dry_run(`parser.py:172-177`). 프로브 6건 모두 "모의 실행".
- **V-5. 계정 제한 경로에 무한 루프·중복 발행 없음** — `publish.py:671-677`: `restrict(30일)` + `events.log` + 예외 1회; `worker.py:205-206`은 실패로 기록만 하고 재시도하지 않음. 행은 `uncertain`으로 남아 `exists()`가 재발행을 막음(재시도 불가는 M-6).
- **V-6. reconcile은 재발행하지 않음** — `reconcile.py` 전체에 `create_article` 호출 없음; `mark`만 수행. `test_reconcile는_uncertain을_done으로_확정`, `…삭제된_글을_failed로` 통과.
- **V-7. `ALLOWED_TASKS` 16종 모두 dispatch** — `worker.py:244-281`이 `publish_*` 4종 + 12종을 모두 분기, 미지 작업은 `ValueError`. `test_모든_허용작업에_처리기가_있다` 통과.
- **V-8. 보고문에 비밀값 미포함** — `status.py`는 login_id/카페/단계/URL만 출력, `Settings`는 `__repr__` 마스킹, `format_report`는 id·status·설명·URL만 조합. 경로 검토 결과 토큰·비밀번호 노출 지점 없음(방어 보강은 m-3).
- **V-9. 시간대 일관성** — 모든 `now`가 `datetime.now(KST)`(`publish.py:417,595,674`, `jobs.py:49,85,99`, `db.now_iso`), 저장은 aware ISO(`+09:00`), `to_iso_z`가 UTC `Z`로 변환하며 naive는 KST 간주. `inspect_failures`의 문자열 비교(`status.py:79-84`)는 동일 오프셋이라 안전. Windows에서 `zoneinfo("Asia/Seoul")` 로딩·`strptime` 문제 없음(테스트 15건 통과).
- **V-10. 중복 발행 키** — `prepare_manuscripts`가 `publications.exists(source, row, hash)`(uncertain/done)와 `duplicate.check_against_history`(캐시 기반 done 이력 + 같은 실행 내 선택분)를 모두 적용(`publish.py:198-214`). legacy §2 "끊긴 행은 자동 재발행 안 함" 충족.
- **V-11. 채널 명령은 모델 폴백 없음** — `worker.py:46` (`not via_channel and rt.llm is not None`), `serve`는 `via_channel=True`. `test_채널_명령은_모델폴백을_쓰지_않는다` 통과. `catalog`·`open_login`은 dry_run과 무관한 조회/세션 작업으로 네트워크·브라우저를 쓰는 것이 의도.

---

## 권장 수정 순서
1. C-1, C-2 (reconcile stage 분기 + source_id 즉시 보존 + 제목 미상 시 매칭 금지) — 데이터 정합성.
2. M-2, M-4, M-5 (작업 상태 판정·전부 실패 시 실패 보고·heartbeat/고아 정리) — 운영 신뢰성.
3. M-3, M-9 (`상태`/`중지` 즉시성) — 사용자 체감.
4. M-1, M-6, M-7, M-8 — 모의 실행 오프라인화, 재시도 정책, 확정 보류 처리.
5. 경미 항목과 테스트 보강(m-10).

---

## 수정 결과

수정 2026-09-19. 전체 테스트 `.venv/Scripts/python.exe -m pytest -q` → **255 passed**(`tests/test_engine.py` 15 → 28건).

### 치명
- **C-1** `reconcile.py`: `DONE_STATUSES`에서 `RESERVED`를 빼고 `_verdict(status, scheduled=…)`로 분리 — `RESERVED`는 예약 건일 때만 완료로 본다. `stage ∈ {daily_created, daily_done, revision_submitting}`(`PARENT_STAGES`)이면 `source_id`를 **부모(일상 글)**로 보고, `board_histories(days_ago=30)`에서 `parent_source_id == daily_id` + `created_at ≥ pub.created_at-15초`인 자식(수정글)을 찾아 그 상태로만 확정한다. 자식이 없거나 후보가 여럿이면 `unresolved`(자동 재발행 없음). `publish.py`는 `revision_submitting` 단계에서 `source_id=daily_id`를 유지해 부모를 남긴다.
- **C-2** `publish.py`: `_create_and_verify`에 `on_created` 훅을 추가해 `create_article` 반환 **직후** `mark(..., "uncertain", "<stage>_created", source_id=…)`를 기록(자사 `created`, 제휴 `daily_created`/`revision_created`). `reconcile.py` 폴백은 제목이 비면 매칭을 시도하지 않고 바로 `unresolved`.

### 중요
- **M-1** `sheets.load_source(..., prefer_cache=True)` 추가(네트워크 호출 0건, 캐시 없으면 `SourceError("원본 캐시가 없습니다. '시트 동기화' 명령을 먼저 실행하세요")`). `load_manuscripts`/`load_accounts`/`plan`이 `spec.dry_run`일 때 이 경로를 쓴다.
- **M-2** `_run_publish`가 이번 작업이 건드린 key(`touched`)만 모아 그중 아직 `uncertain`인 것만 `result["uncertain"]`에 넣는다. `run_once`는 `ok is False` 또는 `failures`가 있으면 `failed`(사유에 실패+미확정 모두 기록), `uncertain`만 있으면 `uncertain`.
- **M-3** `idem_key(text, task)`: 발행 작업만 `sha(text+날짜)`, 그 외(`status/stop/reconcile/inspect/catalog/sync*` 등)는 시각·nonce를 섞어 항상 새 작업. 발행 작업도 이전 작업이 `failed/cancelled`면 새 키로 재등록(`jobs.has_open_job`, `jobs.find_by_idem` 추가).
- **M-4** `prepare_manuscripts`가 시도/실패 수를 `rt.scratch["source_load"]`에 남기고, 전부 실패인데 원고가 0이면 `_run_publish`가 `ok=False` + 한국어 사유("원본을 하나도 읽지 못했습니다: …")로 끝낸다.
- **M-5** `jobs.reap_stale_running()` 추가 — `run_once` 진입 시 리스 만료된 `running` 작업을 `uncertain(error="실행기 중단")`으로 정리. `_run_publish(rt, job_id, spec, owner)`가 슬롯마다 `jobs.heartbeat`를 호출하고, 실패(리스 상실)면 남은 슬롯을 중단. `run_slot(..., heartbeat=…)`은 등록 직후에도 리스를 연장한다.
- **M-6** 확정 거부(`account_restricted/grade/no_membership/consecutive_limit/not_login`)가 **create 이전**에 발생하면 해당 행을 `failed(stage="거부(kind)")`로 내려 다른 계정이 재시도할 수 있게 했다. 글이 이미 생긴 뒤(제휴 daily 성공 등)에는 `uncertain` 유지. `_run_publish`는 `RetryWithOtherAccount`에서 제한 계정을 뺀 풀로 **최대 1회** 다른 계정 재시도.
- **M-7** `wait_written(..., scheduled_at, max_wait_s=120)`으로 호출하고 `PendingError`·"등록 확인 시간 초과"는 실패가 아닌 **보류**로 분류: 행은 `uncertain/stage=written_pending`에 `source_id`·`url`을 남기고 슬롯 결과는 `status="pending"`, 작업 보고는 "예약 확인 대기 N건".
- **M-8** 폴백 매칭 가드 강화: `days_ago=30`, `created_at ≥ pub.created_at-15초`, 계정+제목 일치, `parent_source_id`가 있는 행 제외, 후보가 1건이 아니면 `unresolved`.
- **M-9** `handle_text`가 `stop`을 큐에 넣지 않고 즉시 `jobs.cancel_open` + `data/STOP` 플래그(`request_stop`)로 처리한다. `_run_publish`는 시작 시 플래그를 지우고 **슬롯 사이마다** 확인해 남은 슬롯을 건너뛴다(`result["stopped"]`). `serve`는 수신 목록에서 중지 명령을 먼저 처리한다.

### 경미
- m-1 카페별 `work_type`으로 그룹을 나눠 계정 배정, m-2 수동 배정에서 풀이 비면 `AssignError`(+제한 계정 제외), m-4 `status_report(rt, exclude_job_id=…)`로 자기 작업 제외, m-5 `error`/`errors`도 실패 사유로 수집하고 `sync_*`·`reconcile`은 오류가 있으면 `ok=False`, m-6 자사 흐름 stage를 `submitting`/`created`로 개명, m-7 `context.py` 죽은 분기 삭제·`close()`에서 `_warehouse/_channels/_llm/_llm_ready` 정리, m-8 모의 실행은 중복 `skipped` 행을 DB에 남기지 않음, m-9 `__main__`이 이전에 남은 작업을 먼저 실행했음을 알리고 내 작업까지 이어서 실행, m-10 테스트 13건 추가(제휴 자식 매칭, 즉시발행 RESERVED, 제목 미상 금지, 캐시 전용 모의 실행, 전부 실패, 작업별 uncertain, 조회성 재등록, 실패 후 재등록, 고아 작업 정리, heartbeat, 보류, 중지 즉시성·슬롯 중단).

### 남은 항목(이번 수정 범위 밖)
- **m-3 보고문 sanitize**: `v2r/channels/__init__.py`(다른 에이전트 담당)에 `sanitize(text)`(500자 제한, `token=`/`Bearer `/`password`/이메일 마스킹)를 두고 `notify_all`이 항상 거치게 해야 한다. 엔진 쪽은 예외 문자열을 그대로 넘기는 구조를 유지했다.
- `publications` 테이블에 `title`·`parent_source_id` 열을 추가하면(스키마 `v2r/store/db.py`) C-1/C-2 매칭이 캐시 없이도 가능해진다. 이번에는 스키마 변경 없이 `stage`+`source_id` 규약으로 해결했다.
