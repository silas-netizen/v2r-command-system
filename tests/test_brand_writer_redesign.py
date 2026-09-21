"""브랜드 원고 생성기 재설계(설계서 2026-09-21 A~H) 테스트. 실제 모델은 부르지 않는다."""

from __future__ import annotations

import json

from v2r.content import brand_writer as bw
from v2r.content.manuscript import Manuscript

KEYWORD = "비타민C"

BODY = "\n".join(
    [
        "요즘 얼굴에 잡티가 올라와서",
        "비타민C 찾아보고 있어요ㅠㅠ",
        "거울 볼 때마다 속상해요",
        "",
        "예전에 비타민C 크림도 발라봤는데",
        "별로 달라지는게 없더라구요",
        "",
        "{키워드}",
        "",
        "비타민C 말고 또 뭘 봐야 할까요?",
        "혹시 아시는 분 알려주세요ㅠㅠ",
    ]
)

COMMENTS = {
    "댓글1": "비타민C는 꾸준히 발라야 티가 나요",
    "대댓글1": "아 그렇군요 처음 알았어요ㅠㅠ",
    "댓글2": "비타민C 발라도 각질이 두꺼우면 흡수가 안된대요 각질 정돈되는걸로 바꿔보세요",
    "대댓글2": "헉 그런건 어떤 성분 봐야되요?",
    "대대댓글2": "그린커피 아하바하라고 있어요 아하바하가 두꺼워진 각질을 정돈해줘서 그린커피가 멜라닌까지 닿는 원리에요 단순 미백크림은 겉돌아서 소용없더라구요 검색해보시면 후기 많아요",
    "대대대댓글2": "222 저도 그거 쓰고 확실히 나아졌어요",
    "댓글3": "저도 작년에 똑같이 고민했어요ㅠㅠ",
    "대댓글3": "저만 그런게 아니였네요ㅎㅎ",
    "댓글4": "스크럽 자주 하면 더 올라와요",
    "대댓글4": "아 그거 계속 했는데 큰일이네요",
    "댓글5": "저는 두 달쯤 쓰니까 톤이 확 밝아졌어요",
    "대댓글5": "와 두 달이면 해볼만하네요!",
}


def _manuscript(**kw) -> Manuscript:
    base = dict(
        title="비타민C 잡티에 진짜 효과 있나요?ㅠㅠ",
        body=BODY,
        cafe="씨씨앙",
        keyword=KEYWORD,
        manuscript_type="질문형",
        source="generated:우아덤",
        comments=bw._comment_nodes(dict(COMMENTS)),
    )
    base.update(kw)
    return Manuscript(**base)


def _row(check: str, checks: list[dict]) -> dict:
    return next(c for c in checks if c["항목"] == check)


# --- A. 지침 전문 ------------------------------------------------
def test_guide_text_is_not_truncated_anymore():
    """지침은 앞부분만이 아니라 **전문**이 캐시되는 system에 들어간다."""
    guide = "지침시작\n" + ("규칙 줄입니다 " * 2000) + "\n지침끝"
    system, user = bw.build_body_prompt("우아덤", KEYWORD, guide_text=guide)
    assert "지침시작" in system and "지침끝" in system
    assert len(system) > len(guide)  # 잘라내지 않았다
    assert guide not in user  # user 쪽은 여전히 키워드 몫만

    cmt_sys, _ = bw.build_comments_prompt(
        "우아덤", KEYWORD, "제목", BODY, guide_text=guide
    )
    assert "지침끝" in cmt_sys


def test_system_block_is_byte_identical_for_different_keywords():
    """캐시가 유지되려면 키워드가 바뀌어도 system이 바이트 단위로 같아야 한다."""
    examples = [bw.sheet_text(_manuscript())]
    a, _ = bw.build_body_prompt("우아덤", "비타민C", "씨씨앙", "지침", examples=examples)
    b, _ = bw.build_body_prompt("우아덤", "편평사마귀", "다른카페", "지침", examples=examples)
    assert a == b  # 예시까지 넣어도 키워드가 바뀌면 안 되는 부분은 그대로다
    assert "편평사마귀" not in a


# --- B. few-shot 예시 --------------------------------------------
def _example_cell(**kw) -> str:
    long_body = BODY + "\n\n" + "\n".join(["같은 고민 하시는 분들 많더라구요"] * 6)
    return bw.sheet_text(_manuscript(body=long_body, **kw))


