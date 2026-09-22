"""사용자 최종 결정 7건 (2026-09-22) — 코드로 못 박아 둔다.

보고서: `docs/reports/final-rules-2026-09-22.md`

1. 재시도 원인을 `stats["retry_reasons"]` 에 남긴다 (예전에는 횟수만 남았다)
2. 고민 문장은 **ㅋㅋ ㅎㅎ 가 붙었는가만** 본다 (ㅠㅠ 없음은 위반이 아니다)
3. GPT 교차 검증은 **브랜드 원고에만** (일상 글·일상 글 댓글에서는 부르지 않는다)
4. 의도적 오탈자·띄어쓰기는 **경고**로만 알린다 (재시도를 부르지 않는다)
5. 권위 근거 중복은 **같은 낱말**일 때만 (정부기관 ≠ 농촌진흥청)
6. 장으뜸·뉴더미스 근거 문장은 **마이너스 카피**
7. 사용량 장부에 요금제 한도 종류는 적지 않는다
"""

from __future__ import annotations

import pytest

from v2r.content import brand_writer as bw
from v2r.content import gpt_crosscheck as gx


# ------------------------------------------------------------------ 도우미
def _manuscript(brand: str = "우아덤", body: str = "본문이에요", source: str = "") -> bw.Manuscript:
    return bw.Manuscript(
        title="제목",
        body=body,
        cafe="씨씨앙",
        keyword="비타민C",
        tags=[],
        manuscript_type="질문형",
        source=source if source else f"generated:{brand}",
        comments=bw._comment_nodes({}),
        content_hash="x",
    )


# --- 2. 고민 문장은 ㅋㅋ ㅎㅎ 만 본다 ---------------------------------
def test_고민문장_기준은_ㅋㅋㅎㅎ만_본다():
    rule = bw.rule_for("우아덤")
    checks = bw.validate(_manuscript(), rule)
    items = [c for c in checks if c["항목"].startswith("고민 문장 웃음 표기")]
    assert len(items) == 2  # 본문 / 댓글
    for c in items:
        assert "ㅋㅋ ㅎㅎ 가 붙었는가만 본다" in c["기준"]
        assert "없어도 위반이 아니다" in c["기준"]


def test_ㅠㅠ가_없어도_위반이_아니다():
    """ㅠㅠ 는 있으면 좋을 뿐 필수가 아니다 (사용자 결정 2026-09-22)."""
    assert bw.worry_laughter_problems("착색이 안빠져서 속상해요") == []
    assert bw.worry_laughter_problems("착색이 안빠져서 속상해요ㅠㅠ") == []
    assert bw.worry_laughter_problems("착색이 안빠져서 속상해요ㅋㅋ")


def test_GPT_체크리스트도_ㅋㅋㅎㅎ만_묻는다():
    rule = bw.rule_for("우아덤")
    items = gx.checklist(_manuscript(), rule)
    worry = [t for t in items if t.startswith("고민 문장 웃음 표기")]
    assert worry, "고민 문장 항목이 체크리스트에 있어야 한다"
    for t in worry:
        assert "ㅋㅋ ㅎㅎ 가 붙었는가만 본다" in t
        # `ㅠㅠ 없음`을 위반으로 잡던 문구가 사라졌는지
        assert "문장에는 ㅠㅠ (ㅋㅋ ㅎㅎ 금지)" not in t


# --- 3. GPT 교차 검증은 브랜드 원고에만 -------------------------------
def test_설정에_scope가_brand_only로_들어있다():
    from v2r.config import load_yaml

    cfg = gx.load_config(load_yaml("models"))
    assert cfg["scope"] == "brand_only"


def test_설정이_비어도_가장_좁은_쪽으로_둔다():
    assert gx.load_config({"crosscheck": {"enabled": True}})["scope"] == "brand_only"


def test_브랜드_원고만_허용한다():
    assert gx.allowed_for(_manuscript("우아덤")) is True
    # 자사 카페 일상 글 (xlsx 에서 읽어 온 글)
    assert gx.allowed_for(_manuscript(source="xlsx:일상글")) is False
    # 제휴 일상 글 (출처가 비어 있는 글)
    assert gx.allowed_for(_manuscript(source="없음")) is False
    # 등록되지 않은 브랜드 이름으로 위장해도 막는다
    assert gx.allowed_for(_manuscript(source="generated:일상글")) is False


