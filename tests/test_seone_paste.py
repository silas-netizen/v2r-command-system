"""SE-ONE 붙여넣기 모듈의 순수 함수 테스트. 실제 브라우저는 띄우지 않는다."""

from __future__ import annotations

from v2r.browser.seone_paste import (
    IMAGE_CTYPES,
    component_key,
    extract_image_components,
)


def _img(idx: int, ctype: str = "image", **over) -> dict:
    base = {
        "@ctype": ctype,
        "id": f"SE-img-{idx}",
        "src": f"https://cdn/v2r/{idx}.jpg",
        "path": f"/upload/{idx}.jpg",
        "fileName": f"{idx}.jpg",
        "fileSize": 12345 + idx,
    }
    base.update(over)
    return base


def _doc(components: list[dict]) -> dict:
    return {"document": {"version": "2.8.10", "components": components, "documentId": ""}}


def test_ctypes():
    assert IMAGE_CTYPES == {"image", "imageGroup", "imageStrip"}


def test_extract_returns_only_ready_images():
    doc = _doc(
        [
            {"@ctype": "text", "value": [{"@ctype": "paragraph"}]},
            _img(1),
            _img(2, "imageGroup"),
            _img(3, "imageStrip"),
        ]
    )
    found = extract_image_components(doc)
    assert [c["fileName"] for c in found] == ["1.jpg", "2.jpg", "3.jpg"]


def test_extract_preserves_document_order():
    doc = _doc([_img(5), {"@ctype": "text"}, _img(6), _img(7)])
    assert [c["id"] for c in extract_image_components(doc)] == [
        "SE-img-5",
        "SE-img-6",
        "SE-img-7",
    ]


def test_extract_skips_zero_filesize():
    doc = _doc([_img(1, fileSize=0), _img(2)])
    assert [c["fileName"] for c in extract_image_components(doc)] == ["2.jpg"]


def test_extract_skips_missing_filesize():
    doc = _doc([_img(1, fileSize=None), _img(2, fileSize="not-a-number"), _img(3)])
    assert [c["fileName"] for c in extract_image_components(doc)] == ["3.jpg"]


def test_extract_skips_empty_src_path_filename():
    doc = _doc([_img(1, src="", path="", fileName=""), _img(2, src="  ", path=" ", fileName="")])
    assert extract_image_components(doc) == []


def test_extract_accepts_only_one_reference_field():
    doc = _doc([_img(1, src="", path="")])  # fileName만 남음
    assert len(extract_image_components(doc)) == 1


def test_extract_finds_nested_components():
    doc = {
        "document": {
            "components": [
                {"@ctype": "imageGroup", "id": "grp", "src": "", "path": "", "fileName": "", "fileSize": 0,
                 "images": [_img(9)]},
            ]
        }
    }
    assert [c["fileName"] for c in extract_image_components(doc)] == ["9.jpg"]


def test_extract_handles_bare_list_and_empty():
    assert extract_image_components([_img(1)])[0]["fileName"] == "1.jpg"
    assert extract_image_components({}) == []
    assert extract_image_components(None) == []
    assert extract_image_components([]) == []
    assert extract_image_components("문자열") == []


def test_extract_does_not_loop_on_cycles():
    node = _img(1)
    node["self"] = node
    assert len(extract_image_components([node])) == 1


def test_component_key_is_stable_and_distinct():
    a, b = _img(1), _img(2)
    assert component_key(a) == component_key(dict(a))
    assert component_key(a) != component_key(b)
    no_id = {"@ctype": "image", "src": "https://cdn/x.jpg", "fileSize": 1}
    assert component_key(no_id) == "src:https://cdn/x.jpg"
    assert component_key({"@ctype": "image"})  # 빈 값도 키는 만들어진다
