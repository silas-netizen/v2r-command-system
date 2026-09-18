# 묶음 1~5 교차 검증 보고 (읽기 전용 리뷰)

작성 2026-09-19. 대상: `v2r/config.py`, `v2r/command/*`, `v2r/store/*`, `v2r/api/*`, `v2r/content/*`, `v2r/accounts/*`, `v2r/sources/*`, `v2r/engine/scheduler.py`, `v2r/warehouse/*`, `v2r/browser/*`, `v2r/channels/*`, `v2r/llm/*`, `v2r/knowledge/*`, `config/*.yaml`, `tests/*`.
제외: 동시 작성 중인 `v2r/engine/{context,publish,reconcile,status,worker}.py`, `tests/test_engine.py`.
기준 문서: `docs/DESIGN.md`, `docs/reference/v2r-api-spec.md`, `docs/reference/v2r-auth-protocol.md`, `docs/reference/legacy-business-logic.md`, `docs/reference/handoff.md` §10.

검증 방법: 전 소스 정독 + 기존 테스트 실행(`pytest --ignore=tests/test_engine.py` → 195 passed) + 스크래치 스크립트로 파서 38문장 배터리, 사진 세탁기 실행(EXIF GPS 포함 JPEG, PNG), 시간대·naive datetime·빈 응답 검증 등 프로브 실행.

집계: 치명 2 / 중요 12 / 경미 19 / 확인됨 22.

---

## 치명 (반드시 수정)

### C-1. 글 등록 POST가 5xx에서 자동 재시도되어 중복 발행 가능
- 위치: `v2r/api/client.py:156-158` (`request`의 `kind in {"rate_limited", "server"} and attempt < MAX_ATTEMPTS` → 무조건 재요청), `v2r/api/articles.py:100-113` (`create_article`).
- 문제: `V2RClient.request`는 메서드 구분 없이 429/5xx를 최대 3회 재전송한다. `POST /naver_cafe_articles/naver_cafe_article_source`가 서버에서 처리된 뒤 502/504 등으로 응답이 깨지면, 클라이언트가 같은 페이로드를 다시 POST하여 **같은 글이 2~3건 예약**된다. api-spec §4 "POST 실패/타임아웃 복구 = `board_histories` 폴링으로 채택"을 `create_article`이 구현했지만, `except (httpx.HTTPError, TimeoutError)`만 잡기 때문에 재시도 소진 후 올라오는 `V2RApiError`(kind `server`)는 이력 복구 없이 그대로 전파되고, 그 전에 이미 클라이언트 층에서 중복 POST가 나간 상태다. B2 "재발행 방지" 불변식이 클라이언트 층에서 깨진다.
- 수정:
  1. `V2RClient.request`에 `retry_server: bool = True` 인자를 추가하고, 5xx 재시도는 `method == "GET"` 또는 `retry_server=True`일 때만 수행. 429는 서버가 거부한 것이므로 POST도 재시도 가능.
  2. `create_article`은 `client.post(PATH_CREATE, json=payload, retry_server=False)`로 호출하고, `except V2RApiError as exc: if exc.kind in {"server", "rate_limited"}: found = find_recent_source(...)`로 이력 복구 후, 없으면 그때만 재POST(최대 1회)하거나 `uncertain`으로 올린다.
  3. 테스트 추가: 503 → 응답 없음 → 이력에 동일 제목 존재 시 POST가 1회만 나갔는지 `httpx_mock.get_requests()`로 검증.

### C-2. SE-ONE 문서 생성기가 이미지 부족을 조용히 넘겨 "사진 없는 글"이 만들어짐
- 위치: `v2r/content/seone.py:82-85` (`for _ in matches: if image_index < len(images): ...; image_index += 1`), 이를 정상 동작으로 고정한 테스트 `tests/test_seone.py:64-66` (`test_missing_image_component_is_skipped`).
- 문제: 플레이스홀더 수 > 이미지 컴포넌트 수이면 플레이스홀더만 지우고 이미지 없이 문서를 만든다. api-spec §4 "사진 실패 시 사진 없이 등록하지 않고 발행 중단", DESIGN §6 "이미지는 예약 생성 전에 완전히 준비"에 정면으로 어긋난다. `verify_article`의 이미지 개수 검사는 **등록 후**에만 잡을 수 있으므로, 이 경로로는 글이 이미 서버에 만들어진 뒤 삭제해야 하는 상황이 된다. 동시 작성 중인 `publish.py`가 사전 검사를 하더라도 모듈 계약 자체가 안전하지 않다.
- 수정: `build_document`에서 `count_placeholders(body) != len(images)`이면 `ValueError`(또는 전용 `SeoneError`)를 던지고, 이미지가 남는 경우도 에러. 이미지 없는 원고(`images_enabled=False`)는 플레이스홀더가 0개여야 통과. `test_missing_image_component_is_skipped`를 `pytest.raises`로 뒤집는다.

---

## 중요

### M-1. 두 번째 시각의 오전/오후 미상속 → "오후 2시부터 5시까지"가 14:00~다음날 05:00
- 위치: `v2r/command/parser.py:146-153`, `_to_hhmm` (86-95).
- 재현: `정보성 글 내일 오후 2시부터 5시까지 2개 아이디 abc,def 로` → `window_start=14:00, window_end=05:00`. `일상 글 3개 12시부터 3시까지` → `12:00~03:00`. 스케줄러(`window_bounds`)는 종료 ≤ 시작이면 다음 날로 넘기므로 **야간 15시간 창**으로 예약된다.
- 수정: 둘째 시각에 오전/오후가 없고 첫 시각에 있으면 첫 시각의 것을 상속. 상속해도 종료 ≤ 시작이면 종료에 12시간 더하기(`2시~5시` → 14:00~17:00, `12시~3시` → 12:00~15:00). 테스트 `test_midnight_meridiem_conversion` 옆에 케이스 추가.

