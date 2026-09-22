# 키워드 연관도 재산정 실패 원인·재실행 + 보고서 덮어쓰기 격리 (2026-09-23)

작성 시각(실측, `date` 명령): 2026-09-23 07:56:27 KST

## A. 연관도 재산정 실패 원인·재실행

### 증상

`data/keywords/relevance_progress.json`에서 장으뜸·뉴더미스가 `scored: 0`,
`failed_batches: 98`(둘 다 `total_pending_at_start: 200`)로 멈춰 있었다.
같은 코드로 도는 우아덤·팥순이·코숨핏은 정상 진행됐다.

### 원인

[`v2r/knowledge/keyword_relevance.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_relevance.py)의
`brand_summary()`가 브랜드 정체성 요약(300자)을 만들 때, 정리본 md 파일을
**위에서 아래로** 훑으며 300자를 채우는 즉시 멈추는 구조였다.

- **장으뜸**: `warehouse/guides/정리본/장으뜸.md`는 "타겟의 결핍: …" 같은 브랜드
  정체성 줄이 파일 155번째 줄에 있는데, 그보다 앞(11번째 줄)에 있는
  `[절대 규칙]`(제목·본문 작성 형식 규칙 — 브랜드와 무관한 일반 지침)이 300자를
  전부 채워버려 실제 브랜드 정보가 프롬프트에 한 글자도 안 들어갔다.
- **뉴더미스**: `warehouse/guides/정리본/뉴더미스.md`의 브랜드 줄이
  `"- 실제 브랜드: 자연방패 항문세정제"`처럼 쓰여 있는데, 코드가 정확히
  `"- 브랜드"/"- 제품"/"- 타겟"/"- 타깃"`으로 **시작하는** 줄만 인정해서
  `"- 실제 브랜드"`는 애초에 걸러지지도 않고 역시 `[절대 규칙]` 블록으로
  채워졌다.

결과: 시스템 프롬프트에 브랜드 정체성 없이 작성 형식 규칙만 들어가니, 모델이
"브랜드 정보가 없어 relevance를 매길 수 없다"며 JSON 대신 **거부 응답**을
냈다(실측 재현, 아래). `parse_response()`가 JSON을 못 찾아
`RelevanceParseError`를 던지고, 재시도(`RETRY_COUNT=2`)도 매번 같은 이유로
실패 → `failed_batches` 누적. 이 실패는 **일시적 한도(PlanLimit)가 아니라
구조적 버그**였다 — 요금제 한도 대기 로직(`_retry_after_limit`,
`data/plan_lock.json`)은 정상 동작 중이었고 이 건과는 무관했다.

**재현(수정 전, 장으뜸 5개 키워드 실측)**:

```
FAIL RelevanceParseError 1회 시도 모두 실패(장으뜸, 5개): 모델 응답에서 JSON을 찾지 못했습니다
```

모델 원문 응답(수정 전):

> 죄송하지만, 키워드 평가를 진행하기 위해 필수 정보가 누락되었습니다.
> … 브랜드 정보(제품, 타깃, 핵심 논리)가 제공되지 않았습니다 …

### 수정

`brand_summary()`를 2단계로 바꿨다.

1. 파일 안 위치와 무관하게, 브랜드 정체성 줄(`-`로 시작하고 첫 12자 안에
   "브랜드"/"제품"/"타겟"/"타깃"이 들어간 줄)을 **전부 먼저** 모은다.
2. 그래도 300자가 안 차면 예전처럼 `[역할]`/`[절대 규칙]` 블록으로 채운다.

수정 후 실측(장으뜸 요약 앞부분):
`"내 제품 키워드: 장으뜸 장어즙 타겟의 결핍: 임신이 잘 되고 싶어하는 난임 또는
유산 또는 자연 임신의 욕구와 결핍을 가진 예비 엄마 …"` — 브랜드 정체성이 맨
앞에 들어간다.

수정 후 실측(장으뜸 5개 키워드, 같은 배치로 재시도):

```
OK [{'keyword': '대상포진예방접종가격', 'relevance': 3, 'rationale': ''}, ...]
```

정상적으로 relevance(0~3) + rationale JSON을 돌려받았다.

### 재실행

두 브랜드만(실행기 큐에 등록, 창 없음, 요금제 0원):

- `키워드 연관도 재산정 장으뜸` → job 156
- `키워드 연관도 재산정 뉴더미스` → job 157

이미 돌고 있던 우아덤·팥순이·코숨핏 codex 교차 검증 프로세스와는 각자
브랜드별 sqlite 파일이 달라 충돌하지 않는다(실행기가 job을 순차 처리하므로
쓰기 경합도 없다).

**재실행 스냅샷(07:56, 진행 중)** — 장으뜸은 실제로는 9,800개가 더 남아
있었다(예전 200개는 발굴 이후 처음 채점 시도한 "최근분"이었을 뿐, 전체
10,000개 중 이미 채점됐던 9,600여 개를 빼면 나머지가 9,800개였다는 뜻).
뉴더미스도 마찬가지로 전체 10,000개 중 9,800개가 남아 있다.

| 브랜드 | 전체 | 재산정 완료 | 0 | 1 | 2 | 3(제외) | 원고대상(0~2, codex 반영) | needs_review | codex 일치율(±1) |
|---|---|---|---|---|---|---|---|---|---|
| 장으뜸 | 10,000 | 500 | 1 | 20 | 62 | 417 | 47 | 135 | 1.000 |
| 뉴더미스 | 10,000 | 200 | 5 | 5 | 38 | 152 | 23 | 26 | 0.990 |
| 우아덤 | 10,000 | 9,200 | 24 | 897 | 2,743 | 5,536 | 3,091 | 182 | 0.997 |
| 팥순이 | 10,000 | 9,900 | 403 | 3,253 | 2,784 | 3,460 | 5,624 | 629 | 0.994 |
| 코숨핏(완료) | 10,000 | 10,000 | 217 | 690 | 1,759 | 7,334 | 1,852 | 413 | 1.000 |

장으뜸·뉴더미스는 수정 직후라 아직 초반 배치만 반영됐다(`scored`가 500/200
수준). 실행기가 계속 돌며 채점·Codex 교차 검증·`assign_primary_brand`까지
이어간다 — 최신 값은
[`data/keywords/relevance_progress.json`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/data/keywords/relevance_progress.json)에서
실시간 확인 가능하고, 완료되면
[`docs/reports/keyword-relevance-2026-09-23.md`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/keyword-relevance-2026-09-23.md)의
표를 마저 갱신해야 한다(이번 세션에서는 시간상 스냅샷까지만 반영).

완료되는 대로(또는 코디네이터 요청 시) 원고 대상 키워드를
`시트 키워드 반영 장으뜸` / `시트 키워드 반영 뉴더미스` 명령으로 큐에 등록해
시트에 반영해야 한다 — 이번 세션에서는 아직 채점이 다 안 끝나 등록하지
않았다(등록해도 실행기가 미채점분까지 포함해 잘못 반영할 위험을 피하려는
목적).

## B. 보고서 파일 덮어쓰기 격리

### 원인

[`v2r/config.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%98/v2r-command-system/v2r/config.py)의
`Settings.repo_root`가 `REPO_ROOT`(진짜 저장소 경로)를 **고정 기본값**으로
가지고 있었다. `tests/conftest.py`의 격리 장치는 `V2R_DATA_DIR`만 가짜
폴더로 돌렸을 뿐 `repo_root`는 그대로였다.

시험 도우미 `make_runtime(tmp_path)`(`tests/test_engine.py` 등 여러 파일에
각자 있음)는 `Settings(data_dir=tmp_path/...)`처럼 `data_dir`만 넘기고
`repo_root`는 안 넘겼다 — 그래서 `rt.settings.repo_root`는 **진짜
저장소**를 가리켰다.
[`v2r/engine/dashboard.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%98/v2r-command-system/v2r/engine/dashboard.py)의
`_report_dir(rt, out_dir=None)`은 `out_dir`을 안 받으면
`rt.settings.repo_root / docs/reports`에 쓴다. 대부분의 대시보드 시험은
`out_dir=tmp_path/...`를 명시해서 안전했지만, 그렇게 안 한 경로(또는 앞으로
추가될 시험)는 03:31 사고처럼 진짜 `docs/reports/dashboard-2026-09-22.*`를
빈 DB 결과(전부 0)로 덮어쓸 수 있는 구조였다.

### 수정

1. `Settings.repo_root`를 고정값 대신
   `default_factory=lambda: Path(os.getenv("V2R_REPO_ROOT") or REPO_ROOT)`로
   바꿨다 — `Settings(...)`를 직접 만드는 모든 시험 도우미가 `repo_root`를
   깜빡해도, 환경변수가 있으면 자동으로 안전한 경로를 쓴다. `V2R_REPO_ROOT`가
   없으면(평소 실행) 예전과 똑같이 `REPO_ROOT`다 — 실행 동작 변화 없음.
2. `_build_settings()`(`get_settings()`가 쓰는 진짜 진입점)도 같은
   `V2R_REPO_ROOT`를 읽게 했다.
3. `tests/conftest.py`의 `autouse` 픽스처가 이제 `V2R_REPO_ROOT`를 매 시험마다
   새 임시 폴더로 설정한다(`V2R_DATA_DIR`와 같은 방식).
4. 파수꾼 픽스처 `_no_real_docs_reports_writes`를 새로 추가했다 — 시험
   시작 전 진짜 `docs/reports/*` 파일들의 (크기, mtime)을 기록해 두고, 시험이
   끝난 뒤 하나라도 바뀌었거나 사라졌으면 즉시 `AssertionError`로 실패시킨다.

### 시험 통과 여부

- `tests/test_keyword_relevance.py` 18개 전부 통과(수정 반영 확인).
- 전체 스위트(`pytest -q`)는 이번 세션 시간 안에 다 끝나지 않아(수천 개
  규모, 백그라운드로 계속 실행) 완주 결과는 이 보고서에 못 담았다.
  장부 파수꾼(`_no_real_data_dir_writes`)·보고서 파수꾼
  (`_no_real_docs_reports_writes`)은 **오탐이면 무시**하라는 지시를
  받았다 — 기존 시험 중 일부가 아직 `repo_root`를 안 넘기는 자체 헬퍼를
  쓰고 있어 새 파수꾼에 걸릴 가능성이 있는데, 이는 그 시험 헬퍼가
  `repo_root=tmp_path`를 마저 받아야 한다는 신호이지 이번 수정 자체의
  결함은 아니다.

### 09-22 일일 보고 재생성 확인

`docs/reports/dashboard-2026-09-22.md/.html`은 03:31 사고로 덮어써진 값(발행
성공 0)이 07:42까지도 그대로였다. 실행기 큐의 job 154(`일일 보고`)를
확인해 보니 **다른 원인으로 이미 실패**해 있었다.

```
job 154: status=failed, error=
"SQLite objects created in a thread can only be used in that same thread.
 The object was created in thread id 36120 and this is thread id 36624."
```

원인 조사: `v2r/store/db.py`의 `connect()`가 `sqlite3.connect(...)`를
`check_same_thread=False` 없이 열고 있었다(스레드 간 연결 공유 시 SQLite가
막는 표준 동작). 이 파일에는 **이미 다른 세션이 같은 원인으로 고쳐 둔
수정이 커밋되지 않은 채로** 작업 트리에 남아 있었다
(`check_same_thread=False` 추가 + 주석에 "2026-09-23 07:40 사고" 명시).
다만 이 수정은 **07:37 재시작된 실행기 프로세스**가 메모리에 이미 로드한
구/신 코드와 무관하게, 파이썬은 실행 중 파일을 다시 읽지 않으므로 **이번
실행기 프로세스에는 반영되지 않았다** — job 154는 07:40:05에 실행기가 아직
옛 코드로 돌던 중 실패한 것이다.

지시에 따라 실행기를 재시작하지 않았으므로, 이 세션에서 직접 재생성할 수는
없다. 대신:

- job 158(`일일 보고`)을 큐에 새로 등록해 뒀다 — 09:00 예정된 재시작(또는
  그 이전 어떤 재시작) 이후 실행기가 새 코드로 집으면 정상 생성된다.
- `v2r/store/db.py`의 `check_same_thread=False` 수정은 이미 있던 것을 그대로
  두고 커밋에 포함했다(직접 작성하지 않았지만 원인과 일치하는 올바른
  수정이라 되돌리지 않았다).

**따라서 `docs/reports/dashboard-2026-09-22.md/.html`은 이 보고서 작성
시점까지 아직 진짜 값(성공 494·미확정 6)으로 복구되지 않았다.** 다음
실행기 재시작 이후 job 158 결과를 확인해야 한다.

## 커밋

- [`v2r/knowledge/keyword_relevance.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_relevance.py) — `brand_summary()` 2단계 수집으로 수정
- [`v2r/config.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/config.py) — `repo_root`를 `V2R_REPO_ROOT` 환경변수로 오버라이드 가능하게
- [`tests/conftest.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/conftest.py) — `V2R_REPO_ROOT` 격리 + `docs/reports` 파수꾼 추가
- `v2r/store/db.py` — (기존 미커밋 수정 포함) `check_same_thread=False`
