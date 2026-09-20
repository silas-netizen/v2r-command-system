"""V2R 글 목록 색인(article_index)과 중복 관문 테스트.

사용자 절대 규칙: 자사 카페 V2R 글 목록에 이미 있는 글과 앞으로 발행하는 글 사이에
중복이 절대 없어야 한다. 실제 API는 부르지 않는다(가짜 클라이언트).
"""

from __future__ import annotations

import pytest

from v2r.command.parser import parse_korean_command
from v2r.command.spec import TaskSpec
from v2r.content import duplicate
from v2r.content.manuscript import Manuscript, content_hash
from v2r.engine import article_sync
from v2r.engine import publish as publish_mod
from v2r.store.article_index import normalize_title

from tests.test_engine import make_runtime

CAFES_CFG = {
    "default_board": "자유게시판",
    "self_owned": [
        {"name": "고요한 아침", "cafe_id": 101, "board": "반말일기"},
        {"name": "글로시 마이", "cafe_id": 102, "board": "자유 톡"},
        {"name": "웨딩 노트", "cafe_id": 103, "board": "토크 수다", "excluded": True},
    ],
}


@pytest.fixture()
def rt(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.cafes_cfg = CAFES_CFG
    yield runtime
    runtime.close()


def _m(title: str, body: str = "본문입니다", cafe: str = "") -> Manuscript:
    return Manuscript(
        title=title, body=body, cafe=cafe, content_hash=content_hash(title, body)
    )


# --------------------------------------------------------------------
# 1. 정규화
# --------------------------------------------------------------------
def test_normalize_title_공백_문장부호_대소문자():
    assert normalize_title("Hello, 오늘 날씨!!") == "hello오늘날씨"


def test_normalize_title_이모지_제거():
    assert normalize_title("가을 나들이 🍁🍁") == "가을나들이"


def test_normalize_title_반복_자모_줄이기():
    assert normalize_title("웃겼어ㅋㅋㅋㅋ") == normalize_title("웃겼어ㅋㅋ")
    assert normalize_title("슬퍼ㅠㅠㅠ") == normalize_title("슬퍼ㅠ")
    assert normalize_title("슬퍼ㅠㅠㅠ") != normalize_title("슬퍼")


# --------------------------------------------------------------------
# 2. 저장소 upsert
# --------------------------------------------------------------------
def test_upsert_신규_갱신(rt):
    assert rt.article_index.upsert(cafe_id=101, cafe="고요한 아침", source_id="s1", title="첫 글") is True
    # 같은 (cafe_id, source_id)는 갱신이다
    assert rt.article_index.upsert(cafe_id=101, cafe="고요한 아침", source_id="s1", title="첫 글 수정") is False
    assert rt.article_index.count() == 1
    assert rt.article_index.rows_for_cafe("고요한 아침")[0]["title"] == "첫 글 수정"


def test_upsert_body_hash는_지우지_않는다(rt):
    rt.article_index.upsert(cafe_id=101, cafe="고요한 아침", source_id="s1", title="글", body_hash="H")
    rt.article_index.upsert(cafe_id=101, cafe="고요한 아침", source_id="s1", title="글")
    assert rt.article_index.hash_match("H") is not None


def test_upsert_source_id_없으면_article_id로(rt):
    assert rt.article_index.upsert(cafe_id=101, cafe="고요한 아침", article_id=777, title="옛 글") is True
    row = rt.article_index.rows_for_cafe("고요한 아침")[0]
    assert row["source_id"] == "article:777"
    # 열쇠가 하나도 없으면 아무것도 넣지 않는다
    assert rt.article_index.upsert(cafe_id=101, cafe="고요한 아침", title="키 없음") is False


def test_rows_missing_body와_set_body_hash(rt):
    rt.article_index.upsert(cafe_id=101, cafe="고요한 아침", source_id="s1", title="글")
    assert len(rt.article_index.rows_missing_body("고요한 아침")) == 1
    rt.article_index.set_body_hash(101, "s1", "HASH")
    assert rt.article_index.rows_missing_body("고요한 아침") == []


# --------------------------------------------------------------------
# 3. 동기화 (가짜 클라이언트 · 계정 2개 · 페이지 넘김)
# --------------------------------------------------------------------
class FakeCafeAccount:
    def __init__(self, login_id: str) -> None:
        self.login_id = login_id


class FakeCatalog:
    """카페 계정 2개."""

    def __init__(self) -> None:
        self.asked: list = []

    def cafe_accounts(self, cafe_id):
        self.asked.append(cafe_id)
        return [FakeCafeAccount("acc1"), FakeCafeAccount("acc2")]


class FakeClient:
    """`written_articles`가 페이지를 넘기는 가짜 서버. 계정마다 2페이지."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.details: dict = {}

    def get(self, path, params=None):
        params = params or {}
        self.calls.append({"path": path, **params})
        if path.endswith("written_articles"):
            login = params["naver_login_id"]
            page = int(params.get("page") or 1)
            if page == 1:
                return {
                    "articles": [
                        {"v2r_source_id": f"{login}-1", "subject": f"{login} 첫 글", "articleid": 1},
                        {"v2r_source_id": f"{login}-2", "subject": f"{login} 둘째 글", "articleid": 2},
                    ],
                    "total_count": 3,
                }
            if page == 2:
                return {
                    "articles": [
                        {"v2r_source_id": f"{login}-3", "subject": f"{login} 셋째 글", "articleid": 3}
                    ],
                    "total_count": 3,
                }
            return {"articles": [], "total_count": 3}
        # 글 상세
        return self.details.get(params.get("source_id"), {})


def _wire(rt) -> FakeClient:
    client = FakeClient()
    rt._client = client
    rt._catalog = FakeCatalog()
    return client


def test_sync_cafe_index_계정2개_페이지넘김(rt):
    _wire(rt)
    out = article_sync.sync_cafe_index(rt, "고요한 아침")
    assert out["accounts"] == 2
    # 계정마다 3건 (1페이지 2건 + 2페이지 1건)
    assert out["rows"] == 6
    assert out["new"] == 6
    assert rt.article_index.count("고요한 아침") == 6
    assert out["errors"] == []


def test_sync_cafe_index_두번_돌려도_안_늘어난다(rt):
    _wire(rt)
    article_sync.sync_cafe_index(rt, "고요한 아침")
    out = article_sync.sync_cafe_index(rt, "고요한 아침")
    assert out["new"] == 0 and out["updated"] == 6
    assert rt.article_index.count() == 6


def test_sync_cafe_index_본문까지(rt, monkeypatch):
    client = _wire(rt)
    monkeypatch.setattr(article_sync.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        article_sync.api_articles, "body_lines_of", lambda detail: ["본문 한 줄"]
    )
    out = article_sync.sync_cafe_index(rt, "고요한 아침", with_bodies=True)
    assert out["bodies"] == 6
    expected = content_hash("acc1 첫 글", "본문 한 줄")
    assert rt.article_index.hash_match(expected) is not None
    assert any(c["path"].endswith("/article") for c in client.calls)


def test_sync_all_self_cafes_제외_카페도_포함(rt):
    _wire(rt)
    out = article_sync.sync_all_self_cafes(rt)
    assert [c["cafe"] for c in out["cafes"]] == ["고요한 아침", "글로시 마이", "웨딩 노트"]
    assert out["rows"] == 18
    assert out["total"] == 18
    assert out["ok"] is True
    assert out["warnings"] == []


def test_sync_cafe_index_cafe_id_없으면_오류(rt):
    _wire(rt)
    out = article_sync.sync_cafe_index(rt, "없는 카페")
    assert out["errors"] and out["rows"] == 0


# --------------------------------------------------------------------
# 3-1. 계정 조회 실패 → 경고로만 남기고 카페 동기화는 계속 (재시도 포함)
# --------------------------------------------------------------------
class FlakyClient(FakeClient):
    """`fail_logins`에 든 계정은 매번 실패, `retry_once_logins`는 첫 시도만 실패."""

    def __init__(self, fail_logins=(), retry_once_logins=()) -> None:
        super().__init__()
        self.fail_logins = set(fail_logins)
        self.retry_once_logins = set(retry_once_logins)
        self._retry_seen: set = set()

    def get(self, path, params=None):
        params = params or {}
        if path.endswith("written_articles"):
            login = params.get("naver_login_id")
            if login in self.fail_logins:
                raise RuntimeError(f"{login} 조회 실패")
            if login in self.retry_once_logins and login not in self._retry_seen:
                self._retry_seen.add(login)
                raise RuntimeError(f"{login} 일시 오류")
        return super().get(path, params=params)


def test_sync_cafe_index_계정_실패는_재시도_후_경고로(rt, monkeypatch):
    monkeypatch.setattr(article_sync.time, "sleep", lambda s: None)
    client = FlakyClient(retry_once_logins={"acc1"})
    rt._client = client
    rt._catalog = FakeCatalog()

    out = article_sync.sync_cafe_index(rt, "고요한 아침")

    # acc1은 재시도(2번째 시도)로 결국 성공해 정상 계정과 동일하게 6건
    assert out["errors"] == []
    assert out["warnings"] == []
    assert out["rows"] == 6
    assert out["accounts"] == 2


def test_sync_cafe_index_계정_재시도_후에도_실패면_경고(rt, monkeypatch):
    monkeypatch.setattr(article_sync.time, "sleep", lambda s: None)
    client = FlakyClient(fail_logins={"acc1"})
    rt._client = client
    rt._catalog = FakeCatalog()

    out = article_sync.sync_cafe_index(rt, "고요한 아침")

    # acc1은 계속 실패 → 경고에만 기록되고, acc2는 정상적으로 색인된다
    assert out["errors"] == []
    assert len(out["warnings"]) == 1
    assert "acc1" in out["warnings"][0]
    assert out["rows"] == 3
    assert rt.article_index.count("고요한 아침") == 3


def test_sync_all_self_cafes_일부_계정_실패해도_ok_True(rt, monkeypatch):
    monkeypatch.setattr(article_sync.time, "sleep", lambda s: None)
    client = FlakyClient(fail_logins={"acc1"})
    rt._client = client
    rt._catalog = FakeCatalog()

    out = article_sync.sync_all_self_cafes(rt)

    assert out["ok"] is True
    assert len(out["warnings"]) == 3  # acc1은 카페 3곳 모두에서 실패
    assert out["rows"] == 9  # 카페마다 acc2만 성공(3건)


def test_sync_all_self_cafes_전_계정_실패면_ok_False(rt, monkeypatch):
    monkeypatch.setattr(article_sync.time, "sleep", lambda s: None)
    client = FlakyClient(fail_logins={"acc1", "acc2"})
    rt._client = client
    rt._catalog = FakeCatalog()

    out = article_sync.sync_all_self_cafes(rt)

    assert out["ok"] is False
    assert out["rows"] == 0
    assert len(out["warnings"]) == 6  # 계정 2개 x 카페 3곳


def test_format_sync_summary_경고_없음():
    out = {"rows": 4265, "cafes": [{}] * 5, "warnings": []}
    assert article_sync.format_sync_summary(out) == "색인 동기화: 4,265건 (카페 5)"


def test_format_sync_summary_경고_있음():
    out = {"rows": 4265, "cafes": [{}] * 5, "warnings": ["a: x", "b: y", "c: z"]}
    assert (
        article_sync.format_sync_summary(out)
        == "색인 동기화: 4,265건 (카페 5) — 계정 3개 조회 실패(경고)"
    )


# --------------------------------------------------------------------
# 4. 중복 관문
# --------------------------------------------------------------------
def test_gate_제목_완전일치(rt):
    rt.article_index.upsert(
        cafe_id=101, cafe="고요한 아침", source_id="s1", title="가을 나들이 다녀왔어요"
    )
    dup, why = duplicate.is_duplicate_against_index(
        rt, _m("가을  나들이 다녀왔어요!!"), "고요한 아침"
    )
    assert dup is True and "제목 동일" in why


def test_gate_다른_카페면_제목만으로는_안_걸린다(rt):
    rt.article_index.upsert(
        cafe_id=101, cafe="고요한 아침", source_id="s1", title="가을 나들이 다녀왔어요"
    )
    dup, _ = duplicate.is_duplicate_against_index(
        rt, _m("가을 나들이 다녀왔어요"), "글로시 마이"
    )
    assert dup is False


def test_gate_본문해시는_카페를_넘어서_잡는다(rt):
    m = _m("어떤 제목", "같은 본문입니다")
    rt.article_index.upsert(
        cafe_id=101, cafe="고요한 아침", source_id="s1", title="전혀 다른 제목",
        body_hash=m.content_hash,
    )
    dup, why = duplicate.is_duplicate_against_index(rt, m, "글로시 마이")
    assert dup is True and "본문 동일" in why


def test_gate_제목_유사(rt):
    rt.article_index.upsert(
        cafe_id=101,
        cafe="고요한 아침",
        source_id="s1",
        title="아이와 함께 주말 공원 나들이 다녀온 이야기입니다",
    )
    dup, why = duplicate.is_duplicate_against_index(
        rt, _m("아이와 함께 주말 공원 나들이 다녀온 이야기입니다요"), "고요한 아침"
    )
    assert dup is True and "제목 유사" in why


def test_gate_안_겹치면_통과(rt):
    rt.article_index.upsert(
        cafe_id=101, cafe="고요한 아침", source_id="s1", title="가을 나들이"
    )
    dup, why = duplicate.is_duplicate_against_index(
        rt, _m("전혀 상관없는 겨울 이야기 주제"), "고요한 아침"
    )
    assert dup is False and why == ""


def test_gate_색인이_비면_아무것도_막지_않는다(rt):
    dup, _ = duplicate.is_duplicate_against_index(rt, _m("아무 제목"), "고요한 아침")
    assert dup is False


# --------------------------------------------------------------------
# 5. 선택 단계에서 건너뛰는가
# --------------------------------------------------------------------
def _fake_source(rt, monkeypatch, manuscripts: list[Manuscript]) -> None:
    monkeypatch.setattr(
        publish_mod, "select_source_entries", lambda r, s: [{"name": "시트A"}]
    )
    monkeypatch.setattr(
        publish_mod, "load_manuscripts", lambda r, e, prefer_cache=False: manuscripts
    )


def test_prepare_manuscripts_색인중복_skip(rt, monkeypatch):
    rt.article_index.upsert(
        cafe_id=101, cafe="고요한 아침", source_id="s1", title="이미 올린 글"
    )
    items = [
        Manuscript(
            title="이미 올린 글",
            body="본문",
            cafe="고요한 아침",
            source="시트A",
            source_row=2,
            content_hash="h1",
        ),
        Manuscript(
            title="새로운 글",
            body="본문",
            cafe="고요한 아침",
            source="시트A",
            source_row=3,
            content_hash="h2",
        ),
    ]
    _fake_source(rt, monkeypatch, items)
    skipped: list[dict] = []
    spec = TaskSpec(task="publish_daily", dry_run=True, source="시트A")
    picked = publish_mod.prepare_manuscripts(rt, spec, skipped)
    assert [m.title for m in picked] == ["새로운 글"]
    reasons = [s["reason"] for s in skipped]
    assert any(r.startswith("V2R 기존 글과 중복:") for r in reasons)


def test_prepare_per_cafe_색인중복_skip(rt, monkeypatch):
    rt.article_index.upsert(
        cafe_id=101, cafe="고요한 아침", source_id="s1", title="이미 올린 글"
    )
    items = [
        Manuscript(
            title="이미 올린 글",
            body="본문",
            cafe="고요한 아침",
            source="시트A",
            source_row=2,
            content_hash="h1",
        ),
        Manuscript(
            title="새로운 글",
            body="본문",
            cafe="고요한 아침",
            source="시트A",
            source_row=3,
            content_hash="h2",
        ),
    ]
    _fake_source(rt, monkeypatch, items)
    skipped: list[dict] = []
    spec = TaskSpec(task="publish_daily", dry_run=True, per_cafe=True, count=5)
    picked = publish_mod.prepare_per_cafe(rt, spec, skipped)
    assert [m.title for m in picked] == ["새로운 글"]
    assert any(
        str(s["reason"]).startswith("V2R 기존 글과 중복:") for s in skipped
    )


# --------------------------------------------------------------------
# 6. 발행 시점 색인 기록
# --------------------------------------------------------------------
def test_record_published가_색인에_남긴다(rt):
    article_sync.record_published(
        rt,
        cafe="고요한 아침",
        source_id="new-1",
        login_id="acc1",
        title="방금 올린 글",
        body_hash="HH",
    )
    assert rt.article_index.count("고요한 아침") == 1
    # 바로 다음 원고가 같은 제목이면 막힌다 (재동기화 없이)
    dup, _ = duplicate.is_duplicate_against_index(rt, _m("방금 올린 글"), "고요한 아침")
    assert dup is True


def test_run_slot이_발행뒤_색인에_남긴다(rt, monkeypatch):
    """`run_slot` 성공 경로가 `record_published`를 부른다."""
    calls: list[dict] = []
    monkeypatch.setattr(
        publish_mod.article_sync, "record_published", lambda r, **kw: calls.append(kw)
    )
    src = publish_mod.__dict__
    assert "article_sync" in src
    # run_slot 성공 꼬리 부분을 직접 흉내내지 않고, 소스에 훅이 있는지 확인한다
    import inspect

    body = inspect.getsource(publish_mod.run_slot)
    assert "article_sync.record_published" in body
    assert "is_duplicate_against_index" in body


def test_duplicate_check_보고(rt, monkeypatch):
    rt.article_index.upsert(
        cafe_id=101, cafe="고요한 아침", source_id="s1", title="이미 올린 글"
    )
    items = [
        Manuscript(title="이미 올린 글", body="a", cafe="고요한 아침", source="시트A", source_row=1),
        Manuscript(title="새로운 글", body="b", cafe="고요한 아침", source="시트A", source_row=2),
    ]
    _fake_source(rt, monkeypatch, items)
    spec = TaskSpec(task="duplicate_check", count=10)
    out = article_sync.duplicate_check(rt, spec)
    assert out["checked"] == 2
    assert out["duplicates"] == 1
    assert out["clean"] == 1


# --------------------------------------------------------------------
# 7. 명령 해석
# --------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    ["글 목록 동기화", "자사 카페 글 목록 동기화", "자사 카페 글 목록 동기화 본문까지"],
)
def test_parse_sync_article_index(text):
    spec = parse_korean_command(text)
    assert spec is not None and spec.task == "sync_article_index"


def test_parse_sync_article_index_본문까지_표시():
    spec = parse_korean_command("자사 카페 글 목록 동기화 본문까지")
    assert "본문까지" in (spec.notes or "")


def test_parse_duplicate_check():
    spec = parse_korean_command("중복 검사")
    assert spec is not None and spec.task == "duplicate_check"
