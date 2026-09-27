# 노출 확인 DB 분리 준비 — 2026-09-27

## 요약

발행·노출 확인·키워드 작업 21개 프로세스가 `data/v2r.sqlite` 하나를 같이 써서
09-26 "database is locked"로 일상 글 발행이 중단됐다. 노출 확인 표
(`keyword_exposure`, `exposure_queue`)를 별도 파일 `data/exposure.sqlite`로
옮길 수 있게 **준비**만 끝냈다. 실제 전환(마이그레이션 실행 + 플래그 켜기)은
저부하 시간에 사용자가 진행한다.

## 분리 대상 표

| 표 | 정의 위치 | 접근 코드 |
|---|---|---|
| `keyword_exposure` | `v2r/store/db.py` SCHEMA | `v2r/store/keyword_exposure_store.py` |
| `exposure_queue` | `v2r/store/db.py` SCHEMA | `v2r/store/exposure_queue_store.py` |

두 표 모두 `v2r/store/keyword_exposure_store.exposure_db(rt)` 하나를 거쳐야만
접근하도록 코드를 모았다(`keyword_exposure.py`, `exposure_priority.py`,
`exposure_runner.py`의 모든 `rt.conn` 사용을 이 함수 호출로 바꿈). `dashboard.py`
는 원래부터 `keyword_exposure.summary(rt)`만 호출해 직접 SQL이 없어 손댈 곳이
없었다.

`article_index`(발행 쪽이 함께 읽는 표)는 분리 대상에서 뺐다 — 러너가 우리 글
URL을 찾으려고 `article_index`를 읽는 경로(`v2r/knowledge/keyword_exposure.py`
`_resolve_our_article`)는 계속 메인 DB(`rt.conn`)를 그대로 쓴다.

## 전환 스위치

`config/exposure.yaml`의 `db_path`(기본 빈 문자열 = 기존 동작 그대로). 값을
채우면(`data/exposure.sqlite` 등) `exposure_db(rt)`가 그 파일에 WAL·
busy_timeout(30초) 동일 적용으로 연결한 커넥션을 `rt`당 하나만 캐시해 돌려준다.

## 전환 절차(저부하 시간에 사용자가 진행)

1. 노출 러너(`exposure_runner.py`) 프로세스를 전부 멈춘다.
2. `python scripts/split_exposure_db.py --rename-old`로 표를 복사·검증한다(원본은 `_legacy_` 이름으로 남는다).
3. `config/exposure.yaml`의 `db_path`에 `data/exposure.sqlite`를 채운다.
4. 실행기·노출 러너를 다시 켠다.
5. 대시보드 노출 요약·`exposure_queue` 선점 로그가 정상인지 확인한다.

## 되돌리기

1. `config/exposure.yaml`의 `db_path`를 다시 빈 값으로 되돌린다.
2. `_legacy_keyword_exposure`/`_legacy_exposure_queue`를 원래 이름으로 되돌린다(필요하면).
3. 실행기·노출 러너를 다시 켠다.

## 위험

`--rename-old` 없이 스크립트를 두 번 돌리면 새 파일에 중복 삽입될 수 있으니
(INSERT 전 DELETE는 하지만) 마이그레이션 도중 러너가 다시 켜지지 않게 확인이
필요하다. 별도 파일 분리 후에는 `keyword_exposure`/`exposure_queue`를 직접 보는
임시 SQL·수작업 조회가 있으면(이번 조사에는 없었음) 경로를 놓칠 수 있다.

## 새 파일

- `scripts/split_exposure_db.py` — 마이그레이션 스크립트(`--dry-run`, `--rename-old` 지원).
- `tests/test_exposure_db_split.py` — 분리 스위치 on/off 동작·큐 선점/결과기록/요약 동일성 확인.

관련 코드: [v2r/store/keyword_exposure_store.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/keyword_exposure_store.py) ·
[v2r/store/exposure_queue_store.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/store/exposure_queue_store.py) ·
[config/exposure.yaml](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/config/exposure.yaml) ·
[scripts/split_exposure_db.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/scripts/split_exposure_db.py)