def _rows() -> list[dict]:
    full = _example_cell()
    return [
        {"키워드": "가", "본문": full, "카페명": "씨씨앙", "작성계정": "a", "원고유형": "질문형"},
        {"키워드": "나", "본문": "제목 :\n짧음\n\n본문 :\n짧아요", "카페명": "", "작성계정": "b", "원고유형": "질문형"},
        {"키워드": "다", "본문": full, "카페명": "", "작성계정": "c", "원고유형": "후기형"},
        {"키워드": "라", "본문": full, "카페명": "", "작성계정": "d", "원고유형": "질문형"},
        {"키워드": "마", "본문": full, "카페명": "", "작성계정": "e", "원고유형": "질문형"},
    ]


def test_load_examples_picks_complete_rows_of_same_type_deterministically():
    got = bw.load_examples("우아덤", "질문형", _rows())
    assert len(got) == bw.EXAMPLE_COUNT == 2
    assert got == bw.load_examples("우아덤", "질문형", _rows())  # 늘 같은 선택
    assert "짧아요" not in "".join(got)  # 미완성 행은 버린다


def test_load_examples_separates_manuscript_types():
    assert len(bw.load_examples("팥순이", "후기형", _rows())) == 1


def test_load_examples_without_sheet_returns_nothing(tmp_path):
    assert bw.load_examples("우아덤", "질문형", tmp_path / "없는파일.xlsx") == []
    assert bw.load_examples("우아덤", "질문형", None) == []


def test_examples_go_into_cached_system_block():
    examples = bw.load_examples("우아덤", "질문형", _rows())
    system, _ = bw.build_body_prompt("우아덤", KEYWORD, examples=examples)
    assert "기존 완성 원고 예시" in system
    assert "베끼지 않는다" in system
    assert examples[0].strip()[:20] in system


# --- D. 후기형 전용 댓글 프롬프트 --------------------------------
def test_review_and_question_comment_prompts_are_separate():
    review, _ = bw.build_comments_prompt(
        "팥순이", "곤약젤리", "제목", BODY, manuscript_type="후기형"
    )
    question, _ = bw.build_comments_prompt(
        "팥순이", "곤약젤리", "제목", BODY, manuscript_type="질문형"
    )
    assert "이 글은 **후기형**이다" in review
    assert "후기형 댓글 스레드 고정 흐름" in review
    assert "대대댓글2 — 질문형에서 가장 중요한 자리" not in review  # 질문형 틀은 섞이지 않는다
    assert "대대댓글2 — 질문형에서 가장 중요한 자리" in question
    assert "후기형 댓글 스레드 고정 흐름" not in question


# --- C. 대대댓글2 금칙어 -----------------------------------------
def test_reply2_banned_phrase_is_a_hard_failure():
    bad = _manuscript(
        comments=bw._comment_nodes(
            dict(COMMENTS, 대대댓글2=COMMENTS["대대댓글2"] + " 제품보다 방법이 중요해요")
        )
    )
    problems = bw.failures(bw.validate(bad))
    assert any("대대댓글2 금칙어" in p for p in problems)
    assert bw.failures(bw.validate(_manuscript())) == []


def test_question_prompt_states_reply2_frame_and_bans():
    system, _ = bw.build_comments_prompt("우아덤", KEYWORD, "제목", BODY)
    assert "검색해보시면 후기 많아요" in system
    assert "완전한 문장 3~4개로 쓴다" in system
    assert "근거를 낱말로 나열하지 않는다" in system
    for word in bw.REPLY2_BANNED_PHRASES:
        assert word in system


# --- D. 제품명 실제 최초 언급 ------------------------------------
def test_first_mention_label_is_detected_from_actual_text():
    rule = bw.rule_for("우아덤")
    m = _manuscript()
    assert bw.first_product_mention_label(m, rule) == "대대댓글2"

    early = _manuscript(
        comments=bw._comment_nodes(dict(COMMENTS, 댓글2="그린커피 아하바하 써보세요"))
    )
    assert bw.first_product_mention_label(early, rule) == "댓글2"
    assert any(
        "제품명 최초 언급 위치" in p for p in bw.failures(bw.validate(early))
    )


def test_first_mention_check_fails_when_product_never_appears():
    gone = _manuscript(comments=bw._comment_nodes(dict(COMMENTS, 대대댓글2="그거 찾아보세요")))
    row = _row("제품명 최초 언급 위치", bw.validate(gone))
    assert row["통과"] is False and row["필수"] is True


