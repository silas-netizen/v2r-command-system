# Codex 모델 설정 반영 (2026-09-22 02:26 KST, 실측)

- 모델 **gpt-5.6-sol**, 추론 강도 **medium** — Codex 기본 설정(`~/.codex/config.toml`)과 MCP 서버 등록(`codex mcp-server -c model -c model_reasoning_effort`) 양쪽에 고정.
- 명령줄 호출(①)도 같은 설정을 씀. 재로그인 없음.
- 새 세션부터 `codex` 도구로 바로 사용, 그 전엔 명령줄로 동일하게 호출.