def test_일상글에서는_codex를_아예_부르지_않는다(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - 불리면 안 된다
        raise AssertionError("일상 글에서 codex 를 불렀다")

    monkeypatch.setattr(gx, "run_codex", boom)
    monkeypatch.setattr(gx, "review", boom)
    stats: dict = {}
    out = gx.crosscheck_manuscript(
        None,
        _manuscript(source="xlsx:일상글"),
        stats=stats,
        cfg={"enabled": True, "scope": "brand_only", "min_score": 60},
    )
    assert out["gpt_verdict"] == "미검증"
    assert "brand_only" in stats["gpt_error"]


def test_일상글_경로는_교차검증_모듈을_아예_들이지_않는다():
    """일상 글·일상 글 댓글 코드가 `gpt_crosscheck` 를 import 하지 않는지 (경로 차단)."""
    from pathlib import Path

    root = Path(bw.__file__).resolve().parents[1]
    for name in (
        "content/daily_comments.py",
        "content/manuscript.py",
        "content/sanitize.py",
        "channels/self_cafe_daily.py",
        "channels/affiliate_daily.py",
    ):
        path = root / name
        if not path.exists():
            continue
        assert "gpt_crosscheck" not in path.read_text(encoding="utf-8"), name


def test_교차검증을_부르는_곳은_브랜드_생성_고리_하나뿐이다():
    from pathlib import Path

    root = Path(bw.__file__).resolve().parents[1]
    callers = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "gpt_crosscheck.crosscheck_manuscript(" in p.read_text(encoding="utf-8")
    )
    assert callers == ["engine/worker.py"]


# --- 4. 의도적 오탈자·띄어쓰기 (경고) ---------------------------------
def test_의도적_오탈자를_찾아낸다():
    assert bw.intentional_typo_hits("이거 진짜 안되요 할수있나요")
    assert bw.intentional_typo_hits("정확한 맞춤법으로 쓴 문장입니다") == []


def test_하나도_안_틀리면_경고가_뜬다():
    rule = bw.rule_for("우아덤")
    checks = bw.validate(_manuscript(body="아주 반듯하게 쓴 문장입니다"), rule)
    found = [c for c in checks if c["항목"] == "의도적 오탈자·띄어쓰기"]
    assert len(found) == 1
    c = found[0]
    assert c["통과"] is False
    assert c["필수"] is False  # 경고 수준 — 원고를 버리지 않는다
    assert c["구간"] == bw.INFO_SCOPE


def test_오탈자_경고는_재시도를_부르지_않는다():
    """경고라서 본문·댓글 재시도 고리에 들어가지 않는다 (토큰 절약)."""
    rule = bw.rule_for("우아덤")
    checks = bw.validate(_manuscript(body="아주 반듯하게 쓴 문장입니다"), rule)
    names = bw.violation_names(bw.violations(checks, scope="본문"))
    assert "의도적 오탈자·띄어쓰기" not in names
    assert "의도적 오탈자·띄어쓰기" not in bw.violation_names(bw.violations(checks))


def test_GPT_체크리스트에_오탈자_항목이_있고_있음이_통과다():
    rule = bw.rule_for("우아덤")
    items = gx.checklist(_manuscript(), rule)
    hit = [t for t in items if "의도적 오탈자" in t]
    assert len(hit) == 1, "같은 항목이 두 번 들어가면 점수가 흐려진다"
    assert "본문에 있는가" in hit[0] and "하나라도 있으면 통과" in hit[0]


def test_프롬프트에_일부러_틀리라는_줄이_있다():
    system, _ = bw.build_body_prompt("우아덤", "비타민C", "씨씨앙")
    assert "일부러 조금 틀리게" in system


# --- 5. 권위 근거 중복은 같은 낱말일 때만 -----------------------------
def test_정부기관과_농촌진흥청은_중복이_아니다():
    assert bw.shared_authority_words("정부기관에서 검증했대요", "농촌진흥청 실험이래요") == []
    assert bw.duplicate_authority_problems("정부기관에서 검증했대요", "농촌진흥청 실험이래요") == []


def test_같은_낱말이_둘_다_있으면_중복이다():
    problems = bw.duplicate_authority_problems(
        "항문외과에서 들었는데요", "항문외과 의사가 그러더라구요"
    )
    assert problems and "항문외과" in problems[0]


def test_긴_낱말_안의_짧은_낱말은_따로_세지_않는다():
    assert bw.authority_words_in("국가기관에서 검증받았어요") == {"국가기관"}


