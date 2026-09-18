# V2R 채팅 명령형 운영 시스템 — 설계서 (v1)

작성 2026-09-19. 기준 문서: `docs/reference/handoff.md`(인수인계), `docs/reference/v2r-api-spec.md`, `docs/reference/v2r-auth-protocol.md`, `docs/reference/legacy-business-logic.md`.
전제: V2R은 가상 테스트 사이트. 모든 자료는 가상 과제 자료. 구축 방식은 **V2R API 직접 호출**, 브라우저는 이미지 붙여넣기 한 단계에만 보조로 사용.

## 0. 한 줄 목표
```
채팅 한 줄 → 명령 해석(규칙) → 작업 큐 → 규칙 엔진(계정·카페·시간·중복) → V2R API 호출 → 저장 결과 확인 → 채팅 보고
```
사용자는 EXE 화면을 열지 않는다. 반복 작업에 AI 토큰을 쓰지 않는다.

## 1. 완성 목록 (인수인계 문서 전 항목 → 모듈 매핑)

| # | 인수인계 요구 | 담당 모듈 | 완료 기준 |
|---|---|---|---|
| A1 | 한국어 채팅 명령 해석(토큰 0) | `v2r/command/parser.py` | 표 §4의 모든 명령을 정규식으로 해석, 테스트 통과 |
| A2 | 모호한 문장만 모델 위임 | `v2r/command/llm_fallback.py` | 규칙 실패 시에만 Haiku 호출, 허용 작업 JSON 강제 |
| A3 | 텔레그램/슬랙 원격 명령 + 결과 보고 | `v2r/channels/telegram.py`, `slack.py` | 허용 ID 목록 필수, 진행/완료/실패 즉시 보고 |
| B1 | SQLite 작업 큐, 상태 조회, 중지, 재시작 | `v2r/store/db.py`, `v2r/store/jobs.py` | 상태 `queued/running/done/failed/uncertain/cancelled` |
| B2 | 완료 링크 저장, 재발행 방지 | `v2r/store/publications.py` | 키 `(source_key,row,hash)`, `uncertain` 포함 차단 |
| B3 | 끊김 후 미완료 자동 점검 | `v2r/engine/reconcile.py` | `uncertain` 건을 V2R 이력 API로 `done/failed` 확정 |
| C1 | V2R 로그인(API) | `v2r/api/auth.py` | 폼 로그인 + PoW 챌린지, 토큰 재사용 |
| C2 | V2R API 클라이언트(속도 제한·캐시·재시도) | `v2r/api/client.py` | 0.25초 게이트, GET 캐시 300초, 429 `Retry-After` |
| C3 | 카페·계정·게시판·말머리 카탈로그 | `v2r/api/catalog.py` | 이름 정규화 매칭, 중복이면 에러 |
| C4 | 글 등록·검증·삭제·이력 | `v2r/api/articles.py` | POST 후 GET 재확인 성공 시에만 완료 |
| C5 | SE-ONE 문서(content_json) 생성 | `v2r/content/seone.py` | 문단·빈 줄·이미지 자리 규칙 |
| C6 | 이미지 첨부(SE-ONE에 Ctrl+V) | `v2r/browser/seone_paste.py` | Playwright로 편집기 열고 클립보드 붙여넣기 → 이미지 컴포넌트 추출 |
| D1 | 자사·제휴 계정 분류, 회색 음영 제외 | `v2r/accounts/loader.py`, `rules.py` | 엑셀 배경색/CSV 열 모두 지원 |
| D2 | 자동 배정(LRU·등급·제한계정 제외)·수동 선택 | `v2r/accounts/assign.py` | 부족하면 채우지 않고 에러 |
| D3 | 카페별 게시판·시간 규칙 | `v2r/engine/scheduler.py`, `config/cafes.yaml` | 5~15분 랜덤, 카페별 체인, 자정 넘김 |
| D4 | 댓글·답글 구조 | `v2r/content/comments.py` | 12노드 트리, 분 충돌 시 일괄 이동 |
| E1 | 일상/브랜드/정보성/일괄 발행, 수정글 연결 | `v2r/engine/publish.py` | 제휴: 일상 → (지연) 수정글 → 댓글 |
| E2 | 원고 시트 불러오기(형식 변화 대응) | `v2r/sources/sheets.py` | 헤더 별칭 테이블, gviz CSV, 캐시 |
| E3 | 원고 중복 검사 | `v2r/content/duplicate.py` | 행 단위 skip + 보고 |
| F1 | 창고(이미지·원고 저장) | `v2r/warehouse/store.py` | `warehouse/images`, `warehouse/manuscripts` |
| F2 | 사진 수집기 | `v2r/warehouse/photo_collector.py` | Drive 폴더 → 창고 |
| F3 | 사진 세탁기(EXIF 랜덤) | `v2r/warehouse/photo_washer.py` | 날짜·카메라만 변경, 전부 삭제 금지, 해시 변경 확인 |
| F4 | 일상 글 수집기(+각색, 모델 선택적) | `v2r/warehouse/daily_collector.py` | 공개 페이지만, Sonnet 각색 |
| F5 | 일상 글 댓글 Haiku / 홍보 글 댓글 Sonnet | `v2r/llm/router.py` | 용도별 모델 고정 |
| G1 | Make 시나리오 지침 학습 | `v2r/knowledge/make_import.py` + `warehouse/guides/` | 브라우저 열어 사용자 로그인 → 지침 텍스트만 저장 |
| G2 | 브랜드 시트 참고 | `v2r/sources/sheets.py` | 브랜드별 시트 등록 |
| H1 | 문제 자가 해결 → 실패 시 슬랙/텔레그램 | `v2r/engine/worker.py` | 재시도 정책 후 알림 |
| H2 | 기획·보고 MD → `docs/` | `docs/` | 이 문서 외 `docs/reports/` |
| H3 | 새 저장소 커밋·푸시 | git | `main` 브랜치 |

