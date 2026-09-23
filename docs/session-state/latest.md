# 세션 맥락 저장 (컴팩트) — 2026-09-24 00:43 KST

## 세션 개요

- 저장 시각: 2026-09-24 00:43 (`date` 실측). 세션 시작 2026-09-23 새벽부터 이어진 V2R 채팅 명령형 운영 세션.
- 지금 하던 일: 노출 확인 시스템 재정비(판정 규칙·속도), 키워드 1만 개(연관도 0에서 3) 채우기, 연관도 3 세분화, 시트 새 행 드롭다운·서식 통일, 슬랙 알림 정책 재설계. 백그라운드 일꾼 4개가 돌고 있음(아래 "진행 중").
- 직전 사용자 메시지 5개(원문):
  1. "우아덤 뿐 아니라 5개 모두 적용"
  2. "근데 현실적으로 3까지 확장은 해야 될 것 같아. 우아덤의 경우 무관만 보면 피부과 안과 마운자로 독감예방접종 치과 이건 진짜 무관이면서 검색량도 커서 현실성 떨어짐 / 대상포진 독감 위고비 근처피부과 사마귀 > 이런 건 연관성이 0은 아니기 때문에 당위성 논리 높이면 가능 / 난 이렇게 판단 해"
  3. "시드 다양화 선택지는 바로 적용 하자."
  4. "그리고 각 시트별로 새로운 키워드 추가 할 때 우아덤처럼 그냥 텍스트로 적지 말고, 드랍다운이 있는 열은 복사 해서 서식 통일 시켜줘"
  5. "브랜드 5개 시트 다 노출 확인 되는 거지? 팥순이 로직과 다 같아. 식별어만 다른 거니까."

## 절대 규칙 (이 세션까지 누적, 메모리 파일에도 저장됨)

- 채팅 형식: 모든 답은 `docs/reports/*.md` 파일 + 채팅에는 `[파일](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/...) — 한국어 한 문장`만. 진행 문장·영어·상태 줄 금지. 시스템 알림·일꾼 완료 알림에는 파일이 필요 없으면 아무 텍스트도 내지 않음. 파일 언급은 항상 `file:///` 절대 링크(백틱 파일명 금지). 보고 시각은 `date` 실측.
- **물결표(~) 범위 표기 금지**(취소선으로 보임) → "0에서 2" 또는 엔 대시(–). (2026-09-24)
- 창 띄우기 금지(터미널·크롬, offscreen도 금지). 헤드리스·숨김(VBS+CMD, CMD는 CRLF·`chcp 65001`·상대 경로; VBS 따옴표는 `""path""` 형식, 처음 실행은 `cscript //nologo`).
- 로그인 1회 후 영구 유지, 비밀번호·토큰·API 키는 절대 다루지 않음. 구글 시트 권한 질문 금지(링크 편집 가능 → 헤드리스 익명 편집).
- 알림: 텔레그램 푸시 중지, **슬랙만**(config/notify.yaml `push_channels: [slack]`). 슬랙에는 4종류만: 🔴 진짜 실패(점검 뒤 확정), 📊 정기 보고(09:00 시작 현황판·12/15/18 중간·02:30 일일), 💬 명령 최종 결과 1건, 🟢 자유 대화 답변. "명령을 해석하지 못했습니다" 문구 금지.
- 노출 판정: 자동완성 1순위 표기로 통검 첫 페이지 끝까지 → 우리 제휴·자사 카페 카드의 **대표 글만** 후보(같은 카드의 서브 링크=같은 카페 다른 글이면 밀려남; 댓글 미리보기는 대표 글의 일부) → **댓글에서만**, **댓글2 계열(댓글2→대댓글2→대대댓글2→대대대댓글2)** 에서 브랜드 식별어(팥순이·팥순추출물·팥순ㅇㅣ / 코숨핏 / 뉴더미스·자연방패·자연방패 항문세정제 / 우아덤·그린커피 아하바하·아하바하 / 장으뜸·장으뜸 장어즙, 공백·대소문자 무시) → 노출완. 노출완→밀려남 전환은 5에서 10분 뒤 재확인 후 확정.
- 시트 갱신(노출 검사 후): A 카페(노출완이면 갱신), G 노출 상태, J 최종 편집 일시(`YYYY-MM-DD HH:MM:SS`), K 키워드 검색량(모바일+PC 합), L 노출된 검색량(노출완이면 K, 밀려남이면 0). H 불변, I 비었을 때만 통검 링크. **B에서 F는 건드리지 않음.** 상단 P1/Q1 숫자 대신 P2 표("키워드 총 검색량"/"노출 키워드 총 검색량"). 새 행 추가 시 기존 행의 서식·드롭다운(데이터 확인) 복사.
- 연관도 척도 **0에서 4**: 0 직접, 1 근접, 2 확장, **3 당위성**(생활·증상·상황으로 이어 붙일 논리 있음 → 원고 대상, `bridge_rationale` 저장·원고 프롬프트에 주입), **4 무관**. 원고 대상 = 클로드·Codex 둘 다 0에서 3. 브랜드마다 판단 다름. 우아덤 예: 3=대상포진·독감·위고비·근처피부과·사마귀, 4=피부과·안과·치과·마운자로·독감예방접종.
- 키워드 1만 개 = **연관도 있는(원고 대상) 키워드 1만 개**(무관 포함 아님). 시드 다양화(정리본 표현·경쟁 제품명·자동완성·연관검색어) 적용.
- 통검 1등 글 형식 참고: **카페 글 중 1위**(첫 카페 카드 대표 글)만 참고, 형식만(문장 복사 금지). 적용 여부는 사용자가 원고 전문 비교를 읽고 피드백 예정(현재 `config/bulk.yaml top_reference.enabled: true`).
- 낱말 명령: "컴팩트"/"재부팅"(슬래시 없이도) → `.claude/skills/컴팩트|재부팅/SKILL.md`. 운영 순서: 컴팩트 → 저장 링크 확인 → `/compact` → 재부팅. `/autocompact auto`(1M).
- 권한: 자동 모드. "모두 허용(bypass)"은 이 계정에서 제공 안 됨. 분류기가 막는 것(DB 직접 수정·슬랙 브리지 에이전트 등)은 사용자에게 보고.

