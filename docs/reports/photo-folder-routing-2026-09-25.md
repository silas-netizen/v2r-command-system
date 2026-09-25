# 사진 폴더 키워드 주제 라우팅 (2026-09-25)

## 지시

사용자 규칙(2026-09-25 16:40): 사진은 `warehouse/images/originals/<브랜드>/` 안의 키워드 폴더에서 랜덤 선택 후 세탁(washed/, 400px). 코숨핏은 키워드가 비염 관련이면 비염 폴더, 코골이 관련이면 코골이 폴더에서 고른다. 팥순이는 기존 {키워드}(filename_match, retry_direct_search) / {B/A}(BA 랜덤) 규칙 그대로.

## 변경 내용

1. `v2r/warehouse/store.py`
   - `_topic_folder_names(entry, keyword)`: 브랜드 config의 `keyword_topic_folders`에서 키워드(공백 제거·casefold)에 `match` 낱말이 포함되는 주제를 찾아 `folders` 목록을 돌려준다. 안 걸리면 `None`.
   - `Warehouse.topic_pool(brand, keyword, cfg)`: 걸린 주제의 `folders` 중 디스크에 실제로 있고 사진이 있는 폴더의 원본을 모두 합쳐 랜덤 순서로 돌려준다. `safe_name()`이 `&` → `_` 등 특수문자를 이미 정규화하므로 config의 `"비염_개선이유_기사&자료"`가 디스크의 `비염_개선이유_기사_자료`와 그대로 맞는다.
   - `Warehouse.ensure_keyword_pool(...)`: 시작 시 `topic_pool()`을 먼저 시도하고, 결과가 있으면 그걸 원본 목록으로 쓴다(브랜드 루트 시드 채우기 생략). 안 걸리면 기존 규칙(키워드 폴더 → 비면 브랜드 루트 원본으로 채움) 그대로.
   - 팥순이 등 `keyword_topic_folders`가 없는 브랜드는 `topic_pool()`이 항상 빈 리스트라 동작 변화 없음(단위 시험으로 확인).

2. `config/brands.yaml` — 코숨핏 항목에 추가:
   ```yaml
   keyword_topic_folders:
     - match: [비염, 축농증, 코막힘, 알레르기, 항히스타민, 이비인후과]
       folders: ["비염 괴로움_이미지", "비염_개선이유_기사&자료"]
     - match: [코골이, 양압기, 수면무호흡, 입호흡, 무호흡]
       folders: ["코골이밴드 사진", "코골이_개선이유_기사&자료"]
   ```
   다른 브랜드(뉴더미스·우아덤·장으뜸·팥순이)는 변경 없음.

## 단위 시험

`tests/test_warehouse_store.py`에 추가:
- `test_topic_pool_matches_rhinitis_keyword` — "비염치료" → 비염 두 폴더 합친 풀
- `test_topic_pool_matches_snoring_keyword` — "양압기 부작용" → 코골이 두 폴더 합친 풀
- `test_topic_pool_empty_for_unrelated_keyword` — 무관 키워드는 빈 풀
- `test_ensure_keyword_pool_routes_by_topic` — `ensure_keyword_pool`이 주제 폴더 원본을 그대로 반환
- `test_ensure_keyword_pool_falls_back_when_no_topic_match` — 무관 키워드는 기존 `키워드` 폴더 규칙으로
- `test_batsuni_ba_rule_unaffected_by_topic_routing` — 팥순이 `{B/A}`→BA, `{키워드}`→filename_match 그대로, `topic_pool`은 항상 빈 리스트

시험 결과: `tests/test_warehouse_store.py`, `test_warehouse_daily.py`, `test_photo_stock.py`, `test_decisions_20260919.py` 64건 전부 통과. `-k "publish or warehouse or photo"` 범위 130건 전부 통과(2분 44초).

## 실제 선택 확인 (읽기만, 세탁본은 생성됨)

`Warehouse.topic_pool()` / `keyword_folder()`를 실제 창고(`warehouse/`)에 대해 직접 호출한 결과.

| 브랜드 | 키워드 | 방식 | 선택된 폴더 | 후보 사진 수 |
|---|---|---|---|---|
| 코숨핏 | 비염 | 주제라우팅 | 비염 괴로움_이미지 + 비염_개선이유_기사&자료 | 6 |
| 코숨핏 | 비염치료 | 주제라우팅 | 비염 괴로움_이미지 + 비염_개선이유_기사&자료 | 6 |
| 코숨핏 | 양압기 | 주제라우팅 | 코골이밴드 사진 + 코골이_개선이유_기사&자료 | 8 |
| 코숨핏 | 항히스타민제 | 주제라우팅 | 비염 괴로움_이미지 + 비염_개선이유_기사&자료 | 6 |
| 코숨핏 | 이비인후과 | 주제라우팅 | 비염 괴로움_이미지 + 비염_개선이유_기사&자료 | 6 |
| 뉴더미스 | 두드러기 | 기존규칙(무변경) | 키워드 | 85 |
| 뉴더미스 | 병원 | 기존규칙(무변경) | 키워드 | 85 |
| 뉴더미스 | 먹는약 | 기존규칙(무변경) | 키워드 | 85 |
| 뉴더미스 | 좌욕 | 기존규칙(무변경) | 키워드 | 85 |
| 뉴더미스 | 연고 | 기존규칙(무변경) | 키워드 | 85 |
| 우아덤 | 미백 | 기존규칙(무변경) | 미백(토큰 폴더, 사진 0장) | 0 |
| 우아덤 | 색소침착 | 기존규칙(무변경) | 색소침착(토큰 폴더, 사진 0장) | 0 |
| 우아덤 | 피부착색 | 기존규칙(무변경) | 피부착색(토큰 폴더, 사진 0장) | 0 |
| 우아덤 | 제품 | 기존규칙(무변경) | 제품(토큰 폴더, 사진 0장) | 0 |
| 우아덤 | 키워드 | 기존규칙(무변경) | 키워드 | 30 |
| 장으뜸 | 장어즙 | 기존규칙(무변경) | 키워드 | 55 |
| 장으뜸 | 피로회복 | 기존규칙(무변경) | 키워드 | 55 |
| 장으뜸 | 영양 | 기존규칙(무변경) | 키워드 | 55 |
| 장으뜸 | 건강즙 | 기존규칙(무변경) | 키워드 | 55 |
| 장으뜸 | 키워드 | 기존규칙(무변경) | 키워드 | 55 |

우아덤 4건(미백·색소침착·피부착색·제품)은 `{키워드}` 토큰이 아닌 실제 키워드 문자열이라 기존 규칙상 토큰 이름 그대로의 하위 폴더로 잡혀 사진 0장이다. **이번 작업 범위 밖의 기존 동작**(우아덤 config는 변경하지 않음)이며, 실제 발행 시엔 `ensure_keyword_pool`이 브랜드 루트 원본으로 자동 채운다.

## 배치 파일 (변경만)

- [file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/warehouse/store.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/v2r/warehouse/store.py)
- [file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/config/brands.yaml](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/config/brands.yaml)
- [file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_warehouse_store.py](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/tests/test_warehouse_store.py)

## 실행기 재시작 필요

이 창고 라우팅 규칙은 프로세스 시작 시 `config/brands.yaml`을 읽으므로 **실행기 재시작 필요**.
