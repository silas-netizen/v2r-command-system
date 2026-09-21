# Codex(GPT) MCP 서버 등록 완료 (2026-09-22 02:21 KST, 실측)

## 한 것
- Claude Code 사용자 설정에 `codex` MCP 서버 등록(명령: Codex 앱 내장 `codex.exe mcp-server`). 연결 확인: **✔ Connected**.
- 기존 ChatGPT 로그인 그대로 사용, 비밀번호 입력 없음, 비용 0원(ChatGPT 요금제).

## 사용 시점
- **이 세션은 시작 때 도구 목록을 읽어서**, 새 도구 `codex`는 **세션을 새로 열거나 재시작한 뒤**부터 제 도구로 보입니다. 그 전까지는 명령줄 호출(①)로 같은 GPT를 부릅니다(기능 동일).
- 새 세션에서 처음 codex 도구를 쓸 때 승인 창이 1번 뜹니다.

## 교차 검증 규칙(제안, 바로 적용)
- 원고: 지침 위반 / 부자연스러운 문장 / 근거 논리 3가지만 GPT에 점검시켜 검증 표에 "GPT" 열 추가. 최종 판단은 우리 검증기 + 사용자 결정.
- 코드: 큰 변경(발행 규칙·로그인 유지 장치) 커밋 전 GPT 리뷰 1회.
- Codex는 읽기 전용으로만 실행(파일 수정·명령 실행 없음).

## 같이 진행 중
- 토큰 절약 + 누적 속도 조치는 일꾼에게 맡겼습니다(6건 생성 끝난 뒤 코드 수정 시작, 결과 [token-saving-done-2026-09-22.md](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/token-saving-done-2026-09-22.md)).
- 브랜드별 원고 6건: 4/6 완료, 곧 [brand-final-2026-09-22.md](file:///D:/v2r%20%EC%9E%90%EB%8F%99%ED%99%94/v2r-command-system/docs/reports/brand-final-2026-09-22.md).