## 이번 세션 결정 (2026-09-23 → 24)

- 09:00 발행 실패 원인 = 계정 시트 탭 필터로 gviz가 31행만 → export CSV 폴백·`no_accounts` 재시도 규칙. 발행 16분 지연 후 정상(500/500).
- 웹 세션 점검 오경보 = 상주 크롬(CDP 9333)이 프로필 잠금 → CDP로 점검.
- 되읽기 GET 1회 실패를 "발행 실패"로 세던 것 → 재시도(3·5·8·13·20초)→확인 대기, 발행 종료 시 자동 끊긴 작업 점검, 18:15 저녁 점검 예약.
- 시트 쓰기 사고(2026-09-23 16시대): 격자가 데이터 크기로 잘려 붙여넣기 실패·A1 오염·팥순이 빈 행 3,023개 삽입 → 격자 이진 탐색 후 마지막 행 아래 삽입, 이름 상자 이동 확인 후에만 붙여넣기, 검증 정규화(콤마), 200행 단위. 빈 행 삭제·A1 복구 완료(사용자 승인).
- 팥순이 10행 교차 검증(GPT 5.6-sol) 10개 일치, 최종 노출완 2(한끼통살·비만도 계산기)·밀려남 8(베르베린은 서브 링크). 비만도 23:31 건은 파서 결함 아닌 일시 변동.
- 노출 순환은 00:20에 **중지**(옛 순환은 같은 키워드 반복 검사 결함) → 새 러너(상주 브라우저·조기 종료·우선순위 큐·작업자 2→3)가 정식 경로. 새벽 03:00경 정식 가동 목표.
- 슬랙 직접 대화 브리지(Claude CLI): 분류기가 막음 → 보류. 자유 대화 해석기(실행기 내 LLM)는 구현·적용됨.
- 컴팩트/재부팅 스킬, CLAUDE.md 낱말 명령표 생성.

## 완료한 작업 (커밋, 모두 푸시됨 — 마지막 푸시 후 커밋 62cc912·f6f99be·a3dd30b·00612cd는 미푸시일 수 있음 → 재부팅 때 `git status`/`git log origin/main..HEAD` 확인 후 푸시)