## 2. 폴더 구조
```
v2r-command-system/
  v2r/                     # 파이썬 패키지
    __main__.py            # `python -m v2r "명령"` / `python -m v2r serve`
    config.py              # .env + config/*.yaml 로드 (단일 진입)
    command/  parser.py  spec.py  llm_fallback.py  describe.py
    store/    db.py  jobs.py  publications.py  accounts_state.py  sources_cache.py
    api/      auth.py  client.py  catalog.py  articles.py  errors.py
    content/  manuscript.py  seone.py  comments.py  duplicate.py
    accounts/ loader.py  rules.py  assign.py
    sources/  sheets.py  local_files.py
    engine/   worker.py  publish.py  scheduler.py  reconcile.py  status.py
    browser/  seone_paste.py  session.py
    warehouse/ store.py  photo_collector.py  photo_washer.py  daily_collector.py
    llm/      router.py  anthropic.py
    channels/ telegram.py  slack.py  base.py
    knowledge/ make_import.py
  config/   cafes.yaml  accounts_sources.yaml  sources.yaml  settings.yaml
  data/     v2r.sqlite  session.json  device.json     (git 제외)
  warehouse/ images/ washed/ manuscripts/ guides/      (git 제외)
  docs/     DESIGN.md  reference/  reports/
  tests/
  .env.example  pyproject.toml  README.md
```
비밀(비밀번호·봇 토큰·API 키)은 `.env`에만. `.gitignore`에 `.env data/ warehouse/ .venv/`.

## 3. 데이터 모델 (`v2r/store/db.py`)
```sql
jobs(id, idem_key UNIQUE, task, spec_json, status, lease_owner, lease_until, result_json, error, created_at, updated_at)
publications(source_key, row_number, content_hash, PRIMARY KEY, status, stage, source_id, url, account, cafe, menu_id, scheduled_at, updated_at)
account_state(login_id PRIMARY KEY, last_used_at, restricted_until, restrict_code, fail_count, note)
source_cache(key PRIMARY KEY, payload_json, synced_at, status)
photo_usage(sha256, variant, PRIMARY KEY, used_in_source_id, used_at)
executor_lease(id=1, owner, until)
events(id, job_id, level, message, created_at)   -- 보고·감사 로그
```
`publications.status`: `uncertain | done | failed | skipped`. `stage`: `daily_submitting | daily_done | revision_submitting | done`.

## 4. 명령 문법 (`v2r/command/parser.py`)
순서대로 첫 매치. 기본 `dry_run=True`, `실제|바로 (발행|등록)` 있을 때만 실제.

