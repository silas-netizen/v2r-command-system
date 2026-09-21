"""대대댓글2 실험실 (`v2r.content.reply2_lab`) 검사."""

from __future__ import annotations

import json

import pytest

from v2r.content import brand_writer as bw
from v2r.content import reply2_lab as lab

GOOD = (
    "그린커피 아하바하라고 있어요 아하바하가 두꺼워진 각질을 정돈해줘서 "
    "그린커피가 멜라닌까지 닿는 원리에요 단순 미백크림은 겉돌아서 소용없더라구요 "
    "검색해보시면 후기 많아요"
)
#: 사용자가 나쁜 예로 짚은 문장 (조각난 구 + 명사 나열)
BAD = "팥순ㅇㅣ요 야식 못끊어서 시작했는데 체중 그대로예요 참기만 하면 제자리 농촌진흥청 25% 검증 검색해봐요"


# --- 문장 뜯어보기 ------------------------------------------------
def test_sentences_are_split_on_endings():
    assert len(bw.reply2_sentences(GOOD)) == 4


def test_bad_example_is_rejected_for_being_fragments():
    rule = bw.rule_for("팥순이", "질문형")
    problems = bw.reply2_problems(BAD, rule)
    assert any("낱말 나열" in p or "조각 문장" in p for p in problems)


def test_good_example_passes_every_check():
    assert bw.reply2_problems(GOOD, bw.rule_for("우아덤")) == []


def test_user_good_example_passes():
    text = (
        "팥순ㅇㅣ라고 있어요 국가기관인 농촌진흥청에서 체지방 25% 감소 검증받은 성분이에요 "
        "그냥 참는거로는 한계있어서 검색해보시면 후기 많아요"
    )
    assert bw.reply2_problems(text, bw.rule_for("팥순이", "질문형")) == []


def test_other_brands_may_not_borrow_patsooni_evidence():
    text = (
        "장으뜸 장어즙이라고 있어요 국가기관인 농촌진흥청에서 체지방 25% 감소 "
        "검증받은 성분이에요 그냥 참는걸로는 한계있더라구요 검색해보시면 후기 많아요"
    )
    assert any("근거 도용" in p for p in bw.reply2_problems(text, bw.rule_for("장으뜸")))


# --- 후보 검증 ----------------------------------------------------
def test_too_short_candidate_is_rejected():
    rule = bw.rule_for("우아덤")
    problems = lab.candidate_problems("그린커피 아하바하요 검색해보세요 원리가 달라요", rule)
    assert any(p.startswith("길이") for p in problems)


def test_length_is_allowed_up_to_200_percent():
    rule = bw.rule_for("우아덤")
    padded = GOOD.replace("검색해보시면 후기 많아요", "각질 정돈이 먼저더라구요 검색해보시면 후기 많아요")
    assert len(padded) <= int(bw.REPLY2_MAX_LEN * bw.SOFT_LIMIT_FACTOR)
    assert not any(p.startswith("길이") for p in lab.candidate_problems(padded, rule))


def test_copying_an_example_is_rejected():
    rule = bw.rule_for("우아덤")
    assert any("베끼기" in p for p in lab.candidate_problems(GOOD, rule, [GOOD]))


def test_emoji_is_rejected():
    rule = bw.rule_for("우아덤")
    assert any("이모지" in p for p in lab.candidate_problems(GOOD + " 😀", rule))


# --- 예시 모으기 --------------------------------------------------
def _row(text: str, mtype: str = "질문형") -> dict:
    return {"A": "키워드", "B": text, "C": "", "D": "", "E": mtype}


def test_collect_reply2_reads_every_finished_row():
    article = "\n".join(
        ["제목 :", "제목입니다", "", "본문 :", "본문입니다", ""]
        + [f"{label} :\n{label} 내용이에요\n" for label in bw.COMMENT_LABELS]
    )
    mine = article.replace("대대댓글2 내용이에요", GOOD)
    got = lab.collect_reply2("우아덤", "질문형", [_row(mine), _row(mine)])
    assert got == [GOOD]  # 같은 문장은 한 번만


def test_ranking_puts_the_cleanest_examples_first():
    rule = bw.rule_for("우아덤")
    ranked = lab.rank_examples([BAD, GOOD], rule)
    assert ranked[0] == GOOD


# --- 프롬프트 -----------------------------------------------------
def test_prompt_asks_for_structure_before_length():
    rule = bw.rule_for("우아덤")
    system, user = lab.build_prompt(rule, "본문이에요", "비타민C", [GOOD], 5)
    assert "같은 구조·같은 길이감" in system
    assert "문장이 온전한 것이 가장 먼저다" in system
    assert GOOD in system
    assert "후보 5개를 JSON 배열로" in user