- f8dedab 계정 시트 필터 보정 / cc73cfa 웹 세션 CDP 점검 / 3a7c14c·567087f·23f13fe 시트 쓰기 안전장치 / 35a5414 P2 표 / df71235 되읽기 오경보 근본 해결 / ecefaaa·49aa70f 슬랙 정책 재설계+자유 대화 / 4332960·babb9f4·bc16ced top_reference(카페 전용) / 3d9bfc3·6235192·c324bfd 노출 판정 규칙(댓글 전용·카드 대표/서브·댓글2 계열·시트 A/G/J/K/L) / 4c4f7a9·62cc912·00612cd 러너(상주·조기 종료·우선순위·전역 정지·2단계 확인) / f6f99be 키워드 채우기 순환.
- 보고서(docs/reports): reply-0900-fail-slack, reply-claude-login-alert, daily-publish-fail-rootcause-2026-09-23, sheet-sync-incident-2026-09-23, reply-sheet-repair-exposure-2026-09-23, slack-policy-2026-09-23, top-reference-2026-09-23 (+ -manuscripts), exposure-audit-patsuni-2026-09-23, exposure-speed-plan-2026-09-23, reply-exposure-speed-risk, reply-priority-recheck, reply-keyword-10k-eligible, keyword-fill-loop-2026-09-24, reply-keyword-fill-outlook, reply-relevance-split, reply-patsuni-10-final, reply-five-brands-same-logic, pending-confirmations-2026-09-23, test-suite-cleanup-2026-09-23, reply-compact-reboot-skills, reply-word-commands.

## 진행 중 (재부팅 때 실제 상태 재검증)

- 실행기: 00:10:59 재시작(pid는 serve.log 심장박동 확인). 발행 오늘 500/500 done. 실행 중 작업 없음. 노출 순환 `enabled: false`(중지).
- 백그라운드 일꾼(내 세션 Agent):
  1. 속도 러너(a43bdadbac4cb7738): 작업자 2개 30분 → 3개 15분 실측 후 `docs/reports/exposure-speed-2026-09-24.md` 갱신·commit 예정. 상태 `data/exposure_runner_state.json`, 로그 `logs/exposure-runner-<n>.log`, 스크립트 `scripts/exposure-runner-hidden.vbs`.
  2. 키워드 채우기 순환(aedb9d52906319dd3, 하위 에이전트로 실행): 시드 다양화 4출처 적용 후 재시작·첫 회차 실측 → `keyword-fill-loop-2026-09-24.md` 갱신. 진행 `data/keywords/fill_progress.json`(생성 예정), 로그 `logs/keyword-fill-<브랜드>.log`, 스크립트 `scripts/keyword-fill-hidden.vbs`.
  3. 시트 서식·드롭다운 통일(ab1658a260c90fd86): `copy_row_format` + 오늘 추가 행 보정 → `sheet-format-unify-2026-09-24.md`.
  4. 연관도 3 세분화(a8b3648fefe855805 → 하위 에이전트): 0에서 4 척도, `bridge_rationale`, 기존 3 약 3만 7천 개 재채점(5개 브랜드 병렬, `scripts/relevance-split-hidden.vbs`, 진행 `data/keywords/split_progress.json`) → `relevance-split-2026-09-24.md`. 채우기 순환은 이 커밋 후 `is_manuscript_target`로 교체 예정.
- 세션 크론(세션 한정): 12:02/15:02/18:02 중간 보고 전달, 02:36 일일 현황판 전달. (일회성 06:41·09:09·18:13은 소진/무효.)
- 예약(config/schedule.yaml): 01:00 키워드 발굴 전체, 07:30/07:45 색인, 08:30·18:15 끊긴 작업 점검, 09:00 아침 일상 글, 09:20 웹 세션, 09:25 요금제 세션, 02:30 일일, 12/15/18 중간 보고, 18:00 미처리.

## 미결·대기 (사용자 확인 필요)

- 키워드 시트 반영 재실행(팥순이 2,757·코숨핏 363·우아덤 약 1,020건) — 서식 통일·연관도 재채점 끝난 뒤 새 조건(0에서 3)으로 재실행 여부.
- 통검 1등 글 형식 참고 적용 여부(사용자 원고 전문 비교 읽고 피드백 예정).
- 슬랙 직접 대화 방식(브리지 승인 / 원격 제어 / 해석기) — 보류 중.
- 대량 원고 첫 시험(`대량 원고 전체 2건`), 996건 옛 글 검색 노출 변경, 웨딩 노트·헬씨 트리 가입, Make 로그인, 텔레 푸시 재개, "모두 허용" 권한.
- 다음 할 일 순서: ① 러너 실측 결과 확인·정식 가동(03:00 목표) ② 연관도 재채점 완료 → 원고 대상 수 재보고 → 채우기 순환 조건 교체 ③ 서식 통일 완료 확인 ④ 브랜드별 10개 GPT 교차검증 아침 보고 ⑤ 미푸시 커밋 푸시.

