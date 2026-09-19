"""브랜드(제휴) 바이럴 원고 생성기.

Make 시나리오 지침(`warehouse/guides/★NEW 카페 바이럴★/`)을 브랜드별 규칙 표로
증류해, 키워드 하나로 **제목 + 본문 + 댓글 12개**를 만든다.

- 본문은 `brand_body` 용도(모델), 댓글은 `brand_comments` 용도로 나눠 부른다.
- 결과 `Manuscript`의 댓글 구조는 `parse_affiliate_rows`/`parse_article`이 만드는
  것과 같아서(라벨·깊이·역할) 기존 발행 사슬이 그대로 받아 쓴다.
- 발행도 시트 쓰기도 하지 않는다. 검토용 JSON/MD만 남긴다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from v2r.content.manuscript import CommentNode, Manuscript, content_hash, tags_from_keyword
from v2r.llm.prompts import BRAND_BODY_SYSTEM, BRAND_COMMENTS_SYSTEM, HUMAN_TONE_RULES

#: 댓글 12개 라벨 (읽는 순서 = live-comment-order.md §1)
COMMENT_LABELS: tuple[str, ...] = (
    "댓글1",
    "대댓글1",
    "댓글2",
    "대댓글2",
    "대대댓글2",
    "대대대댓글2",
    "댓글3",
    "대댓글3",
    "댓글4",
    "대댓글4",
    "댓글5",
    "대댓글5",
)

#: 라벨 → 작성 계정 역할 (live-comment-order.md §1·§2)
ACCOUNT_ROLES_COMMON: dict[str, str] = {
    "댓글1": "댓글풀 A1",
    "대댓글1": "본문 작성자",
    "댓글2": "댓글풀 A2",
    "대댓글2": "본문 작성자",
    "댓글3": "댓글풀 A3",
    "대댓글3": "본문 작성자",
    "댓글4": "댓글풀 A4",
    "대댓글4": "본문 작성자",
    "댓글5": "댓글풀 A5",
    "대댓글5": "본문 작성자",
}
#: 원고유형별로 갈리는 두 자리
ACCOUNT_ROLES_BY_TYPE: dict[str, dict[str, str]] = {
    "질문형": {"대대댓글2": "댓글2 계정(A2)", "대대대댓글2": "여분 댓글풀 계정(A6)"},
    "후기형": {"대대댓글2": "여분 댓글풀 계정(A6)", "대대대댓글2": "본문 작성자"},
}

#: 본문 글자 수 허용 오차 (지침 상한의 몇 배까지 통과로 볼지)
LENGTH_TOLERANCE = 1.15


class BrandWriteError(RuntimeError):
    """브랜드 원고 생성/검증 실패."""


@dataclass(frozen=True)
class BrandRule:
    """브랜드(+원고유형)별 작성 규칙. 지침에서 뽑은 값만 담는다."""

    brand: str
    manuscript_type: str = "질문형"
    #: 댓글에서 최초로 꺼내는 제품명
    product: str = ""
    #: 댓글에서 제품명을 이렇게 표기한다 (팥순이는 `팥순ㅇㅣ`)
    product_in_comment: str = ""
    #: 본문에 절대 들어가면 안 되는 낱말 (브랜드명·제품명 등)
    banned_in_body: tuple[str, ...] = ()
    target: str = ""
    one_thing: str = ""
    authority: str = ""
    body_max: int = 250
    keyword_count: int = 3
    comment_max: int = 30
    comment2_max: int = 70
    #: `{키워드}`를 몇 번째 문단 뒤에 둘지
    placeholder_after_paragraph: int = 2
    #: 추가 자리표시자 (팥순이 후기형의 `{B/A}`)
    extra_placeholder: str = ""
    #: 제품명이 처음 등장해도 되는 라벨
    first_mention_label: str = "대대댓글2"
    body_structure: tuple[str, ...] = ()
    body_notes: tuple[str, ...] = ()
    comment_notes: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.brand}/{self.manuscript_type}"

    @property
    def comment_label_of_product(self) -> str:
        return self.first_mention_label


_VIRAL_STRUCTURE = (
    "오프닝: 구체적 페르소나와 결핍을 드러내 공감을 만든다 (과장된 극적 표현 금지)",
    "솔루션: 문제가 생긴 과정 → 예전에 해봤지만 효과 없던 노력 → 아직 남은 고민",
    "클로징: 고민을 한 번 더 짚고 해결 방법을 알려달라는 조언 요청으로 끝낸다",
)
_VIRAL_BODY_NOTES = (
    "제목은 작성 키워드로 시작하고 공백 포함 30자 이내 한 문장 물음형으로 쓴다",
    "제목에 공감을 부르는 우는 이모티콘(ㅠㅠ)을 섞는다",
    "모바일 기준으로 한 줄 20자 이내 한 문단 4줄 이내로 문단을 자주 나눈다",
    "제품명이나 브랜드명을 본문에서 절대 말하지 않는다 (댓글에서 나오게 남겨 둔다)",
)
_VIRAL_COMMENT_NOTES = (
    "댓글1은 제품과 무관하게 작성자의 질문에 답하는 중립적 정보다 (광고 아님을 증명)",
    "대댓글1은 작성자가 처음 알게 된 듯한 반응만 쓴다 (정보를 아는 척 공유하면 안 된다)",
    "댓글1과 댓글2 안에는 작성 키워드가 한 글자도 빠짐없이 들어가야 한다",
    "댓글2는 권위재를 근거로 원씽 이동을 제시한다 제품명은 절대 쓰지 않는다",
    "대댓글2는 작성자가 제품이 아니라 성분이나 방법을 되묻는 질문이다",
    "대대댓글2에서 제품명을 처음 꺼내고 프레임을 분리한 뒤 검색해보라고 유도한다",
    "대대대댓글2는 제3자가 222 저도 효과 봤어요 식으로 여론을 만든다",
    "댓글3은 비슷한 고민 경험 공유 대댓글3은 맞장구다",
    "댓글4는 작성자가 시도한 잘못된 방법을 가볍게 짚어 준다 (두 문장 내외)",
    "댓글5는 제3자 여론 보강이다 수치와 기간을 구체적으로 쓰고 다른 제품을 언급하지 않는다",
    "대댓글5는 효과에 호기심을 보이는 반응이다",
)


def _viral_rule(**kwargs: Any) -> BrandRule:
    """바이럴 4종(우아덤·장으뜸·코숨핏·뉴더미스) 공통값으로 규칙을 만든다."""
    base: dict[str, Any] = {
        "body_structure": _VIRAL_STRUCTURE,
        "body_notes": _VIRAL_BODY_NOTES,
        "comment_notes": _VIRAL_COMMENT_NOTES,
    }
    base.update(kwargs)
    notes = base.pop("extra_comment_notes", ())
    if notes:
        base["comment_notes"] = tuple(base["comment_notes"]) + tuple(notes)
    extra_body = base.pop("extra_body_notes", ())
    if extra_body:
        base["body_notes"] = tuple(base["body_notes"]) + tuple(extra_body)
    return BrandRule(**base)


#: 브랜드(+원고유형)별 규칙표
BRAND_RULES: dict[str, BrandRule] = {}


def _register(rule: BrandRule) -> None:
    BRAND_RULES[rule.key] = rule


_register(
    _viral_rule(
        brand="우아덤",
        product="그린커피 아하바하",
        banned_in_body=("그린커피 아하바하", "아하바하", "그린커피"),
        target="착색 미백 색소침착으로 고민하는 타겟",
        one_thing=(
            "착색된 부위는 각질이 두꺼워 미백 성분이 흡수되지 않는다 "
            "각질 정돈(AHA·BHA)과 멜라닌 억제(그린커피 추출물)가 같이 되어야 한다"
        ),
        authority="피부과 의사 레이저 시술 후 관리 논문 카페 추천",
    )
)
_register(
    _viral_rule(
        brand="장으뜸",
        product="장으뜸 장어즙",
        banned_in_body=("장으뜸", "장어즙"),
        target="난임 유산 자연임신을 바라는 임신 준비 타겟",
        one_thing="기력이 좋아야 착상이 잘 된다 내 몸에 실제로 작용하는 것이 중요하다",
        authority="산부인과 원장님 난임 카페 추천 가족 권유",
        extra_comment_notes=(
            "대댓글2에는 제품 이라는 낱말을 절대 쓰지 않는다 성분이나 방법으로 묻는다",
            "대대대댓글2에는 장으뜸 이라는 브랜드명을 쓰지 않는다 (효과 여론만 남긴다)",
            "댓글2는 방법을 알게 된 뒤 임신에 성공했다는 확실한 효과로 마무리한다",
        ),
    )
)
_register(
    _viral_rule(
        brand="코숨핏",
        product="코숨핏",
        banned_in_body=("코숨핏", "광고"),
        target="코골이 비염으로 고민하는 타겟",
        one_thing="기도 근육을 강화해야 코골이와 비염이 잡힌다",
        authority="수면클리닉 이비인후과 의사 논문",
        extra_body_notes=(
            "코골이 소재라면 글쓴이는 반드시 코를 고는 본인이 아니라 배우자나 여자친구 시점이다",
            "본문에 광고 라는 낱말을 절대 쓰지 않는다 (카페 필터에 걸린다)",
        ),
    )
)
_register(
    _viral_rule(
        brand="뉴더미스",
        product="자연방패 항문세정제",
        banned_in_body=("자연방패", "뉴더미스", "항문세정제"),
        target="치질 항문 가려움 통증 사타구니 습진으로 고민하는 타겟",
        one_thing="항문 보호막을 지켜 주는 관리가 근본 해결책이다",
        authority="항문외과 의사 치질 카페 추천",
        extra_comment_notes=(
            "항문외과 의사가 알려줬다 는 표현을 반복해서 쓰지 않는다 권위재를 매번 다르게 고른다",
        ),
    )
)

#: 팥순이 질문형 — 다이어트 4단계 구조, 본문 200자, 댓글 전부 50자
_register(
    BrandRule(
        brand="팥순이",
        manuscript_type="질문형",
        product="팥순추출물 다이어트",
        product_in_comment="팥순ㅇㅣ",
        banned_in_body=("팥순이",),
        target="체중 감량 다이어트를 하는 타겟",
        one_thing="팥순추출물이 붓기가 아니라 체중 감량에 작용한다",
        authority="다이어트 카페 추천 후기",
        body_max=200,
        keyword_count=3,
        comment_max=50,
        comment2_max=50,
        placeholder_after_paragraph=2,
        body_structure=(
            "1단계 다이어트 중 고민 상황 (50자)",
            "2단계 키워드를 접하게 된 계기 (50자)",
            "3단계 핵심 궁금증 (50~60자)",
            "4단계 후기 요청 멘트 (40~50자)",
        ),
        body_notes=_VIRAL_BODY_NOTES
        + (
            "붓기와 직접 이어지는 키워드가 아니면 붓기보다 체중 감량 다이어트 내용으로 쓴다",
            "본문에서는 팥순이 라고 절대 쓰지 않는다",
            "반드시 다이어트와 관련된 질문을 본문에 적는다",
        ),
        comment_notes=_VIRAL_COMMENT_NOTES
        + (
            "모든 댓글은 50자를 넘지 않는다",
            "제품은 대대댓글2에서만 팥순ㅇㅣ 로 표기해 처음 꺼낸다 (팥순이 라고 붙여 쓰지 않는다)",
            "팥순추출물 이라고 반드시 언급한다 다른 방법 새로운 방법 같은 간접 표현은 쓰지 않는다",
            "네이버 를 언급하지 않는다",
            "팥순이 제품인데요 팥순이 검색하시면 같은 고정 표현을 쓰지 않는다",
        ),
    )
)

#: 팥순이 후기형 — 작성자가 이미 써 본 사람. 첫 문단 뒤 {키워드}, 병행 효과에 {B/A}
_register(
    BrandRule(
        brand="팥순이",
        manuscript_type="후기형",
        product="팥순추출물 다이어트",
        product_in_comment="팥순ㅇㅣ",
        banned_in_body=("팥순이",),
        target="체중 감량 다이어트를 하는 타겟",
        one_thing="키워드 단독으로는 한계가 있고 팥순추출물 다이어트를 병행해야 한다",
        authority="다이어트 카페 추천 후기",
        body_max=300,
        keyword_count=4,
        comment_max=50,
        comment2_max=50,
        placeholder_after_paragraph=1,
        extra_placeholder="{B/A}",
        first_mention_label="대댓글2",
        body_structure=(
            "키워드 단독 사용 후기로 시작",
            "느낀 점과 아쉬운 점",
            "팥순추출물 다이어트를 발견한 계기",
            "병행 효과 (여기에 {B/A} 표시)",
            "같은 고민을 하는 사람에게 추천하며 마무리",
        ),
        body_notes=(
            "제목에 팥순이 를 쓰지 않고 이거 이것만 같은 대명사로 바꾼다",
            "제목은 공백 포함 30자 이내이고 물음형 후기형을 섞어 궁금증을 만든다",
            "이것만 추가했더니 두 달째 같은 특정 문구를 반복하지 않는다",
            "본문에서는 팥순추출물 또는 팥순추출물 다이어트 로만 쓰고 팥순이 는 절대 쓰지 않는다",
            "키워드에 대한 객관적 설명을 길게 늘어놓지 않는다",
            "모바일 기준으로 한 줄 20자 이내 한 문단 4줄 이내로 문단을 자주 나눈다",
            "마무리의 감량 수치는 -10.0kg~-12.0kg 사이에서 소수 첫째 자리까지 매번 다르게 쓴다",
        ),
        comment_notes=_VIRAL_COMMENT_NOTES
        + (
            "모든 댓글은 50자를 넘지 않는다",
            "작성자는 이미 써 본 사람이라 대댓글2에서 작성자가 직접 팥순ㅇㅣ 를 꺼낸다",
            "대대댓글2는 여분 댓글 계정이 저도 이거 먹는 중이라고 거든다",
            "대대대댓글2는 작성자가 맞장구치며 마무리한다",
            "네이버 를 언급하지 않는다",
        ),
    )
)


def rule_for(brand: str, manuscript_type: str = "") -> BrandRule:
    """브랜드(+원고유형) 규칙. 없으면 BrandWriteError."""
    mtype = (manuscript_type or "").strip() or "질문형"
    rule = BRAND_RULES.get(f"{brand}/{mtype}") or BRAND_RULES.get(f"{brand}/질문형")
    if rule is None:
        raise BrandWriteError(
            f"브랜드 규칙이 없습니다: {brand} (등록된 브랜드: "
            + ", ".join(sorted({r.brand for r in BRAND_RULES.values()}))
            + ")"
        )
    return rule


# ------------------------------------------------------------------ 세기
def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def strip_placeholders(text: str) -> str:
    """`{...}` 자리표시자를 지운 텍스트."""
    return re.sub(r"\{[^{}\r\n]*\}", "", text or "")


def body_length(body: str) -> int:
    """본문 글자 수 (공백 제외, 자리표시자 제외)."""
    return len(_squash(strip_placeholders(body)))


def keyword_hits(body: str, keyword: str) -> int:
    """본문에 키워드가 몇 번 들어갔는지 (공백 무시)."""
    needle = _squash(keyword)
    if not needle:
        return 0
    return _squash(strip_placeholders(body)).count(needle)


def paragraphs(body: str) -> list[str]:
    """빈 줄로 나눈 문단 목록."""
    return [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]


# ------------------------------------------------------------------ 프롬프트
def _guide_excerpt(guide_text: str, max_chars: int = 3500) -> str:
    """지침 원문에서 앞부분만 참고 자료로 붙인다."""
    text = (guide_text or "").strip()
    if not text:
        return ""
    return "\n<지침 원문 참고>\n" + text[:max_chars] + "\n"


def build_body_prompt(
    brand: str,
    keyword: str,
    cafe: str = "",
    guide_text: str = "",
    manuscript_type: str = "",
) -> tuple[str, str]:
    """본문 생성용 (system, user) 프롬프트."""
    rule = rule_for(brand, manuscript_type)
    system = BRAND_BODY_SYSTEM.format(tone=HUMAN_TONE_RULES)

    lines: list[str] = [
        f"브랜드: {rule.brand} (원고유형 {rule.manuscript_type})",
        f"작성 키워드: {keyword}",
        f"타겟: {rule.target}",
    ]
    if cafe:
        lines.append(f"올릴 카페: {cafe}")
    lines.append("")
    lines.append("<반드시 지켜야 할 규칙>")
    lines.append(f"- 본문 글자 수는 공백 제외 {rule.body_max}자를 넘지 않는다")
    lines.append("- 분량을 맞추려고 중간에 끊지 말고 반드시 끝을 맺는다")
    lines.append(
        f"- 작성 키워드 `{keyword}` 를 한 글자도 빼먹지 말고 본문에 정확히 "
        f"{rule.keyword_count}번 넣는다"
    )
    if rule.banned_in_body:
        lines.append(
            "- 본문에 다음 낱말을 절대 쓰지 않는다: " + ", ".join(rule.banned_in_body)
        )
    lines.append(
        f"- 본문 {rule.placeholder_after_paragraph}번째 문단이 끝난 뒤 줄 하나에 "
        "`{키워드}` 만 단독으로 적는다 (앞뒤로 빈 줄을 둔다). "
        "이때 {키워드}는 키워드 값이 아니라 글자 그대로 `키워드` 다"
    )
    if rule.extra_placeholder:
        lines.append(
            f"- 병행 효과를 말하는 문단 뒤에 줄 하나에 `{rule.extra_placeholder}` 만 단독으로 적는다"
        )
    lines.append("")
    lines.append("<본문 구조>")
    lines.extend(f"- {s}" for s in rule.body_structure)
    lines.append("")
    lines.append("<작성 지침>")
    lines.extend(f"- {s}" for s in rule.body_notes)
    lines.append(_guide_excerpt(guide_text))
    lines.append('출력: {"title": "제목", "body": "본문"}')
    return system, "\n".join(lines)


def build_comments_prompt(
    brand: str,
    keyword: str,
    title: str,
    body: str,
    manuscript_type: str = "",
    guide_text: str = "",
) -> tuple[str, str]:
    """댓글 12개 생성용 (system, user) 프롬프트."""
    rule = rule_for(brand, manuscript_type)
    system = BRAND_COMMENTS_SYSTEM.format(tone=HUMAN_TONE_RULES)
    product = rule.product_in_comment or rule.product

    lines: list[str] = [
        f"브랜드: {rule.brand} (원고유형 {rule.manuscript_type})",
        f"작성 키워드: {keyword}",
        f"댓글에서 쓸 제품 표기: {product}",
        f"원씽 이동(설득 논리): {rule.one_thing}",
        f"쓸 수 있는 권위재: {rule.authority}",
        "",
        "<본문>",
        f"제목: {title}",
        strip_placeholders(body).strip(),
        "",
        "<댓글 글자 수>",
        f"- 댓글2를 뺀 모든 댓글은 {rule.comment_max}자를 넘지 않는다",
        f"- 댓글2도 {rule.comment2_max}자를 넘지 않는다",
        "- 줄 나눔 없이 한 줄로 쓴다",
        "",
        "<12개 구조와 역할>",
        "댓글1 / 대댓글1 / 댓글2 / 대댓글2 / 대대댓글2 / 대대대댓글2 /"
        " 댓글3 / 대댓글3 / 댓글4 / 대댓글4 / 댓글5 / 대댓글5 — 정확히 이 12개만",
        f"- 제품명({product})은 {rule.first_mention_label} 에서 처음 나온다."
        f" 그 앞 댓글에는 절대 제품명을 쓰지 않는다",
    ]
    lines.extend(f"- {s}" for s in rule.comment_notes)
    lines.append(_guide_excerpt(guide_text, 2500))
    return system, "\n".join(lines)


# ------------------------------------------------------------------ 검증
def _check(item: str, expected: str, actual: str, ok: bool, hard: bool = True) -> dict:
    return {"항목": item, "기준": expected, "실제": actual, "통과": ok, "필수": hard}


def validate(manuscript: Manuscript, rule: BrandRule | None = None) -> list[dict]:
    """원고를 검증해 항목별 결과를 돌려준다 (예외를 던지지 않는다)."""
    rule = rule or rule_for(brand_of(manuscript), manuscript.manuscript_type)
    body = manuscript.body or ""
    keyword = manuscript.keyword or ""
    checks: list[dict] = []

    length = body_length(body)
    limit = int(rule.body_max * LENGTH_TOLERANCE)
    checks.append(
        _check("본문 글자 수(공백 제외)", f"{rule.body_max}자 이하", f"{length}자", length <= limit)
    )

    hits = keyword_hits(body, keyword)
    checks.append(
        _check("키워드 포함 횟수", f"{rule.keyword_count}회 이상", f"{hits}회", hits >= rule.keyword_count)
    )

    squashed = _squash(body)
    found = [w for w in rule.banned_in_body if _squash(w) and _squash(w) in squashed]
    checks.append(
        _check(
            "본문 브랜드·제품명 미언급",
            "없음" if not rule.banned_in_body else ", ".join(rule.banned_in_body) + " 금지",
            ", ".join(found) if found else "없음",
            not found,
        )
    )

    has_ph = bool(re.search(r"^\s*\{키워드\}\s*$", body, re.MULTILINE))
    checks.append(_check("{키워드} 자리표시자", "단독 줄로 1개", "있음" if has_ph else "없음", has_ph))
    if rule.extra_placeholder:
        extra = rule.extra_placeholder in body
        checks.append(
            _check(
                f"{rule.extra_placeholder} 자리표시자",
                "본문에 1개",
                "있음" if extra else "없음",
                extra,
            )
        )

    labels = [re.sub(r"\s+", "", c.label) for c in manuscript.comments]
    ok_labels = labels == list(COMMENT_LABELS)
    checks.append(
        _check("댓글 12개 구조", " ".join(COMMENT_LABELS), f"{len(labels)}개", ok_labels)
    )

    # 글자 수·최초 언급은 경고(필수 아님)
    too_long = [
        f"{c.label}({len(c.text)}자)"
        for c in manuscript.comments
        if len(c.text) > (rule.comment2_max if c.label == "댓글2" else rule.comment_max)
    ]
    checks.append(
        _check(
            "댓글 글자 수",
            f"댓글2 {rule.comment2_max}자 그 외 {rule.comment_max}자 이하",
            ", ".join(too_long) if too_long else "모두 통과",
            not too_long,
            hard=False,
        )
    )

    product = rule.product_in_comment or rule.product
    before = COMMENT_LABELS[: COMMENT_LABELS.index(rule.first_mention_label)]
    early = [
        c.label
        for c in manuscript.comments
        if re.sub(r"\s+", "", c.label) in before
        and any(_squash(p) in _squash(c.text) for p in (product, rule.product) if p)
    ]
    checks.append(
        _check(
            "제품명 최초 언급 위치",
            f"{rule.first_mention_label} 에서 처음",
            ", ".join(early) + " 에서 먼저 나옴" if early else "규칙대로",
            not early,
            hard=False,
        )
    )
    return checks


def failures(checks: list[dict]) -> list[str]:
    """필수 검증 실패 항목의 한국어 설명."""
    return [
        f"{c['항목']}: 기준 {c['기준']}, 실제 {c['실제']}"
        for c in checks
        if c["필수"] and not c["통과"]
    ]


def brand_of(manuscript: Manuscript) -> str:
    """원고에 새겨 둔 브랜드 이름 (`source`가 `generated:<브랜드>`)."""
    src = manuscript.source or ""
    return src.split(":", 1)[1] if ":" in src else src


# ------------------------------------------------------------------ 생성
def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _comment_nodes(payload: Any) -> list[CommentNode]:
    """모델 응답 → CommentNode 12개 (라벨 순서 고정)."""
    table: dict[str, str] = {}
    if isinstance(payload, dict):
        inner = payload.get("comments") if "comments" in payload else payload
        if isinstance(inner, dict):
            table = {re.sub(r"\s+", "", str(k)): _as_text(v) for k, v in inner.items()}
        elif isinstance(inner, list):
            payload = inner
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                label = re.sub(r"\s+", "", _as_text(item.get("label") or item.get("라벨")))
                if label:
                    table[label] = _as_text(item.get("text") or item.get("내용"))
    nodes: list[CommentNode] = []
    for label in COMMENT_LABELS:
        depth = len(label) - len(label.lstrip("대"))
        nodes.append(
            CommentNode(
                label=label,
                text=table.get(label, ""),
                depth=depth,
                role={0: "comment", 1: "reply", 2: "reply2", 3: "reply3"}[depth],
            )
        )
    return nodes


def _clean_body(body: str) -> str:
    """모델이 남긴 마크다운 장식과 과한 빈 줄을 정리한다."""
    out = re.sub(r"\*{1,3}", "", body or "")
    out = re.sub(r"^\s*본문\s*[:：]\s*", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip("\n")


def generate_manuscript(
    rt: Any,
    brand: str,
    keyword: str,
    cafe: str = "",
    manuscript_type: str = "",
    guide_text: str = "",
) -> Manuscript:
    """키워드 한 개로 제목·본문·댓글 12개를 만든다. 검증 실패 시 한 번 재시도."""
    llm = getattr(rt, "llm", None) or rt
    if llm is None:
        raise BrandWriteError("모델을 쓸 수 없습니다 (ANTHROPIC_API_KEY를 확인하세요)")
    rule = rule_for(brand, manuscript_type)
    if not (keyword or "").strip():
        raise BrandWriteError("작성 키워드가 비어 있습니다")

    body_sys, body_user = build_body_prompt(
        brand, keyword, cafe, guide_text, rule.manuscript_type
    )
    last_error: list[str] = []
    for attempt in (1, 2):
        user = body_user
        if last_error:
            user += "\n\n<직전 시도에서 어긴 규칙 — 이번에는 반드시 지킬 것>\n" + "\n".join(
                f"- {e}" for e in last_error
            )
        try:
            data = llm.complete_json("brand_body", body_sys, user, max_tokens=1600)
        except Exception as exc:  # 모델 오류
            raise BrandWriteError(f"본문 생성 실패({brand}/{keyword}): {exc}") from exc
        if not isinstance(data, dict):
            last_error = ["JSON 객체 하나만 출력해야 합니다"]
            continue
        title = _as_text(data.get("title") or data.get("제목"))
        body = _clean_body(_as_text(data.get("body") or data.get("본문")))
        draft = Manuscript(
            title=title,
            body=body,
            cafe=cafe,
            keyword=keyword,
            tags=tags_from_keyword(keyword),
            manuscript_type=rule.manuscript_type,
            source=f"generated:{rule.brand}",
            comments=_comment_nodes({}),
            content_hash=content_hash(title, body),
        )
        body_fail = [
            f
            for f in failures(validate(draft, rule))
            if not f.startswith("댓글 12개")
        ]
        if not body_fail:
            break
        last_error = body_fail
    else:
        raise BrandWriteError(
            f"본문 검증 실패({brand}/{keyword}): " + " / ".join(last_error)
        )

    cmt_sys, cmt_user = build_comments_prompt(
        brand, keyword, draft.title, draft.body, rule.manuscript_type, guide_text
    )
    comment_error: list[str] = []
    for attempt in (1, 2):
        user = cmt_user
        if comment_error:
            user += "\n\n<직전 시도에서 어긴 규칙 — 이번에는 반드시 지킬 것>\n" + "\n".join(
                f"- {e}" for e in comment_error
            )
        try:
            payload = llm.complete_json("brand_comments", cmt_sys, user, max_tokens=2000)
        except Exception as exc:
            raise BrandWriteError(f"댓글 생성 실패({brand}/{keyword}): {exc}") from exc
        draft.comments = _comment_nodes(payload)
        missing = [c.label for c in draft.comments if not c.text]
        if not missing:
            break
        comment_error = ["빠진 댓글: " + ", ".join(missing)]
    else:
        raise BrandWriteError(
            f"댓글 검증 실패({brand}/{keyword}): " + " / ".join(comment_error)
        )

    problems = failures(validate(draft, rule))
    if problems:
        raise BrandWriteError(
            f"원고 검증 실패({brand}/{keyword}): " + " / ".join(problems)
        )
    draft.content_hash = content_hash(draft.title, draft.body)
    return draft


# ------------------------------------------------------------------ 출력
def sheet_text(manuscript: Manuscript) -> str:
    """시트 B열에 그대로 붙여 넣을 수 있는 원고 텍스트."""
    lines = ["제목 :", manuscript.title, "", "본문 :", manuscript.body, ""]
    for node in manuscript.comments:
        lines.append(f"{node.label} :")
        lines.append(node.text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def to_dict(manuscript: Manuscript) -> dict:
    """저장용 dict (원고 + 시트 텍스트 + 검증 결과)."""
    data = manuscript.model_dump()
    data["brand"] = brand_of(manuscript)
    data["sheet_text"] = sheet_text(manuscript)
    data["checks"] = validate(manuscript)
    return data


def save_json(manuscript: Manuscript, path: str | Path) -> Path:
    """원고 1건을 JSON으로 저장."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(to_dict(manuscript), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def _md_escape(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def review_block(manuscript: Manuscript, heading_level: int = 2) -> str:
    """원고 1건의 사람이 읽는 블록 (제목·본문·댓글 표·검증 표)."""
    rule = rule_for(brand_of(manuscript), manuscript.manuscript_type)
    roles = dict(ACCOUNT_ROLES_COMMON)
    roles.update(ACCOUNT_ROLES_BY_TYPE.get(rule.manuscript_type, {}))
    h = "#" * heading_level
    out: list[str] = [
        f"{h} {manuscript.keyword} ({rule.brand} / {rule.manuscript_type})",
        "",
        f"- 키워드: `{manuscript.keyword}`",
        f"- 카페: {manuscript.cafe or '(지정 없음)'}",
        f"- 원고유형: {rule.manuscript_type}",
        f"- 본문 글자 수(공백 제외): {body_length(manuscript.body)}자"
        f" / 키워드 {keyword_hits(manuscript.body, manuscript.keyword)}회",
        "",
        f"{h}# 제목",
        "",
        f"> {manuscript.title}",
        "",
        f"{h}# 본문 (자리표시자 그대로)",
        "",
        "```text",
        manuscript.body,
        "```",
        "",
        f"{h}# 댓글 12개",
        "",
        "| 라벨 | 작성 계정 역할 | 글자 수 | 내용 |",
        "|---|---|---:|---|",
    ]
    for node in manuscript.comments:
        out.append(
            f"| {node.label} | {roles.get(node.label, '-')} | {len(node.text)} |"
            f" {_md_escape(node.text)} |"
        )
    out += ["", f"{h}# 검증 결과", "", "| 항목 | 기준 | 실제 | 결과 |", "|---|---|---|---|"]
    for check in validate(manuscript, rule):
        mark = "통과" if check["통과"] else ("실패" if check["필수"] else "경고")
        out.append(
            f"| {_md_escape(check['항목'])} | {_md_escape(check['기준'])} |"
            f" {_md_escape(check['실제'])} | {mark} |"
        )
    out.append("")
    return "\n".join(out)


def write_review_md(
    manuscripts: Manuscript | list[Manuscript],
    path: str | Path,
    title: str = "",
) -> Path:
    """사람이 읽는 검토용 MD를 쓴다."""
    items = [manuscripts] if isinstance(manuscripts, Manuscript) else list(manuscripts)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    head = title or (
        f"브랜드 원고 검토 — {brand_of(items[0]) if items else ''}"
    )
    parts = [f"# {head}", "", f"원고 {len(items)}건. 발행하지 않았고 시트에도 쓰지 않았다.", ""]
    for m in items:
        parts.append(review_block(m, heading_level=2))
    target.write_text("\n".join(parts), encoding="utf-8")
    return target


__all__ = [
    "BRAND_RULES",
    "BrandRule",
    "BrandWriteError",
    "COMMENT_LABELS",
    "body_length",
    "build_body_prompt",
    "build_comments_prompt",
    "failures",
    "generate_manuscript",
    "keyword_hits",
    "review_block",
    "rule_for",
    "save_json",
    "sheet_text",
    "validate",
    "write_review_md",
]