def test_review_prompt_uses_its_own_role():
    rule = bw.rule_for("팥순이", "후기형")
    system, _ = lab.build_prompt(rule, "본문", "베르베린", ["예시에요"], 3)
    assert "여분 댓글풀 계정" in system
    assert "이거" in system


# --- 실행 ---------------------------------------------------------
class FakeLLM:
    """후보를 미리 정해 둔 가짜 모델."""

    def __init__(self, rounds: list[list[str]]) -> None:
        self.rounds = list(rounds)
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, purpose, system, user, max_tokens=1200):
        self.calls.append((system, user))
        return self.rounds.pop(0) if self.rounds else []


def test_run_bundle_retries_until_enough_candidates(tmp_path):
    variants = [
        GOOD.replace("단순 미백크림", f"{word} 크림") for word in ("일반", "흔한", "보통")
    ]
    llm = FakeLLM([[BAD, variants[0]], [variants[1], variants[2]]])
    result = lab.run_bundle(
        llm, "우아덤", "질문형", n=3, source={"body": "본문이에요", "keyword": "비타민C"}
    )
    assert len(result.passed) == 3
    assert result.rounds == 2
    assert result.rejected and "낱말 나열" in " ".join(result.rejected[0][1])
    # 두 번째 요청에는 직전 탈락 사유가 붙는다
    assert "직전 후보들이 걸린 사유" in llm.calls[1][1]


def test_run_bundle_reports_missing_body():
    result = lab.run_bundle(FakeLLM([]), "우아덤", "질문형", n=3, source={})
    assert "원고 JSON" in result.error


