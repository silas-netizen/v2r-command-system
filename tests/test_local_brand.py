"""원본 종류 `local_brand`(원고폴더 JSON) 테스트."""

from __future__ import annotations

import json

from v2r.sources.local_brand import (
    DEFAULT_BOARD,
    DEFAULT_CAFE,
    KIND,
    folder_entry,
    load_folder,
)


def _write(path, name, data):
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_폴더의_json을_원고목록으로_읽는다(tmp_path):
    folder = tmp_path / "twins-2026-09-25"
    _write(
        folder,
        "01.json",
        {
            "title": "제목1",
            "body": "본문1",
            "keyword": "쌍둥이유모차",
            "tags": ["쌍둥이유모차"],
            "manuscript_type": "후기형",
            "_note": "무시되어야 함",
        },
    )
    _write(
        folder,
        "02.json",
        {"title": "제목2", "body": "본문2", "cafe": "다른카페", "account": "user02"},
    )
    items = load_folder(folder, source="twins-2026-09-25")
    assert len(items) == 2
    by_title = {m.title: m for m in items}
    m1 = by_title["제목1"]
    assert m1.cafe == DEFAULT_CAFE
    assert m1.board == DEFAULT_BOARD
    assert m1.account == ""
    assert m1.source == "twins-2026-09-25"
    assert m1.content_hash
    m2 = by_title["제목2"]
    assert m2.cafe == "다른카페"  # 파일에 명시된 값은 기본값을 덮지 않는다
    assert m2.account == "user02"


def test_밑줄로_시작하는_키는_무시된다(tmp_path):
    folder = tmp_path / "brand"
    _write(folder, "01.json", {"title": "t", "body": "b", "_internal": {"x": 1}})
    items = load_folder(folder)
    assert len(items) == 1


def test_빈_문자열_필드도_기본값으로_채운다(tmp_path):
    folder = tmp_path / "brand"
    _write(folder, "01.json", {"title": "t", "body": "b", "cafe": "", "board": ""})
    m = load_folder(folder)[0]
    assert m.cafe == DEFAULT_CAFE
    assert m.board == DEFAULT_BOARD


def test_언더스코어로_시작하는_파일은_원고가_아니다(tmp_path):
    folder = tmp_path / "brand"
    _write(folder, "01.json", {"title": "t", "body": "b"})
    _write(folder, "_results.json", {"어떤키": {"ok": True}})
    items = load_folder(folder)
    assert len(items) == 1
    assert items[0].title == "t"


def test_source_row는_파일명에서_안정적으로_나온다(tmp_path):
    folder = tmp_path / "brand"
    _write(folder, "a.json", {"title": "a", "body": "a"})
    _write(folder, "b.json", {"title": "b", "body": "b"})
    first = {m.title: m.source_row for m in load_folder(folder)}
    # 파일을 하나 더 추가해도 기존 파일들의 source_row는 변하지 않는다
    _write(folder, "c.json", {"title": "c", "body": "c"})
    second = {m.title: m.source_row for m in load_folder(folder)}
    assert first["a"] == second["a"]
    assert first["b"] == second["b"]
    assert len({second["a"], second["b"], second["c"]}) == 3


def test_없는_폴더는_빈_목록(tmp_path):
    assert load_folder(tmp_path / "없음") == []


def test_folder_entry_형식():
    entry = folder_entry("D:/base", "twins-2026-09-25")
    assert entry["name"] == "twins-2026-09-25"
    assert entry["kind"] == KIND
    assert entry["path"].endswith("twins-2026-09-25")