## 핵심 파일·경로

- 실행기: `v2r/engine/worker.py`, `sidecar.py`, `publish.py`, `monitor.py`, `recovery_rules.py`; 채널 `v2r/channels/__init__.py`, `freeform.py`; 시트 `v2r/sources/sheets_writer.py`, `sheets.py`; 노출 `v2r/knowledge/keyword_exposure.py`, `exposure_runner.py`, `exposure_priority.py`, `config/exposure.yaml`; 키워드 `keyword_relevance.py`, `keyword_fill_loop.py`, `naver_keyword_tool.py`; 원고 `v2r/content/top_reference.py`, `bulk_generate.py`, `brand_queue.py`, `brand_writer.py`.
- 설정: `config/notify.yaml`, `schedule.yaml`, `brands.yaml`(identifiers), `bulk.yaml`(top_reference), `cafes.yaml`, `accounts_sources.yaml`(계정 시트 1UgcAv…, gid 218285244).
- 브랜드 시트 ID: 팥순이 1OwR_LSjO1ofOojldtSIqoxv0gieNSMx_t35_5G1VTCc(노출 현황 gid 1325327696), 코숨핏 gid 607459493, 우아덤 gid 1454846576, 장으뜸 gid 295921914, 뉴더미스 gid 1782605844(각 두 번째 탭). 시트 백업 `data/sheet_backups/`.
- 슬랙 채널 카페-클로드 C0C3GD9CA05. Codex CLI `C:\Users\user\AppData\Local\OpenAI\Codex\bin\fd4c151a749f3ab4\codex.exe`(gpt-5.6-sol medium / gpt-6-astra low). 브라우저 `.pw-browsers`(`PLAYWRIGHT_BROWSERS_PATH`). 상주 크롬 CDP 9333(프로필 browser-profile-claude).
- 스킬: `.claude/skills/컴팩트`, `.claude/skills/재부팅`; 메모리 `C:\Users\user\.claude\projects\D--v2r----\memory\` (새 파일: feedback-exposure-representative-link, feedback-no-tilde, project-relevance-scale, project-compact-reboot-skills).

## 숫자 현황 (00:43 실측)

- 오늘(09-23) 발행 500건 완료, 실패 0. 노출 확인 오늘 1,159건(건당 45초) → 러너로 개선 중. 원고 대상(0에서 2, 재채점 전): 우아덤 1,203 / 장으뜸 283 / 팥순이 3,447 / 뉴더미스 474 / 코숨핏 363. 시트 P2 표 값: 팥순이 4,453,285/58,230 등.

## 주의할 함정

- gviz CSV는 시트 필터를 따름(export CSV는 무시). 격자가 데이터 크기로 잘린 탭이 있음(팥순이·코숨핏). 이름 상자 이동 실패 시 붙여넣기가 A1에 떨어짐(가드 있음). Ctrl+End는 데이터 끝이지 격자 끝이 아님.
- AppData 가상화(MSIX): 브라우저·도구는 저장소 안. VBS 따옴표·CMD 인코딩 실수로 오류창이 뜬 적 있음(사용자 강하게 지적).
- 실행기 재시작 시 running 작업의 임대 15분(lease) — 죽은 pid 임대는 DB로 풀 수 있으나 분류기가 막음.
- 일꾼이 지시를 어기고 하위 Agent를 만들어 "진행 중"이라며 종료하는 일이 두 번 있었음(그래도 하위가 완료 알림을 줌).
- 노출 판정은 네이버 결과 실시간 변동이 있음(비만도 계산기) → 2단계 확인 규칙.
- 이 채팅의 시각 감각이 실제와 어긋날 수 있음 → 항상 `date` 실측.

## 재부팅 직후 처리할 것 (00:45 추가)

- 연관도 3 세분화 일꾼(a8b3648fefe855805)의 하위 에이전트가 "5개 브랜드 전체 재채점" 승인을 사용자에게서 직접 듣겠다며 멈춰 있음. 사용자는 이미 "우아덤 뿐 아니라 5개 모두 적용"이라고 승인했음 → 재부팅 후 그 일꾼에게 사용자 원문을 인용해 다시 전달하거나, 멈춰 있으면 새 일꾼으로 같은 사양(docs/reports/reply-relevance-split.md 기준)을 다시 띄운다. 진행 파일 `data/keywords/split_progress.json`·`docs/reports/relevance-split-2026-09-24.md` 존재 여부로 판단.