def test_대대대댓글2_고정멘트는_중복_검사에서_뺀다():
    """팥순이 후기형 대대대댓글2의 `국가기관 인증` 은 정리본이 못 박은 고정 멘트다."""
    rule = bw.rule_for("팥순이", "후기형")
    m = _manuscript("팥순이")
    m.manuscript_type = "후기형"
    table = {
        "댓글2": "농촌진흥청에서 검증받은 성분이래요",
        "대대댓글2": "저도 이거 먹는중인데 9kg 빠졌어요",
        "대대대댓글2": "국가기관 인증까지 받은 거라 믿고 먹어요",
    }
    for c in m.comments:
        c.text = table.get(c.label, "그렇군요")
    checks = bw.validate(m, rule)
    dup = [c for c in checks if c["항목"] == "권위 근거 중복"]
    assert dup and dup[0]["통과"] is True


# --- 6. 장으뜸·뉴더미스 근거 문장은 마이너스 카피 ---------------------
PLUS_COPY = (
    "장으뜸 장어즙이라고 있어요 배합이랑 원물 함량을 따져 만들어서 흡수가 "
    "잘된대요 철분만 먹을땐 그대로더라구요 검색해보시면 후기 많아요"
)
MINUS_COPY = (
    "장으뜸 장어즙이라고 있어요 배합이랑 원물 함량을 안 따지면 흡수가 안 돼서 "
    "소용없대요 철분만 먹을땐 그대로더라구요 검색해보시면 후기 많아요"
)


def test_플러스_카피는_필수_위반이다():
    rule = bw.rule_for("장으뜸")
    problems = bw.reply2_problems(PLUS_COPY, rule)
    assert any("마이너스 카피" in p for p in problems)


def test_마이너스_카피는_통과한다():
    rule = bw.rule_for("장으뜸")
    assert bw.reply2_problems(MINUS_COPY, rule) == []


def test_뉴더미스도_마이너스_카피를_요구한다():
    rule = bw.rule_for("뉴더미스")
    plus = (
        "자연방패 항문세정제라고 있어요 항문 보호막을 강화해주는 성분이 들어가서 "
        "접근이 달라요 그냥 씻기만 하는건 의미없더라구요 검색해보시면 후기 많아요"
    )
    assert any("마이너스 카피" in p for p in bw.reply2_problems(plus, rule))


def test_다른_브랜드에는_적용하지_않는다():
    rule = bw.rule_for("우아덤")
    assert bw.minus_copy_problems("그린커피 아하바하가 멜라닌을 억제해요", rule) == []


def test_골든_문장이_모두_마이너스_카피다():
    for brand in bw.MINUS_COPY_BRANDS:
        rule = bw.rule_for(brand, "질문형")
        golden = bw.load_golden_reply2(brand, "질문형")
        assert golden, f"{brand} 골든 문장이 비어 있다"
        for g in golden:
            assert bw.reply2_problems(g, rule) == [], f"{brand}: {g}"


def test_마이너스_카피_규칙이_프롬프트에_박혀_있다():
    for brand in bw.MINUS_COPY_BRANDS:
        rule = bw.rule_for(brand, "질문형")
        block = "\n".join(bw.reply2_prompt_block(rule))
        assert "마이너스 카피" in block
    # 다른 브랜드에는 붙지 않는다
    other = "\n".join(bw.reply2_prompt_block(bw.rule_for("우아덤")))
    assert "마이너스 카피" not in other


def test_마이너스_카피_판정은_한_문장_안에서_본다():
    assert bw.is_minus_copy_sentence("성분이 없으면 소용없대요") is True
    assert bw.is_minus_copy_sentence("성분이 들어 있어서 좋대요") is False
    assert bw.is_minus_copy_sentence("성분이 없으면 좋대요") is False


# --- 1. 재시도 원인 기록 ----------------------------------------------
def test_재시도_원인을_stats에_남긴다():
    stats: dict = {}
    bw._note_retry(stats, "댓글", ["댓글3 역할 — 기준 A 인데 실제 B", "권위 근거 중복 — 기준 C 인데 실제 D"])
    bw._note_retry(stats, "댓글", ["댓글3 역할 — 기준 A 인데 실제 B"])
    assert stats["retry_reasons"]["댓글"] == [
        ["댓글3 역할", "권위 근거 중복"],
        ["댓글3 역할"],
    ]


def test_항목_이름만_뽑는다():
    assert bw.violation_names(["권위 근거 중복 — 기준 X 인데 실제 Y"]) == ["권위 근거 중복"]


# --- 7. 사용량 장부에 요금제 한도 종류는 적지 않는다 ------------------
def test_장부_칸에_한도_종류가_없다(tmp_path):
    import json

    from v2r.llm import usage_ledger

    path = usage_ledger.append_call(
        tmp_path, "plan", "brand_body", "claude-sonnet-5", {"input_tokens": 1}
    )
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "한도" not in "".join(row.keys())
    for banned in ("plan_limit", "limit", "quota", "limit_kind", "한도종류"):
        assert banned not in row


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
