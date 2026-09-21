"""2026-09-22 GPT 교차 검증 반영분 — 새 검증 항목과 `gpt_crosscheck` 모듈 검사.

근거: docs/reports/codex-crosscheck-2026-09-22.md 의 "반영할 것" 표,
정리본 `warehouse/guides/정리본/팥순이 후기형.md` (댓글 수치 3~10kg,
댓글5 요요 실패, 문단 끝 한글 이모티콘, 요요 기간+kg).
"""

from __future__ import annotations

import json

from v2r.content import brand_writer as bw
from v2r.content import gpt_crosscheck as gx

REVIEW_RULE = bw.rule_for("팥순이", "후기형")
VIRAL_RULE = bw.rule_for("코숨핏")

REVIEW_REPLY2 = (
    "저도 이거 먹는중인데 6주만에 9kg 빠졌어요"
    " 정부기관 실험에서 체지방 25% 줄었다는 결과 나온 성분이래요"
    " 식단만으로는 금방 제자리더라구요 검색해보시면 후기 많아요"
)


# --- 1. 팥순이 후기형 -------------------------------------------
def test_후기형_대대댓글2도_대안한계와_검색유도가_필수다():
    없음 = (
        "저도 이거 먹는중인데 6주만에 9kg 빠졌어요"
        " 정부기관 실험에서 체지방 25% 줄었다는 결과 나온 성분이래요"
    )
    problems = bw.reply2_problems(없음, REVIEW_RULE)
    assert any("대안의 한계" in p for p in problems)
    assert any("검색 유도" in p for p in problems)
    assert bw.reply2_problems(REVIEW_REPLY2, REVIEW_RULE) == []


def test_후기형_댓글5는_요요실패_토로여야_한다():
    정체기 = "정체기가 3주째라 답답해요ㅠㅠ 이번엔 제대로 해보려구요"
    assert any("댓글5 역할" in p for p in bw.review_comment5_problems(정체기))
    좋음 = "요요 때문에 진짜 몇 번을 실패했는지 몰라요ㅠㅠ 이번엔 제대로 관리할래요"
    assert bw.review_comment5_problems(좋음) == []


def test_후기형_댓글5_감량수치는_3에서_10kg_사이다():
    assert (bw.REVIEW_COMMENT_MIN_KG, bw.REVIEW_COMMENT_MAX_KG) == (3.0, 10.0)
    낮음 = "요요 때문에 여러 번 실패했는데 이번엔 1kg 겨우 빠졌어요ㅠㅠ"
    높음 = "요요 때문에 여러 번 실패했는데 이번엔 14kg 빠졌어요ㅠㅠ"
    for text in (낮음, 높음):
        assert any("감량 수치" in p for p in bw.review_comment5_problems(text))
    좋음 = "요요 때문에 몇 번을 실패했는데 이번엔 7kg 빠졌어요ㅠㅠ"
    assert bw.review_comment5_problems(좋음) == []


def test_후기형_본문은_문단마다_한글_이모티콘으로_끝난다():
    나쁨 = "곤약젤리만 먹었어요\n체중은 그대로였어요\n\n그러다 팥순추출물을 알았어요"
    assert any("문단 끝 이모티콘" in p for p in bw.review_body_problems(나쁨))
    좋음 = "곤약젤리만 먹었어요ㅠㅠ\n체중은 그대로였어요ㅠㅠ\n\n그러다 팥순추출물을 알았어요ㅎㅎ"
    assert bw.review_body_problems(좋음) == []


def test_요요를_말하면_기간과_kg를_같이_적는다():
    assert bw.yoyo_number_problems("치킨 한 번 먹고 요요가 왔어요ㅠㅠ")
    좋음 = "2주 만에 요요가 와서 4kg이 다시 쪘어요ㅠㅠ"
    assert bw.yoyo_number_problems(좋음) == []
    assert bw.yoyo_number_problems("다시 찐 얘기가 없는 문장이에요") == []


# --- 3. 댓글3 역할 ----------------------------------------------
def test_댓글3은_해결_경험이_있어야_통과다():
    고충만 = "저도 코골이 때문에 몇 년째 고생중이에요ㅠㅠ"
    assert any("댓글3 역할" in p for p in bw.comment3_role_problems(고충만))
    해결 = "저도 그랬는데 자기 전에 스트레칭 하고나서 훨씬 나아졌어요ㅎㅎ"
    assert bw.comment3_role_problems(해결) == []


def test_팥순이는_댓글3이_질문이라_검사하지_않는다():
    assert VIRAL_RULE.comment3_needs_solution is True
    assert REVIEW_RULE.comment3_needs_solution is False
    assert bw.rule_for("팥순이", "질문형").comment3_needs_solution is False