def test_report_lists_examples_passed_and_rejected(tmp_path):
    result = lab.BundleResult(
        brand="우아덤",
        manuscript_type="질문형",
        keyword="비타민C",
        examples=[GOOD, GOOD, GOOD, GOOD],
        passed=[GOOD],
        rejected=[(BAD, ["낱말 나열 — 그렇다"])],
        rounds=2,
    )
    path = lab.save_report([result], day="2026-09-21", out_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert path.name == "reply2-candidates-2026-09-21.md"
    assert "## 우아덤(질문형)" in text
    assert "1. " + GOOD in text  # 번호로 고를 수 있다
    assert "사유: 낱말 나열" in text
    assert text.count("- " + GOOD) == 3  # 기존 예시는 3개만 보여 준다


def test_bundles_for_picks_the_right_pairs():
    assert lab.bundles_for("전체", "") == list(lab.DEFAULT_BUNDLES)
    assert lab.bundles_for("팥순이", "") == [("팥순이", "질문형"), ("팥순이", "후기형")]
    assert lab.bundles_for("팥순이", "후기형") == [("팥순이", "후기형")]
    assert lab.bundles_for("전체", "후기형") == [("팥순이", "후기형")]


def test_latest_manuscript_reads_generated_plan(tmp_path, monkeypatch):
    folder = tmp_path / "warehouse/manuscripts/generated-plan/우아덤"
    folder.mkdir(parents=True)
    (folder / "질문형-비타민C.json").write_text(
        json.dumps({"body": "본문이에요", "keyword": "비타민C"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert lab.latest_manuscript("우아덤", "질문형")["keyword"] == "비타민C"
    assert lab.latest_manuscript("우아덤", "후기형") == {}


@pytest.mark.parametrize("payload", [["가"], {"candidates": ["가"]}, [{"text": "가"}]])
def test_model_answer_shapes_are_all_understood(payload):
    assert lab._as_list(payload) == ["가"]


# --- 골든 문장 (사용자가 고른 본보기, 2026-09-21) --------------------
GOLDEN_YAML = {"우아덤": {"질문형": [GOOD]}}


def test_golden_is_loaded_per_brand_and_type():
    assert bw.load_golden_reply2("우아덤", "질문형", GOLDEN_YAML) == [GOOD]
    assert bw.load_golden_reply2("우아덤", "후기형", GOLDEN_YAML) == []
    assert bw.load_golden_reply2("없는브랜드", "질문형", GOLDEN_YAML) == []


def test_real_config_has_the_picked_sentences():
    assert len(bw.load_golden_reply2("우아덤", "질문형")) == 3
    assert len(bw.load_golden_reply2("코숨핏", "질문형")) == 3
    assert len(bw.load_golden_reply2("팥순이", "질문형")) == 4
    assert len(bw.load_golden_reply2("팥순이", "후기형")) == 4


def test_golden_goes_in_front_as_a_must_follow_flow():
    rule = bw.rule_for("우아덤")
    block = bw.golden_block(rule, [GOOD])
    assert "반드시 따른다" in "\n".join(block)
    assert "1. " + GOOD in block


def test_comments_prompt_puts_golden_before_sheet_examples():
    rule = bw.rule_for("우아덤")
    system, _ = lab.build_prompt(rule, "본문", "비타민C", [GOOD, "시트 예시에요"], 5, golden=[GOOD])
    assert system.index("골든 문장") < system.index("기존 완성 원고의 대대댓글2")
    assert system.count(GOOD) == 1  # 골든은 아래 예시 목록에서 빠진다


# --- 브랜드 고유 논리 (장으뜸·뉴더미스) -----------------------------
def test_jangeuddeum_needs_absorption_and_blend_logic():
    rule = bw.rule_for("장으뜸")
    bad = ("장으뜸 장어즙이라고 있어요 산부인과 원장님이 권하셔서 먹었어요 "
           "그냥 참고 버티는걸로는 안되더라구요 검색해보시면 후기 많아요")
    assert any("브랜드 논리" in p for p in bw.reply2_problems(bad, rule))
    good = ("장으뜸 장어즙이라고 있어요 배합이랑 원물 함량을 따져 만들어서 몸에 흡수가 "
            "잘된대요ㅎㅎ 아무거나 먹으면 소용없더라구요 검색해보시면 후기 많아요")
    assert bw.reply2_problems(good, rule) == []


def test_newdermis_needs_barrier_logic():
    rule = bw.rule_for("뉴더미스")
    bad = ("자연방패 항문세정제라고 있어요 항문외과 의사가 추천하는 세정제에요 "
           "좌욕만으로는 한계가 있더라구요 검색해보시면 후기 많아요")
    assert any("브랜드 논리" in p for p in bw.reply2_problems(bad, rule))
    good = ("자연방패 항문세정제라고 있어요 항문 보호막을 강화해주는 성분이 들어가서 "
            "접근이 달라요ㅋㅋ 그냥 씻기만 하는건 소용없더라구요 검색해보시면 후기 많아요")
    assert bw.reply2_problems(good, rule) == []


def test_logic_note_is_written_into_the_prompt():
    rule = bw.rule_for("장으뜸")
    system, _ = lab.build_prompt(rule, "본문", "배란일", [GOOD], 5)
    assert "배합과 원물 함량" in system


# --- 팥순이 후기형 감량 수치 ----------------------------------------
REVIEW_OK = "저도 이거 먹는중인데 6주만에 9kg 빠졌어요 정부기관 실험에서 체지방 25% 줄었다는 결과 나온 성분이래요"


def test_review_reply2_needs_at_least_8kg():
    rule = bw.rule_for("팥순이", "후기형")
    assert bw.REVIEW_MIN_KG == 8
    assert bw.reply2_problems(REVIEW_OK, rule) == []
    small = REVIEW_OK.replace("9kg", "5kg")
    assert any("감량 수치" in p for p in bw.reply2_problems(small, rule))
    none = REVIEW_OK.replace("6주만에 9kg 빠졌어요", "6주째인데 많이 빠졌어요")
    assert any("감량 수치" in p for p in bw.reply2_problems(none, rule))


def test_review_type_skips_the_copy_check():
    """후기형은 기존 예시와 같아도 된다 (사용자 결정 2026-09-21)."""
    rule = bw.rule_for("팥순이", "후기형")
    assert lab.candidate_problems(REVIEW_OK, rule, [REVIEW_OK]) == []
    viral = bw.rule_for("우아덤")
    assert any("베끼기" in p for p in lab.candidate_problems(GOOD, viral, [GOOD]))


# --- 여러 브랜드를 한 번에 -------------------------------------------
def test_bundles_for_takes_several_brands():
    assert lab.bundles_for("장으뜸,뉴더미스", "") == [
        ("장으뜸", "질문형"),
        ("뉴더미스", "질문형"),
    ]


def test_report_can_carry_the_golden_table():
    result = lab.BundleResult(brand="장으뜸", manuscript_type="질문형", passed=[GOOD])
    text = lab.report_markdown([result], day="2026-09-21", title="2차 후보", golden=True)
    assert text.startswith("# 2차 후보 (2026-09-21)")
    assert "## 채택된 골든 문장" in text
    assert "### 우아덤(질문형)" in text