# --- D. 팥순이 필수 멘트 -----------------------------------------
def _patsooni(mtype: str, **table) -> Manuscript:
    base = {
        "댓글1": "곤약젤리는 포만감이 좋아요",
        "대댓글1": "오 감사해요ㅎㅎ",
        "댓글2": "곤약젤리만 먹다 요요 왔는데 팥순추출물 같이 하고 5kg 뺐어요",
        "대댓글2": "팥순ㅇㅣ 먹고 있어요 3주째에요",
        "대대댓글2": "팥순ㅇㅣ라고 있어요 국가기관인 농촌진흥청에서 체지방 25% 감소 검증받은 성분이에요 그냥 참는거로는 한계있더라구요 검색해보시면 후기 많아요",
        "대대대댓글2": "맞아욬ㅋㅋ 국가기관 인증이라 믿음가요",
        "댓글3": "곤약젤리 맛은 어때요?",
        "대댓글3": "생각보다 먹을만해요ㅎㅎ",
        "댓글4": "저도 보고 바로 구매했어요",
        "대댓글4": "오 잘하셨어요ㅎㅎ",
        "댓글5": "요요 없이 8주 동안 6kg 빠졌어요",
        "대댓글5": "와 찾아봐야겠네요!",
    }
    base.update(table)
    return _manuscript(
        manuscript_type=mtype,
        source="generated:팥순이",
        keyword="곤약젤리",
        comments=bw._comment_nodes(base),
    )


def test_patsooni_required_phrases_are_enforced_by_label():
    ok = _patsooni("질문형")
    assert _row("필수 멘트", bw.validate(ok))["통과"] is True

    bad = _patsooni("질문형", 대대댓글2="팥순ㅇㅣ라고 있어요 다이어트 카페에서 많이들 추천하는 성분이에요 그냥 참는거로는 한계있더라구요 검색해보시면 후기 많아요")
    row = _row("필수 멘트", bw.validate(bad))
    assert row["통과"] is False and row["필수"] is True
    assert "대대댓글2" in row["실제"]


def test_patsooni_review_type_requires_purchase_mention_in_comment4():
    rule = bw.rule_for("팥순이", "후기형")
    assert ("댓글4", ("구매했", "주문했", "강추")) in rule.required_phrases
    bad = _patsooni("후기형", 댓글4="저도 한번 해볼까 고민되네요")
    row = _row("필수 멘트", bw.validate(bad))
    assert row["통과"] is False and "댓글4" in row["실제"]


def test_required_phrases_are_written_into_the_prompt():
    system, _ = bw.build_comments_prompt(
        "팥순이", "곤약젤리", "제목", BODY, manuscript_type="질문형"
    )
    assert "반드시 들어가야 하는 멘트" in system
    assert "농촌진흥청" in system and "25%" in system


# --- F. 본문 형식 ------------------------------------------------
def test_line_break_check_fails_when_too_many_long_lines():
    long_body = "\n".join(["가" * 40, "나" * 40, "", "{키워드}", "", "다" * 40])
    row = _row("본문 줄 나눔", bw.validate(_manuscript(body=long_body)))
    assert row["통과"] is False and row["필수"] is True
    # 한 줄만 길면 통과 (20% 이하)
    assert _row("본문 줄 나눔", bw.validate(_manuscript()))["통과"] is True


def test_placeholder_paragraph_index_is_checked():
    assert bw.placeholder_paragraph_index(BODY) == 2
    moved = BODY.replace("{키워드}\n\n", "").replace(
        "요즘 얼굴에 잡티가 올라와서", "{키워드}\n\n요즘 얼굴에 잡티가 올라와서"
    )
    row = _row("{키워드} 문단 위치", bw.validate(_manuscript(body=moved)))
    assert row["통과"] is False and "0번째" in row["실제"]


def test_opening_repetition_is_only_a_warning(tmp_path):
    (tmp_path / "우아덤").mkdir()
    (tmp_path / "우아덤" / "a.json").write_text(
        json.dumps({"body": BODY}, ensure_ascii=False), encoding="utf-8"
    )
    openings = bw.load_recent_openings(tmp_path)
    assert openings == ["요즘"]

    checks = bw.validate(_manuscript(), recent_openings=openings)
    row = _row("오프닝 반복", checks)
    assert row["통과"] is False and row["필수"] is False
    assert bw.failures(checks) == []  # 경고라 실패가 아니다
    assert bw.load_recent_openings(tmp_path / "없는폴더") == []


