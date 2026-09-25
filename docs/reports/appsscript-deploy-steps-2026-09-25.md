# Apps Script 재배포 순서 (2026-09-25 21:30 정리)

앞선 안내의 "시트 안에서 만들었다면…" 문장은 불필요한 말이었다. 프로젝트는 https://script.google.com/home 목록에서 찾으면 된다.

## 붙여 넣을 코드
- 최종 코드 파일: [v2r_sheet_api.gs](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/appsscript/v2r_sheet_api.gs)
- 파일을 열어 전체 선택(Ctrl+A) → 복사(Ctrl+C).

## 링크
- Apps Script 내 프로젝트 목록: https://script.google.com/home
- 지금 배포된 웹앱 URL(바뀌지 않음): https://script.google.com/macros/s/AKfycbxpF5qH6hKz5bjODRsz_8KlB6PD7biAnN9rg735RX84AW7m-XZEEU4FdaSAOp35CUPG/exec
- (시트 링크는 필요 없음. 위 프로젝트 목록 링크 하나만 쓰면 됨.)

## 순서 (5분)
1. https://script.google.com/home 을 연다. 목록에 오늘 날짜로 수정된 프로젝트가 하나 보인다(아까 배포한 것). 그것을 클릭해서 연다. 이름이 무엇이든 오늘 것이면 맞다.
2. 왼쪽 파일 목록의 `코드.gs`(또는 Code.gs)를 클릭 → 편집창 내용을 전부 지우고(Ctrl+A, Delete) 복사한 코드를 붙여 넣는다(Ctrl+V).
3. 저장(Ctrl+S 또는 디스크 아이콘).
4. 오른쪽 위 파란 **배포** 버튼 → **배포 관리**.
5. 목록의 기존 배포 오른쪽 **연필(수정)** 클릭.
6. "버전" 칸을 **새 버전**으로 바꾼다(설명은 비워도 됨). 나머지(웹 앱, 실행 사용자 = 나, 액세스 = 모든 사용자)는 그대로.
7. **배포** 클릭 → 완료. URL이 아까와 같은지만 확인(같아야 정상).
8. 권한 창이 다시 뜨면: 계정 선택 → "고급" → "…(안전하지 않음)으로 이동" → 허용.

## 끝난 뒤
- 따로 알릴 필요 없음. 10분 안에 제가 자동 감지해 5개 시트 형식 정리(팥순이 A1, 중복·빈 행, A·G 드롭다운, 노출완 카페 채움)를 실행하고 결과를 [현황판 9절](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/status-2026-09-25.md)에 적는다.
- 확인 방법: 붙여 넣기가 잘못되면 저장할 때 편집기가 빨간 줄로 오류를 표시한다. 그러면 스크린샷을 보내 주면 된다.
