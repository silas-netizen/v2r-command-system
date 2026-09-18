# 옛 코드(v2r-auto-posting)의 업무 규칙 정리 — 재설계용

> 출처: 브랜치 `cursor/internal-command-system-415f` 분석 (2026-09-19). `app/` = 내부 명령 시스템 초기판, `v2r_auto/` = 기존 GUI/EXE.
> 모든 자료는 가상 테스트 사이트(V2R) 기준.

## 1. 명령 문법 (정규식, 순서대로 첫 매치)

| 우선순위 | task | 정규식 | 예시 |
|---:|---|---|---|
| 1 | `inspect_failures` | `(실패\|미완성).*(점검\|재시도\|모아)` | "최근 7일 실패 글 점검해줘" |
| 2 | `sync_all_sources` | `전체\s*(원본\|시트).*(동기화\|갱신)` | "전체 원본 지금 동기화" |
| 3 | `sync_sources` | `(원본\|시트).*(동기화\|갱신)` | "시트 갱신해줘" |
| 4 | `collect_daily` | `일상\s*글.*(수집\|가져와\|크롤)` | "일상 글 수집해줘" |
| 5 | `open_login` | `로그인\s*(창\|세션\|준비)` | "로그인 창 열어줘" |
| 6 | `stop` | `(중지\|멈춰\|중단\|취소)` | "작업 중지" |
| 7 | `status` | `(상태\|현황\|진행)` | "상태 알려줘" |
| 8 | `publish_brand` | `(브랜드\|수정)\s*글` | "브랜드 글 올려줘" |
| 9 | `publish_info` | `정보성\s*글` | "정보성 글 발행" |
| 10 | `publish_batch` | `(일괄\|배치)\s*(발행\|등록)` | "일괄 발행 해줘" |
| 11 | `publish_daily` | `(일상\s*글\|올려\|발행\|등록)` | "일상 글 20개 올려줘" |

슬롯 추출:
| 필드 | 규칙 | 기본값 |
|---|---|---|
| 시간창 | `(오전\|오후)?\s*(\d{1,2})시(\s*(\d{1,2})분)?` 2개 → 시작/종료 | 09:00~18:00 |
| count | `(일상\s*글\|글)\s*(\d+)\s*개` | 0 |
| account_count | `(아이디\|계정)\s*(\d+)\s*개` | 0 |
| interval | `(\d+)\s*분\s*간격` | 10 |
| cafe / brand | 리터럴 목록 매칭 | "" |
| start_date | 모레/내일/오늘/`YYYY-MM-DD` | 오늘(KST) |
| account_mode | "자동" 포함 또는 `아이디 \S+` 없음 → auto | auto |
| dry_run | `(실제\|바로)\s*(발행\|등록)` 있으면 False, **기본 True** | True |

LLM 폴백: 규칙 미매치 + API 키 있을 때만. 텔레그램/슬랙 경유는 폴백 없음(허용 명령만).

## 2. 작업 큐 / 상태

SQLite (`data/v2r.sqlite`, WAL):
- `jobs(id, idempotency_key UNIQUE, task, payload_json, status, lease_owner, lease_until, result_json, error, created_at, updated_at)`
- `history(id, cafe, board, title, body, url, created_at)` — 중복 판정 코퍼스
- `source_cache(key, payload_json, synced_at, status)`
- `executor_lease(id=1, owner, until)` — 전역 단일 실행기 잠금(다중 PC 방지)
- `photo_combos((source_sha256, slot), combo_index, job_id, path)`
- `source_publications((source_key, row_number, content_hash), status, url, account, cafe, updated_at)` — **중복 발행 방지 키**

상태: `queued → running → {registered | published | failed | uncertain | cancelled}`. 리스 900초.

체크포인트: 등록 버튼 **직전** `submitting` → `uncertain` 저장. `published` 단계에서만 `registered`. `publication_exists()`는 `{registered, published, uncertain}` 모두 존재로 간주 → 끊긴 행은 자동 재발행 안 하고 수동 확인 대상.

내용 중복 검사: NFKC+casefold+`[0-9a-z가-힣]`만 → bigram Dice. 완전일치=완전중복, ≥0.92=유사 검토.

## 3. 계정