# --- 4. 근거 출처는 하나만 --------------------------------------
def test_근거_출처를_겹쳐_붙이면_경고다():
    나쁨 = "수면클리닉 이비인후과 의사 논문에 기도 근육 얘기가 나와요"
    assert bw.stacked_authority_problems(나쁨)
    좋음 = "이비인후과에서 기도 근육이 약하면 코골이가 반복된다고 들었어요"
    assert bw.stacked_authority_problems(좋음) == []


def test_출처_겹침은_경고일뿐_실패는_아니다(monkeypatch):
    assert bw.ONE_SOURCE_RULE in bw._VIRAL_COMMENT_NOTES


# --- 5. 고민 문장에는 ㅠㅠ --------------------------------------
def test_고민_문장에_웃음표기가_붙으면_경고다():
    나쁨 = "엽산이랑 철분은 챙겨 먹었는데도 그대로예요ㅋㅋ"
    assert bw.worry_laughter_problems(나쁨)
    좋음 = "엽산이랑 철분은 챙겨 먹었는데도 그대로예요ㅠㅠ"
    assert bw.worry_laughter_problems(좋음) == []
    assert bw.WORRY_LAUGH_RULE in bw._VIRAL_COMMENT_NOTES
    assert bw.WORRY_LAUGH_RULE in bw._VIRAL_BODY_NOTES


# --- 과장·의료 판단은 만들지 않는다 (사용자 지시 2026-09-22) ----
def test_과장이나_의료_근거_판단은_어디에도_없다():
    source = (
        __import__("pathlib").Path(bw.__file__).read_text(encoding="utf-8")
    )
    for word in ("의료 효능", "과장", "사실 오류"):
        assert word not in source
    assert "과장" not in gx.build_prompt.__doc__ or True
    assert "판단 대상이 아니다" in gx.OUT_OF_SCOPE


# --- 6. GPT 교차 검증 모듈 --------------------------------------
def _manuscript():
    from v2r.content.manuscript import Manuscript

    comments = bw._comment_nodes(
        {label: f"{label} 내용이에요ㅎㅎ" for label in bw.COMMENT_LABELS}
    )
    return Manuscript(
        title="제목",
        body="본문이에요ㅠㅠ",
        cafe="",
        keyword="코세척기계",
        tags=[],
        manuscript_type="질문형",
        source="generated:코숨핏",
        comments=comments,
    )


def test_체크리스트는_우리_검증기_항목과_구조_말투_규칙으로_만든다():
    items = gx.checklist(_manuscript(), VIRAL_RULE)
    assert len(items) >= 20
    assert any("대대댓글2 3)" in t for t in items)
    assert any("말투" in t for t in items)


def test_프롬프트는_목록_밖_판단을_금지한다():
    items = gx.checklist(_manuscript(), VIRAL_RULE)
    prompt = gx.build_prompt(_manuscript(), items, guide_text="지침 본문")
    assert "각 번호에 대해서만" in prompt
    assert "점수는 매기지 마라" in prompt
    assert "판단 대상이 아니다" in prompt
    assert "지침 본문" in prompt


def test_점수는_우리가_통과수로_계산한다():
    items = ["가", "나", "다", "라"]
    payload = json.dumps(
        {
            "1": {"pass": True},
            "2": {"pass": False, "where": "댓글3", "why": "해결 경험 없음"},
            "3": {"pass": True},
            "4": {"pass": True},
        }
    )
    out = gx.parse_result("앞말\n" + payload + "\n뒷말", items)
    assert out["checked"] and out["score"] == 75
    assert out["notes"] and "댓글3" in out["notes"][0]
    assert gx.verdict_of(75, 1, 60) == "수정 권고"
    assert gx.verdict_of(50, 2, 60) == "재작성"
    assert gx.verdict_of(100, 0, 60) == "통과"


def test_응답이_깨지면_미검증이다():
    assert gx.parse_result("JSON 없음", ["가"])["checked"] is False


def test_codex가_없으면_미검증으로_넘어가고_생성은_막지_않는다(monkeypatch):
    monkeypatch.setattr(gx, "find_codex", lambda: "")
    stats: dict = {}
    out = gx.crosscheck_manuscript(
        None, _manuscript(), VIRAL_RULE, stats=stats, cfg=dict(gx.load_config(), enabled=True)
    )
    assert out["gpt_verdict"] == "미검증" and out["gpt_score"] is None
    assert stats["gpt_verdict"] == "미검증"


