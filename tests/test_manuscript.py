"""원고 파서·해시 테스트."""

from v2r.content.manuscript import (
    Manuscript,
    content_hash,
    parse_article,
    tags_from_keyword,
)

SAMPLE = """# 원고
제목 : 주말 나들이 후기
본문 : 첫 줄입니다.

둘째 줄 {사진} 입니다.
---
**댓글 세트**
댓글1 : 좋아요
대댓글1 : 감사합니다
대대댓글2 : 저도요
대대대댓글2 : 네네
"""


def test_parse_article_기본():
    m = parse_article(SAMPLE)
    assert m.title == "주말 나들이 후기"
    assert m.body.splitlines()[0] == "첫 줄입니다."
    assert "{사진}" in m.body
    assert "" in m.body.splitlines()  # 빈 줄 보존
    labels = [c.label for c in m.comments]
    assert labels == ["댓글1", "대댓글1", "대대댓글2", "대대대댓글2"]
    assert [c.depth for c in m.comments] == [0, 1, 2, 3]
    assert [c.role for c in m.comments] == ["comment", "reply", "reply2", "reply3"]


def test_content_hash_공백정규화():
    a = content_hash("제목  입니다", "본문   내용")
    b = content_hash(" 제목 입니다 ", "본문 내용")
    assert a == b
    assert a != content_hash("다른 제목", "본문 내용")


def test_tags_from_keyword():
    assert tags_from_keyword(" 강남 맛집 ") == ["강남맛집"]
    assert tags_from_keyword("") == []


def test_manuscript_기본값():
    m = Manuscript(title="ㄱ", body="ㄴ")
    assert m.images_enabled is True
    assert m.tags == [] and m.comments == []