- `Account(login_id, work_type, linked, excluded, grade, nickname, shade)`. 비밀번호 필드 없음.
- 자사/제휴 구분 = `work_type` ∈ {"자사 카페", "제휴 작업"}, `linked == "V2R"` 필수.
- task→work_type: `publish_info`→자사, `publish_brand`→제휴, 그 외 자사. **카페가 씨씨앙/양평맘/쌍둥이맘(모여라)면 무조건 제휴.**
- 회색 음영 제외: 엑셀 B열 셀 배경색 ∈ {D9D9D9, B7B7B7, CCCCCC, 999999, 808080} → excluded. CSV는 `제외`/`음영` 열 truthy({y,yes,true,1,회색,음영}). 매니저 등급({m, 매니저, manager}) 제외.
- 배정: 명시 계정이 풀에 없으면 에러(대체 없음). 자동은 부족하면 **에러**(채우지 않음). 로테이션 단순 라운드로빈. (LRU/180일 이력 기반 배정은 구 GUI 코드에만 있음 → 새 시스템에서 구현)
- 계정 시트: spreadsheet `1UgcAvHFCpC5N9joC9T5WCATK834F3XAtRrepFv6XbEs`, gid `218285244`, 시트명 "아이디 리스트". 열 B=ID, H=작업 구분, I=연동, J=등급.
- 시트 읽기: `…/gviz/tq?tqx=out:csv&gid=<gid>`, 실패 시 마지막 캐시 유지.

소스 목록(`config/sources.json`):
| name | kind | spreadsheet ID | gid |
|---|---|---|---|
| 일상글목록 | sheet | `1vSON0Rej9anDQXcAOXyBrCr50B4MMqZ79FahF4cDPJw` | 2015103906 |
| 랜덤일상 | sheet | 동일 | 1842684291 |
| 계정시트 | accounts | `1UgcAvHFCpC5N9joC9T5WCATK834F3XAtRrepFv6XbEs` | 218285244 |
| 각색 전체 | sheet | `1WlNgw9Jzr7dWYAabqVB3yn220--uucVb` | 21220889 |
| 기존일상시트 | sheet | `14N_Mjji4mWqi9ib_4T_AGI1ruYdHOeKT` | 755409539 |
| 정보성 글 | sheet | `1vSON…` | 1193993260 |
| 제외 문서 | — | `1DLQgLWBo1c4CDkgvH4fjkuDrRM1C03XT` | 동기화 금지 |

## 4. 카페 / 게시판 / 시간

| 제휴 카페 | 게시판 | menu_id | cafe_id | 수정글 지연 |
|---|---|---:|---:|---:|
| 씨씨앙 | 자유수다방(구) (수정글은 원고 지정 시 328) | 2458 | 25016228 | +4h |
| 양평맘 | 이모저모 이야기 | 14 | 22788814 | +20h |
| 쌍둥이맘 모여라 | 가족업체 자유게시판 | 664 | 10174516 | +22h |

자사 카페 ID: 쌍둥이맘 모여라 10174516, 고요한 아침 14567700, 글로시 마이 15175096, 웨딩 노트 15441090, 송도포털 16149995, 헬씨 트리 23708088, 러브 인썸 26616683, 마이 웨딩 드림 26680163. 테스트 카페 31670254(태극), 31670256(소나무).

시간 규칙: `Asia/Seoul`. 시간창 시작 포함/종료 제외, 자정 넘김 지원, 창 밖이면 다음 날 시작으로 이월. 구 규칙(새 시스템에 반영): 자사 첫 글 +5~15분 랜덤, 같은 카페 다음 글은 이전 예약 +5~15분, 카페별 체인 독립, 테스트 카페 한 줄 글은 즉시 발행, 이미 넣은 예약은 설정 변경으로 고치지 않음.

이름 매칭: `[^0-9a-z가-힣]` 제거 + casefold → 정확 1건 채택, 여러 건이면 에러(임의 선택 금지).

## 5. 콘텐츠

