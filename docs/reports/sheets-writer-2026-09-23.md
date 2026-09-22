# 구글 시트 쓰기 — 로그인·OAuth·토큰 없이 (2026-09-23)

## 요약

`v2r/sources/sheets_writer.py`를 **헤드리스 Playwright**로 다시 구현했다.
브랜드 시트가 전부 "링크 있는 누구나 편집" 상태라는 전제(사용자 확인) 위에서,
구글 OAuth 동의·토큰 없이도 실제 시트에 값을 쓰고 CSV로 검증하는 방식이다.
우아덤 시트(`1QYhQFnZznQ-NjIjwiIlmJcf6zkQlb6t3TBK-W6OwJm0`)에서 실제로
시험했고, 성공했다.

## 방식

1. 로그인하지 않은 새 Playwright 컨텍스트(헤드리스, 사용자 프로필 재사용
   안 함)로 `https://docs.google.com/spreadsheets/d/<id>/edit#gid=<gid>` 를
   연다. `wait_until="networkidle"`은 구글 시트가 웹소켓/폴링으로 계속
   네트워크를 쓰기 때문에 절대 끝나지 않는다 — `domcontentloaded` +
   이름 상자(`#t-name-box`) 등장 대기로 바꿨다.
2. 이름 상자에 대상 셀 주소(예 `H8220`)를 입력하고 Enter로 커서를 옮긴다.
3. 선택된 셀에 TSV 문자열(탭=열 구분, 개행=행 구분)을 **paste
   `ClipboardEvent`**로 주입한다 — `DataTransfer`에 `text/plain`을 담아
   `document.activeElement`에 `dispatchEvent`. 여러 행·열이 한 번에
   채워진다. 빈 문자열은 paste로 지워지지 않아(클립보드가 비어 있으면
   무시됨) `Delete` 키로 대체했다.
4. 저장 완료 표시(`#docs-title-saving-status`)를 기다린다.
5. `export?format=csv&gid=`로 다시 읽어 값 일치를 확인한다. 이 시트에서는
   `export?format=csv`가 400을 돌려줘서(일부 시트에서 발생하는 걸로 보임)
   `gviz/tq?tqx=out:csv`로 자동 폴백하도록 만들었다 — 어느 쪽이든 로그인
   없이 읽힌다.
6. 쓰기·검증 실패 시 최대 3회 재시도(지수적 대기).
7. 브랜드(스프레드시트 ID)별 파일 잠금(`data/locks/sheet-<id>.lock`)으로
   동시 쓰기를 막는다. 60초 대기 후 실패, 300초 넘은 잠금은 죽은 프로세스로
   보고 재사용한다.

## 인터페이스 (기존 시그니처 유지)

- `update_rows(spreadsheet_id, sheet, rows, key_column=..., header=..., gid=...)`
  — `key_column` 기준으로 있으면 갱신, 없으면 끝에 append.
- `append_rows(spreadsheet_id, sheet, rows, header=..., gid=...)` — 1,000행
  단위로 나눠 paste. 딕셔너리 목록·리스트 목록 둘 다 받는다.
- `update_by_key(spreadsheet_id, sheet, key_value, updates, key_column="H", gid=...)`
  — 노출 순환용. H열 키워드로 행을 찾아 `updates`(열 문자 → 값, 예
  `{"G": "확인", "I": "...", "J": "...", "L": "...", "O": "..."}`)를 반영.
- `set_cell(spreadsheet_id, sheet, cell, value, gid=...)` — P1/Q1 합계 등
  단일 셀.
- `has_credentials(...)`는 호환용으로 남겨두되 이제 항상 `True`(자격증명이
  필요 없어졌으므로).

## 실제 시험 (우아덤 시트)

### 1) Z1 쓰기/지우기 (무손상 확인)

- `set_cell(sid, "탭", "Z1", "v2r-test", gid=0)` → `{'written': True, 'mode': 'sheets'}`
- CSV로 재확인: `Z1 == "v2r-test"` 확인.
- `set_cell(sid, "탭", "Z1", "", gid=0)` → `{'written': True, 'mode': 'sheets'}` (Delete 키 경로)
- CSV로 재확인: `Z1 == ""` — 원상 복구 확인.
- 사람이 채운 A~F열(비밀번호 E열 포함)은 건드리지 않았다.

### 2) 발굴 키워드 100개 append (`노출 현황` 탭, gid=1454846576)

- 시트 탭 구성 확인: `게시글 쓰기 원본`(gid 0, 261행), `노출 현황`(gid
  1454846576, 8,219행 — 이 중 기존 키워드가 있는 행은 3,823행, 나머지는
  G열만 "밀려남"으로 채워진 예약/빈 행).
- `data/keywords/우아덤.sqlite`(다른 일꾼이 담당 중인 연관도 재산정 결과,
  읽기만 함)에서 `relevance_llm=3`(최고 등급)이면서 시트 H열에 아직 없는
  키워드 127개 중 검색량(`total`) 순 상위 100개를 골랐다.
- 매핑: G=미확인, H=키워드, I=통합검색 URL(`search.naver.com` 쿼리),
  K=키워드 검색량(천단위 콤마), M=비고(`rationale`, 현재는 대부분 빈칸),
  N=본문 분류(`relevance_llm=3` 라벨 — 연관도 재산정 값이 채워지면
  그 값으로 교체 예정).
- `append_rows(...)` → `{'written': 100, 'mode': 'sheets'}`.
- CSV로 재확인: 시트 행 수 8,219 → 8,319(정확히 +100). 8220행(`피부과`),
  마지막 8319행(`스테비아`) 값이 기대대로 들어간 것을 확인.
- 기존 3,823개 데이터 행·8,096개 예약 행(A~F, 비밀번호 포함)은 전혀
  건드리지 않았다(항상 표 끝(`len(table)+1`행)부터 append하므로).

## 속도

- 셀 하나(Z1) 쓰기+검증: 브라우저 기동 포함 약 8~10초.
- 100행 append(TSV 1회 paste)+검증: 약 12초 — 브라우저 기동·저장 대기가
  대부분이고, paste 자체는 행 수에 거의 영향받지 않는다(1,000행 단위 청크는
  아직 실측 못함 — 다음 대량 반영 때 측정 예정).

## 테스트

`tests/test_sheets_writer.py` 10개 — 전부 실제 브라우저/네트워크 없이
`_write_tsv_at`/`_read_export_csv`를 monkeypatch해서 순수 로직만 검증:
TSV 조립, 셀 파싱(`_col_letter`/`_parse_cell`), 검증 일치/불일치, 재시도
후 포기, 브랜드 파일 잠금(정상·stale 회수), `update_by_key`/`append_rows`
행 계산. `pytest -q` 전체 스위트 결과는 별도로 확인(느린 통합 테스트 다수
포함, 장부 파수꾼 관련 실패는 다른 일꾼 작업 중이라 무시).

## 남은 일

- 명령 `시트 키워드 반영 <브랜드|전체>` — 연관도 재산정이 끝난 나머지
  키워드(브랜드당 1만 개 목표)를 이번에 확인한 방식으로 붙이는 명령. 이번
  구현의 `append_rows`를 그대로 호출하면 되므로 재산정 완료 후 바로 연결
  가능.
- `M`(비고) 열은 현재 대부분 `rationale`이 비어 있어 빈 칸으로 들어갔다 —
  재산정 결과에 근거가 채워지면 그 값으로 다시 채우면 된다.
