"""브랜드 원고 생성기 테스트. 실제 모델은 부르지 않는다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from v2r.command.parser import parse_korean_command
from v2r.content import brand_writer as bw
from v2r.content.manuscript import CommentNode, Manuscript
from v2r.engine import worker
from v2r.sources.keyword_list import PASSWORD_HEADERS, load_pushed_keywords

REPO = Path(__file__).resolve().parent.parent
XLSX_우아덤 = REPO / "data" / "brand_sheet_우아덤.xlsx"

KEYWORD = "비타민C"

BODY = "\n".join(
    [
        "요즘 얼굴에 올라온 잡티 때문에 비타민C 찾아보고 있어요ㅠㅠ",
        "거울 볼 때마다 속상해요",
        "",
        "예전에 비타민C 들어간 크림도 발라봤는데",
        "별로 달라지는게 없더라구요",
        "",
        "{키워드}",
        "",
        "비타민C 말고 또 뭘 봐야 할까요?",
        "혹시 방법 아시는 분 알려주세요ㅠㅠ",
    ]
)

COMMENTS = {
    "댓글1": "비타민C는 꾸준히 발라야 티가 나요",
    "대댓글1": "아 그렇군요 처음 알았어요ㅠㅠ",
    "댓글2": "비타민C 발라도 각질이 두꺼우면 흡수가 안된대요 각질 정돈되는걸로 바꿔보세요",
    "대댓글2": "헉 그런건 어떤 성분 봐야되요?",
    "대대댓글2": "그린커피 아하바하 찾아보세요 흡수가 달라요",
    "대대대댓글2": "222 저도 그거 쓰고 확실히 나아졌어요",
    "댓글3": "저도 작년에 똑같이 고민했어요ㅠㅠ",
    "대댓글3": "저만 그런게 아니였네요ㅎㅎ",
    "댓글4": "스크럽 자주 하면 더 올라와요",
    "대댓글4": "아 그거 계속 했는데 큰일이네요",
    "댓글5": "저는 두 달쯤 쓰니까 톤이 확 밝아졌어요",
    "대댓글5": "와 두 달이면 해볼만하네요!",
}


def _nodes(table: dict[str, str] | None = None) -> list[CommentNode]:
    data = dict(COMMENTS)
    if table:
        data.update(table)
    return bw._comment_nodes(data)


def _manuscript(**kw) -> Manuscript:
    base = dict(
        title="비타민C 잡티에 진짜 효과 있나요?ㅠㅠ",
        body=BODY,
        cafe="씨씨앙",
        keyword=KEYWORD,
        manuscript_type="질문형",
        source="generated:우아덤",
        comments=_nodes(),
    )
    base.update(kw)
    return Manuscript(**base)


class FakeLLM:
    """본문·댓글 두 용도를 흉내 내는 대역."""

    def __init__(self, body: str = BODY, comments: dict | None = None) -> None:
        self.body = body
        self.comments = dict(comments or COMMENTS)
        self.calls: list[tuple[str, str, str]] = []
        self.usage = {"input_tokens": 10, "output_tokens": 20}

    def complete_json(self, purpose, system, user, max_tokens=1200):
        self.calls.append((purpose, system, user))
        if purpose == "brand_body":
            return {"title": "비타민C 잡티에 진짜 효과 있나요?ㅠㅠ", "body": self.body}
        return dict(self.comments)

    def model_for(self, purpose):
        return "fake"


# --- 키워드 목록 -------------------------------------------------
@pytest.mark.skipif(not XLSX_우아덤.exists(), reason="샘플 엑셀 없음")
def test_load_pushed_keywords_from_xlsx():
    items = load_pushed_keywords("우아덤", xlsx_path=XLSX_우아덤)
    assert items, "밀려남 키워드가 하나는 있어야 한다"
    assert items[0]["keyword"] == "비타민C"
    assert {"keyword", "cafe"} == set(items[0])
    # 키워드 중복 없음
    keys = [i["keyword"] for i in items]
    assert len(keys) == len(set(keys))


@pytest.mark.skipif(not XLSX_우아덤.exists(), reason="샘플 엑셀 없음")
def test_keyword_reader_skips_password_column():
    from v2r.sources.keyword_list import rows_from_xlsx

    rows = rows_from_xlsx(XLSX_우아덤)
    assert rows
    for row in rows:
        for key in row:
            assert not any(h in str(key).replace(" ", "").casefold() for h in PASSWORD_HEADERS)


def test_load_pushed_keywords_limit(tmp_path):
    assert load_pushed_keywords("우아덤", xlsx_path=XLSX_우아덤, limit=2).__len__() == 2


# --- 프롬프트 ---------------------------------------------------
def test_body_prompt_carries_brand_rules():
    system, user = build = bw.build_body_prompt("우아덤", KEYWORD, "씨씨앙")
    assert "쉼표" in system and "마침표" in system
    assert "250자" in user
    assert "3번" in user
    assert "그린커피 아하바하" in user  # 본문 금칙어로 명시
    assert "{키워드}" in user
    assert "2번째 문단" in user
    assert build is not None


def test_body_prompt_patsooni_review_type():
    _, user = bw.build_body_prompt("팥순이", "곤약젤리", manuscript_type="후기형")
    assert "300자" in user
    assert "4번" in user
    assert "{B/A}" in user
    assert "1번째 문단" in user


def test_comments_prompt_has_12_labels_and_limits():
    _, user = bw.build_comments_prompt("우아덤", KEYWORD, "제목", BODY)
    for label in bw.COMMENT_LABELS:
        assert label in user
    assert "30자를 넘지 않는다" in user
    assert "70자를 넘지 않는다" in user
    assert "대대댓글2 에서 처음 나온다" in user


def test_comments_prompt_brand_specific_bans():
    _, user = bw.build_comments_prompt("장으뜸", "배란일 계산기", "제목", BODY)
    assert "제품 이라는 낱말을 절대 쓰지 않는다" in user
    assert "대대대댓글2에는 장으뜸" in user

    _, patsooni = bw.build_comments_prompt(
        "팥순이", "곤약젤리", "제목", BODY, manuscript_type="질문형"
    )
    assert "50자를 넘지 않는다" in patsooni
    assert "팥순ㅇㅣ" in patsooni


def test_rule_for_unknown_brand():
    with pytest.raises(bw.BrandWriteError):
        bw.rule_for("없는브랜드")


# --- 검증 -------------------------------------------------------
def test_validate_passes_for_good_manuscript():
    assert bw.failures(bw.validate(_manuscript())) == []


def test_validate_rejects_brand_name_in_body():
    bad = _manuscript(body=BODY.replace("크림도", "그린커피 아하바하도"))
    problems = bw.failures(bw.validate(bad))
    assert any("브랜드" in p for p in problems)


def test_validate_rejects_missing_placeholder():
    bad = _manuscript(body=BODY.replace("{키워드}", "그래서 고민이에요"))
    assert any("{키워드}" in p for p in bw.failures(bw.validate(bad)))


def test_validate_rejects_wrong_comment_count():
    bad = _manuscript(comments=_nodes()[:5])
    assert any("댓글 12개" in p for p in bw.failures(bw.validate(bad)))


def test_validate_rejects_low_keyword_count():
    bad = _manuscript(body=BODY.replace("비타민C", "그거", 2))
    assert any("키워드 포함 횟수" in p for p in bw.failures(bw.validate(bad)))


def test_validate_flags_long_comment_as_warning():
    """상한(30자)을 넘어도 1.5배(45자) 안이면 경고로만 잡는다."""
    m = _manuscript(comments=_nodes({"댓글3": "가" * 40}))
    checks = bw.validate(m)
    row = next(c for c in checks if c["항목"] == "댓글 글자 수")
    assert row["통과"] is False and row["필수"] is False
    assert "댓글3 40자 → 30자로 줄일 것" in row["실제"]  # 목표 글자 수를 짚어 준다
    assert bw.failures(checks) == []  # 경고는 실패가 아니다


def test_validate_hard_fails_beyond_1_5x_length():
    """1.5배를 넘기면 경고가 아니라 실패다."""
    m = _manuscript(comments=_nodes({"댓글3": "가" * 60}))
    problems = bw.failures(bw.validate(m))
    assert any("심각 초과" in p and "30자로 줄일 것" in p for p in problems)


def test_soft_limits_off_makes_every_excess_hard():
    m = _manuscript(comments=_nodes({"댓글3": "가" * 40}))
    problems = bw.failures(bw.validate(m, soft_limits=False))
    assert any(p.startswith("댓글 글자 수:") for p in problems)


def test_comment_limits_are_configurable_per_brand():
    viral = bw.rule_for("우아덤")
    patsooni = bw.rule_for("팥순이", "질문형")
    assert (viral.root_max, viral.comment2_max) == (30, 70)
    assert (patsooni.root_max, patsooni.comment2_max) == (50, 50)
    assert viral.comment_max == viral.root_max  # 옛 이름도 그대로 읽힌다
    loose = bw.BrandRule(brand="우아덤", root_max=45, comment2_max=90)
    m = _manuscript(comments=_nodes({"댓글3": "가" * 40}))
    assert bw.validate(m, loose)[0] is not None
    row = next(c for c in bw.validate(m, loose) if c["항목"] == "댓글 글자 수")
    assert row["통과"] is True


# --- 내부 용어 누출 --------------------------------------------
def test_validate_rejects_internal_terms_in_body():
    bad = _manuscript(body=BODY.replace("별로 달라지는게 없더라구요", "프레임이 아예 다르더라구요"))
    assert any("내부 용어" in p for p in bw.failures(bw.validate(bad)))


def test_validate_rejects_internal_terms_in_comments():
    bad = _manuscript(comments=_nodes({"대대댓글2": "그린커피 아하바하요 프레임이 달라요"}))
    problems = bw.failures(bw.validate(bad))
    assert any("댓글 내부 용어" in p and "프레임" in p for p in problems)


def test_placeholder_is_not_counted_as_internal_term():
    assert bw.failures(bw.validate(_manuscript())) == []  # 본문의 {키워드}는 봐준다


# --- 대대댓글2 물러서기 금지 -----------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "그린커피 아하바하 쓰는데 제품보다 방법이 중요한거같아요",
        "그린커피 아하바하요 근데 결국은 습관이더라구요",
        "그린커피 아하바하 써보세요 개인차 있으니 참고만 하세요",
    ],
)
def test_validate_rejects_retreat_phrases_in_first_mention(text):
    bad = _manuscript(comments=_nodes({"대대댓글2": text}))
    problems = bw.failures(bw.validate(bad))
    assert any("물러서기 금지" in p for p in problems)


def test_comments_prompt_forbids_retreat_and_gives_examples():
    system, user = bw.build_comments_prompt("우아덤", KEYWORD, "제목", BODY)
    assert "물러서지" in system and "좋은 보기 1)" in system and "좋은 보기 2)" in system
    assert "쓰게 된 계기" in system and "느낀 변화" in system
    assert "제품보다 방법이 중요" in user and "참고만" in user
    assert "프레임" in user  # 금칙어로 명시


def test_prompts_forbid_internal_terms():
    body_sys, _ = bw.build_body_prompt("우아덤", KEYWORD)
    cmt_sys, _ = bw.build_comments_prompt("우아덤", KEYWORD, "제목", BODY)
    for text in (body_sys, cmt_sys):
        assert "내부 용어 누출 금지" in text


# --- 페르소나 다양화 --------------------------------------------
def test_body_prompt_injects_persona_seed():
    _, user = bw.build_body_prompt("우아덤", KEYWORD)
    who = bw.persona_for("우아덤", KEYWORD, "질문형")
    assert "이 인물로만 써라" in user
    for value in who.values():
        assert value in user


def test_persona_pool_is_big_and_stable():
    assert len(bw.PERSONA_SEEDS) >= 12
    assert bw.persona_for("우아덤", KEYWORD) == bw.persona_for("우아덤", KEYWORD)
    for seed in bw.PERSONA_SEEDS:
        assert {"나이대", "상황", "말투", "시각"} == set(seed)


def test_two_keywords_of_same_brand_get_different_personas():
    a = bw.persona_for("우아덤", "비타민C", "질문형")
    b = bw.persona_for("우아덤", "편평사마귀", "질문형")
    assert a != b


# --- 후기형 댓글 역할 -------------------------------------------
def test_review_type_comments_prompt_states_roles():
    _, user = bw.build_comments_prompt(
        "팥순이", "카무트효소", "제목", BODY, manuscript_type="후기형"
    )
    assert "대댓글2 = 본문 작성자" in user
    assert "대대댓글2 = 여분 댓글풀 계정" in user
    assert "대대대댓글2 = 본문 작성자" in user
    assert "되묻지 않는다" in user


def test_author_labels_differ_by_manuscript_type():
    assert "대대대댓글2" in bw.author_labels("후기형")
    assert "대대대댓글2" not in bw.author_labels("질문형")


def _review_manuscript(**table) -> Manuscript:
    base = {
        "대댓글2": "저는 팥순ㅇㅣ 먹고 있어요 두 달째에요",
        "대대댓글2": "저도 이거 먹는중인데 확실히 다르더라구요",
        "대대대댓글2": "맞아욬ㅋㅋ 같이 하시는 분들 많더라구요",
    }
    base.update(table)
    return _manuscript(
        manuscript_type="후기형",
        source="generated:팥순이",
        keyword="카무트효소",
        comments=_nodes(base),
    )


def test_review_type_rejects_author_asking_about_own_product():
    bad = _review_manuscript(대대대댓글2="오 그거 어디서 사요?")
    problems = bw.failures(bw.validate(bad))
    assert any("작성자 되묻기 금지" in p for p in problems)


def test_review_type_rejects_author_asking_about_effect():
    bad = _review_manuscript(대댓글2="그럼 어떤 성분을 같이 하신거예요?")
    assert any("작성자 되묻기" in p for p in bw.failures(bw.validate(bad)))


def test_review_type_allows_pool_account_to_ask():
    """여분 풀 계정(대대댓글2)이 묻는 것은 막지 않는다."""
    ok = _review_manuscript(대대댓글2="저도 이거 먹는중이에요 효과 어때요?")
    assert not any("작성자 되묻기" in p for p in bw.failures(bw.validate(ok)))


def test_question_type_author_may_ask_back():
    """질문형 작성자는 원래 되묻는 자리라 막지 않는다."""
    assert bw.failures(bw.validate(_manuscript())) == []


def test_retry_note_tells_exact_length_target():
    """길이로 다시 시킬 때 노드별 목표 글자 수를 그대로 넘긴다."""

    class LongComments(FakeLLM):
        def __init__(self):
            super().__init__()
            self.n = 0

        def complete_json(self, purpose, system, user, max_tokens=1200):
            if purpose == "brand_body":
                return {"title": "제목", "body": BODY}
            self.n += 1
            self.calls.append((purpose, system, user))
            if self.n == 1:
                return dict(COMMENTS, 댓글3="가" * 60)
            return dict(COMMENTS)

    llm = LongComments()
    bw.generate_manuscript(llm, "우아덤", KEYWORD)
    note = llm.calls[1][2]
    assert "댓글3 60자 → 30자로 줄일 것" in note


# --- 생성 -------------------------------------------------------
def test_generate_manuscript_with_fake_llm():
    llm = FakeLLM()
    m = bw.generate_manuscript(llm, "우아덤", KEYWORD, "씨씨앙")
    assert m.keyword == KEYWORD and m.cafe == "씨씨앙"
    assert [c.label for c in m.comments] == list(bw.COMMENT_LABELS)
    assert "{키워드}" in m.body
    assert bw.brand_of(m) == "우아덤"
    purposes = [c[0] for c in llm.calls]
    assert purposes == ["brand_body", "brand_comments"]


def test_generate_keeps_retrying_until_it_passes():
    """한 번 어긋났다고 포기하지 않고 구체적 위반을 짚어 다시 시킨다."""
    bad_body = BODY.replace("{키워드}", "음")

    class Flaky(FakeLLM):
        def __init__(self, good_at: int):
            super().__init__()
            self.n = 0
            self.good_at = good_at

        def complete_json(self, purpose, system, user, max_tokens=1200):
            if purpose == "brand_body":
                self.n += 1
                self.calls.append((purpose, system, user))
                return {
                    "title": "제목",
                    "body": BODY if self.n >= self.good_at else bad_body,
                }
            return super().complete_json(purpose, system, user, max_tokens)

    llm = Flaky(good_at=4)
    stats: dict = {}
    m = bw.generate_manuscript(llm, "우아덤", KEYWORD, stats=stats)
    assert llm.n == 4 and "{키워드}" in m.body
    assert stats["body_attempts"] == 4 and stats["unresolved"] == []
    note = llm.calls[1][2]
    assert "직전 시도에서 어긴 규칙" in note
    assert "{키워드} 자리표시자" in note  # 무엇이 어긋났는지 구체적으로 알려 준다


def test_generate_stops_at_attempt_cap_and_records_rules():
    """상한(6회)까지 가면 실패로 보고하지 않고 남은 규칙만 적어 둔다."""
    llm = FakeLLM(body=BODY.replace("{키워드}", "음"))
    stats: dict = {}
    m = bw.generate_manuscript(llm, "우아덤", KEYWORD, stats=stats)
    assert stats["body_attempts"] == bw.MAX_ATTEMPTS == 6
    assert stats["hit_cap"] is True
    assert any("{키워드} 자리표시자" in r for r in stats["unresolved"])
    assert m.title and len(m.comments) == 12  # 원고는 버리지 않는다


def test_generate_regenerates_only_comments_when_body_passed():
    """본문이 통과했으면 본문은 그대로 두고 댓글만 다시 만든다."""

    class BadComments(FakeLLM):
        def __init__(self):
            super().__init__()
            self.body_calls = 0
            self.comment_calls = 0

        def complete_json(self, purpose, system, user, max_tokens=1200):
            if purpose == "brand_body":
                self.body_calls += 1
                return {"title": "제목", "body": BODY}
            self.comment_calls += 1
            self.calls.append((purpose, system, user))
            if self.comment_calls < 3:
                return {k: "" for k in bw.COMMENT_LABELS}
            return dict(COMMENTS)

    llm = BadComments()
    stats: dict = {}
    bw.generate_manuscript(llm, "우아덤", KEYWORD, stats=stats)
    assert llm.body_calls == 1  # 본문은 한 번만 만들었다
    assert stats["comment_attempts"] == 3
    assert "직전 시도에서 어긴 규칙" in llm.calls[1][2]


def test_generate_raises_when_model_errors():
    class Broken(FakeLLM):
        def complete_json(self, *a, **k):
            raise RuntimeError("네트워크 오류")

    with pytest.raises(bw.BrandWriteError):
        bw.generate_manuscript(Broken(), "우아덤", KEYWORD)


def test_violations_are_concrete():
    bad = _manuscript(body=BODY.replace("비타민C", "그거", 2))
    items = bw.violations(bw.validate(bad), scope="본문")
    assert any("키워드 포함 횟수" in i and "3회 이상" in i and "1회" in i for i in items)


def test_generate_requires_keyword():
    with pytest.raises(bw.BrandWriteError):
        bw.generate_manuscript(FakeLLM(), "우아덤", "  ")


# --- 출력 -------------------------------------------------------
def test_sheet_text_round_trips_through_parse_article():
    from v2r.content.manuscript import parse_article

    m = _manuscript()
    parsed = parse_article(bw.sheet_text(m))
    assert parsed.title == m.title
    assert [c.label for c in parsed.comments] == list(bw.COMMENT_LABELS)


def test_write_review_md(tmp_path):
    path = bw.write_review_md(_manuscript(), tmp_path / "draft.md")
    text = path.read_text(encoding="utf-8")
    assert "## 비타민C (우아덤 / 질문형)" in text
    assert "### 제목" in text and "### 본문" in text
    assert "{키워드}" in text
    for label in bw.COMMENT_LABELS:
        assert f"| {label} |" in text
    assert "댓글풀 A1" in text and "본문 작성자" in text
    assert "검증 결과" in text and "통과" in text


def test_review_md_account_roles_follow_manuscript_type(tmp_path):
    m = _manuscript(manuscript_type="후기형", source="generated:팥순이", keyword="곤약젤리")
    text = bw.review_block(m)
    row = next(line for line in text.splitlines() if line.startswith("| 대대대댓글2 |"))
    assert "본문 작성자" in row


def test_save_json(tmp_path):
    path = bw.save_json(
        _manuscript(),
        tmp_path / "우아덤" / f"{KEYWORD}.json",
        {"attempts": 3, "unresolved": []},
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["brand"] == "우아덤"
    assert data["keyword"] == KEYWORD
    assert "제목 :" in data["sheet_text"]
    assert len(data["comments"]) == 12
    assert data["stats"]["attempts"] == 3
    assert all(c["통과"] for c in data["checks"] if c["필수"])


# --- 명령 해석 --------------------------------------------------
@pytest.mark.parametrize(
    "text, count",
    [
        ("브랜드 원고 생성 우아덤 1건", 1),
        ("우아덤 원고 1개 만들어줘", 1),
        ("장으뜸 원고 3개 만들어줘", 3),
    ],
)
def test_parser_routes_generate_brand(text, count):
    spec = parse_korean_command(text)
    assert spec is not None
    assert spec.task == "generate_brand"
    assert spec.count == count
    assert spec.brand in ("우아덤", "장으뜸")


def test_parser_reads_manuscript_type():
    spec = parse_korean_command("팥순이 후기형 원고 2개 만들어줘")
    assert spec.task == "generate_brand" and spec.manuscript_type == "후기형"


def test_parser_still_routes_daily_generation():
    assert parse_korean_command("일상 글 5개 생성해줘").task == "generate_daily"


# --- 작업 실행 --------------------------------------------------
def test_worker_generate_brand(tmp_path, monkeypatch):
    from tests.test_engine import make_runtime

    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    rt._llm = FakeLLM()
    rt._llm_ready = True
    monkeypatch.setattr(
        "v2r.sources.keyword_list.load_pushed_keywords",
        lambda brand, cfg=None, xlsx_path=None, limit=0: [
            {"keyword": KEYWORD, "cafe": "씨씨앙"}
        ],
    )
    spec = parse_korean_command("우아덤 원고 1개 만들어줘")
    out = worker._generate_brand(rt, spec)
    assert out["ok"] is True and out["generated"] == 1
    assert out["attempts"][KEYWORD] == 2 and out["unresolved"] == []
    saved = tmp_path / "warehouse" / "manuscripts" / "generated" / "우아덤" / f"{KEYWORD}.json"
    assert saved.exists()
    report = Path(out["report"])
    assert report.exists() and "비타민C" in report.read_text(encoding="utf-8")
    rt.close()


def test_worker_generate_brand_without_llm(tmp_path):
    from tests.test_engine import make_runtime

    rt = make_runtime(tmp_path)
    rt.settings.repo_root = tmp_path
    out = worker._generate_brand(rt, parse_korean_command("우아덤 원고 1개 만들어줘"))
    assert out["ok"] is False and "ANTHROPIC_API_KEY" in out["error"]
    rt.close()


def test_generate_brand_task_is_allowed():
    from v2r.command.spec import ALLOWED_TASKS

    assert "generate_brand" in ALLOWED_TASKS
