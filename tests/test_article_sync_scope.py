"""`sync_all_cafes` scope 라우팅 테스트 (자사/제휴/전체).

브랜드 원고는 제휴 카페(씨씨앙·양평맘 등)에 올라가는데, 옛 `sync_all_self_cafes`는
자사 카페만 돌아서 키워드 노출 검사가 우리 글을 못 찾았다. 이 테스트는 새
`sync_all_cafes(rt, scope=...)`가 scope에 따라 올바른 카페 집합을 도는지,
`parser`가 `제휴 카페 글 목록 동기화` / `전체 글 목록 동기화` 문구를 같은
작업(`sync_article_index`)으로 잡는지 확인한다. 실제 API는 부르지 않는다
(계정 조회·글 목록 조회를 대역으로 바꾼다).
"""

from __future__ import annotations

import pytest

from v2r.command.parser import parse_korean_command
from v2r.command.spec import TaskSpec
from v2r.engine import article_sync
from v2r.engine import publish as publish_mod
from v2r.engine import worker

from tests.test_engine import make_runtime


def _cafes_cfg() -> dict:
    return {
        "self_owned": [
            {"name": "고요한 아침", "cafe_id": 101},
            {"name": "웨딩 노트", "cafe_id": 102, "excluded": True},
        ],
        "affiliate": [
            {"name": "씨씨앙", "cafe_id": 201},
            {"name": "양평맘", "cafe_id": 202},
        ],
    }


def _patch_calls(monkeypatch):
    """카페별 `_accounts`/`_fetch_written_articles` 호출을 기록만 하는 대역."""
    seen: list[str] = []

    def fake_accounts(rt, cafe_id):
        seen.append(cafe_id)
        return [f"user{cafe_id}"]

    def fake_fetch(rt, cafe_id, login_id):
        return [{"source_id": f"s{cafe_id}", "title": f"제목{cafe_id}", "naver_login_id": login_id}]

    monkeypatch.setattr(article_sync, "_accounts", fake_accounts)
    monkeypatch.setattr(article_sync, "_fetch_written_articles", fake_fetch)
    return seen


def test_affiliate_cafe_names는_제휴_카페_이름만_돌려준다(tmp_path):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = _cafes_cfg()
    assert publish_mod.affiliate_cafe_names(rt) == ["씨씨앙", "양평맘"]
    rt.close()


def test_scope_self는_자사_카페만_돈다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = _cafes_cfg()
    seen = _patch_calls(monkeypatch)
    out = article_sync.sync_all_cafes(rt, "self")
    assert seen == [101, 102]  # 제외 카페도 포함(중복 방지 목적)
    assert {c["cafe"] for c in out["cafes"]} == {"고요한 아침", "웨딩 노트"}
    assert out["rows"] == 2
    rt.close()


def test_scope_affiliate는_제휴_카페만_돈다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = _cafes_cfg()
    seen = _patch_calls(monkeypatch)
    out = article_sync.sync_all_cafes(rt, "affiliate")
    assert seen == [201, 202]
    assert {c["cafe"] for c in out["cafes"]} == {"씨씨앙", "양평맘"}
    assert out["rows"] == 2
    rt.close()


def test_scope_all은_자사와_제휴를_모두_돈다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = _cafes_cfg()
    seen = _patch_calls(monkeypatch)
    out = article_sync.sync_all_cafes(rt, "all")
    assert sorted(seen) == [101, 102, 201, 202]
    assert out["rows"] == 4
    rt.close()


def test_scope_잘못된값은_에러(tmp_path):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = _cafes_cfg()
    with pytest.raises(ValueError):
        article_sync.sync_all_cafes(rt, "이상한값")
    rt.close()


@pytest.mark.parametrize(
    "text",
    [
        "자사 카페 글 목록 동기화",
        "제휴 카페 글 목록 동기화",
        "전체 글 목록 동기화",
        "글 목록 동기화",
    ],
)
def test_파서는_모든_문구를_sync_article_index로_잡는다(text):
    spec = parse_korean_command(text)
    assert spec is not None
    assert spec.task == "sync_article_index"
    assert spec.notes == text


@pytest.mark.parametrize(
    "text,expect_cafe_ids",
    [
        ("자사 카페 글 목록 동기화", [101, 102]),
        ("글 목록 동기화", [101, 102]),  # 옛 문구는 하위 호환으로 자사만
        ("제휴 카페 글 목록 동기화", [201, 202]),
        ("전체 글 목록 동기화", [101, 102, 201, 202]),
    ],
)
def test_dispatch가_문구에서_scope를_고른다(tmp_path, monkeypatch, text, expect_cafe_ids):
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = _cafes_cfg()
    seen = _patch_calls(monkeypatch)
    spec = TaskSpec(task="sync_article_index", notes=text)
    job = {"id": 1, "spec_json": spec.to_json()}
    out = worker.dispatch(rt, job)
    assert sorted(seen) == sorted(expect_cafe_ids)
    assert out["ok"] is True
    rt.close()