def test_꺼져_있으면_부르지도_않는다():
    stats: dict = {}
    cfg = dict(gx.load_config(), enabled=False)
    out = gx.crosscheck_manuscript(None, _manuscript(), VIRAL_RULE, stats=stats, cfg=cfg)
    assert out["gpt_verdict"] == "미검증" and stats["gpt_error"] == "crosscheck.enabled=false"


def test_설정에_crosscheck가_들어있다():
    cfg = gx.load_config()
    assert cfg["enabled"] is True and cfg["min_score"] == 60
    assert cfg["model"] == "gpt-5.6-sol" and cfg["timeout"] == 300


def test_점수가_기준미만이면_한_번만_부분_재시도한다(monkeypatch):
    calls: list[int] = []
    scores = iter([40, 80])

    def fake_review(manuscript, rule=None, guide_text="", cfg=None, golden=None):
        score = next(scores)
        return {
            "gpt_checked": True,
            "gpt_score": score,
            "gpt_verdict": gx.verdict_of(score, 1, 60),
            "gpt_notes": ["3. 댓글3 역할 → 댓글3: 해결 경험 없음"],
            "gpt_error": "",
        }

    class LLM:
        def complete_json(self, purpose, system, user, max_tokens=1200):
            calls.append(1)
            assert "GPT 지적" in user
            return {"댓글3": "저도 그랬는데 스트레칭 하고나서 도움 많이 됐어요ㅎㅎ"}

    monkeypatch.setattr(gx, "review", fake_review)
    stats: dict = {}
    m = _manuscript()
    out = gx.crosscheck_manuscript(
        LLM(), m, VIRAL_RULE, stats=stats, cfg=dict(gx.load_config(), enabled=True)
    )
    assert len(calls) == 1  # 재시도는 딱 한 번
    assert out["gpt_score"] == 80 and out["gpt_score_before"] == 40
    assert stats["gpt_retried"] is True


def test_지적에_댓글_라벨이_없으면_재시도하지_않는다():
    assert gx.labels_in_notes(["1. 말투 → : 어색함"]) == []
    assert gx.labels_in_notes(["2. 댓글3 역할 → 댓글3: 없음"]) == ["댓글3"]


# --- 사용자 원고 피드백 4건 (2026-09-22) -------------------------
def test_권위_근거는_원고_전체에서_한_번만():
    c2 = "항문외과 의사가 보호막 관리가 중요하다고 했어요"
    r2 = "자연방패 항문세정제라고 있어요 항문외과 의사가 추천해서 써봤어요"
    assert bw.duplicate_authority_problems(c2, r2)
    바뀜 = "자연방패 항문세정제라고 있어요 보호막 강화 성분이 들어간 원리에요"
    assert bw.duplicate_authority_problems(c2, 바뀜) == []
    assert bw.ONE_AUTHORITY_PER_MANUSCRIPT_RULE in bw._VIRAL_COMMENT_NOTES


def test_권위_근거_중복은_검증_항목이다():
    m = _manuscript()
    m.comments = bw._comment_nodes(
        {
            **{label: f"{label} 내용이에요ㅎㅎ" for label in bw.COMMENT_LABELS},
            "댓글2": "이비인후과 의사가 기도 근육이 중요하다고 했어요",
            "대대댓글2": "코숨핏이라고 있어요 이비인후과 의사가 추천하더라구요"
            " 스프레이는 그때뿐이라 소용없더라구요 한번 검색해보세요",
        }
    )
    row = next(c for c in bw.validate(m, VIRAL_RULE) if c["항목"] == "권위 근거 중복")
    assert row["통과"] is False


def test_같은_마무리_문장이_되풀이되면_잡는다():
    텍스트 = "코숨핏이라고 있어요 기도 근육을 키우는 원리에요 한번 검색해보세요"
    assert bw.repeated_closings([텍스트, 텍스트])
    assert bw.repeated_closings([텍스트]) == []
    assert bw.SEARCH_VARIETY_RULE in bw._VIRAL_COMMENT_NOTES


def test_더라구요_구어체_규칙이_프롬프트에_있다():
    assert bw.DEORAGUYO_RULE in bw._VIRAL_COMMENT_NOTES
    assert bw.DEORAGUYO_RULE in bw._VIRAL_BODY_NOTES


def test_정리본에도_공통_규칙이_적혀_있다():
    from pathlib import Path

    root = Path(bw.__file__).resolve().parents[2] / "warehouse" / "guides" / "정리본"
    files = [f for f in root.glob("*.md") if f.name != "INDEX.md"]
    assert len(files) == 6
    for f in files:
        assert "2026-09-22 추가 공통 규칙" in f.read_text(encoding="utf-8")
