"""대대댓글2 전용 실험실 (사용자 지시 2026-09-21).

제품을 처음 꺼내는 자리인 **대대댓글2**만 따로 뽑아 본다.

1. `data/brand_sheet_<브랜드>.xlsx` 의 완성 원고에서 기존 대대댓글2를 전부 모은다
2. 그 중 검증을 가장 잘 통과하는 것들을 few-shot 예시로 붙여 후보 N개를 받는다
3. 하드 검증(구조 4요소 / 완전한 문장 / 길이 / 금칙어 / 이모지 / 베끼기)을 전부 통과한
   후보가 N개가 될 때까지 다시 받는다 (최대 `MAX_ROUNDS`회)
4. `docs/reports/reply2-candidates-<날짜>.md` 에 브랜드(유형)마다
   [기존 예시 3개] → [통과 후보 N개] → [탈락 후보와 사유] 를 적는다

    python -m v2r.content.reply2_lab --brand 전체 --type 질문형 --n 5
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from v2r.content import brand_writer as bw
from v2r.content.sanitize import has_emoji

log = logging.getLogger(__name__)

#: 후보를 다시 받아 보는 최대 횟수
MAX_ROUNDS = 10

#: 프롬프트에 붙일 기존 대대댓글2 예시 수
FEW_SHOT_COUNT = 8

#: 기존 예시와 이만큼 닮으면 베낀 것으로 본다
SIMILARITY_MAX = 0.9

#: 실행 대상 기본 묶음 (브랜드, 원고유형)
DEFAULT_BUNDLES: tuple[tuple[str, str], ...] = (
    ("우아덤", "질문형"),
    ("장으뜸", "질문형"),
    ("코숨핏", "질문형"),
    ("뉴더미스", "질문형"),
    ("팥순이", "질문형"),
    ("팥순이", "후기형"),
)

#: 보고서를 적는 자리
REPORT_DIR = Path("docs/reports")


# ------------------------------------------------------------------ 기존 예시 모으기
def collect_reply2(
    brand: str, manuscript_type: str = "질문형", rows_or_path: Any = None
) -> list[str]:
    """브랜드 시트의 완성 원고에서 **대대댓글2 텍스트만** 전부 뽑는다.

    `brand_writer.load_examples` 와 같은 자리(`게시글 쓰기 원본` 탭, B열 본문)를
    읽지만, 완성 원고 2건이 아니라 **대대댓글2 전부**를 모은다는 점만 다르다.
    같은 문장이 여러 번 나오면 한 번만 남긴다.
    """
    from v2r.content.manuscript import parse_article

    want = (manuscript_type or "").strip() or "질문형"
    rows: list[dict]
    if isinstance(rows_or_path, list):
        rows = list(rows_or_path)
    else:
        path = Path(rows_or_path or f"data/brand_sheet_{brand}.xlsx")
        if not path.exists():
            log.warning("브랜드 시트가 없습니다: %s", path)
            return []
        from v2r.sources.keyword_list import rows_from_xlsx
        from v2r.sources.sheets import _restore_header_row

        rows = _restore_header_row(rows_from_xlsx(path, bw.EXAMPLE_SHEET))[0]

    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mtype, text = bw._example_row_text(row)
        if ((mtype or "").strip() or "질문형") != want:
            continue
        text = (text or "").strip()
        if "제목" not in text:
            continue
        try:
            parsed = parse_article(text)
        except Exception:  # pragma: no cover - 파서가 못 읽는 셀은 버린다
            continue
        for node in parsed.comments:
            if re.sub(r"\s+", "", node.label) != "대대댓글2":
                continue
            value = (node.text or "").strip()
            key = re.sub(r"\s+", "", value)
            if len(key) < 10 or key in seen:
                continue
            seen.add(key)
            out.append(value)
    return out


def rank_examples(examples: list[str], rule: bw.BrandRule) -> list[str]:
    """검증을 가장 잘 통과하는 순서로 정렬한다 (few-shot으로 붙일 것 고르기)."""
    return sorted(examples, key=lambda t: (len(bw.reply2_problems(t, rule)), -len(t)))


# ------------------------------------------------------------------ 검증
def similarity(a: str, b: str) -> float:
    """공백을 지운 두 문장의 닮은 정도 (0~1)."""
    return SequenceMatcher(
        None, re.sub(r"\s+", "", a or ""), re.sub(r"\s+", "", b or "")
    ).ratio()


def candidate_problems(
    text: str, rule: bw.BrandRule, examples: list[str] | None = None
) -> list[str]:
    """후보 한 줄의 탈락 사유 전부 (전부 하드 검증이다).

    길이는 사용자 지시(2026-09-21)대로 **상한의 200%까지 허용**하고
    `REPLY2_MIN_LEN` 미만만 떨어뜨린다.
    """
    value = (text or "").strip()
    if not value:
        return ["빈 줄"]
    problems = list(bw.reply2_problems(value, rule))
    size = len(value)
    cap = int(bw.REPLY2_MAX_LEN * bw.SOFT_LIMIT_FACTOR)
    if size < bw.REPLY2_MIN_LEN:
        problems.append(f"길이 — {size}자로 너무 짧다 ({bw.REPLY2_MIN_LEN}자 이상)")
    elif size > cap:
        problems.append(f"길이 — {size}자로 너무 길다 ({cap}자 이하)")
    if has_emoji(value):
        problems.append("이모지 — 그림 이모지는 쓰지 않는다")
    # 후기형은 "기존 예시와 같아도 된다"는 사용자 결정(2026-09-21)이라 베끼기 검사를 뺀다
    for one in [] if rule.manuscript_type == "후기형" else (examples or []):
        if similarity(value, one) >= SIMILARITY_MAX:
            problems.append(f"베끼기 — 기존 예시와 {int(similarity(value, one) * 100)}% 같다")
            break
    return problems


# ------------------------------------------------------------------ 프롬프트
REPLY2_SYSTEM = """너는 네이버 카페 바이럴 댓글을 쓴다.
지금 쓰는 것은 **대대댓글2** 한 줄뿐이다. 다른 댓글은 쓰지 않는다.
대대댓글2는 그 글에서 제품이 처음 나오는 자리라 가장 중요하다.

