"""정리본 지침을 **프롬프트에 넣을 때만** 줄이는 필터 (2026-09-22 토큰 절약).

원본 md(`warehouse/guides/정리본/*.md`)는 한 글자도 바꾸지 않는다. 프롬프트에
실어 보낼 때만 아래를 덜어낸다.

1. **코드가 이미 검사하는 규칙** — 줄 나눔 28자·글자 수·`{키워드}` 자리·자리표시자·
   웃음 표기·출력 형식·금지 목록. 이건 `brand_writer.validate()` 가 통과할 때까지
   다시 시키므로 프롬프트에 또 적을 이유가 없다.
2. **Make 운영 메모** — 모듈 ID·모델 설정·`{{42.`0`}}` 같은 시나리오 변수·
   `## 변경 로그` 이하(옮김/합침/충돌/삭제 없음 확인) 전체.
3. **중복 문장** — 같은 문장이 본문용·댓글용에 두 번 적힌 것은 처음 하나만 남긴다.

남기는 것: 브랜드·제품·타겟 정보, 설득 요소(대체제 제거 논리·원씽 이동·프레임 분리),
본문 구조와 댓글 세트 역할, 예시 흐름 — 즉 **모델만 할 수 있는 판단**에 쓰이는 내용.
"""

from __future__ import annotations

import re

__all__ = [
    "DROP_BRACKET_SECTIONS",
    "compress_guide_text",
    "compression_ratio",
]

#: 통째로 버리는 `[...]` 절 (코드 검증기가 담당하거나 출력 형식인 것)
DROP_BRACKET_SECTIONS: tuple[str, ...] = (
    "출력 형식",
    "출력 구조 형식",
    "출력 금지 목록",
    "금지 목록",
    "형식",
    "형식 규칙",
    "분량·가독성 형식",
    "분량과 가독성",
    "글자 수",
    "AI 티 제거 규칙",
    "AI 티 제거 / 맞춤법 규칙",
    "AI 티 제거 / 맞춤법 스타일",
    "AI 티 제거·문장부호 규칙",
    "적용 체크리스트",
)

#: 통째로 버리는 마크다운 절 (`## 변경 로그` 이하 전부 — 정리 작업 기록이다)
DROP_MARKDOWN_PREFIXES: tuple[str, ...] = ("## 변경 로그",)

#: 이 조각이 들어 있는 **줄 하나**를 버린다 (Make 운영 메모·검증기 담당 규칙)
DROP_LINE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"Make에 그대로 붙여넣기",
        r"Make 시나리오",
        r"모듈 ID",
        r"모듈 \d+ = ",
        r"max_tokens",
        r"temperature",
        r"\{\{\d+\.",              # {{42.`0`}} 같은 시나리오 변수
        r"choices\[\]\.message",
        r"원본 그대로 유지",
        r"위 시스템 규칙대로",
        r"변경 로그 충돌",
        # --- 코드 검증기가 이미 잡는 규칙들 -------------------------------
        r"공백\s*(제외|포함).{0,12}\d+\s*자",
        r"\d+\s*자를?\s*(절대\s*)?(넘지|초과)",
        r"\d+\s*(글)?자를?\s*넘지\s*않",
        r"한 줄이? .{0,10}\d+\s*(글)?자",
        r"줄 나눔",
        r"문단 나눔",
        r"\{키워드\}",
        r"\{B/A\}",
        r"중괄호",
        r"말줄임표",
        r"쉼표(와|,)?\s*마침표",
        r"띄어쓰기를",
        r"맞춤법",
        r"이모지",
        r"나열식|정리식",
        r"제목에?\s*\*\s*표시|\*\s*또는 \*\*",
        r"구분 기호",
        r"여러 세트|세트 라벨|세트 1",
        r"ㅋㅋ.{0,6}ㅎㅎ|ㅎㅎ.{0,6}ㅋㅋ|ㅠㅠ.{0,6}ㅋㅋ",
        r"물음표\(\?\)",
        r"제목 (여러 개|1개만)",
    )
)


def _section_key(line: str) -> str:
    """`[출력 형식]` → `출력 형식` (대괄호 절 이름). 절 머리가 아니면 빈 문자열."""
    text = line.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        # `[브랜드 정보 — 코숨핏]` 처럼 꼬리표가 붙어도 앞 이름으로 판정한다
        return re.split(r"\s*[—\-–]\s*", inner, maxsplit=1)[0].strip()
    return ""


def _norm(line: str) -> str:
    """중복 판정을 위한 정규화 (공백·번호 매김을 떼고 본다)."""
    text = re.sub(r"^\s*(\d+[.)]|[-*•])\s*", "", line.strip())
    return re.sub(r"\s+", "", text)


def compress_guide_text(text: str) -> str:
    """정리본 지침에서 **프롬프트에 넣을 몫**만 남긴다 (원본 파일은 건드리지 않는다)."""
    raw = (text or "").strip()
    if not raw:
        return ""
    out: list[str] = []
    seen: set[str] = set()
    dropping_section = False          # `[...]` 절 통째로 버리는 중인가
    dropping_markdown = False         # `## 변경 로그` 이하를 버리는 중인가
    for line in raw.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(p) for p in DROP_MARKDOWN_PREFIXES):
            dropping_markdown = True
            continue
        if dropping_markdown:
            # 변경 로그 다음의 `##` 절이 나오면 다시 받는다 (실제로는 파일 끝까지다)
            if stripped.startswith("## ") and not any(
                stripped.startswith(p) for p in DROP_MARKDOWN_PREFIXES
            ):
                dropping_markdown = False
            else:
                continue
        key = _section_key(stripped)
        if key:
            dropping_section = key in DROP_BRACKET_SECTIONS
            if dropping_section:
                continue
        elif dropping_section:
            # 다음 절 머리(`[...]`)나 마크다운 제목을 만날 때까지 계속 버린다
            if stripped.startswith("#") or stripped in ("```",):
                dropping_section = False
            else:
                continue
        if any(p.search(stripped) for p in DROP_LINE_PATTERNS):
            continue
        norm = _norm(stripped)
        if norm:
            if norm in seen:
                continue
            seen.add(norm)
        elif out and not out[-1].strip():
            continue  # 빈 줄이 잇달면 하나만 남긴다
        out.append(line.rstrip())
    # 남은 코드펜스 짝이 깨졌을 수 있으니 그냥 지운다 (지침 본문에는 뜻이 없다)
    out = [ln for ln in out if ln.strip() != "```" and not ln.strip().startswith("```")]
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def compression_ratio(before: str, after: str) -> float:
    """줄어든 비율 (0.30 이면 30% 축소)."""
    size = len(before or "")
    if not size:
        return 0.0
    return 1.0 - (len(after or "") / size)
