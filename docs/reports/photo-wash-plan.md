# 사진 세탁 시스템 — 계획과 실행 (2026-09-19)

## 목표
- 원본 1장 → 날짜·카메라 정보만 다른 변형 여러 장 → 창고 보관 → 발행 때 미사용 변형 배정
- 브랜드/키워드 폴더에 사진이 없으면 자동으로 채우고, 원본 자체가 없으면 새 사진을 확보

## 규칙 0 (절대 규칙) — 세탁 안 된 원본은 절대 첨부하지 않는다
- 발행에 붙는 사진은 **반드시 `images/washed/` 아래 경로**여야 한다. `images/originals/`의 파일이 그대로 첨부되는 경로는 존재하지 않는다.
- 강제 지점: `store.pick_variant`(세탁본 폴더 밖 경로는 돌려주지 않음, `Warehouse.is_washed`로 검사), `store.ensure_keyword_pool`(세탁본이 하나도 없으면 **즉석에서 세탁**하고, 세탁이 실패하면 `NoPhotoError` — 원본으로 물러나지 않는다).
- 새로 들어온 사진(`inbox/new`)도 적재 즉시 세탁한다(`photo_request.collect_new`).
- 회귀 방지 테스트: `tests/test_photo_stock.py::test_picked_photo_is_always_under_washed`, `::test_no_photo_error_when_washing_fails`.

## 구조 (이미 구현됨)
| 단계 | 모듈 | 동작 |
|---|---|---|
| 수집 | `warehouse/photo_collector.py` | `inbox/image` → `images/originals/<브랜드>/<폴더>/` 복사, sha256 중복 제거 (1,166장 적재 완료) |
| 세탁 | `warehouse/photo_washer.py` | EXIF 중 촬영일시·카메라(Make/Model)·렌즈·노출·일련번호만 랜덤 교체, GPS 등 나머지 보존, 재인코딩(q92~96)+1px 크롭으로 파일 해시 변경 |
| 보관 | `warehouse/store.py` | `images/washed/<원본sha>/<n>.jpg`, 변형 번호는 이어서 증가(덮어쓰기 없음) |
| 배정 | `engine/publish.py` | `{토큰}`마다 키워드 폴더에서 미사용 변형 1장, `photo_usage`에 기록 → 같은 변형 재사용 없음 |
| 보충 | `store.ensure_keyword_pool` | 키워드 폴더 비면 브랜드 루트 사진으로 씨앗 → 변형 생성 |

## 실행 계획 (오늘)
1. **일괄 세탁**: 브랜드별 키워드 폴더 원본 전부 → 원본당 변형 3장 (팥순이 545·BA 150, 코숨핏 31, 뉴더미스 85, 장으뜸 54, 우아덤 29 → 약 2,800장). 명령: `사진 세탁 3장씩`.
2. **재고 규칙**: 키워드 폴더의 "미사용 변형 수 < 30"이면 자동으로 원본당 +2장 추가 세탁 (`wash_photos` 작업이 매일 1회 점검).
3. **재고 보고**: `상태` 명령과 운영 현황판에 브랜드별 원본/변형/미사용 수 표시.

## 부족한 사진 새로 확보하는 방법 (3단계)
1. **자동 씨앗**: 키워드 폴더가 비면 같은 브랜드의 다른 폴더·루트 사진을 씨앗으로 복사해 변형 생성 (사용자 개입 0).
2. **자동 생성 요청서**: 브랜드에 원본이 아예 없거나 사용자가 "새 사진" 요구 시, 프로그램이 브랜드·키워드·Make 지침(제품 설명)을 바탕으로 **GPT용 이미지 생성 프롬프트 묶음**(예: "팥순이 단호박 샐러드 식단 사진, 자연광, 스마트폰 촬영 느낌, 텍스트 없음")을 만들어 텔레그램으로 보냄. 사용자는 GPT 채팅에 "만들어줘"로 붙여넣고 결과 이미지를 `warehouse/inbox/new/<브랜드>/<키워드>/`에 저장.
3. **자동 수거**: 프로그램이 `inbox/new`를 감시해 새 사진을 원본으로 적재 → 즉시 세탁 → 배정 가능 상태로 전환, 텔레그램으로 "N장 확보" 보고.

(인수인계 규칙: 이미지 생성은 GPT 채팅에 요청. API 자동 생성은 사용자가 OpenAI 키를 주면 4단계로 추가 가능.)

## 구현 위치 (2026-09-19 반영)
| 기능 | 위치 | 명령 |
|---|---|---|
| 일괄 세탁(원본당 N장, 브랜드 필터, 재실행 가능) | `engine/worker.py::_wash_photos` | `사진 세탁 3장씩`, `팥순이 사진 세탁` |
| 재고 집계·부족 판정·자동 보충 | `warehouse/stock.py` (`inventory` / `low_stock` / `top_up`) | — |
| 재고 보고 | `engine/status.py::photo_stock_lines` | `상태` |
| GPT 생성 프롬프트 + 요청 발송 | `warehouse/photo_request.py` (`build_gpt_prompts` / `request_photos`) | `브랜드 X 키워드 Y 사진 요청` |
| 새 사진 수거 + 즉시 세탁 | `warehouse/photo_request.py::collect_new` | `새 사진 수거` |
| 발행 중 사진 없음 → 자동 요청 | `engine/worker.py::_request_missing_photos` (`NoPhotoError` 처리부) | — |

안전장치: 한 번의 `사진 세탁` 실행에서 새로 만드는 변형은 기본 3,000장까지(`worker.DEFAULT_MAX_NEW`). 한도에 닿으면 멈추고 보고하며, 같은 명령을 다시 실행하면 채워진 원본은 건너뛰고 이어서 채운다.
