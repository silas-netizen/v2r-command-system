"""통합 엔진(Runtime·publish·reconcile·worker) 테스트. 실제 API는 호출하지 않는다."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from v2r.api.catalog import Cafe, Menu
from v2r.api.errors import V2RApiError
from v2r.command.spec import ALLOWED_TASKS, TaskSpec
from v2r.config import Settings
from v2r.engine import publish as publish_mod
from v2r.engine import reconcile as reconcile_mod
from v2r.engine import worker
from v2r.engine.context import Runtime
from v2r.sources import sheets
from v2r.store.db import KST, connect

CAFE = Cafe(cafe_id=14567700, name="고요한 아침")
MENU = Menu(menu_id=101, name="자유게시판")


class FakeCatalog:
    """카탈로그 대역."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def resolve(self, cafe_name, board_name, login_id):
        self.calls.append((cafe_name, board_name, login_id))
        return CAFE, MENU, None

    def cafe_accounts(self, cafe_id):
        return []

    def cafes(self):
        return [CAFE]

    def menus(self, cafe_id, login_ids):
        return [MENU]


def make_runtime(tmp_path) -> Runtime:
    """임시 sqlite 위의 Runtime."""
    settings = Settings(
        v2r_email="tester@example.com",
        v2r_password="",
        data_dir=tmp_path / "data",
        warehouse_dir=tmp_path / "warehouse",
        db_path=tmp_path / "data" / "v2r.sqlite",
    )
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "warehouse").mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    rt = Runtime.open(settings, conn)
    rt._catalog = FakeCatalog()
    rt._client = object()
    rt._llm = None
    rt._llm_ready = True
    rt._channels = []
    return rt


def make_spec(**kwargs) -> TaskSpec:
    base = {
        "task": "publish_daily",
        "cafe": "고요한 아침",
        "board": "자유게시판",
        "dry_run": True,
        "manuscripts": [
            {
                "title": f"테스트 제목 {i}",
                "body": f"첫 줄 {i}\n둘째 줄",
                "account": f"user{i}",
                "source": "테스트시트",
                "source_row": i + 1,
                "content_hash": f"hash{i}",
                "images_enabled": False,
            }
            for i in range(3)
        ],
    }
    base.update(kwargs)
    return TaskSpec(**base)


# --------------------------------------------------------------------
# 드라이런
# --------------------------------------------------------------------
def test_드라이런은_슬롯만_계획하고_API를_부르지_않는다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    spec = make_spec()

    def boom(*a, **k):
        raise AssertionError("드라이런에서 API를 호출했습니다")

    monkeypatch.setattr(publish_mod.api_articles, "create_article", boom)
    monkeypatch.setattr(publish_mod.api_articles, "get_article", boom)

    manuscripts = publish_mod.prepare_manuscripts(rt, spec)
    assert len(manuscripts) == 3
    slots = publish_mod.plan(rt, spec, manuscripts)
    assert len(slots) == 3
    assert [s.account for s in slots] == ["user0", "user1", "user2"]
    assert all(s.scheduled_at is not None for s in slots)

    outs = [publish_mod.run_slot(rt, spec, s) for s in slots]
    assert all(o["status"] == "planned" for o in outs)
    assert rt.publications.list_uncertain() == []
    rt.close()


def test_테스트카페는_즉시발행(tmp_path):
    rt = make_runtime(tmp_path)
    spec = make_spec(cafe="태극", board="자유게시판")
    slots = publish_mod.plan(rt, spec, publish_mod.prepare_manuscripts(rt, spec))
    assert all(s.scheduled_at is None for s in slots)
    rt.close()


def test_count로_자른다(tmp_path):
    rt = make_runtime(tmp_path)
    spec = make_spec(count=2)
    assert len(publish_mod.prepare_manuscripts(rt, spec)) == 2
    rt.close()


# --------------------------------------------------------------------
# 실제 발행
# --------------------------------------------------------------------
def _one_slot(rt, spec):
    manuscripts = publish_mod.prepare_manuscripts(rt, spec)
    return publish_mod.plan(rt, spec, manuscripts)[0]