def test_repeated_opening_triggers_one_regeneration():
    """오프닝이 겹치면 딱 한 번 다시 쓴다 (설계서 F-c)."""
    fresh = BODY.replace("요즘 얼굴에 잡티가 올라와서", "어제부터 얼굴에 뭐가 나서")

    class Flaky:
        def __init__(self) -> None:
            self.bodies = 0
            self.usage = {"input_tokens": 0, "output_tokens": 0}

        def complete_json(self, purpose, system, user, max_tokens=1200):
            if purpose == "brand_body":
                self.bodies += 1
                return {"title": "제목", "body": BODY if self.bodies == 1 else fresh}
            return dict(COMMENTS)

        def model_for(self, purpose):
            return "fake"

    llm = Flaky()
    m = bw.generate_manuscript(llm, "우아덤", KEYWORD, recent_openings=["요즘"])
    assert llm.bodies == 2  # 한 번만 더 쓴다
    assert bw.opening_word(m.body) == "어제부터"


# --- E/G/H. 길이·수치·근거 --------------------------------------
def test_reply2_gets_its_own_length_limit():
    """대대댓글2는 110자가 상한이고 그 200%(220자)까지 통과다."""
    rule = bw.rule_for("우아덤")
    m = _manuscript(comments=bw._comment_nodes(dict(COMMENTS, 대대댓글2="가" * 150)))
    assert _row("댓글 글자 수", bw.validate(m))["통과"] is True
    over = _manuscript(comments=bw._comment_nodes(dict(COMMENTS, 대대댓글2="가" * 221)))
    assert _row("댓글 글자 수", bw.validate(over))["통과"] is False
    assert rule.reply2_max == 110 == bw.REPLY2_MAX_LEN


def test_comment5_needs_a_number():
    bad = _manuscript(comments=bw._comment_nodes(dict(COMMENTS, 댓글5="확실히 좋아졌어요")))
    row = _row("댓글5 수치", bw.validate(bad))
    assert row["통과"] is False and row["필수"] is True
    assert _row("댓글5 수치", bw.validate(_manuscript()))["통과"] is True


def test_comment4_gets_methods_tried_from_the_body():
    assert bw.methods_already_tried(BODY) == ["예전에 비타민C 크림도 발라봤는데"]
    _, user = bw.build_comments_prompt("우아덤", KEYWORD, "제목", BODY)
    assert "이미 해 봤다고 한 방법" in user
    assert "예전에 비타민C 크림도 발라봤는데" in user


def test_cliche_list_is_in_both_prompts():
    body_sys, _ = bw.build_body_prompt("우아덤", KEYWORD)
    cmt_sys, _ = bw.build_comments_prompt("우아덤", KEYWORD, "제목", BODY)
    for text in (body_sys, cmt_sys):
        assert "상투구 회피" in text
    assert "형식 절대 규칙" in body_sys


def test_failing_labels_route_new_checks_into_partial_retry():
    """새 검증 항목도 걸린 댓글만 다시 쓰게 라벨로 이어진다."""
    bad = _manuscript(
        comments=bw._comment_nodes(
            dict(COMMENTS, 댓글5="확실히 좋아졌어요", 대대댓글2=COMMENTS["대대댓글2"] + " 제품보다 방법이 중요해요")
        )
    )
    labels = bw.failing_comment_labels(bw.validate(bad))
    assert "댓글5" in labels and "대대댓글2" in labels
    assert "댓글1" not in labels


# --- 2026-09-21 수정: 내부 용어 오탐 / 자리표시자 결정적 보정 / 미완성 표시 ----
def test_internal_term_check_ignores_words_that_only_contain_the_term():
    """`근무 불규칙한` 같은 평범한 말이 내부 용어로 잡히면 안 된다."""
    body = BODY.replace("거울 볼 때마다 속상해요", "근무가 불규칙한 편이라 더 그래요")
    assert bw.leaked_internal_terms(body) == []
    assert _row("내부 용어 미노출", bw.validate(_manuscript(body=body)))["통과"] is True
    # 댓글도 마찬가지
    ok = _manuscript(comments=bw._comment_nodes(dict(COMMENTS, 댓글1="근무 불규칙한 사람은 더 힘들어요")))
    assert _row("댓글 내부 용어 미노출", bw.validate(ok))["통과"] is True


