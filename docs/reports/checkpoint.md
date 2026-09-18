# 세션 체크포인트 (2026-09-19 08:40 KST)

## 상태
- 코드: GitHub main 최신(448 tests). 실행기 `serve` 콘솔 세션 상주(PID 8004), 텔레그램 응답 중.
- 실발행: 13건 완료·검증, 제휴 3건 댓글 복구 완료.
- 예약 작업: V2R-Serve(로그온), V2R-Reconcile(08:30), V2R-GptKeepalive(09:00), V2R-GptLogin(1회, 로그인 창 띄우기용).

## 진행 중 일꾼
1. GPT 일꾼(id a19bc561caa8dfd3e): 로그인 감지 강화(비로그인 화면 오판 수정) + `gpt_chat.ask` + 제휴 일상 글 GPT 웹 생성(`generate_affiliate_daily`, `affiliate_daily_pool.jsonl`) + 제휴 체인이 그 풀만 읽도록.
2. 후기형 일꾼(id a385393bc5b61e8d0): 팥순이 후기형 탭 찾기·등록, `manuscript_type` 필터, 실발행 1건·역할 검증, 카페 제외 규칙(웨딩 노트·헬씨 트리 `excluded: true`).

## 완료 후 할 일
- GPT 로그인 창 재실행: PowerShell `Start-ScheduledTask V2R-GptLogin` → 사용자가 크롬 창에서 ChatGPT 로그인 → "됨" 확인.
- 테스트 → 커밋·푸시(승인 카드 필요 시 default 모드 전환 후 auto 복귀).
- 운영 현황판(HTML, SQLite 자동 갱신), SE-ONE 사진 붙여넣기 실사용 검증, 효율표 갱신.

## 사용자 규칙(요약)
- 채팅: 요약 절, 4문장 이상은 MD 파일. 승인 자동(auto). 모바일 연결 on.
- 사진: 세탁본만, 가로 400px, 키워드 폴더만, GPT 웹 생성·AI 티 금지, 배치 규칙. 태그: 키워드 공백 제거, 없으면 태그 없이.
- 카페 이름 줄임말 허용. 자사 일상 글 = 각색 엑셀(중복검사 생략). 제휴 일상 글 = GPT 웹 2줄 풀.
- 제외 카페: 웨딩 노트·헬씨 트리(예약 2건은 유지).