### M-2. `status` 패턴이 발행 문장을 가로챔
- 위치: `v2r/command/parser.py:23` (`(상태|현황|진행)`), 순서상 `publish_*` 앞.
- 재현: `일상 글 5개 발행 진행해` → `status`(count=5까지 뽑아놓고 작업만 틀림). `카페 목록 진행 중인거` → `status`.
- 수정: `status` 패턴을 `(상태|현황|진행\s*(상황|중|률))`처럼 명사형으로 좁히거나, 발행 슬롯(`글 N개`, `올려`, `발행`)이 함께 있으면 `status` 매치를 무시하는 가드 추가. DESIGN §4 표도 함께 갱신.

### M-3. `N개`가 `글` 뒤에 붙지 않으면 count=0
- 위치: `v2r/command/parser.py:60` (`RE_COUNT = (?:일상\s*글|글)\s*(\d+)\s*개`).
- 재현: `정보성 글 내일 오후 2시부터 5시까지 2개 …` → `count=0`. `일상 글 수집해줘 10개` → `count=0`.
- 수정: 계정 수 표현(`아이디|계정 N개`)을 먼저 제거한 뒤 남은 `(\d+)\s*개`를 count로 채택. `RE_ACCOUNT_COUNT`는 그대로 유지.

### M-4. JSON 명령 경로가 dry-run 규칙을 우회
- 위치: `v2r/command/parser.py:128-137`.
- 문제: `{"task":"publish_daily","dry_run":false}`처럼 JSON을 보내면 `(실제|바로)\s*(발행|등록)` 문구 없이 `dry_run=False`가 된다(`tests/test_parser.py:117` `test_json_input`이 이를 정상으로 고정). 채널(텔레그램/슬랙)에서 들어온 텍스트도 같은 함수를 타므로 안전 불변식 3 "`(실제|바로)(발행|등록)`만 실제" 가 깨진다.
- 수정: JSON 경로에서도 `data["dry_run"] = RE_REAL.search(raw) is not None and not data.get("dry_run", True)`가 아니라 단순히 `data["dry_run"] = True`로 강제하고, 실제 실행은 한국어 문구로만 허용. 또는 채널 경유(`channel != "cli"`)에서는 JSON 입력을 거부. 테스트 수정 필요.

### M-5. `AccountStateStore.is_restricted`가 naive datetime에서 `TypeError`로 크래시
- 위치: `v2r/store/accounts_state.py:63` (`datetime.fromisoformat(row["restricted_until"]) > datetime.fromisoformat(ref)`), `_iso` (11-12).
- 재현: `restrict("acc", datetime(2030,1,1))`(naive) 후 `is_restricted("acc")` → `TypeError: can't compare offset-naive and offset-aware datetimes` (`except ValueError`로는 잡히지 않음).
- 수정: `_iso`에서 naive면 `replace(tzinfo=KST)`로 보정해 저장하고, 비교 시 `except (ValueError, TypeError)` → `True`(보수적으로 제한 중으로 간주). 27000 코드 처리 경로에서 `timedelta(days=30)`만 더한 naive 값이 들어올 가능성이 크다.

### M-6. naive datetime을 UTC로 간주해 9시간 어긋난 예약 가능
- 위치: `v2r/api/articles.py:42-43` (`to_iso_z`: `dt.replace(tzinfo=timezone.utc)`), `v2r/content/comments.py:127-128` (`_iso`: naive면 변환 없이 `Z` 붙임).
- 재현: `to_iso_z(datetime(2026,9,19,10,0))` → `2026-09-19T10:00:00Z`(실제 의도 KST 10시 = `01:00:00Z`). 프로젝트 전역이 KST(`scheduler.plan_slots`는 aware KST를 반환)이므로 naive 값이 섞이면 조용히 9시간 뒤로 밀린다.
- 수정: 두 함수 모두 naive 입력에 `ValueError("aware datetime 필요")`를 던지거나, 최소한 KST로 간주(`replace(tzinfo=KST)`) 후 UTC 변환. `comments._iso`는 `articles.to_iso_z`를 재사용해 한 곳으로 모은다.

### M-7. `find_recent_source`가 오래된 동일 제목 글을 "방금 만든 글"로 채택할 수 있음
- 위치: `v2r/api/articles.py:220-221` (`if since is not None and created is not None and created < since: continue`), 224 (`status`가 빈 값이면 통과).
- 문제: `created_at`이 없거나 파싱 불가하면 시각 필터를 건너뛴다. 같은 계정이 어제 같은 제목(브랜드 시트는 키워드가 제목이므로 재사용 빈도 높음)으로 올린 글이 `days_ago=1` 범위에 있으면 그 `source_id`를 반환 → 새 행이 `done`으로 잘못 기록되고 실제 글은 없다(실패가 성공으로 보고됨).
- 수정: `created`가 `None`이면 채택하지 않고 건너뛴다. `since`는 필수로 만들고, 가능하면 `menu_id`도 비교. 매치가 2건 이상이면 `None`(불확실)으로 남긴다.