| task | 패턴 | 예 |
|---|---|---|
| `inspect_failures` | `(실패\|미완성\|불확실).*(점검\|재시도\|모아\|확인)` | 실패 글 점검 |
| `reconcile` | `(끊긴\|미완료).*(이어\|재개\|점검)` | 끊긴 작업 이어가 |
| `sync_all_sources` | `전체\s*(원본\|시트).*(동기화\|갱신)` | |
| `sync_sources` | `(원본\|시트).*(동기화\|갱신)` | |
| `generate_daily` | `일상\s*글.*(생성\|만들어)` | 일상 글 30개 만들어줘 |
| `collect_daily` | `일상\s*글.*(수집\|가져와)` | |
| `collect_photos` | `사진.*(수집\|가져와)` | |
| `wash_photos` | `사진.*(세탁\|변형)\s*(\d+)?` | 사진 세탁 30장 |
| `learn_guides` | `(메이크\|make\|지침).*(학습\|읽어\|가져와)` | |
| `open_login` | `로그인\s*(창\|세션\|준비)` | |
| `stop` | `(중지\|멈춰\|중단\|취소)` | |
| `status` | `(상태\|현황\|진행)` | |
| `catalog` | `(카페\|게시판\|계정)\s*(목록\|카탈로그)` | |
| `publish_brand` | `(브랜드\|수정)\s*글` | |
| `publish_info` | `정보성\s*글` | |
| `publish_batch` | `(일괄\|배치)\s*(발행\|등록)` | |
| `publish_daily` | `(일상\s*글\|올려\|발행\|등록)` | |

슬롯: 시간창, 개수, 계정 수, 간격, 카페, 브랜드, 게시판(`게시판 (\S+)`), 날짜, 계정 모드, 지정 계정(`아이디 (\S+)`), 시트 이름(`시트 (\S+)`), 즉시(`즉시|바로`).
`TaskSpec` 필드: `task, count, account_mode, account_count, accounts, window_start, window_end, interval_min, interval_max, start_date, cafe, board, brand, source, dry_run, immediate, notes, manuscripts`.

## 5. 실행 흐름 (`v2r/engine/worker.py`)
1. `handle_text(text, channel)` → `parse()` → 실패 시 `llm_fallback` (채널 경유는 폴백 없음) → `jobs.enqueue(idem_key=sha(text+date))`.
2. `run_once()` → 실행기 리스 획득 → `queued` 1건 `running` → `dispatch(task)`.
3. 발행 task: `sources`에서 원고 로드 → `duplicate` 행 단위 검사 → `publications.exists` 필터 → `accounts.assign` → `scheduler.plan` → 슬롯마다 `publish.run_slot()`.
4. `run_slot()`: 이미지 필요 시 `warehouse`에서 세탁본 선택 → `seone_paste`로 컴포넌트 확보 → `seone.build_content_json` → `publications.mark(uncertain, stage)` → `articles.create()` → `articles.verify()` → `publications.mark(done, url)` → `events` 기록 → 채널 보고.
5. 실패: `errors.classify()` → 재시도 가능(429/5xx/토큰만료)은 정책대로 재시도, 계정 제한(27000)은 `account_state.restricted_until` 기록 후 다른 계정으로, 그 외는 `failed` + 알림.
6. `reconcile`: `uncertain` 전건 → `board_histories`/`article`로 실제 존재 확인 → `done`/`failed` 확정. 자동 재발행은 하지 않는다.

