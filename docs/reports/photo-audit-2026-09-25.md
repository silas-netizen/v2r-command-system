# 쌍둥이맘 브랜드 수정글 사진 오발행 감사 (2026-09-25)

## 요약

2026-09-25 16:50–16:56 쌍둥이맘 모여라 카페에 등록된 브랜드 수정글(자식 글) 20건을 전수 추적했다.
그 중 **코숨핏 5건**이 옛 인박스 폴더(보건복지부·대한의학회 계열 의학 삽화, "…_기사_자료"·"…_이미지")
사진을 쓰고 있어 부적합으로 판정했고, 사용자가 지정한 `originals/코숨핏/비염`·`originals/코숨핏/코골이`
실사진으로 교체(세탁 400px 후 `update_article`)했다. 나머지 15건(뉴더미스·우아덤·장으뜸)은 모두
`키워드` 폴더의 실사진(제품·신체 클로즈업)이라 적합으로 판정했다. 제목·본문·댓글·태그는 건드리지 않았다.

추적 방법: `data/v2r.sqlite`의 `publications`에서 해당 시간대 20건을 찾은 뒤(각 행의 `source_id`가
이미 수정글 자체였다 — 부모 일상 글은 바로 다음 줄의 `stage='daily_used'` 행으로 소비 기록만 남는다),
`v2r.api.articles.get_article`로 본문의 이미지 URL을 받아 다운로드·SHA-256 해시를 구하고,
`warehouse/images/washed/<원본 SHA-256>/…` 폴더 이름 규칙(코드: `v2r/warehouse/store.py`)을 거꾸로 이용해
원본 파일을 특정했다. `photo_usage` 표의 기록(8건)과도 교차 확인해 일치를 확인했다.

## 20건 표

