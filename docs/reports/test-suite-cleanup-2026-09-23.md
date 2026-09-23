# 시험 전수 정리 보고 (2026-09-23)

실측 시각: 2026-09-23 09:56 (`date` 실행 기준)

## 결과 요약

| 단계 | 결과 |
| --- | --- |
| 시작 전(누적 보고) | 24건 실패 |
| 1차 전수 실행(본 작업 시작) | **24 failed, 15 errors**, 1319 passed |
| 조치 후 최종 전수 실행 | **0 failed, 0 errors**, 1343 passed (27분 21초) |

전수 실행 명령: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider`
(`--timeout` 플러그인이 없어 그 옵션은 빼고 실행함.)

## 원인별 분류·조치

### (c) 파수꾼 오탐 — 실행기가 실시간으로 쓰는 장부·보고서 (15건 ERROR)

`tests/conftest.py`의 `_no_real_data_dir_writes`·`_no_real_docs_reports_writes`
파수꾼이 "크기가 조금이라도 달라지면 실패"로 판정했다. 09:00 발행이 실행 중인
실행기가 `data/llm_usage-*.jsonl`·`docs/reports/*`에 정상적으로 계속 줄을
덧붙이는 것까지 오탐으로 잡은 것.

- 영향받은 시험(ERROR at teardown): `test_api_client`, `test_brand_writer`,
  `test_bulk_generate`, `test_emoji`, `test_engine`(사진 부족 에러),
  `test_gpt_images`(2건), `test_llm_router`, `test_naver_session`,
  `test_parser`, `test_plan_command`, `test_post_limit_20260922`,
  `test_self_cafe_daily`(2건), `test_warehouse_daily`
- 조치: [`tests/conftest.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/conftest.py)
  두 파수꾼을 "파일이 **줄어들거나 사라지면**만 실패"로 바꿈. 정상적인 덧붙임
  (증가)은 더 이상 실패로 잡지 않되, 과거 사고(빈 결과로 덮어쓰기·삭제)는
  그대로 잡아낸다.

### (b) 코드 버그 — `config_dir`이 시험용 가짜 `repo_root`를 따라간 것

`V2R_REPO_ROOT`(2026-09-23 새로 추가된 시험 격리용 환경변수)가
`config_dir`까지 함께 가짜 빈 폴더로 돌려버려서, "실제 `config/*.yaml`과
값이 같은지" 확인하는 시험들이 전부 빈 설정을 보게 됐다. `config/`는
읽기 전용 저장소 설정이므로 `data/`·`docs/reports`(쓰기 산출물)와 달리
격리할 필요가 없었는데 같이 묶여 있던 것.

- 영향받은 시험(FAILED): `test_decisions_20260919`(4건: 브랜드 별칭·토큰
  폴더·keyword_folder 경로·import_inbox), `test_engine`(8건: 태극 즉시발행,
  처리기 커버리지, 등록확인 보류, 자사 테스트 카페 게시판, 게시판 별칭 2건,
  발행제외 카페 2건, 댓글 역할표), `test_final_rules_20260922`(장으뜸 골든
  문장), `test_gpt_crosscheck`, `test_monitor`(18:00 미처리 알림),
  `test_plan_command`(요금제 세션 점검 09:25), `test_reply2_lab`(3건),
  `test_schedule`(09:00 일상 글), `test_stall_20260920`
- 조치: [`v2r/config.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/config.py)
  `_build_settings()`에서 `config_dir`을 `V2R_REPO_ROOT`가 아니라 항상 진짜
  `REPO_ROOT / "config"`로 고정. `repo_root` 필드는 그대로 두어 보고서·데이터
  산출물 경로 격리는 유지.
- 곁가지 버그 2건도 같은 원인이라 함께 고침: `v2r/engine/worker.py`의
  `_sheet_sync_keywords`, `v2r/knowledge/keyword_exposure.py`의
  `load_cafe_registry`/`brand_identifiers`가 `rt.settings.repo_root`로
  `config/brands.yaml`·`config/cafes.yaml`을 찾고 있었다. 실제 운영에서는
  `repo_root`가 곧 저장소 루트라 문제없었지만, 시험이 `repo_root`만 격리하는
  구조가 된 지금은 어긋난다. `rt.settings.config_dir.parent`로 바꿔 항상
  진짜 설정 폴더를 보게 함.

### (a) 시험 쪽 기대값·준비물이 낡은 것 (3건)

- `tests/test_engine.py::test_모든_허용작업에_처리기가_있다` — 오늘(09-23)
  새로 추가된 `sheet_sync_keywords` 작업(`v2r/sources/sheets_writer.py`,
  헤드리스 브라우저로 진짜 구글 시트를 편집)이 이 "모든 허용 작업에 대역이
  갖춰졌는지" 커버리지 시험에 대역 처리가 빠져 있었다. `config_dir` 수정
  직후에는 오히려 진짜 시트 잠금 파일(`data/locks/...`)까지 건드리며
  60초 대기 후 타임아웃으로 드러났다. `sheets_writer.sync_keywords_all`·
  `sync_keywords_to_sheet`을 가짜로 막음.
- `tests/test_sidecar.py::test_틱이_예약과_감시와_수신과_가벼운_작업을_모두_돌린다`
  — `미처리 알림` 가벼운 작업이 실제로 "성공"하려면
  `docs/reports/pending.md`가 있어야 하는데, 시험이 파일을 준비하지 않아
  (격리된 가짜 `repo_root`엔 당연히 없음) 늘 "실패"로 나왔다. 시험 목적은
  파일 내용이 아니라 가벼운 작업이 같은 틱에서 바로 실행되는지 확인하는
  것이므로, 시험 안에서 가짜 `pending.md`를 만들어 주도록 고침.
- `tests/test_keyword_exposure_cycle.py::test_brand_identifiers_설정에서_읽는다`
  — "설정 파일 없음 → 브랜드명 폴백" 동작을 확인하려고 `repo_root`를 빈
  폴더로 바꿔치기했는데, `config_dir` 분리 이후엔 그걸로 설정 파일을 숨길
  수 없다. `config_dir` 자체를 빈 폴더로 바꾸도록 시험을 고침.

`test_decisions_20260919*` (결정 기록 시험)는 위 (b) 조치만으로 전부
통과했고, 시험이 인코딩한 결정 내용 자체는 손대지 않았다.

## 최종 확인

```
PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider
1343 passed in 1641.44s (0:27:21)
```

파수꾼 오탐 제외 사유 없이 **실패 0건**.

## 바뀐 파일

- [`tests/conftest.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/conftest.py)
- [`v2r/config.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/config.py)
- [`v2r/engine/worker.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/engine/worker.py)
- [`v2r/knowledge/keyword_exposure.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/knowledge/keyword_exposure.py)
- [`tests/test_engine.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_engine.py)
- [`tests/test_sidecar.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_sidecar.py)
- [`tests/test_keyword_exposure_cycle.py`](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_keyword_exposure_cycle.py)

`v2r/engine/publish.py`(발행 로직)는 건드리지 않았고, `v2r/knowledge/keyword_relevance.py`도
읽기만 했다.
