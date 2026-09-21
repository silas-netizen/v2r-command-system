"""게시판 쓰기 권한 조회의 신뢰성 테스트 (2026-09-21 실측 버그).

버그: `Catalog.menus`가 429를 만난 계정을 조용히 건너뛰어, 자사 카페 4곳이
하루 종일 계정 3개만 썼다. 실제 API는 부르지 않는다(전부 대역).
"""

from __future__ import annotations

from v2r.api import catalog as catalog_mod
from v2r.api.catalog import Cafe, Catalog, Menu
from v2r.api.errors import V2RApiError
from v2r.command.spec import TaskSpec, today_kst
from v2r.engine import publish as publish_mod

from tests.test_engine import make_runtime

CAFE = Cafe(cafe_id=14567700, name="고요한 아침")
SELF_CFG = {
    "default_board": "자유게시판",
    "self_owned": [{"name": "고요한 아침", "cafe_id": 14567700, "board": "자유게시판"}],
}


class _Acc:
    def __init__(self, login_id: str, work_type: str = "자사 카페") -> None:
        self.login_id = login_id
        self.work_type = work_type
        self.excluded = False


def _menu_payload(login_id: str) -> dict:
    return {"data": [{"menuId": 101, "menuName": "자유게시판", "writable": True}]}


class _FakeClient:
    """정해진 순서대로 예외/응답을 돌려주는 클라이언트 대역."""

    def __init__(self, script: dict[str, list]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    def get(self, path, params=None, **kwargs):
        login_id = (params or {}).get("naver_login_id", "")
        self.calls.append(login_id)
        queue = self.script.get(login_id) or []
        item = queue.pop(0) if queue else _menu_payload(login_id)
        if isinstance(item, Exception):
            raise item
        return item


def _rate_limited(retry_after=None) -> V2RApiError:
    return V2RApiError("429", status=429, kind="rate_limited", retry_after=retry_after)


# --------------------------------------------------------------------
# 1. Catalog.menus: 재시도 / 영구 오류
# --------------------------------------------------------------------
def test_menus_retries_rate_limited_until_success(monkeypatch):
    """429는 건너뛰지 않고 지수 백오프로 다시 부른다."""
    waits: list[float] = []
    monkeypatch.setattr(catalog_mod.time, "sleep", lambda s: waits.append(s))
    client = _FakeClient({"peecics": [_rate_limited(), _rate_limited()]})
    cat = Catalog(client)

    menus = cat.menus(CAFE.cafe_id, ["peecics"])

    assert [m.name for m in menus] == ["자유게시판"]
    assert menus[0].writable_accounts == {"peecics"}
    assert client.calls == ["peecics"] * 3
    assert waits == [catalog_mod.MENU_RETRY_WAITS[0], catalog_mod.MENU_RETRY_WAITS[1]]
    assert cat.unhealthy_accounts == {} and cat.last_menu_skips == {}


def test_menus_uses_retry_after_header(monkeypatch):
    """`Retry-After`가 있으면 그 값만큼 기다린다."""
    waits: list[float] = []
    monkeypatch.setattr(catalog_mod.time, "sleep", lambda s: waits.append(s))
    cat = Catalog(_FakeClient({"qbneb": [_rate_limited(retry_after=7)]}))
    cat.menus(CAFE.cafe_id, ["qbneb"])
    assert waits == [7.0]


def test_menus_gives_up_after_max_retries(monkeypatch):
    """끝까지 안 풀리면 사유를 남기고 건너뛴다 (조용히 사라지지 않는다)."""
    monkeypatch.setattr(catalog_mod.time, "sleep", lambda s: None)
    client = _FakeClient({"polbbo": [_rate_limited() for _ in range(9)]})
    cat = Catalog(client)

    assert cat.menus(CAFE.cafe_id, ["polbbo"]) == []
    assert len(client.calls) == len(catalog_mod.MENU_RETRY_WAITS) + 1
    assert cat.last_menu_skips == {"polbbo": "rate_limited"}
    assert cat.unhealthy_accounts == {"polbbo": "rate_limited"}


def test_menus_skips_permanent_error_without_retry(monkeypatch):
    """영구 오류(가입 정보 없음)는 재시도하지 않고 건너뛴다."""
    slept: list[float] = []
    monkeypatch.setattr(catalog_mod.time, "sleep", lambda s: slept.append(s))
    err = V2RApiError("없음", status=404, kind="no_membership")
    client = _FakeClient({"ghost": [err], "live": []})
    cat = Catalog(client)

    menus = cat.menus(CAFE.cafe_id, ["ghost", "live"])

    assert slept == []
    assert client.calls == ["ghost", "live"]
    assert menus[0].writable_accounts == {"live"}
    assert cat.last_menu_skips == {"ghost": "no_membership"}


def test_menus_retries_network_error(monkeypatch):
    """네트워크 오류도 일시 오류로 보고 다시 부른다."""
    monkeypatch.setattr(catalog_mod.time, "sleep", lambda s: None)
    client = _FakeClient({"peecics": [OSError("연결 끊김")]})
    cat = Catalog(client)
    assert cat.menus(CAFE.cafe_id, ["peecics"])[0].writable_accounts == {"peecics"}


# --------------------------------------------------------------------
# 2. 하루 단위 DB 캐시
# --------------------------------------------------------------------
class _CountingCatalog:
    """menus 호출 횟수를 세는 카탈로그 대역."""

    def __init__(self, writable: set[str], skips: dict[str, str] | None = None) -> None:
        self.writable = writable
        self.skips = skips or {}
        self.calls: list[list[str]] = []
        self.last_menu_skips: dict[str, str] = {}

    def cafes(self):
        return [CAFE]

    def cafe_accounts(self, cafe_id):
        from v2r.api.catalog import CafeAccount

        return [CafeAccount(login_id=i, member_key="k") for i in sorted(self.writable | set(self.skips))]

    def menus(self, cafe_id, login_ids):
        self.calls.append(list(login_ids))
        self.last_menu_skips = {i: self.skips[i] for i in login_ids if i in self.skips}
        allowed = {i for i in login_ids if i in self.writable}
        if not allowed:
            return []
        return [Menu(menu_id=101, name="자유게시판", writable_accounts=allowed)]


def test_menu_cache_hit_calls_no_api(tmp_path):
    """같은 날 같은 카페는 프로세스가 달라도 API를 다시 부르지 않는다."""
    rt = make_runtime(tmp_path)
    cat = _CountingCatalog(writable={"a", "b", "c"})
    rt._catalog = cat
    menus, skipped = publish_mod.cafe_menus(rt, CAFE, ["a", "b", "c"])
    assert len(cat.calls) == 1 and skipped == {}
    assert menus[0].writable_accounts == {"a", "b", "c"}
    rt.close()

    # 새 프로세스(=새 Runtime, 같은 DB)에서도 캐시를 쓴다
    rt2 = make_runtime(tmp_path)
    cat2 = _CountingCatalog(writable={"a", "b", "c"})
    rt2._catalog = cat2
    menus2, _ = publish_mod.cafe_menus(rt2, CAFE, ["a", "b", "c"])
    assert cat2.calls == []  # API 0회
    assert menus2[0].writable_accounts == {"a", "b", "c"}
    assert rt2.sources_cache.get(publish_mod._menu_cache_key(CAFE.cafe_id, today_kst()))
    rt2.close()


def test_menu_cache_fetches_only_new_accounts(tmp_path):
    """계정이 늘어나면 캐시에 없는 계정만 추가로 조회한다."""
    rt = make_runtime(tmp_path)
    cat = _CountingCatalog(writable={"a", "b", "c"})
    rt._catalog = cat
    publish_mod.cafe_menus(rt, CAFE, ["a", "b"])
    menus, _ = publish_mod.cafe_menus(rt, CAFE, ["a", "b", "c"])
    assert cat.calls == [["a", "b"], ["c"]]
    assert menus[0].writable_accounts == {"a", "b", "c"}
    rt.close()


# --------------------------------------------------------------------
# 3. 자사 카페 안전장치: 시트 기준 보정 + 경고
# --------------------------------------------------------------------
def test_self_cafe_pool_is_repaired_from_sheet(tmp_path):
    """권한 조회가 부분 실패하면 시트 회원을 쓰기 가능으로 보고 풀을 되살린다."""
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = SELF_CFG
    everyone = [f"user{i:02d}" for i in range(12)]
    rt._catalog = _CountingCatalog(
        writable={"user00", "user01", "user02"},
        skips={i: "rate_limited" for i in everyone[3:]},
    )
    pool = [_Acc(i) for i in everyone]
    spec = TaskSpec(task="publish_daily", cafe="고요한 아침", board="자유게시판", dry_run=False)

    narrowed, deferred = publish_mod._pool_for_cafe(rt, spec, "고요한 아침", "자유게시판", pool)

    assert deferred is False
    assert len(narrowed) == 12  # 3개로 쪼그라들지 않는다
    events = rt.scratch.get("plan_events") or []
    assert any(lv == "warn" and "게시판 권한 조회가 부분 실패: 3/12" in msg for lv, msg in events)
    rt.close()


def test_healthy_self_cafe_pool_has_no_warning(tmp_path):
    """정상 조회(전원 쓰기 가능)면 경고도 보정도 없다."""
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = SELF_CFG
    everyone = [f"user{i:02d}" for i in range(12)]
    rt._catalog = _CountingCatalog(writable=set(everyone))
    spec = TaskSpec(task="publish_daily", cafe="고요한 아침", board="자유게시판", dry_run=False)

    narrowed, _ = publish_mod._pool_for_cafe(
        rt, spec, "고요한 아침", "자유게시판", [_Acc(i) for i in everyone]
    )

    assert len(narrowed) == 12
    assert not (rt.scratch.get("plan_events") or [])
    rt.close()


# --------------------------------------------------------------------
# 4. 계획 단계 이벤트
# --------------------------------------------------------------------
def _m(cafe: str, i: int):
    from v2r.content.manuscript import Manuscript

    return Manuscript(
        title=f"제목 {i}",
        body=f"본문 {i}\n둘째 줄",
        cafe=cafe,
        board="자유게시판",
        source="테스트시트",
        source_row=i + 1,
        content_hash=f"h{i}",
        images_enabled=False,
    )


def test_plan_logs_account_pool_line(tmp_path, monkeypatch):
    """카페별 '계정 풀 N개 / 오늘 10개 중 M개 사용' 한 줄을 남긴다."""
    rt = make_runtime(tmp_path)
    rt.cafes_cfg = SELF_CFG
    pool = [_Acc(f"user{i:02d}") for i in range(12)]
    monkeypatch.setattr(publish_mod, "load_accounts", lambda rt, prefer_cache=False: pool)
    monkeypatch.setattr(publish_mod, "eligible", lambda *a, **k: list(pool))
    monkeypatch.setattr(publish_mod, "_pool_for_cafe", lambda rt, spec, c, b, p: (list(p), False))
    monkeypatch.setattr(publish_mod, "_cafe_members", lambda rt, cafe: (CAFE, [a.login_id for a in pool]))

    spec = TaskSpec(task="publish_daily", count=4, per_cafe=True, immediate=True, dry_run=False)
    publish_mod.plan(rt, spec, [_m("고요한 아침", i) for i in range(4)])

    events = rt.scratch.get("plan_events") or []
    assert any(
        lv == "info" and "계정 풀 12개" in msg and "오늘 10개 중" in msg for lv, msg in events
    ), events

    # worker가 job_id로 옮겨 적는다
    job_id = rt.jobs.enqueue(TaskSpec(task="publish_daily"), "menuperm-1")
    publish_mod.flush_plan_events(rt, job_id)
    assert rt.scratch.get("plan_events") is None
    logged = [r["message"] for r in rt.events.recent(job_id=job_id, limit=50)]
    assert any("계정 풀 12개" in m for m in logged)
    rt.close()


def test_plan_event_uses_job_id_from_scratch(tmp_path):
    rt = make_runtime(tmp_path)
    job_id = rt.jobs.enqueue(TaskSpec(task="publish_daily"), "menuperm-2")
    rt.scratch["job_id"] = job_id
    publish_mod.plan_event(rt, "info", "계정 풀 7개 / 7개 사용")
    assert "plan_events" not in rt.scratch
    assert any("계정 풀 7개" in r["message"] for r in rt.events.recent(job_id=job_id, limit=20))
    rt.close()