## 6. 규칙 요약
- **계정**: `work_type`(자사 카페/제휴 작업) + `linked=V2R` + 회색 음영 아님 + 매니저 아님 + `restricted_until < now` + 댓글 전용 계정 아님. 자동 배정은 `last_used_at` 오래된 순(LRU). 부족하면 에러.
- **카페**: 씨씨앙/양평맘/쌍둥이맘 모여라는 제휴. 제휴 수정글 지연 4h/20h/22h. 테스트 카페 글은 즉시 발행.
- **시간**: `Asia/Seoul`. 시간창 안에서 첫 글 +5~15분, 카페별 체인으로 이전 예약 +5~15분(또는 명령의 고정 간격). 창 밖이면 다음 날 창 시작.
- **댓글**: 루트 5 + 대댓글 5 + 대대댓글 + 대대대댓글 = 12. 오프셋 5~9/15~19/26/36분. 같은 계정 같은 분 충돌 시 12개 일괄 이동. 일상 글 댓글 생성 Haiku, 홍보 글 Sonnet.
- **이미지**(결정 1, 2026-09-19 / `config/brands.yaml`): 사진은 **`originals/<브랜드>/<키워드 또는 토큰>` 폴더에서만** 고른다. 본문 `{토큰}` 1개 = 사진 1장. 토큰 → 폴더는 `token_folder_name()` (팥순이·장으뜸·뉴더미스의 `{키워드}`/`{A열 키워드}` → `키워드`, 팥순이 `{B/A}` → `BA`, 그 외 토큰 그대로). 팥순이 `키워드` 폴더만 **파일명 매칭**, 나머지는 무작위. 폴더가 비었으면 `ensure_keyword_pool()`이 **브랜드 폴더 루트와 인박스(`inbox/image[/image]/<브랜드>`) 원본을 복사해 채우고** 세탁본을 만든다. 브랜드 폴더 바로 아래 사진은 직접 쓰지 않는다. 원본이 하나도 없으면 `NoPhotoError`("사진이 필요합니다 …") → 채널(텔레그램) 알림. 세탁은 `DateTime*`, `Make/Model`, 렌즈·노출 필드만 랜덤. 원본은 보존.
- **일상 글 풀**(결정 2·3): `publish_daily`와 제휴 일상 글은 `warehouse/inbox/sheets/*각색*.xlsx`(kind `xlsx_daily`, A~F열, 등록시간·상태가 찬 행은 제외)를 **먼저** 쓰고, 이어서 `warehouse/manuscripts/daily_pool.jsonl`(kind `daily_pool`, `generate_daily`가 만든 제목 1줄·본문 1줄 짧은 글)을 쓴다. 둘 다 사진 없음. `content_hash`로 중복 제거, 한 실행 안 재사용 금지, 사용분은 `publications`에 기록.
- **브랜드 원고 시트**: `config/sources.yaml: brand_sheets`(브랜드 → spreadsheet_id, A~J 배치 → `parse_affiliate_rows`). `publish_brand`는 `spec.brand`가 있으면 그 시트만, 없으면 전부. 완료 링크가 `http`로 시작하는 행은 건너뛴다.
- **완료 판단**: POST 응답 `source_id` + GET `article` 재조회 일치 + (즉시 발행이면 `history.status ∈ {DONE,SUCCESS}`) 일 때만 `done`. 그 외 `uncertain`.

## 7. 모델 사용 정책 (`v2r/llm/router.py`)
| 용도 | 모델 ID |
|---|---|
| 모호 명령 해석 | `claude-haiku-4-5` |
| 일상 글 댓글 | `claude-haiku-4-5` |
| 홍보 글 댓글 | `claude-sonnet-5` |
| 일상 글 수집·각색, 짧은 일상 글 생성(`DAILY_SHORT_SYSTEM`, 20건씩 배치) | `claude-sonnet-5` |
호출 전 항상 "규칙으로 해결 가능한가" 검사. 키 없으면 해당 기능만 비활성, 나머지는 동작.

## 8. 구현 순서 (병렬 가능 묶음)
- **묶음 1 (기반)**: `config.py`, `store/*`, `command/*`, `__main__.py`, 테스트. 외부 의존 없음.
- **묶음 2 (V2R API)**: `api/*`, `content/seone.py`, `engine/reconcile.py`. 테스트는 가짜 서버.
- **묶음 3 (규칙)**: `accounts/*`, `sources/*`, `engine/scheduler.py`, `content/comments.py`, `content/duplicate.py`.
- **묶음 4 (창고)**: `warehouse/*`, `browser/seone_paste.py`.
- **묶음 5 (채널·LLM·지식)**: `channels/*`, `llm/*`, `knowledge/*`.
- **통합**: `engine/worker.py`, `engine/publish.py` → 드라이런 → 테스트 카페 실제 1건.

## 9. 의존 라이브러리
`httpx`, `pydantic`, `pyyaml`, `python-dotenv`, `openpyxl`, `pillow`, `piexif`, `playwright`, `anthropic`, `pytest`. 표준 `sqlite3`.