### M-8. `wait_written`이 예약 시각까지 스레드를 블로킹
- 위치: `v2r/api/articles.py:252-258` (`time.sleep(wait)`; `wait`는 예약-15초까지, 상한 없음).
- 문제: 예약 글에 대해 호출하면 몇 시간 동안 `time.sleep`으로 워커가 멈춘다. DESIGN §6 완료 판단은 "즉시 발행이면 history.status" 조건이므로 예약 글은 이 함수를 타면 안 되고, 타더라도 상한이 있어야 한다.
- 수정: `scheduled_at`이 `now + 60초`보다 뒤면 즉시 `V2RApiError("예약 글은 wait_written 대상 아님")` 또는 `{"status": "RESERVED"}`를 반환하고, 폴링은 `reconcile`이 담당. `max_block_seconds` 인자(기본 120) 추가.

### M-9. 텔레그램 봇 토큰이 경고 로그에 노출될 수 있음
- 위치: `v2r/channels/telegram.py:62,69-72` (`url = f".../bot{self.token}/{method}"`, `resp.raise_for_status()` 후 `log.warning("… %s", exc)`).
- 문제: `httpx.HTTPStatusError` 메시지는 `"Client error '404 Not Found' for url 'https://api.telegram.org/bot<TOKEN>/getUpdates'"` 형태로 **URL 전체**를 포함한다. 안전 불변식 2 "비밀번호·토큰을 로그에 남기지 않음" 위반.
- 수정: `except httpx.HTTPStatusError as exc: log.warning("텔레그램 %s HTTP %s", method, exc.response.status_code)`로 분리하고, 그 외 예외는 `str(exc).replace(self.token, "***")`로 마스킹. 슬랙(`slack.py:73`)은 URL에 토큰이 없어 해당 없음.

### M-10. `make_variants` 재실행 시 기존 세탁본을 같은 이름으로 덮어씀
- 위치: `v2r/warehouse/photo_washer.py:317` (`dest = out_dir / f"{len(results)}.jpg"`).
- 재현: 같은 `out_dir`에 5장 만든 뒤 2장 추가 요청 → `0.jpg`, `1.jpg`가 **새 내용으로 교체**됨(스크립트로 확인). `photo_usage(sha256, variant)`와 `Warehouse.pick_variant`는 stem(`"0"`)으로 사용 여부를 기록하므로, 이미 발행에 쓴 변형이 다른 내용으로 바뀌고 새 변형은 "이미 사용됨"으로 영영 선택되지 않는다.
- 수정: 시작 번호를 `max(기존 stem)+1`로 잡고(`Warehouse.washed_variants` 재사용), 기존 조합(Make/Model/DateTimeOriginal)도 `combos`에 미리 넣어 중복 조합을 피한다.

### M-11. 세탁기가 픽셀을 자르고 재인코딩함 — "메타데이터만 변경" 원칙과 다름
- 위치: `v2r/warehouse/photo_washer.py:243,259-270` (`tweak_pixels=True` 기본, 가장자리 1px crop + 랜덤 품질 92~96 재인코딩), 272-277.
- 확인 결과: 변형마다 크기가 `(199,150)`/`(200,149)`로 달라지고, 원본 JPEG를 매번 디코드→재인코딩해 세대 손실이 생긴다. handoff §10 "변경 대상은 촬영 날짜, 카메라 정보 부분", legacy §6 "성공 판정 = 카메라 메타 변경 또는 파일 해시 변경" — EXIF만 바꿔도 해시는 바뀌므로(`tweak_pixels=False` 프로브에서 `hash changed True` 확인) 픽셀 변경은 요구사항 밖이다.
- 수정: JPEG 원본은 `piexif.insert(piexif.dump(exif), src_bytes)`로 **바이트 그대로** EXIF만 교체(재인코딩 없음). PNG/WebP만 흰 배경 합성 후 JPEG q95. `tweak_pixels`는 기본 `False`로 두고 명시 옵션으로만 허용. GPS 보존·카메라/날짜 변경·해시 변경은 정상 동작 확인(아래 확인됨 항목).

### M-12. 댓글 페이로드의 `root_start_at`이 루트/자식 간 기준이 다름
- 위치: `v2r/content/comments.py:154` (루트: `it["root_start_at"]` = 글 예약시각), 181 (자식: `root["start_at"]` = 루트 댓글 시각).
- 재현: 글 01:30 예약, 댓글2 01:36 → 루트의 `root_start_at=01:30:00Z`, 자식 답글의 `root_start_at=01:36:00Z`. api-spec §5 예시는 `root_start_at`이 글 시각(01:30)이다. 서버가 이 값을 기준으로 상대 지연을 계산한다면 답글 예약이 어긋난다.
- 수정: 자식에도 `it.get("root_start_at")`(글 시각)을 쓰도록 통일. 서버 의미가 "부모 댓글 시각"으로 확정되면 루트도 그 의미로 맞추되, 어느 쪽이든 한 기준으로. 첫 실제 1건에서 `GET article`의 `naver_cafe_article_source_comments`로 서버 해석을 확정하고 테스트에 고정.

---

## 경미

### m-1. 재시도 시 호출자 지정 헤더가 유실
- `v2r/api/client.py:132` `extra_headers = kwargs.pop("headers", None)`가 `while` 루프 안에 있어 두 번째 시도부터 `None`. 루프 밖으로 이동.

