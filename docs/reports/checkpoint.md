# 세션 체크포인트 (2026-09-19 오후)

## 상태
- 코드: GitHub main 최신(커밋 f9b8e88, 483 tests). 실행기 `serve` 상주, 텔레그램 응답 중.
- 실발행: 자사·제휴 13건 + 후기형 1건 완료·검증. 제휴 3건 댓글 복구 완료.
- GPT 세션: 사용자 로그인 완료, 쿠키 유효 365일(`python -m v2r.warehouse.gpt_images --check` OK).
- 예약 작업: V2R-Serve(로그온), V2R-Reconcile(08:30), V2R-GptKeepalive(09:00), V2R-GptLoginHold(1회용 창 유지).

## 방금 완료(GPT 일꾼)
- 로그인 판정 강화: 로그인 버튼 없음 + 계정 요소 있음 + 인증 URL 아님 3조건. 작성창만으로 판정 금지(회귀 테스트).
- `gpt_chat.ask`(웹 세션 질문·응답, 토큰 0), `generate_affiliate_pool_via_gpt` → `warehouse/manuscripts/affiliate_daily_pool.jsonl`.
- 명령 `제휴 일상 글 N개 생성` → `generate_affiliate_daily`. 제휴 체인은 이 풀만 사용(비면 경고 후 옛 시트 폴백).

## 다음 할 일
1. 사용자 화면 테스트: 텔레그램 `브랜드 우아덤 키워드 키워드 사진 1개 생성`(GPT 사진 1장), `제휴 일상 글 10개 생성`.
2. SE-ONE 사진 붙여넣기 실사용 검증(사진 있는 브랜드 행), V2R 브라우저 로그인 1회.
3. 운영 현황판(HTML, SQLite 자동 갱신) — artifact-capabilities 스킬.

## 사용자 규칙(요약)
- 채팅: 요약 절, 4문장 이상은 MD 파일. 승인 자동(auto). 모바일 연결 on.
- 사진: 세탁본만, 가로 400px, 키워드 폴더만, GPT 웹 생성·AI 티 금지. 태그: 키워드 공백 제거, 없으면 태그 없이.
- 카페 줄임말 허용. 자사 일상 글 = 각색 엑셀(중복검사 생략). 제휴 일상 글 = GPT 웹 2줄 풀.
- 제외 카페: 웨딩 노트·헬씨 트리(예약 2건 유지).