{tone}

{internal}

출력
- JSON 배열 하나만 출력한다: ["후보1", "후보2", ...]
- 각 원소는 줄바꿈 없는 댓글 한 줄이다
- 코드펜스와 설명 문장을 붙이지 않는다"""


def build_prompt(
    rule: bw.BrandRule,
    body: str,
    keyword: str,
    examples: list[str],
    n: int,
    problems: list[str] | None = None,
    golden: list[str] | None = None,
) -> tuple[str, str]:
    """`(system, user)` 프롬프트. system은 브랜드마다 고정이라 캐시를 탄다."""
    from v2r.llm.prompts import HUMAN_TONE_RULES, NO_INTERNAL_TERMS_RULE

    product = rule.product_in_comment or rule.product
    lines: list[str] = [
        REPLY2_SYSTEM.format(tone=HUMAN_TONE_RULES, internal=NO_INTERNAL_TERMS_RULE),
        "",
        f"브랜드: {rule.brand} (원고유형 {rule.manuscript_type})",
        f"댓글에서 쓸 제품 표기: {product}",
        f"원씽 이동(설득 논리): {rule.one_thing}",
        f"쓸 수 있는 권위재: {rule.authority}",
    ]
    if rule.manuscript_type == "후기형":
        lines += [
            "",
            "<대대댓글2 — 후기형에서는 역할이 다르다>",
            "- 여분 댓글풀 계정이 저도 이거 먹는 중이라고 거드는 자리다",
            f"- 제품명({product})을 다시 쓰지 말고 `이거` 로 받는다",
            "- 기간과 감량 수치를 숫자로 적는다 (5주만에 7kg 처럼 매번 다르게)",
            "- 정부기관 실험에서 체지방 25% 감소 결과가 나왔다는 근거를 한 문장으로 붙인다",
            f"- 감량 수치는 {bw.REVIEW_MIN_KG}kg 이상으로 쓴다 (기간은 매번 다르게 바꾼다)",
            "- 완전한 문장으로 쓰고 반드시 서술 어미(~요 ~에요 ~더라구요)로 끝낸다",
        ]
        lines += bw.golden_block(rule, golden)
    else:
        lines += bw.reply2_prompt_block(rule, golden)
    if rule.required_phrases:
        need = [o for label, o in rule.required_phrases if label == "대대댓글2"]
        if need:
            lines.append("")
            lines.append("<반드시 들어가야 하는 멘트>")
            lines.extend("- " + " 또는 ".join(o) + " 를 반드시 넣는다" for o in need)
    picked = {re.sub(r"\s+", "", t) for t in (golden or [])}
    rest = [t for t in examples if re.sub(r"\s+", "", t) not in picked]
    if rest:
        lines += ["", "<기존 완성 원고의 대대댓글2 — 이 구조와 길이감을 그대로 따른다>"]
        lines += [f"{i}. {t}" for i, t in enumerate(rest, start=1)]
    lines += [
        "",
        "위 예시와 **같은 구조·같은 길이감**으로 쓴다."
        " 예시 문장을 그대로 베끼면 안 된다 (소재와 표현을 바꾼다)",
        "길이를 맞추려고 조사나 서술어를 빼지 마라 — 문장이 온전한 것이 가장 먼저다",
    ]
    system = "\n".join(lines)

    user_lines = [
        f"작성 키워드: {keyword}",
        "",
        "<이 글의 본문 — 이 흐름에 맞는 대대댓글2를 쓴다>",
        bw.strip_placeholders(body or "").strip(),
        "",
        f"이 본문에 어울리는 대대댓글2 후보 {n}개를 JSON 배열로 돌려라",
        "- 후보끼리도 서로 다른 소재·다른 문장 구조로 쓴다",
    ]
    if problems:
        user_lines += [
            "",
            "<직전 후보들이 걸린 사유 — 이번에는 반드시 피할 것>",
            *(f"- {p}" for p in problems),
        ]
    return system, "\n".join(user_lines)


# ------------------------------------------------------------------ 실행
@dataclass
class BundleResult:
    """브랜드(유형) 한 묶음의 결과."""

    brand: str
    manuscript_type: str
    keyword: str = ""
    examples: list[str] = field(default_factory=list)
    golden: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    rejected: list[tuple[str, list[str]]] = field(default_factory=list)
    rounds: int = 0
    error: str = ""

    @property
    def title(self) -> str:
        return f"{self.brand}({self.manuscript_type})"


def latest_manuscript(brand: str, manuscript_type: str) -> dict:
    """오늘 생성해 둔 원고 JSON 하나 (`generated-plan` 우선, 없으면 `generated`)."""
    for root in ("warehouse/manuscripts/generated-plan", "warehouse/manuscripts/generated"):
        folder = Path(root) / brand
        if not folder.exists():
            continue
        files = sorted(
            (p for p in folder.glob("*.json") if p.stem.startswith(manuscript_type)),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("body"):
                return data
    return {}


def run_bundle(
    llm: Any,
    brand: str,
    manuscript_type: str,
    n: int = 5,
    max_rounds: int = MAX_ROUNDS,
    source: dict | None = None,
) -> BundleResult:
    """브랜드(유형) 하나에서 통과 후보 `n`개를 모은다."""
    rule = bw.rule_for(brand, manuscript_type)
    result = BundleResult(brand=brand, manuscript_type=rule.manuscript_type)
    ranked = rank_examples(collect_reply2(brand, rule.manuscript_type), rule)
    if not ranked:
        result.error = "기존 대대댓글2 예시를 찾지 못했습니다"
        return result
    result.examples = ranked
    # 사용자가 고른 골든 문장이 있으면 예시 **맨 앞**에 둔다 (시트 예시보다 우선)
    golden = bw.load_golden_reply2(brand, rule.manuscript_type)
    result.golden = list(golden)
    seen_few = {re.sub(r"\s+", "", t) for t in golden}
    few = golden + [t for t in ranked if re.sub(r"\s+", "", t) not in seen_few]
    few = few[:FEW_SHOT_COUNT]

    data = source if source is not None else latest_manuscript(brand, rule.manuscript_type)
    body = str(data.get("body") or "")
    keyword = str(data.get("keyword") or "")
    if not body:
        result.error = "오늘 생성된 원고 JSON을 찾지 못했습니다"
        return result
    result.keyword = keyword

    seen: set[str] = set()
    notes: list[str] = []
    for round_no in range(1, max(1, int(max_rounds)) + 1):
        result.rounds = round_no
        want = n - len(result.passed)
        system, user = build_prompt(
            rule, body, keyword, few, want + 2, notes[-6:], golden=golden
        )
        try:
            payload = llm.complete_json("brand_comments", system, user, max_tokens=1800)
        except Exception as exc:
            # 응답이 깨지는 일은 다음 판에서 풀리는 경우가 많다 (한 번 걸렸다고 접지 않는다)
            log.warning("[%s/%s] %d판 모델 호출 실패: %s", brand, manuscript_type, round_no, exc)
            result.error = f"모델 호출 실패: {exc}"
            continue
        result.error = ""
        for item in _as_list(payload):
            value = re.sub(r"\s+", " ", str(item or "")).strip()
            key = re.sub(r"\s+", "", value)
            if not key or key in seen:
                continue
            seen.add(key)
            problems = candidate_problems(value, rule, ranked)
            if problems:
                result.rejected.append((value, problems))
                notes.extend(problems)
            else:
                result.passed.append(value)
        if len(result.passed) >= n:
            break
    result.passed = result.passed[:n]
    return result


def _as_list(payload: Any) -> list[str]:
    """모델 응답을 문자열 목록으로 편다."""
    if isinstance(payload, list):
        out: list[str] = []
        for item in payload:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out.append(str(item.get("text") or item.get("내용") or ""))
        return out
    if isinstance(payload, dict):
        for key in ("candidates", "후보", "items", "comments"):
            if key in payload:
                return _as_list(payload[key])
    return []


# ------------------------------------------------------------------ 보고서
def golden_table(day: str = "") -> list[str]:
    """`config/reply2_golden.yaml` 에 저장된 골든 문장 표."""
    lines = ["", "## 채택된 골든 문장 (`config/reply2_golden.yaml`)", ""]
    empty = True
    for brand, mtype in DEFAULT_BUNDLES:
        items = bw.load_golden_reply2(brand, mtype)
        if not items:
            continue
        empty = False
        lines += ["", f"### {brand}({mtype}) — {len(items)}개", ""]
        lines += [f"{i}. {t}" for i, t in enumerate(items, start=1)]
    return [] if empty else lines


def report_markdown(
    results: list[BundleResult], day: str = "", title: str = "", golden: bool = False
) -> str:
    """사람이 번호로 고를 수 있는 후보 문서."""
    day = day or date.today().isoformat()
    lines = [
        f"# {title or '대대댓글2 후보'} ({day})",
        "",
        "기존 원고의 대대댓글2 구조([제품 소개] → [근거 문장] → [대안의 한계] →"
        " [검색 유도])를 그대로 따르게 다시 뽑은 것입니다.",
        "브랜드마다 **번호로 골라** 주시면 그 문장 틀로 본 생성기를 고정하겠습니다.",
        "",
        "| 브랜드(유형) | 통과 | 탈락 | 재요청 |",
        "| --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r.title} | {len(r.passed)}개 | {len(r.rejected)}개 | {r.rounds}회 |"
        )
    for r in results:
        lines += ["", f"## {r.title}", ""]
        if r.keyword:
            lines.append(f"- 본문 키워드: `{r.keyword}`")
        if r.error:
            lines.append(f"- 오류: {r.error}")
        lines += ["", "### 기존 대대댓글2 (참고)", ""]
        lines += [f"- {t}" for t in r.examples[:3]] or ["- (없음)"]
        lines += ["", f"### 통과 후보 {len(r.passed)}개", ""]
        if r.passed:
            lines += [f"{i}. {t}" for i, t in enumerate(r.passed, start=1)]
        else:
            lines.append("(없음)")
        lines += ["", f"### 탈락 후보 {len(r.rejected)}개와 사유", ""]
        if r.rejected:
            for text, problems in r.rejected:
                lines.append(f"- {text}")
                lines.append(f"  - 사유: {' / '.join(problems)}")
        else:
            lines.append("(없음)")
    if golden:
        lines += golden_table(day)
    return "\n".join(lines) + "\n"


def save_report(
    results: list[BundleResult],
    day: str = "",
    out_dir: Any = None,
    name: str = "",
    title: str = "",
    golden: bool = False,
) -> Path:
    day = day or date.today().isoformat()
    folder = Path(out_dir or REPORT_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name or f"reply2-candidates-{day}.md")
    path.write_text(report_markdown(results, day, title, golden), encoding="utf-8")
    return path


# ------------------------------------------------------------------ 명령
def bundles_for(brand: str, manuscript_type: str) -> list[tuple[str, str]]:
    """`--brand` / `--type` 을 실제 묶음 목록으로 바꾼다."""
    raw = (brand or "전체").strip()
    want_type = (manuscript_type or "").strip()
    names = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    everything = not names or any(n in ("전체", "all") for n in names)
    out = [(b, t) for b, t in DEFAULT_BUNDLES if everything or b in names]
    if want_type:
        out = [(b, t) for b, t in out if t == want_type]
        if not out and not everything:
            out = [(n, want_type) for n in names]
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="대대댓글2 후보 뽑기")
    parser.add_argument("--brand", default="전체")
    parser.add_argument("--type", dest="mtype", default="")
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=MAX_ROUNDS)
    parser.add_argument("--out", default="")
    parser.add_argument("--name", default="", help="보고서 파일 이름")
    parser.add_argument("--title", default="", help="보고서 제목")
    parser.add_argument(
        "--golden", action="store_true", help="채택된 골든 문장 표를 보고서에 붙인다"
    )
    args = parser.parse_args(argv)

    from v2r.llm.router import LLMRouter

    llm = LLMRouter.from_settings()
    results: list[BundleResult] = []
    for brand, mtype in bundles_for(args.brand, args.mtype):
        log.info("[%s/%s] 후보 뽑는 중…", brand, mtype)
        result = run_bundle(llm, brand, mtype, n=args.n, max_rounds=args.rounds)
        log.info(
            "[%s/%s] 통과 %d개 탈락 %d개 (%d회)%s",
            brand,
            mtype,
            len(result.passed),
            len(result.rejected),
            result.rounds,
            f" 오류: {result.error}" if result.error else "",
        )
        results.append(result)
    path = save_report(
        results,
        out_dir=args.out or None,
        name=args.name,
        title=args.title,
        golden=args.golden,
    )
    log.info("보고서: %s", path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