- 원고 `Manuscript(title, body, cafe, board, source, keyword, account, comments[], source_row, content_hash)`.
- 원고 본문 파서: `제목 :` / `본문 :` / `댓글N :` / `대댓글N :` / `대대댓글N :` 계층. 태그 = 키워드 공백 제거.
- 제휴/브랜드 시트 A~J: A 키워드, B 본문, C 카페명, D 작성계정, E 원고유형(질문형/후기형), F 완료링크(`^https?://`만 완료), G 말머리, H 계정유형(실명/비실명; D·H 둘 다 비면 건너뜀), I 이미지없음(`y`), J 게시판명(씨씨앙 전용).
- 각색 CSV/Excel 필수 헤더: `카페명 / 게시판명 / 각색제목 / 각색본문`.
- **제휴 흐름**: 랜덤일상 풀에서 원본 일상 글 → 등록·검증(`daily_registered`) → 원본 예약 + 지연시간에 수정글(`parent_source_id`) 등록·검증 → 댓글 예약 → `published`. 결과 URL = 수정글.
- 댓글 트리 12개: 댓글1~5(depth0), 대댓글 5개(depth1), 대대댓글2(depth2), 대대대댓글2(depth3). 일반 댓글은 댓글 전용 계정 무작위, 대댓글은 본문 작성계정. 시간 충돌 시 12개 전체 일괄 이동.
- 일상 글 수집: 공개 페이지(HTML/OG/JSON-LD/RSS)만, 로그인 필요 페이지는 `blocked`.

## 6. 이미지

- Drive 루트 폴더 ID `13ouLpDi-mSJctFuw3iKCg-z4FZLPjKiK`. 본문 `{폴더명}` 플레이스홀더 → `루트/브랜드/폴더명` 무작위. 브랜드 = 시트 제목 마지막 괄호.
- 팥순이/장으뜸/뉴더미스는 `{키워드}` → 폴더 "키워드"; 팥순이 `{B/A}` → 폴더 "BA".
- 한 실행 내 중복 사진 배제. 다운로드 실패 시 다른 후보로 교체.
- 사진 세탁(옛 방식): 외부 GUI "포토 워셔 v2.0"을 pywinauto로 조작 → 매우 취약. **새 시스템은 자체 EXIF 변경으로 대체.** 대상 필드: `Make, Model, DateTime, DateTimeOriginal, DateTimeDigitized, ExposureTime, FNumber, ISOSpeedRatings, FocalLength, LensMake, LensModel, BodySerialNumber, LensSerialNumber`. 전부 삭제 금지(가산점 유지). 성공 판정 = 카메라 메타 변경 또는 파일 해시 변경. PNG/WebP는 흰 배경 합성 후 JPEG q95.

## 7. 알림

- 텔레그램: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_CHAT_IDS` 둘 다 필요. 허용 chat_id 아니면 거부. `getUpdates` 폴링.
- 슬랙: webhook 발신만. (옛 코드는 채널 검증 미설정 시 전부 허용 → 새 시스템은 반드시 허용 목록 필수)
- 보고 포맷: `작업 {id} {status}: {설명}` / `작업 {id} 실패: {메시지}`.

## 8. 모델 분담 (사용자 지정, 새 시스템에서 구현)

| 용도 | 모델 |
|---|---|
| 모호한 명령 해석 | Haiku 4.5 (저비용) |
| 일상 글 댓글 | Haiku 4.5 |
| 홍보 글 댓글 | Sonnet 5 |
| 일상 글 수집·각색 | Sonnet 5 |
| 이미지 생성 | GPT (사용자 수동) |

## 9. 옛 코드 약점 → 새 시스템 해결 목록

1. 비밀번호가 문서에 평문 커밋 → `.env`만 사용, git 제외.
2. 크래시 후 자동 재개 없음 → `uncertain` 건을 V2R 이력(`board_histories`/`article`)으로 자동 점검해 `registered`/`failed` 확정.
3. 계정 배정 LRU/180일/등급/댓글계정 제외 미구현 → 배정 엔진에 구현.
4. 코드 27000 계정 제한 30일 제외 미연결 → 계정 상태 테이블로 통합.
5. 중복 판정 실패 시 배치 전체 중단 → 행 단위 skip + 보고.
6. 슬랙 허용 채널 미설정 시 전부 허용 → 허용 목록 필수.
7. 설정 파일과 하드코딩 이중화 → 단일 `config/*.yaml`.
8. 게시판 이름 불일치 → V2R 카탈로그 API로 실시간 조회 + 별칭 테이블.
9. 5~15분 랜덤 간격, 카페별 체인 미구현 → 스케줄러에 구현.
10. 이미지·세탁이 발행 흐름과 미연결 → 창고(warehouse) 모듈로 통합.
11. `publish_batch` 등 미구현 분기 → 모든 명령에 실행기 연결.
