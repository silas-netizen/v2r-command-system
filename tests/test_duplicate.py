"""중복 검사 테스트."""

from v2r.content.duplicate import (
    check_against_history,
    compare,
    dice_score,
    normalize_text,
)
from v2r.content.manuscript import Manuscript


def _m(title: str, body: str) -> Manuscript:
    return Manuscript(title=title, body=body)


def test_normalize_text():
    assert normalize_text("ABC, 가-나!") == "abc가나"


def test_dice_score_범위():
    assert dice_score("가나다라", "가나다라") == 1.0
    assert dice_score("가나다라", "") == 0.0
    assert 0.0 < dice_score("가나다라마", "가나다라바") < 1.0


def test_compare_완전일치():
    exact, score = compare(_m("제목", "본문 내용"), _m("제 목", "본문   내용!"))
    assert exact is True and score == 1.0


def test_check_against_history():
    base = _m("가을 나들이", "오늘은 아이와 함께 공원에 다녀왔습니다.")
    assert check_against_history(base, [base]) == "exact"
    similar = _m("가을 나들이", "오늘은 아이와 함께 공원에 다녀왔습니다!")
    assert check_against_history(similar, [base]) in ("exact", "similar")
    other = _m("전혀 다른 글", "완전히 상이한 주제로 작성한 원고입니다.")
    assert check_against_history(other, [base]) is None


def test_행단위_skip_배치중단없음():
    history = [_m("A", "aaaa bbbb cccc")]
    rows = [_m("A", "aaaa bbbb cccc"), _m("B", "전혀 다른 내용 입니다 여기는")]
    results = [check_against_history(r, history) for r in rows]
    assert results[0] == "exact" and results[1] is None
