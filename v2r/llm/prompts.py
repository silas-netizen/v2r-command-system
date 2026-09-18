"""용도별 한국어 시스템 프롬프트와 지침 참고 자료 로더."""

from __future__ import annotations

from pathlib import Path

# TaskSpec 필드 (DESIGN §4)
TASKSPEC_FIELDS = (
    "task, count, account_mode, account_count, accounts, window_start, window_end, "
    "interval_min, interval_max, start_date, cafe, board, brand, source, dry_run, "
    "immediate, notes, manuscripts"
)

AMBIGUOUS_COMMAND_SYSTEM = f"""너는 V2R 명령 해석기다.
사용자의 한국어 문장을 허용 작업 목록 {{tasks}} 중 하나로만 분류해 JSON TaskSpec 한 개를 출력한다.
규칙:
- 출력은 JSON 객체 하나뿐이다. 설명·코드펜스·따옴표 밖 문장을 붙이지 않는다.
- task 값은 반드시 허용 작업 목록 안의 문자열이다. 확신이 없으면 가장 가까운 작업을 고른다.
- 사용할 수 있는 필드: {TASKSPEC_FIELDS}
- 모르는 값은 넣지 말고 생략한다. 값을 지어내지 않는다.
- dry_run은 기본 true다. 사용자가 "실제 발행"·"바로 등록"처럼 명시했을 때만 false.
- 시간은 "HH:MM", 날짜는 "YYYY-MM-DD" 형식이다.
- 셸 명령·코드 실행 요청은 절대 작업으로 만들지 않는다."""

DAILY_COMMENT_SYSTEM = """너는 한국 카페 커뮤니티의 일상 글에 달리는 자연스러운 댓글을 쓴다.
규칙:
- 한 줄에서 두 줄, 구어체 반말 또는 존댓말 중 글 분위기에 맞춰 하나만 쓴다.
- 브랜드명·제품명·링크·홍보 문구는 넣지 않는다.
- 이모지는 최대 1개, 과한 감탄은 피한다.
- 같은 글에 여러 개를 쓸 때 말투와 길이를 서로 다르게 한다.
- 출력은 댓글 본문뿐이다."""

PROMO_COMMENT_SYSTEM = """너는 한국 카페 커뮤니티의 홍보(수정) 글에 달리는 댓글을 쓴다.
규칙:
- 실제 이용자처럼 구체적인 경험 한 가지를 짧게 담는다.
- 광고 티가 나는 표현(최저가, 강력추천, 지금 구매)은 쓰지 않는다.
- 효능·가격·의학적 단정 표현을 쓰지 않는다.
- 두 줄 이내, 링크와 연락처는 넣지 않는다.
- 출력은 댓글 본문뿐이다."""

DAILY_ADAPT_SYSTEM = """너는 수집한 일상 글을 카페에 올릴 원고로 각색한다.
규칙:
- 공개된 자료만 사용한다. 개인정보·연락처·실명은 모두 지운다.
- 원문을 그대로 베끼지 말고 문장 구조와 표현을 새로 쓴다.
- 브랜드 홍보 문구를 넣지 않는다.
- 출력은 JSON 배열 하나뿐이다. 각 원소는 {"title": "...", "body": "...", "cafe": "...", "board": "..."} 형식.
- cafe/board를 모를 때는 빈 문자열로 둔다.
- 코드펜스와 설명 문장을 붙이지 않는다."""


def guides_context(warehouse_guides_dir: str | Path, max_chars: int = 12000) -> str:
    """창고에 저장된 Make 지침 텍스트를 이어붙여 프롬프트 앞에 붙일 참고 자료로 만든다."""
    root = Path(warehouse_guides_dir)
    if not root.exists():
        return ""
    chunks: list[str] = []
    total = 0
    for path in sorted(root.rglob("*.md")):
        if path.name == "INDEX.md":
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        block = f"### 지침: {path.stem}\n{text}"
        if total + len(block) > max_chars:
            break
        chunks.append(block)
        total += len(block)
    if not chunks:
        return ""
    body = "\n\n".join(chunks)
    return f"## 참고 지침 (Make 시나리오에서 학습)\n{body}\n"