def test_rule_word_is_no_longer_an_internal_term():
    assert "규칙" not in bw.INTERNAL_TERMS


def test_internal_term_still_caught_as_a_real_word():
    body = BODY.replace("거울 볼 때마다 속상해요", "프레임이 아예 다르더라구요")
    assert bw.leaked_internal_terms(body) == ["프레임"]
    assert _row("내부 용어 미노출", bw.validate(_manuscript(body=body)))["통과"] is False


def test_placeholder_is_inserted_by_code_at_the_fixed_paragraph():
    """`{키워드}`는 모델이 아니라 코드가 정해진 문단 뒤에 넣는다."""
    rule = bw.rule_for("우아덤", "질문형")
    naked = bw.strip_placeholders(BODY)
    fixed = bw.apply_placeholders(naked, rule)
    assert bw.placeholder_paragraph_index(fixed) == rule.placeholder_after_paragraph == 2
    # 엉뚱한 자리에 있던 것도 제자리로 옮겨 준다
    moved = "{키워드}\n\n" + naked
    assert bw.placeholder_paragraph_index(bw.apply_placeholders(moved, rule)) == 2
    # 이미 제자리면 개수가 늘지 않는다
    assert bw.apply_placeholders(fixed, rule).count("{키워드}") == 1


def test_extra_placeholder_goes_after_its_structure_paragraph():
    """팥순이 후기형의 `{B/A}` 도 코드가 넣는다 (병행 효과 문단 뒤 = 4번째)."""
    rule = bw.rule_for("팥순이", "후기형")
    assert rule.extra_placeholder == "{B/A}"
    assert rule.extra_placeholder_after_paragraph == 4
    body = "\n\n".join(f"문단{i}" for i in range(1, 6))
    out = bw.apply_placeholders(body, rule)
    blocks = bw.paragraphs(out)
    assert blocks[1] == "{키워드}"  # 첫 문단 뒤
    assert blocks.index("{B/A}") == 5  # 자리표시자 1개가 앞에 끼어 있으므로 5번째 자리
    assert blocks[4] == "문단4" and blocks[6] == "문단5"
    assert out.count("{B/A}") == 1


def test_bare_keyword_paragraph_is_removed_before_placing():
    """모델이 `{키워드}` 대신 키워드 값을 한 줄로 써 두면 그 줄은 지운다."""
    rule = bw.rule_for("우아덤", "질문형")
    body = bw.strip_placeholders(BODY).strip() + f"\n\n{KEYWORD}\n"
    out = bw.apply_placeholders(body, rule, KEYWORD)
    assert KEYWORD not in bw.paragraphs(out)  # 덩그러니 남은 줄은 없다
    assert bw.placeholder_paragraph_index(out) == 2
    # 키워드를 안 주면 예전처럼 건드리지 않는다
    assert KEYWORD in bw.paragraphs(bw.apply_placeholders(body, rule))


def test_keyword_shortfall_message_says_how_many_to_add():
    """키워드 횟수 부족은 `몇 개 더 넣어야 하는지`까지 말해 준다."""
    body = BODY.replace("예전에 비타민C 크림도 발라봤는데", "예전에 크림도 발라봤는데")
    row = _row("키워드 포함 횟수", bw.validate(_manuscript(body=body)))
    assert row["통과"] is False
    assert "1개를 더 넣어야 한다" in row["실제"] and KEYWORD in row["기준"]


def test_body_fix_prompt_revises_instead_of_rewriting():
    user = bw.build_body_fix_user(KEYWORD, BODY, ["키워드 포함 횟수 — 기준 4회 인데 실제 2회"])
    assert "직전에 쓴 본문" in user and "이것만 고쳐라" in user
    assert "키워드 포함 횟수" in user
    assert "그건 프로그램이 알아서 넣는다" in user


def test_review_md_marks_unfinished_manuscripts(tmp_path):
    """검증을 통과 못 한 원고는 검토 파일에 `미완성`이라고 적는다."""
    bad = _manuscript(body=BODY.replace("거울 볼 때마다 속상해요", "프레임이 아예 다르더라구요"))
    path = bw.write_review_md([bad], tmp_path / "r.md")
    text = path.read_text(encoding="utf-8")
    assert "미완성 1건" in text and "미완성 — 검증 실패" in text
    good = bw.write_review_md([_manuscript()], tmp_path / "ok.md").read_text(encoding="utf-8")
    assert "미완성" not in good and "완료 — 검증 전부 통과" in good