def test_등록_전에_uncertain으로_먼저_표시한다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, count=1)
    seen: dict = {}

    def fake_create(client, **kwargs):
        rows = rt.publications.list_uncertain()
        seen["before_create"] = [(r["source_key"], r["row_number"], r["stage"]) for r in rows]
        return "SRC-1"

    monkeypatch.setattr(publish_mod.api_articles, "create_article", fake_create)
    monkeypatch.setattr(publish_mod.api_articles, "get_article", lambda c, sid: {"ok": True})
    monkeypatch.setattr(publish_mod.api_articles, "verify_article", lambda detail, **k: [])
    monkeypatch.setattr(publish_mod.api_articles, "wait_written", lambda *a, **k: {})

    out = publish_mod.run_slot(rt, spec, _one_slot(rt, spec))

    assert seen["before_create"] == [("테스트시트", 1, "submitting")]
    assert out["status"] == "done"
    assert out["url"].endswith("SRC-1")
    assert rt.publications.list_uncertain() == []
    row = rt.publications.by_source_id("SRC-1")
    assert row["status"] == "done"
    assert rt.account_state.last_used_map().get("user0")
    rt.close()


def test_검증_불일치면_uncertain으로_남는다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, count=1)
    monkeypatch.setattr(publish_mod.api_articles, "create_article", lambda c, **k: "SRC-2")
    monkeypatch.setattr(publish_mod.api_articles, "get_article", lambda c, sid: {})
    monkeypatch.setattr(
        publish_mod.api_articles, "verify_article", lambda detail, **k: ["제목 불일치"]
    )

    with pytest.raises(publish_mod.PublishError):
        publish_mod.run_slot(rt, spec, _one_slot(rt, spec))

    left = rt.publications.list_uncertain()
    assert len(left) == 1 and left[0]["row_number"] == 1
    # 등록 직후 source_id를 보존해야 reconcile이 엉뚱한 글을 잡지 않는다 (C-2)
    assert left[0]["source_id"] == "SRC-2"
    assert rt.publications.by_source_id("SRC-2")["status"] == "uncertain"
    rt.close()


def test_계정제한이면_제한기록하고_다른계정요청(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, count=1)

    def restricted(client, **kwargs):
        raise V2RApiError("계정 제한", status=400, code="27000")

    monkeypatch.setattr(publish_mod.api_articles, "create_article", restricted)

    with pytest.raises(publish_mod.RetryWithOtherAccount):
        publish_mod.run_slot(rt, spec, _one_slot(rt, spec))

    assert rt.account_state.is_restricted("user0")
    # 서버가 요청을 거부해 글이 생기지 않았다 → failed로 내려 다른 계정이 재시도할 수 있어야 한다 (M-6)
    assert rt.publications.list_uncertain() == []
    row = rt.conn.execute(
        "SELECT status, stage FROM publications WHERE source_key = '테스트시트'"
    ).fetchone()
    assert row["status"] == "failed"
    rt.close()


def test_이미지가_있으면_브라우저_없이는_실패(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, count=1)
    slot = _one_slot(rt, spec)
    slot.images = [tmp_path / "없는파일.jpg"]
    monkeypatch.setattr(
        publish_mod.api_articles, "create_article", lambda c, **k: pytest.fail("호출 금지")
    )
    with pytest.raises(publish_mod.PublishError):
        publish_mod.run_slot(rt, spec, slot, browser_page=None)
    rt.close()


def test_사진이_없으면_발행_전에_사진필요_오류(tmp_path):
    """결정 1(2026-09-19): 원본이 하나도 없으면 NoPhotoError(텔레그램 알림용)."""
    from v2r.warehouse.store import NoPhotoError

    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, brand="우아덤")
    spec.manuscripts[0]["images_enabled"] = True
    spec.manuscripts[0]["body"] = "첫 줄\n{이미지1}\n둘째 줄"
    with pytest.raises(NoPhotoError) as exc:
        publish_mod.plan(rt, spec, publish_mod.prepare_manuscripts(rt, spec))
    assert "사진이 필요합니다" in str(exc.value)
    assert "우아덤" in str(exc.value)
    rt.close()


def test_사진이_부족하면_발행_전에_에러(tmp_path):
    """원본은 있지만 미사용 세탁본이 없으면 PublishError."""
    from PIL import Image

    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, brand="우아덤")
    spec.manuscripts[0]["images_enabled"] = True
    spec.manuscripts[0]["body"] = "첫 줄\n{이미지1}\n둘째 줄"
    wh = rt.warehouse
    wh.ensure_dirs()
    folder = wh.keyword_folder("우아덤", "이미지1")
    folder.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 20), (3, 4, 5)).save(folder / "a.jpg")
    original = next(folder.iterdir())
    sha = wh.sha256(original)
    variant = wh.washed_folder(sha)
    variant.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 20), (6, 7, 8)).save(variant / "0.jpg")
    publish_mod.record_variant_use(rt, sha, variant / "0.jpg", "SRC-USED")

    with pytest.raises(publish_mod.PublishError) as exc:
        publish_mod.plan(rt, spec, publish_mod.prepare_manuscripts(rt, spec))
    assert "사진이 부족합니다" in str(exc.value)
    rt.close()


