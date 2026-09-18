# v2r-command-system

V2R(가상 테스트 사이트) 운영 업무를 채팅 한 줄로 시작하고, 내부 규칙이 V2R API 호출로 완료까지 책임지는 시스템.

- 설계서: [docs/DESIGN.md](docs/DESIGN.md)
- 인수인계 원문: [docs/reference/handoff.md](docs/reference/handoff.md)
- V2R API 사양: [docs/reference/v2r-api-spec.md](docs/reference/v2r-api-spec.md)
- 로그인 프로토콜: [docs/reference/v2r-auth-protocol.md](docs/reference/v2r-auth-protocol.md)

## 설치
```bash
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
copy .env.example .env   # 값 채우기
```

## 사용
```bash
python -m v2r "상태 알려줘"
python -m v2r "내일 오전 9시부터 오후 6시까지 일상 글 5개 올려줘. 아이디 3개 자동, 10분 간격"
python -m v2r serve        # 텔레그램/슬랙 명령 대기
```
기본은 검증 모드(dry run). `실제 발행`이라고 써야 실제 등록한다.