| # | 키워드 | 자식 글 | 교체 전 파일 · 폴더 | 판정 | 교체 후 파일 · 폴더 |
|---|---|---|---|---|---|
| 1 | 그릭요거트 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRQKCS36NE9VFWVJQBJKAT) | `0274f2e4594324e2.jpg` · 뉴더미스/키워드 | 적합(재발방지로 교체) | `8a2aafa4d61034cb.jpg` · 뉴더미스/키워드 |
| 2 | 덴마크유산균 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRQVBR9N7EEEKJ4TJA7MC3) | `0274f2e4594324e2.jpg` · 뉴더미스/키워드 | 적합(재발방지로 교체) | `17544aee842ef3fc.jpg` · 뉴더미스/키워드 |
| 3 | 비데 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRR1QDWPM31DYC7T1ZHFEW) | `0274f2e4594324e2.jpg` · 뉴더미스/키워드 | 적합(재발방지로 교체) | `e502ae8420f7edc6.jpg` · 뉴더미스/키워드 |
| 4 | 유산균 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRR9QPZYAXQNFQJXC3RT1Y) | `0274f2e4594324e2.jpg` · 뉴더미스/키워드 | 적합(재발방지로 교체) | `f4db012f96b302aa.jpg` · 뉴더미스/키워드 |
| 5 | 장염증상 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRRH7NC77DJPZR56TKVFF7) | `0274f2e4594324e2.jpg` · 뉴더미스/키워드 | 적합(재발방지로 교체) | `7f63ad721d36512e.jpg` · 뉴더미스/키워드 |
| 6 | 나이아신아마이드 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRRRW1000V2ZWRWC4S15FD) | `03d9811663d533d9.jpg` · 우아덤/키워드 | 적합(재발방지로 교체) | `3630ad14f0100fad.jpg` · 우아덤/키워드 |
| 7 | 리쥬란 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRS0TF1ASN2JAQYSG2R7PD) | `03d9811663d533d9.jpg` · 우아덤/키워드 | 적합(재발방지로 교체) | `af2d5d2456fc7d10.jpg` · 우아덤/키워드 |
| 8 | 멜라닌 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRS9HS25B9435TZC2PM60M) | `03d9811663d533d9.jpg` · 우아덤/키워드 | 적합(재발방지로 교체) | `45814d500998aacc.jpg` · 우아덤/키워드 |
| 9 | 비타민C | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRSKMC3SGSZJRFK8RYF0FG) | `03d9811663d533d9.jpg` · 우아덤/키워드 | 적합(재발방지로 교체) | `142b424f63c55129.jpg` · 우아덤/키워드 |
| 10 | 주근깨 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRSWVMRXSSPPKZF76VHNTE) | `03d9811663d533d9.jpg` · 우아덤/키워드 | 적합(재발방지로 교체) | `863e6eca71a81533.jpg` · 우아덤/키워드 |
| 11 | 오메가3 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRT4DQPA3JPX1KFN6KBKGJ) | `0821d7443f44d984.jpg` · 장으뜸/키워드 | 적합(재발방지로 교체) | `d3e9b5b38bf6d36c.jpg` · 장으뜸/키워드 |
| 12 | 이노시톨 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRTBWP2ZXZ58497G9YFWFM) | `0821d7443f44d984.jpg` · 장으뜸/키워드 | 적합(재발방지로 교체) | `c1310e742e8ee223.jpg` · 장으뜸/키워드 |
| 13 | 임신 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRTM6M887CTSJW6NWSJSPE) | `0821d7443f44d984.jpg` · 장으뜸/키워드 | 적합(재발방지로 교체) | `1a046c43a00507bc.jpg` · 장으뜸/키워드 |
| 14 | 자궁근종 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRTVR7CGYWQP8W1BVN3VKS) | `0821d7443f44d984.jpg` · 장으뜸/키워드 | 적합(재발방지로 교체) | `3757baba3516c972.jpg` · 장으뜸/키워드 |
| 15 | 장어 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRV38A9JMQ7D720GTFMP44) | `0821d7443f44d984.jpg` · 장으뜸/키워드 | 적합(재발방지로 교체) | `dd645a4d6ea0fa2e.jpg` · 장으뜸/키워드 |
| 16 | 비염 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRVMRGTSRJJ92TJ7AH68X9) | `e00e63d84191f007.jpg` · 코숨핏/비염 괴로움_이미지(의학 삽화) | **부적합**(자료성 삽화, 지정 폴더 아님) | `6.jpg` · 코숨핏/비염(실사진) |
| 17 | 비염치료 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BRW6ZQK7RWTWDTP1Q8K2GM) | `8135c4ff7e0fcd2e.jpg` · 코숨핏/비염 괴로움_이미지(의학 삽화) | **부적합** | `3.jpg` · 코숨핏/비염(실사진) |
| 18 | 양압기 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BS0CH7XJ481P4D3ZFYQ25A) | `09f0801938890cda.png` · 코숨핏/코골이_개선이유_기사_자료(KBS 생로병사 방송 캡처) | **부적합**(방송 캡처·자막) | `2.jpg` · 코숨핏/코골이(실사진) |
| 19 | 이비인후과 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BS0PW4DPJDA7H1T1W5C70B) | `e00e63d84191f007.jpg` · 코숨핏/비염 괴로움_이미지(의학 삽화) | **부적합** | `13.jpg` · 코숨핏/비염(실사진) |
| 20 | 항히스타민제 | [열기](https://v2r.daboja.im/nc/articleDetail/01M3BS1751KE5PD54T6QN03W8H) | `2e6e72ba026e8dfe.jpg` · 코숨핏/비염 괴로움_이미지(의학 삽화) | **부적합** | `9.jpg` · 코숨핏/비염(실사진) |

교체 5건 모두 `update_article` 응답 `{"is_success": true}` 확인, 제목·본문 텍스트·태그·댓글은 그대로 두고
이미지 컴포넌트(`src`/`path`/`domain`/`fileSize`/`width`/`height`/`fileName`)만 새 업로드로 바꿨다.
새 세탁본은 `data/v2r.sqlite`의 `photo_usage`에도 기록해 다음 발행이 같은 사진을 또 쓰지 않게 했다.

## 추가 발견: 같은 실행 안에서 같은 원본이 반복 (뉴더미스·우아덤·장으뜸)

위 표 1–15행을 보면 뉴더미스 5건이 전부 `0274f2e4594324e2.jpg`, 우아덤 5건이 전부 `03d9811663d533d9.jpg`,
장으뜸 5건이 전부 `0821d7443f44d984.jpg` 로 **폴더(85·30·55장)에 사진이 많은데도 한 장에 몰렸다.**

원인(`v2r/engine/publish.py` `pick_images` / `v2r/warehouse/store.py` `pick_variant`):
`키워드` 폴더 원본 목록을 정렬된 고정 순서로 훑다가, 첫 원본을 골라 쓴 뒤 다음 건에서도 다시 그 원본부터
본다. 그 원본은 예전부터 쌓인 세탁본(`washed/<원본 SHA-256>/0.jpg, 1.jpg, …`)이 여러 개 있어서
`pick_variant`가 "아직 안 쓴 변형"을 계속 찾아내 버려, 한 번도 다음 원본으로 못 넘어갔다.

고침(`v2r/engine/publish.py::pick_images`):
- 원본 목록을 매번 섞는다(`random.shuffle`).
- 실행(런타임) 단위로 "이미 쓴 원본 SHA-256" 집합을 브랜드별로 기억해, 아직 안 쓴 원본을 먼저 시도하고
  원본 풀이 다 떨어졌을 때만 재사용으로 넘어간다.
- 원본에 세탁본이 아직 하나도 없으면(빈 원본) 그 원본만 즉석에서 세탁해 채운다(예전엔 폴더 전체
  세탁본 수만 보고 "이미 충분하다"고 건너뛰어 새 원본이 영영 안 세탁되는 문제가 있었다).
- 사진이 정말 부족할 때(원본보다 필요 장수가 많을 때)는 여전히 `PublishError`로 막고
  "사진 생성 승인" 알림을 보낸다 — 조용히 세탁본을 더 만들어내지 않는다(기존 승인 절차 유지).

시험: `tests/test_pick_images_diversity.py` 2건 추가.
- 원본 5장·발행 5건 → 5건 모두 다른 원본이어야 한다.
- 원본 2장·발행 3건째 → 앞 2건은 서로 다른 원본, 3건째는 품절 에러(자동 증산 금지).
전체 회귀: `tests/test_pick_images_diversity.py` + `tests/test_engine.py` 64건 통과.

이미 발행된 15건(뉴더미스·우아덤·장으뜸 각 5건)은 위 표대로 서로 다른 원본으로 다시 골라 세탁 후
`update_article`로 사진만 교체했다(응답 전부 `{"is_success": true}`, `photo_usage`에도 기록).

## 브랜드 폴더 출처 점검

| 브랜드 | 폴더 | 표본 확인 | EXIF | 판정 |
|---|---|---|---|---|
| 뉴더미스 | 루트(4장) | 실사진(제품 박스) | 있음 | 적합 |
| 뉴더미스 | 두드러기 | 피부 클로즈업 실사진 | 있음 | 적합 |
| 뉴더미스 | 먹는 약 | 약 포장 실사진(영수증 자체 블러 처리됨) | 있음 | 적합 |
| 뉴더미스 | 병원 | 병원 내부 실사진 | 있음 | 적합 |
| 뉴더미스 | 연고사진 | 연고 실사진 | 있음 | 적합 |
| 뉴더미스 | 좌욕사진 | 좌욕기 실사진 | 없음(폰 저장 과정에서 소실 추정) | 적합(내용상 실사진) |
| 뉴더미스 | 키워드 | 위 폴더들에서 채워짐(빈 폴더 자동 보충) | — | 적합 |
| 우아덤 | 사진 | 식사·라이프스타일 실사진 | 없음 | 적합 |
| 우아덤 | 키워드 | 사진 폴더에서 채워짐 | — | 적합 |
| 장으뜸 | 루트(31장) | 영양제·임신테스트기 등 실사진 | 없음 | 적합 |
| 장으뜸 | 키워드 | 루트에서 채워짐 | — | 적합 |
| 팥순이 | BA | 비포/애프터 실사진(얼굴 블러 처리됨) | 있음 | 적합 |
| 팥순이 | 키워드 | BA에서 채워짐 | — | 적합 |
| **코숨핏** | **비염 괴로움_이미지 · 코골이_개선이유_기사_자료 등 구 인박스 폴더** | **의학 삽화·방송 캡처(자료성)** | — | **부적합 — 발행 대상에서 제외** |
| 코숨핏 | 비염(15장) · 코골이(15장) · 키워드 | 사용자가 오늘 17:00 새로 적재한 실사진 | 일부 있음 | 적합(유일하게 허용) |

EXIF 없는 파일은 다운로드·재저장 과정에서 메타데이터가 빠진 것으로 보이며, 육안상 모두 실제 촬영 사진
(제품·신체 부위·생활 장면)이라 자료 사진 문제는 없다. 인박스 원본 수집 로그(파일명 외 별도 출처 메타데이터)는
창고에 남아있지 않아 더 이상 추적할 단서가 없었다.

## 코드/설정 변경

- `config/brands.yaml` — 코숨핏 `known_folders`를 `키워드`·`비염`·`코골이` 3개만 남기고, 옛 "…_기사&자료"·
  "…_이미지" 계열 인박스 폴더를 모두 제거했다(발행 시 랜덤 선택 후보에서 원천 차단). `keyword_topic_folders`
  라우팅(비염/코골이 키워드 매칭)은 이미 오늘 17:00에 사용자가 고쳐놓은 상태라 그대로 뒀다.
- `data/v2r.sqlite` `photo_usage` — 교체한 5건의 새 세탁본 사용 기록 추가(중복 재사용 방지용, 코드 변경 아님).

## 재발 방지 메모

이번 오발행은 config가 17:00에 고쳐지기 전(16:50–16:56)에 실행기가 옛 설정으로 이미 돌고 있어 생긴 것이다.
지금은 `known_folders`에 부적합 폴더가 아예 없으므로 같은 원인으로는 재발하지 않는다.