# --------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------
def test_reconcile는_uncertain을_done으로_확정(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.publications.mark(
        "테스트시트", 5, "hashX", "uncertain", "daily_submitting", source_id="SRC-9"
    )
    monkeypatch.setattr(
        reconcile_mod.api_articles,
        "get_article",
        lambda c, sid: {"naver_cafe_article_history": {"status": "DONE", "fail_reason": ""}},
    )
    result = reconcile_mod.reconcile(rt)
    assert result == {
        "checked": 1,
        "done": 1,
        "failed": 0,
        "unresolved": [],
        "errors": [],
    }
    assert rt.publications.by_source_id("SRC-9")["status"] == "done"
    rt.close()


def test_reconcile는_삭제된_글을_failed로(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.publications.mark("테스트시트", 6, "hashY", "uncertain", source_id="SRC-10")

    def deleted(client, sid):
        raise V2RApiError("삭제됨", status=400, code="DELETED_NAVER_CAFE_ARTICLE_SOURCE")

    monkeypatch.setattr(reconcile_mod.api_articles, "get_article", deleted)
    result = reconcile_mod.reconcile(rt)
    assert result["failed"] == 1
    rt.close()


# --------------------------------------------------------------------
# worker
# --------------------------------------------------------------------
def test_채널_명령은_모델폴백을_쓰지_않는다(tmp_path):
    rt = make_runtime(tmp_path)

    class ExplodingRouter:
        enabled = True

        def complete_json(self, *a, **k):
            raise AssertionError("채널 명령에서 모델을 호출했습니다")

    rt._llm = ExplodingRouter()
    out = worker.handle_text(rt, "@@ 알 수 없는 문장 @@", via_channel=True)
    assert out["ok"] is False
    rt.close()


def test_handle_text는_같은_명령을_한_번만_등록(tmp_path):
    rt = make_runtime(tmp_path)
    a = worker.handle_text(rt, "일상 글 3개 올려")
    b = worker.handle_text(rt, "일상 글 3개 올려")
    assert a["ok"] and a["job_id"] == b["job_id"]
    rt.close()


def test_run_once는_작업을_실행하고_완료로_끝낸다(tmp_path):
    rt = make_runtime(tmp_path)
    worker.handle_text(rt, "상태 알려줘")
    out = worker.run_once(rt, "tester")
    assert out["status"] == "done"
    assert "V2R 현황" in out["result"]["report"]
    assert worker.run_once(rt, "tester") is None
    rt.close()


def test_run_once는_예외를_failed로_기록(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    worker.handle_text(rt, "상태 알려줘")
    monkeypatch.setattr(
        worker, "dispatch", lambda rt_, job, owner=None: (_ for _ in ()).throw(RuntimeError("터짐"))
    )
    out = worker.run_once(rt, "tester")
    assert out["status"] == "failed" and "터짐" in out["error"]
    job = rt.jobs.recent(1)[0]
    assert job["status"] == "failed"
    rt.close()


def test_모든_허용작업에_처리기가_있다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)

    # 외부 자원은 전부 대역으로
    monkeypatch.setattr(publish_mod, "load_manuscripts", lambda rt_, entry: [])
    monkeypatch.setattr(publish_mod, "refresh_source", lambda rt_, entry: 0)
    monkeypatch.setattr(
        "v2r.warehouse.daily_collector.collect",
        lambda urls, timeout=10: [{"status": "ok", "title": "t", "text": "b", "source": "s"}],
    )
    monkeypatch.setattr(
        "v2r.warehouse.photo_collector.collect_from_folder",
        lambda d, b, f, w=None: {"scanned": 0, "added": 0, "duplicated": 0, "skipped": 0, "errors": []},
    )
    monkeypatch.setattr(
        "v2r.knowledge.make_import.learn_make_guides", lambda d, **k: {"saved": []}
    )
    monkeypatch.setattr("v2r.browser.session.open_site", lambda **k: (None, None, object()))
    monkeypatch.setattr("v2r.browser.session.ensure_logged_in", lambda page, site=None, **k: True)
    monkeypatch.setattr("v2r.browser.session.close", lambda *a, **k: None)

    for index, task in enumerate(sorted(ALLOWED_TASKS)):
        spec = TaskSpec(
            task=task,
            cafe="고요한 아침",
            notes="https://example.com/a" if task == "collect_daily" else "",
        )
        job = {"id": 1000 + index, "spec_json": spec.to_json()}
        result = worker.dispatch(rt, job)
        assert isinstance(result, dict), task
        assert "ok" in result, task
    rt.close()


# --------------------------------------------------------------------
# C-1 / C-2: 제휴 체인과 source_id 보존
# --------------------------------------------------------------------
def _history_row(**kw) -> dict:
    row = {
        "source_id": "SRC-CHILD",
        "title": "테스트 제목 0",
        "status": "RESERVED",
        "naver_account_login_id": "user0",
        "parent_source_id": None,
        "created_at": datetime.now(KST).isoformat(),
    }
    row.update(kw)
    return row


def test_제휴_수정글_미확정은_자식을_찾아야_확정된다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.publications.mark(
        "테스트시트",
        1,
        "hashA",
        "uncertain",
        "revision_submitting",
        source_id="DAILY-1",
        account="user0",
        cafe="고요한 아침",
        scheduled_at=datetime.now(KST).isoformat(),
    )
    monkeypatch.setattr(
        reconcile_mod.api_articles,
        "get_article",
        lambda c, sid: pytest.fail("부모 source_id로 확정하면 안 됩니다"),
    )
    # 자식(수정글)이 아직 없다 → 일상 글이 RESERVED여도 미확정으로 남아야 한다
    monkeypatch.setattr(
        reconcile_mod.api_articles, "board_histories", lambda c, cafe_id, **k: []
    )
    result = reconcile_mod.reconcile(rt)
    assert result["done"] == 0 and result["unresolved"] == ["테스트시트#1"]
    assert rt.publications.list_uncertain()[0]["status"] == "uncertain"

    # 자식이 생기면 그 source_id로 확정한다
    monkeypatch.setattr(
        reconcile_mod.api_articles,
        "board_histories",
        lambda c, cafe_id, **k: [_history_row(parent_source_id="DAILY-1")],
    )
    result = reconcile_mod.reconcile(rt)
    assert result["done"] == 1
    assert rt.publications.by_source_id("SRC-CHILD")["status"] == "done"
    rt.close()


def test_즉시발행_RESERVED는_완료로_보지_않는다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.publications.mark("테스트시트", 2, "hashB", "uncertain", "created", source_id="SRC-R")
    monkeypatch.setattr(
        reconcile_mod.api_articles,
        "get_article",
        lambda c, sid: {"naver_cafe_article_history": {"status": "RESERVED"}},
    )
    result = reconcile_mod.reconcile(rt)
    assert result["done"] == 0 and result["unresolved"] == ["테스트시트#2"]
    rt.close()


def test_제목을_모르면_계정만으로_확정하지_않는다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    rt.publications.mark(
        "명령첨부", 1, "hashC", "uncertain", "submitting", account="user0", cafe="고요한 아침"
    )
    monkeypatch.setattr(
        reconcile_mod.api_articles,
        "board_histories",
        lambda c, cafe_id, **k: [_history_row(title="전혀 다른 글", status="DONE")],
    )
    result = reconcile_mod.reconcile(rt)
    assert result["done"] == 0 and result["unresolved"] == ["명령첨부#1"]
    rt.close()


# --------------------------------------------------------------------
# M-1 / M-4: 모의 실행 오프라인, 원본 전부 실패
# --------------------------------------------------------------------
def _sheet_runtime(rt, monkeypatch):
    rt.sources_cfg = {
        "sources": [{"name": "일상글목록", "kind": "sheet", "spreadsheet_id": "SID", "gid": 0}]
    }

    def boom(url, timeout=20):
        raise sheets.SourceError("네트워크 호출 금지")

    monkeypatch.setattr(sheets, "fetch_csv", boom)


def test_모의실행은_캐시만_쓰고_네트워크를_부르지_않는다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    _sheet_runtime(rt, monkeypatch)
    rt.sources_cache.put(
        "일상글목록", {"rows": [{"제목": "캐시 글", "본문": "첫 줄\n둘째 줄"}]}
    )
    spec = TaskSpec(task="publish_daily", cafe="고요한 아침", dry_run=True)
    picked = publish_mod.prepare_manuscripts(rt, spec)
    assert [m.title for m in picked] == ["캐시 글"]
    rt.close()


def test_캐시가_없으면_시트_동기화를_안내한다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    _sheet_runtime(rt, monkeypatch)
    spec = TaskSpec(task="publish_daily", cafe="고요한 아침", dry_run=True)
    out = worker._run_publish(rt, 1, spec)
    assert out["ok"] is False  # 전부 실패를 완료로 보고하지 않는다 (M-4)
    assert any("시트 동기화" in (s.get("reason") or "") for s in out["skipped"])
    rt.close()


# --------------------------------------------------------------------
# M-2 / M-3 / M-5 / M-7 / M-9
# --------------------------------------------------------------------
def test_다른_작업의_uncertain은_이번_작업_상태에_영향이_없다(tmp_path):
    rt = make_runtime(tmp_path)
    rt.publications.mark("옛날시트", 9, "oldhash", "uncertain", "submitting")
    spec = make_spec(dry_run=True)
    out = worker._run_publish(rt, 1, spec)
    assert out["uncertain"] == [] and out["ok"] is True
    rt.close()


def test_조회성_명령은_여러_번_등록된다(tmp_path):
    rt = make_runtime(tmp_path)
    a = worker.handle_text(rt, "상태 알려줘")
    b = worker.handle_text(rt, "상태 알려줘")
    assert a["job_id"] != b["job_id"]
    rt.close()


def test_실패한_발행_작업은_다시_등록할_수_있다(tmp_path):
    rt = make_runtime(tmp_path)
    first = worker.handle_text(rt, "일상 글 3개 올려")
    rt.jobs.finish(first["job_id"], "failed", None, "테스트 실패")
    second = worker.handle_text(rt, "일상 글 3개 올려")
    assert second["job_id"] != first["job_id"]
    rt.close()


def test_리스가_끊긴_running_작업을_정리한다(tmp_path):
    rt = make_runtime(tmp_path)
    worker.handle_text(rt, "상태 알려줘")
    job = rt.jobs.acquire("다른PC", lease_seconds=1)
    rt.conn.execute(
        "UPDATE jobs SET lease_until = ? WHERE id = ?",
        ((datetime.now(KST) - timedelta(hours=1)).isoformat(timespec="seconds"), job["id"]),
    )
    assert rt.jobs.reap_stale_running() == 1
    assert rt.jobs.get(int(job["id"]))["status"] == "uncertain"
    rt.close()


def test_슬롯마다_heartbeat를_호출한다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    worker.handle_text(rt, "일상 글 3개 올려")
    job = rt.jobs.acquire("tester")
    beats: list[int] = []
    monkeypatch.setattr(
        rt.jobs, "heartbeat", lambda jid, owner, **k: beats.append(jid) or True
    )
    spec = make_spec(dry_run=True)
    worker._run_publish(rt, int(job["id"]), spec, "tester")
    assert len(beats) == 3
    rt.close()


def test_등록확인_보류는_실패가_아니라_미확정(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=False, count=1, cafe="태극")  # 테스트 카페 = 즉시 발행
    monkeypatch.setattr(publish_mod.api_articles, "create_article", lambda c, **k: "SRC-P")
    monkeypatch.setattr(publish_mod.api_articles, "get_article", lambda c, sid: {})
    monkeypatch.setattr(publish_mod.api_articles, "verify_article", lambda detail, **k: [])

    def pending(*a, **k):
        raise publish_mod.api_articles.PendingError("예약이 멉니다")

    monkeypatch.setattr(publish_mod.api_articles, "wait_written", pending)

    out = publish_mod.run_slot(rt, spec, _one_slot(rt, spec))
    assert out["status"] == "pending" and out["source_id"] == "SRC-P"
    row = rt.publications.by_source_id("SRC-P")
    assert row["status"] == "uncertain" and row["stage"] == "written_pending"
    assert row["url"]
    rt.close()


def test_중지는_큐를_기다리지_않고_즉시_처리된다(tmp_path):
    rt = make_runtime(tmp_path)
    worker.handle_text(rt, "일상 글 3개 올려")
    out = worker.handle_text(rt, "중지")
    assert out["ok"] and out.get("stopped") and out["cancelled"] >= 1
    assert worker.stop_flag_path(rt).exists()
    assert worker.stop_requested(rt) is True
    assert rt.jobs.recent(5)[-1]["status"] == "cancelled"
    rt.close()


def test_중지_요청이_있으면_남은_슬롯을_건너뛴다(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path)
    spec = make_spec(dry_run=True)
    calls: list[str] = []

    def fake_slot(rt_, spec_, slot, **kwargs):
        calls.append(slot.manuscript.title)
        worker.request_stop(rt_)  # 첫 슬롯 실행 중 중지 요청이 들어왔다
        return {"status": "planned", "title": slot.manuscript.title}

    monkeypatch.setattr(publish_mod, "run_slot", fake_slot)
    out = worker._run_publish(rt, 1, spec)
    assert len(calls) == 1
    assert out.get("stopped") is True and out["slots"] == 3
    rt.close()
