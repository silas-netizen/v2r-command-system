"""SE-ONE 문서 생성 테스트."""

from __future__ import annotations

import json

import pytest

from v2r.content import seone


def _components(doc: dict) -> list[dict]:
    return doc["document"]["components"]


def _lines(component: dict) -> list[str]:
    return [
        "".join(node["value"] for node in para["nodes"])
        for para in component["value"]
    ]


def test_document_structure() -> None:
    doc = seone.build_document("첫 줄\n둘째 줄", [])
    document = doc["document"]
    assert document["version"] == "2.9.0"
    assert document["language"] == "ko-KR"
    assert document["theme"] == "default"
    assert document["documentId"] == ""
    assert len(document["id"]) == 26 and document["id"] == document["id"].upper()
    assert document["di"]["dif"] is False
    comps = _components(doc)
    assert len(comps) == 1
    assert comps[0]["@ctype"] == "text"
    assert _lines(comps[0]) == ["첫 줄", "둘째 줄"]


def test_empty_lines_preserved() -> None:
    doc = seone.build_document("가\n\n나", [])
    assert _lines(_components(doc)[0]) == ["가", "", "나"]


def test_empty_body_makes_one_empty_text_component() -> None:
    comps = _components(seone.build_document("", []))
    assert len(comps) == 1
    assert _lines(comps[0]) == [""]


def test_placeholder_inserts_image_and_keeps_empty_paragraph() -> None:
    img = {"@ctype": "image", "id": "SE-img", "src": "x", "fileSize": 10}
    doc = seone.build_document("위\n{사진}\n아래", [img])
    comps = _components(doc)
    assert [c["@ctype"] for c in comps] == ["text", "image", "text"]
    assert _lines(comps[0]) == ["위", ""]
    assert _lines(comps[2]) == ["아래"]


def test_placeholder_with_text_on_same_line() -> None:
    img = {"@ctype": "image", "id": "SE-img"}
    doc = seone.build_document("안녕{사진}", [img])
    comps = _components(doc)
    assert [c["@ctype"] for c in comps] == ["text", "image"]
    assert _lines(comps[0]) == ["안녕"]


def test_missing_image_component_raises() -> None:
    """사진이 모자라면 조용히 넘기지 않고 실패한다 (api-spec §4)."""
    with pytest.raises(ValueError) as exc:
        seone.build_document("{사진}\n{사진}", [{"@ctype": "image"}])
    assert "사진 수가 부족합니다: 자리 2, 사진 1" in str(exc.value)


def test_extra_image_component_raises() -> None:
    with pytest.raises(ValueError):
        seone.build_document("{사진}", [{"@ctype": "image"}, {"@ctype": "image"}])


def test_crlf_body_has_no_carriage_return() -> None:
    doc = seone.build_document("가\r\n{사진}\r\n나", [{"@ctype": "image"}])
    comps = _components(doc)
    assert [c["@ctype"] for c in comps] == ["text", "image", "text"]
    assert _lines(comps[0]) == ["가", ""]
    assert _lines(comps[2]) == ["나"]
    assert seone.body_lines_for_verify("a\r\n{사진}\r\nb") == ["a", "", "b"]


def test_count_and_body_lines_for_verify() -> None:
    body = "가\n{사진}\n나{사진2}"
    assert seone.count_placeholders(body) == 2
    assert seone.body_lines_for_verify(body) == ["가", "", "나"]


def test_content_json_serialization() -> None:
    raw = seone.content_json("한글 줄", [])
    assert ", " not in raw
    assert "한글 줄" in raw
    assert json.loads(raw)["document"]["version"] == "2.9.0"