### m-2. GET 캐시가 같은 dict 객체를 돌려줌
- `v2r/api/client.py:122,144`. 호출자가 결과를 수정하면 캐시가 오염된다. `copy.deepcopy` 또는 `json.loads(json.dumps(...))`로 복사해 반환.

### m-3. 오류 분류가 본문 전체 부분문자열 매칭
- `v2r/api/errors.py:133-137` `"27000" in haystack`(haystack에 `body` 포함). 본문의 숫자(조회수, ID 등)에 `27000`이 포함되면 계정이 30일 제한 대상으로 오분류될 수 있다. 코드 비교는 `code`/`reason` 필드로만, 본문 검색은 문자열 토큰(`TOKEN_ERROR`, `NOT_LOGIN` 등)으로 한정.

### m-4. 카탈로그: 비정상 계정 사유가 사라지고, `writable` 누락 시 전부 제외, 말머리를 게시판명으로 매칭
- `v2r/api/catalog.py:209-211` 예외를 `unhealthy_accounts`에만 넣고 `resolve`(257)는 "쓸 수 있는 게시판이 없습니다"라고만 말함 → 메시지에 `unhealthy_accounts[login_id]` 포함.
- 217 `_truthy(field(d, "writable", "isWritable"))`: 필드 자체가 없으면 제외. `default=True`로 두고 명시 `False`만 제외.
- 262-267 `heads`를 `board_name`으로 매칭하는 것은 의미가 없음 → `resolve(cafe_name, board_name, login_id, head_name: str = "")`로 분리.

### m-5. `PublicationStore.mark`가 상태를 무조건 덮어써 `done` → `uncertain` 역행 가능
- `v2r/store/publications.py:55`. `status = CASE WHEN publications.status='done' AND excluded.status='uncertain' THEN publications.status ELSE excluded.status END` 같은 가드, 또는 호출부에서 상태 전이 검증.

### m-6. `reply_member.member_key`가 비어 있어도 전송
- `v2r/content/comments.py:168-173`. `members`에 계정이 없으면 `""`로 채움. `CommentError`로 실패시켜야 서버에 잘못된 답글이 예약되지 않는다.

### m-7. CRLF 본문의 `\r`이 문단에 남음
- `v2r/content/seone.py:72` `split("\n")`. `"a\r\n{사진}\r\nb"` → 문단 `['a\r', '\r']`. `splitlines()` 또는 `.replace("\r\n", "\n")` 선처리. `body_lines_for_verify`도 동일.

### m-8. 파서 슬롯 오탐 모음
- `parser.py:66` `RE_SOURCE = 시트\s*(\S+)`: `시트 갱신` → `source="갱신"`, `전체 시트 동기화` → `source="동기화"`. `sync_*`/`status`류 작업에서는 슬롯 추출을 건너뛰기.
- `parser.py:59` `RE_CLOCK`이 `2시간`의 `2시`를 시각으로 봄 → `일상 글 3개 2시간 후에` → `window_start=02:00`. `(\d{1,2})\s*시(?!간)` 부정 전방탐색 추가.
- `일상 글 1개 바로 올려` → `immediate=True, dry_run=True`(스펙 준수이나 사용자가 혼동할 문구). `describe_spec` 출력에 "모의 실행"이 함께 표시되므로 현행 유지 가능. `일괄 발행 실제로 해줘`도 `dry_run=True`로 남음(스펙 준수). README에 "실제 발행/바로 등록 외 표현은 모의"라고 명시.

### m-9. `dataclasses.asdict(Settings)`로 비밀 노출 가능
- `v2r/config.py:24-52`는 `__repr__`만 가림. `asdict`/`vars`는 그대로. 비밀 필드를 `field(repr=False)`로 두고, 로그·보고 코드에서 `asdict(settings)` 사용 금지 규칙을 주석으로 명시하거나 `to_public_dict()` 제공.

### m-10. `DeviceProfile.load`가 키 하나만 빠져도 지문을 새로 생성해 덮어씀
- `v2r/api/auth.py:122-131`. 기기 일관성(auth-protocol §4)에 어긋남. 누락 키만 기본값으로 보충하고 `device_id`는 유지.

### m-11. `solve_pow` 무한 루프 가능
- `v2r/api/auth.py:171-176`. `difficulty`가 비정상적으로 크면 종료 없음. 상한(예: `n > 50_000_000` → `V2RApiError`)과 `difficulty > 8` 거부.

### m-12. 챌린지 판정이 느슨해 잘못된 비밀번호로 2회 로그인 시도
- `v2r/api/auth.py:281` `400 <= status < 500 and isinstance(err.extra, dict)`. 비밀번호 오류 응답에 `extra: {}`가 있으면 PoW를 풀고 같은 비밀번호로 재시도 → 일일 로그인 한도 2배 소모. `err.code`가 챌린지 계열(첫 실행 로그로 확정)일 때만 진행.

### m-13. 텔레그램은 chat_id만 검사, 발신자(sender)는 미검사
- `v2r/channels/telegram.py:103`. 허용 방이 그룹이면 그룹 구성원 누구나 명령 가능. `TELEGRAM_ALLOWED_USER_IDS`(선택) 추가 권장.

### m-14. 슬랙 `ts` 문자열 비교
- `v2r/channels/slack.py:116-118` `ts > newest`는 문자열 비교. 현재 자릿수가 같아 동작하지만 `float(ts)` 비교로 바꿔야 안전.

