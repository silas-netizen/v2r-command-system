# 일상 글 발행 사고 (2026-09-26)

## 무슨 일이 있었나
- 09-26 09:00 시작한 자사 카페 일상 글 발행(작업 240)이 12:50:31에 `database is locked` 오류 한 번으로 작업 전체가 `failed`로 끝났다(190건에서 멈춤).
- 그 뒤 22:59까지 10시간 동안 아무도 다시 시도하지 않았고, 슬랙 🔴 알림도 가지 않았다.
- 같은 시각 노출 러너 6개·키워드 워커 15개가 같은 `data/v2r.sqlite`를 함께 쓰고 있어 잠금 경합이 컸다.

## 원인
1. **한 건의 DB 잠금이 작업 전체를 죽였다** — `v2r/engine/worker.py`의 발행 루프는 슬롯마다 `publish.PublishError`/`RetryWithOtherAccount`만 잡는다. `rt.publications.mark()`가 던진 `sqlite3.OperationalError("database is locked")`는 아무 데서도 안 잡혀 `_run_publish` → `dispatch` → `run_once`까지 그대로 올라가 작업 240 전체가 `failed`로 끝났다.
2. **감시(monitor)의 실패 알림이 허용 범주 밖이었다** — `monitor.py`의 자동 알림은 `category=f"monitor_alert:..."`로 나갔는데, `config/notify.yaml`의 `critical_categories`(슬랙으로 실제 내보내는 목록)에 `monitor_alert`가 없어 `channels/__init__.py`의 `_critical_allowed()`가 조용히 로그로만 강등시켰다 — 그래서 🔴가 전혀 안 갔다.
3. **재큐 조건이 안 맞았다** — 그 시점 `RETRYABLE_RE`(다시 해볼 만한 실패 판정)에 "잠금/locked/busy"가 없어 `database is locked`가 재시도 대상으로 인식되지 않았고, 재큐 상한도 사슬당 1회뿐이라 애초에 자동 복구 여지가 좁았다.
4. (부수) `logs/serve.log`에 반복된 "심장박동 기록 실패: WinError 32/Permission denied"는 같은 프로세스 안에서 본 루프와 사이드카 스레드가 같은 이름의 `.json.tmp`에 동시에 썼기 때문(파일명 충돌) — 사고 자체와는 별개지만 같은 시간대 로그를 어지럽혔다.

## 고친 것
1. `v2r/store/publications.py`: `mark()` DB 쓰기가 잠금(locked/busy)을 만나면 지수 백오프로 최대 5회 재시도(`retry_on_lock`). 그래도 안 되면 오류를 그대로 올린다.
2. `v2r/engine/worker.py`: 발행 루프에 `sqlite3.OperationalError` 전용 처리 추가 — 재시도가 다 소진돼도 **그 글 1건만 실패로 남기고 다음 글로** 넘어간다(작업 전체를 죽이지 않음).
3. `v2r/engine/monitor.py`: publish_daily가 DB 잠금으로 실패하면 (a) `RETRYABLE_RE`에 잠금 문구 포함, (b) 재큐 상한을 3회로, 발행 허용 시간(08:00~02:00 KST) 안에서만, (c) 슬랙 알림을 허용 범주(`publish_failed_confirmed`)로 보내 🔴가 실제로 나가게 함. 그 밖의 일반 실패(네트워크 지연 등)는 기존처럼 1회·조용히 처리해 메시지 과다를 막았다.
4. `v2r/engine/schedule.py`, `v2r/engine/sidecar.py`: 심장박동 임시 파일 이름에 프로세스+스레드+호출 식별자를 더해 동시 쓰기 충돌(WinError 32/Permission denied)을 없앴다.
5. `data/v2r.sqlite`는 이미 WAL + `busy_timeout=30000`이 걸려 있었다(추가 변경 없음) — 경합 자체는 남아 있으니 위 재시도·건너뛰기가 실질적 방어선이다.

## 반영 시점
- **코드는 지금 바로 저장됐지만, 지금 도는 실행기(job 255)는 재시작 전까지 옛 코드로 돈다.** 다음 재시작(내일 새벽 02:30 이후 예정된 재시작)부터 반영된다.
- 그 전에 같은 잠금이 다시 나면 이번에도 190건 근처에서 통째로 멈출 수 있다(옛 동작 그대로).

## 사용자가 할 일
- 없음. 다음 재시작 때 자동으로 반영된다. 재시작 전에 같은 사고가 또 나면 알려드리겠다.