### m-15. 콘솔 출력 인코딩(cp949) — 리다이렉트 시 `UnicodeEncodeError` 가능
- `v2r/__main__.py`, `v2r/browser/session.py:71`, `v2r/knowledge/make_import.py:40,319`의 `print`. 파일 `open()`/`read_text`/`write_text`는 전부 `encoding="utf-8"`이 지정되어 있어 위반 0건(바이너리 `open(path, "rb")`만 예외). 다만 `python -m v2r … > log.txt`처럼 리다이렉트하면 cp949로 기록되어 시트 제목의 이모지·특수문자에서 예외. `__main__`에서 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` 1줄 추가.

### m-16. 스케줄러: 창 넘김 시 다음 날 첫 슬롯이 `w_start` 정각
- `v2r/engine/scheduler.py:93-96`. 첫 글 규칙(+5~15분)과 달리 창 시작 정각에 배치. `first_for(w_start)`로 통일.

### m-17. `assign(count<=0)` 에러 vs `TaskSpec.account_count` 기본 0
- `v2r/accounts/assign.py:43-44`. 명령에 계정 수가 없으면 기본이 0이라 통합 층이 `count`를 반드시 채워야 함(예: `count = spec.account_count or spec.count`). 통합 시 주의 항목.

### m-18. `create_article`이 `parent_source_id: null`을 항상 전송
- `v2r/api/articles.py:97`. api-spec §4 "`parent_source_id`는 수정글일 때만". 서버가 null을 허용할 가능성이 높지만 첫 실제 1건에서 확인하고, 아니면 `if parent_source_id: payload["parent_source_id"] = ...`.

### m-19. `verify_article`: 즉시 발행(`start_at=None`) 글에 서버가 실제 시각을 채우면 "예약시각 불일치"
- `v2r/api/articles.py:402-407`. 실패 방향(보수적)이므로 안전하지만 즉시 발행이 전부 `uncertain`이 될 수 있음. `start_at is None`이면 예약시각 검사를 생략하고 `history.status`로 판정.

---

## 확인됨 (정상)

### API 페이로드
- `articles.DEFAULT_WRITE_OPTIONS`(`articles.py:22-32`) 9개 키·값이 api-spec §4와 일치. `build_destination`(52-74) 키 11개(`cafe_id, cafe_name, head_id, head_name, menu_id, menu_name, naver_login_id, start_at, target_view_count, use_comment_ai, parent_id`) 일치, `use_comment_ai=True`, `parent_id=None`.
- `to_iso_z`는 aware 입력에서 `YYYY-MM-DDTHH:MM:SSZ`(UTC) 출력, `None` → `None`(즉시 발행) 확인.
- `create_article` 페이로드 최상위 키 `tag_list, title, content_json, cafe_write_options, comments, destination, likes, parent_source_id` 일치. `source_id`는 `response["naver_cafe_article_source"]["source_id"]` 우선.
- `seone.build_document`(`seone.py:60-105`): `version "2.9.0"`, `theme/language`, `id = uuid4().hex.upper()[:26]`(26자), `di` 블록 값(`dif False`, `dis "N"`, `st 2827`) 일치, 한 줄=paragraph 1개, 빈 줄 빈 paragraph 보존, 플레이스홀더만 있던 줄도 빈 paragraph 유지, 이미지 컴포넌트 삽입 위치, 컴포넌트 0개면 빈 텍스트 1개, `json.dumps(..., ensure_ascii=False, separators=(",",":"))`.
- `comments.to_api_payload`: 루트 5 + 자식 7 = 12노드, 1단계 중첩, depth≥2는 `reply_member{member_key, naver_login_id, nick}`, 각 노드 키 `contents, naver_login_id, start_at, repeat_count 0, interval_seconds 0, root_start_at, reply_member, comments` 일치(단 M-12 참고). 오프셋 5~9/15~19/26/36분 일치. 같은 계정·같은 분 충돌 시 번들 전체 1분 이동·24h 상한.

### 인증 프로토콜
- `auth._post_login`(255-270): `files={"username": (None, email), "password": (None, password)}` → multipart/form-data, 필드명 `username`/`password` 일치.
- 헤더 `X-Device-Id`, `X-Browser-Signal`(정렬 키·공백 없는 JSON), `X-Browser-Signal-Status`, 재시도 시 `X-Challenge-Proof` 일치. 비-GET 요청에 신호 헤더 자동 첨부(`client.py:130-131`).
- PoW: `f"{version}:{path}:{fp_hash}:{nonce}:{timestamp}:{proof_nonce}"`, `path="/auths/login"`, `"0"*difficulty` 접두 최소 nonce(테스트에서 최소성 검증). `fp_hash = SHA-256(정렬 키 JSON)`, 지문 키 13개 snake_case 정확히 일치, `webdriver False`, `platform "Win32"`, `timezone "Asia/Seoul"`, `version "v1"`. proof JSON 키 8개 일치.
- 토큰: `data/session.json` 재사용, 403 + `TOKEN_ERROR` → `invalidate()` 후 정확히 1회 재로그인(`client.py:150-154`, 테스트 `test_token_expired_only_once`). 로그인 429는 `retry_after` 보존 후 즉시 예외.
- 비밀 노출: `Settings.__repr__/__str__` 마스킹, `V2RApiError.__repr__`에 body 미포함, 소스 어디에도 비밀번호/토큰 `log`·`print` 없음(grep 확인). `.env`에 실제 값이 있으나 `.gitignore`에 `.env`, `data/`, `warehouse/`, `*.sqlite*`, `.venv/` 포함. 저장소 파일에서 비밀번호 패턴·`xoxb-`(테스트의 가짜값 `xoxb-abc` 제외)·`sk-ant-`·텔레그램 토큰 형태 검출 없음.

### 안전 불변식
- `TaskSpec.dry_run` 기본 `True`; 파서 `RE_REAL = (실제|바로)\s*(발행|등록)` 일치(`실제발행`, `바로 등록` 통과, `실제로 해줘`·`바로 올려`는 모의 유지). `llm_fallback`도 원문 정규식으로 `dry_run` 강제(모델 출력 무시).
- `assign(auto)`: 부족하면 `AssignError`(채우지 않음); `manual`: 풀에 없는 계정이면 에러. `catalog.match_name`/`rules.find_affiliate`: 정규화 후 1건만 채택, 여러 건이면 `CatalogError`/`CafeMatchError`, 임의 선택 없음.
- `PublicationStore.exists`는 `("uncertain","done")`을 발행됨으로 간주(`skipped`/`failed`는 재시도 가능).
- 텔레그램: 토큰 + 허용 chat_id 둘 다 있어야 `enabled`; 허용 외 방 수신·발신 모두 차단. 슬랙: 수신은 봇 토큰 + 허용 채널 목록 둘 다 필수(`can_receive`), 허용 외 채널 발신 차단, webhook만 있으면 발신 전용.
- 메시지 텍스트로 셸 실행 없음. `subprocess.run`은 `browser/seone_paste.py:194`(PowerShell 클립보드, 고정 스크립트에 로컬 파일 경로만 삽입)이 유일하며 사용자 입력이 아님. `eval/exec/os.system/shell=True` 없음. 프롬프트(`llm/prompts.py:23`)도 셸 요청 금지 명시.
- `browser/session.py`·`knowledge/make_import.py`: 비밀번호 자동 입력 없음, 사용자가 직접 로그인. `sources.EXCLUDED_DOCUMENT_IDS`로 제외 문서 동기화 거부.

### 속도 제한·캐시·재시도
- `client._rate_gate`: 모듈 전역 `threading.Lock` + `_last_call_at` → 모든 `V2RClient` 인스턴스가 0.25초 게이트 공유. GET 캐시 key=완성 URL, TTL 300초, 대상 4경로 일치. `Retry-After` 우선·최대 900초, 없으면 (10, 30)초, 총 3회 시도(테스트로 확인). 이력 5페이지·`days_ago=30`, 404는 빈 목록.

### 파서 배터리(38문장) 정상 판정
- `브랜드 글 3개 씨씨앙에 실제 발행` → `publish_brand, count 3, cafe 씨씨앙, dry_run False`. `사진 세탁 30장`/`사진 30장 세탁해` → `wash_photos 30`. `끊긴 작업 이어가`/`미완료 작업 재개해줘` → `reconcile`. `상태` → `status`. `카페 목록`/`계정 목록 보여줘` → `catalog`. `양평맘에 수정글 1개 바로 등록` → `publish_brand, 양평맘, dry_run False`. `오늘 오후 3시 30분부터 오후 6시까지 일상 글 4개 아이디 3개 자동` → 15:30~18:00, count 4, account_count 3. `일상 글 10개 오후 11시부터 오전 2시까지` → 23:00~02:00(자정 넘김). `12월 25일`/`2026-12-01`/`내일`/`모레` 날짜 정상. `쌍둥이맘 모여라 카페 게시판 가족업체자유게시판에 …` → 카페·게시판(조사 제거) 정상. `아이디 abc01 abc02 사용해서 …` → manual, 2계정. `아이디 5개로 …` → auto, account_count 5. `코숨핏 브랜드 글 1개 씨씨앙 실제 발행 게시판 자유수다방(구)` → 브랜드·카페·게시판 모두 정상. `발행 중지` → `stop`. `정보성 글 발행 상태 알려줘` → `status`(의도 부합).
- 오탐은 M-1, M-2, M-3, M-4, m-8에 정리.

### 사진 세탁기 실행 확인
- 한글+공백 경로(`…\v2r 한글 공백 xxx\원본 사진.jpg`)에서 `make_variants(src, 5)` 성공. 5개 변형 모두 `GPS` IFD(위도·경도·고도)와 `Artist` 태그가 원본과 동일하게 보존, `Make/Model/DateTime/DateTimeOriginal` 모두 변경, sha256 모두 원본과 다름, (Make, Model, DateTimeOriginal) 조합 5개 서로 다름, `OffsetTime* = +09:00`.
- PNG(RGBA 투명) → `prepare_jpeg`로 흰 배경 JPEG 변환 확인, `wash(png)`도 JPEG 출력. 픽셀 변경 관련은 M-11 참고.

### Windows 특이사항
- 텍스트 파일 I/O 전 구간 `encoding="utf-8"` 지정(위반 0건). SQLite를 한글·공백 경로에 생성 정상. `pyproject.toml`에 `tzdata; sys_platform == 'win32'` 포함, `ZoneInfo("Asia/Seoul")` 로드 정상. `warehouse.safe_name`이 경로 구분자·특수문자 제거. `piexif.insert(…, str(path))` 한글 경로 정상.

### 실패가 성공으로 보고될 가능성 점검
- `verify_article({})`(빈 상세) → `["제목 불일치…", "게시판 불일치…", "본문 문단 불일치…"]`로 비어 있지 않음. 단 기대값이 전부 비어 있을 때(`title=""`, `body_lines=[]`)는 `제목 불일치: None != ''` 1건만 남으므로 호출부는 `title`을 반드시 넘겨야 한다. 실패→성공 오보 가능 경로는 M-7(`find_recent_source`)과 C-1(중복 POST 후 첫 응답 채택) 두 곳.
- `uncertain` 유실 가능 경로: m-5(`mark` 역행)과 M-8(`wait_written` 블로킹 중 프로세스 종료 시 상태 미기록 — 호출부가 `mark(uncertain)` 선행해야 함, DESIGN §5-4 순서대로면 안전).

---

## 수정 결과 (2026-09-19)

동시 작성 중인 `v2r/engine/{context,publish,reconcile,status,worker}.py`, `v2r/__main__.py`,
`tests/test_engine.py`는 건드리지 않았다.
검증: `.venv/Scripts/python.exe -m pytest -q --ignore=tests/test_engine.py` → **227 passed**.

### 치명
| 항목 | 결과 | 내용 |
|---|---|---|
| C-1 글 등록 POST 자동 재시도 | **수정** | `V2RClient.request(idempotent=...)` 추가(기본 GET=True, 그 외 False). 비멱등 요청은 1회만 전송하고 5xx는 `kind="ambiguous"`로 올린다. `create_article`은 `idempotent=False`로 POST하고, httpx 오류·`server`/`ambiguous`/`rate_limited` **모든** 경우에 `find_recent_source` 복구를 먼저 시도한 뒤 재-raise. 테스트: `test_post_is_not_retried_on_server_error`, `test_post_rate_limited_is_not_retried`, `test_post_can_opt_into_retry`, `test_create_article_does_not_repost_on_5xx`(POST 1회 검증), `test_create_article_raises_ambiguous_when_no_history` |
| C-2 SE-ONE 이미지 부족 무시 | **수정** | `build_document`가 자리 수 ≠ 사진 수면 `ValueError("사진 수가 부족합니다: 자리 N, 사진 M")`(남는 경우는 "사진 수가 많습니다"). `test_missing_image_component_is_skipped` → `test_missing_image_component_raises`로 뒤집고 초과 케이스 테스트 추가 |

### 중요
| 항목 | 결과 | 내용 |
|---|---|---|
| M-1 둘째 시각 오전/오후 미상속 | **수정** | 둘째 시각에 오전/오후가 없고 종료 ≤ 시작이면 오후로 해석. `오후 2시부터 5시까지`→14:00~17:00, `12시부터 3시까지`→12:00~15:00. 명시적 표기(`오후 11시~오전 2시`)는 그대로 자정 넘김 |
| M-2 `status`가 발행 문장 가로챔 | **수정** | `status` 패턴을 `(상태\|현황\|진행\s*(?:상황\|중\|률))`로 좁히고, 개수/`목록` 표현이 있으면 발행·카탈로그 작업을 `status`/`stop`보다 우선. `일상 글 5개 발행 진행해`→publish_daily, `카페 목록 진행 중인거`→catalog, `정보성 글 발행 상태 알려줘`→status 유지 |
| M-3 `N개`가 `글` 뒤가 아니면 count=0 | **수정** | 계정 수 표현(`아이디\|계정 N개`)을 제거한 뒤 남은 `(\d+)개`를 발행·수집 작업에서 count로 채택 |
| M-4 JSON 경로가 dry-run 우회 | **수정** | JSON의 `dry_run: false`는 `notes`에 `(실제\|바로)(발행\|등록)`가 있을 때만 존중, 그 외에는 `dry_run=True` 강제. `test_json_input` 수정 + `test_json_input_real_publish_needs_phrase_in_notes` 추가. README에도 명시 |
| M-5 `is_restricted` naive datetime 크래시 | **수정** | 저장·비교 모두 naive를 KST로 보정, `except (ValueError, TypeError)` → 보수적으로 `True`(제한 중) |
| M-6 naive datetime을 UTC로 간주 | **수정** | `articles.to_iso_z`가 naive를 KST로 간주해 UTC 변환. `comments._iso`는 `to_iso_z`를 재사용해 한 곳으로 모음. 테스트 2건 추가 |
| M-7 `find_recent_source` 오래된 글 채택 | **수정** | `created_at`이 없거나 파싱 불가하면 채택하지 않음, 조건 만족 행이 2건 이상이면 `None`(불확실). 테스트 2건 추가 |
| M-8 `wait_written` 장시간 블로킹 | **수정** | `max_wait_s`(기본 120초) 추가. 예약 시각이 그보다 멀면 `PendingError`를 던져 호출자(reconcile)가 뒤로 미루게 함. naive `scheduled_at`도 KST로 해석 |
| M-9 텔레그램 토큰 로그 노출 | **수정** | `httpx.HTTPStatusError`는 상태코드만 기록, 그 외 예외 메시지는 토큰을 `***`로 마스킹. `test_telegram_error_log_hides_token` 추가 |
| M-10 `make_variants` 기존 세탁본 덮어씀 | **수정** | 기존 `N.jpg`의 최대 번호 다음부터 번호를 이어 붙이고, 기존 변형의 (Make, Model, DateTimeOriginal) 조합도 중복 검사에 포함. `test_make_variants_does_not_overwrite_existing` 추가 |
| M-11 픽셀 크롭·재인코딩 | **부분 수정(의도적)** | 지시대로 픽셀 보정 자체는 유지하되 모듈 상수 `DEFAULT_TWEAK_PIXELS`(기본 True)로 기본값을 한 곳에서 바꿀 수 있게 하고, `wash`/`make_variants`의 `tweak_pixels`를 `bool \| None`로 바꿔 docstring에 트레이드오프(1px 축소·세대 손실 vs EXIF만 교체)를 명시. 보고서가 권한 "기본 False + `piexif.insert`로 바이트 보존"은 발행 품질 영향이 있어 보류 |
| M-12 `root_start_at` 기준 불일치 | **수정** | api-spec "root_start_at 답글일 때만"에 맞춰 **루트 댓글은 `None`**, 자식 답글은 **루트 댓글의 `start_at`**으로 통일. `test_root_start_at는_루트댓글_시각이고_루트는_None` 추가 |

### 경미
| 항목 | 결과 | 내용 |
|---|---|---|
| m-1 재시도 시 헤더 유실 | **수정** | `kwargs.pop("headers")`를 `while` 루프 밖으로. `test_custom_headers_survive_retry` |
| m-2 GET 캐시가 같은 객체 반환 | **수정** | 저장·반환 모두 `copy.deepcopy`. `test_cache_returns_copy` |
| m-3 오류 분류가 본문 부분문자열 매칭 | **수정** | 숫자 코드(27000/20004/33007)는 `code`/`reason`에서만 찾음. 문자열 토큰(`TOKEN_ERROR`, `NOT_LOGIN`, `연속으로 등록` 등)은 기존대로 본문 포함 |
| m-4 카탈로그 3건 | **수정** | ① `resolve` 실패 메시지에 `unhealthy_accounts[login_id]` 사유 포함 ② `writable`은 `default=True`로 두고 명시 `False`만 제외 ③ `resolve(..., head_name="")` 인자 분리, 말머리는 `head_name`으로만 매칭 |
| m-5 `mark`가 `done`→`uncertain` 역행 | **수정** | upsert에 `CASE WHEN publications.status='done' AND excluded.status='uncertain'` 가드. `test_publications_exists_includes_uncertain`에 검증 추가 |
| m-6 빈 `member_key`로 답글 전송 | **수정** | `member_key`가 없으면 `CommentError`. `test_member_key_없으면_에러` |
| m-7 CRLF의 `\r` 잔존 | **수정** | `seone._normalize`로 CRLF/CR → LF 선처리(`build_document`, `count_placeholders`, `body_lines_for_verify` 공통). `test_crlf_body_has_no_carriage_return` |
| m-8 파서 슬롯 오탐 | **수정** | ① 동기화·상태류 작업에서는 시트 슬롯 추출 생략 ② `RE_CLOCK`에 `(?!간)` 부정 전방탐색(`2시간` 오인 방지) ③ dry-run 표현 혼동은 README에 명시 |
| m-9 `asdict(Settings)` 비밀 노출 | **수정** | `Settings.SECRET_FIELDS` + `to_public_dict()` 추가, docstring에 `asdict`/`vars` 금지 명시 |
| m-10 `DeviceProfile.load` 지문 재생성 | **수정** | 누락 키만 기본값으로 보충하고 `device_id`와 기존 값은 유지, 보충 시 다시 저장 |
| m-11 `solve_pow` 무한 루프 | **수정** | `MAX_POW_DIFFICULTY=8` 초과 거부, `MAX_POW_ATTEMPTS=50_000_000` 상한 후 `V2RApiError` |
| m-12 느슨한 챌린지 판정 | **수정** | `_needs_challenge()` 신설 — 코드/사유에 `CHALLENGE\|POW\|PROOF`가 있거나 `extra`에 `challenge/nonce/difficulty/signature` 키가 있을 때만 PoW 재시도. `extra: {}`인 비밀번호 오류는 즉시 실패 |
| m-13 텔레그램 발신자 미검사 | **수정** | `TelegramChannel(allowed_user_ids=...)` + 설정 `TELEGRAM_ALLOWED_USER_IDS`(비우면 제한 없음). `test_telegram_allowed_user_ids` |
| m-14 슬랙 `ts` 문자열 비교 | **수정** | `_ts()`로 `float` 비교 |
| m-15 콘솔 출력 인코딩 | **보류(범위 밖)** | `v2r/__main__.py`는 동시 작성 중이라 수정 금지 대상. `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` 1줄은 해당 작업자가 넣어야 함 |
| m-16 창 넘김 시 첫 슬롯 정각 | **수정** | 이월 시에도 `first_for(w_start)` 적용. `test_plan_slots_창밖이면_다음날_창시작` 기대값 갱신 |
| m-17 `assign(count<=0)` vs 기본 0 | **보류(통합 층)** | 코드 결함이 아니라 호출부 계약 문제. `spec.account_count or spec.count`를 채우는 책임은 동시 작성 중인 엔진 층에 있음 |
| m-18 `parent_source_id: null` 상시 전송 | **보류** | 보고서 자체가 "첫 실제 1건에서 확인 후" 권고. 현재 페이로드는 api-spec 확인 항목이라 그대로 두고, 서버가 거부하면 조건부 포함으로 바꾼다 |
| m-19 즉시 발행 예약시각 불일치 | **수정** | `start_at is None`이면 예약시각 검사를 생략. `test_verify_article_skips_start_at_when_immediate` |
