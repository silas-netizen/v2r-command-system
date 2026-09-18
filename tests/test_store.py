"""저장소 테스트."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from v2r.command.spec import TaskSpec
from v2r.store.accounts_state import AccountStateStore
from v2r.store.db import connect, init_schema
from v2r.store.events import EventLog
from v2r.store.jobs import JobStore
from v2r.store.publications import PublicationStore
from v2r.store.sources_cache import SourceCache

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "test.sqlite")
    init_schema(c)
    yield c
    c.close()


def test_schema_tables(conn):
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "jobs",
        "publications",
        "account_state",
        "source_cache",
        "photo_usage",
        "executor_lease",
        "events",
    } <= names


def test_enqueue_idempotent(conn):
    store = JobStore(conn)
    spec = TaskSpec(task="publish_daily", count=3)
    a = store.enqueue(spec, "key-1")
    b = store.enqueue(spec, "key-1")
    assert a == b
    assert len(store.recent()) == 1
    c = store.enqueue(spec, "key-2")
    assert c != a
    assert len(store.recent()) == 2


def test_acquire_finish_and_lease(conn):
    store = JobStore(conn)
    store.enqueue(TaskSpec(task="status"), "k1")
    store.enqueue(TaskSpec(task="stop"), "k2")

    job = store.acquire("pc-a")
    assert job is not None and job["status"] == "running" and job["task"] == "status"

    # 다른 실행기는 리스를 못 잡는다
    assert store.acquire("pc-b") is None
    # 같은 주인은 다음 건을 잡는다
    job2 = store.acquire("pc-a")
    assert job2 is not None and job2["task"] == "stop"

    assert store.heartbeat(int(job["id"]), "pc-a") is True
    store.finish(int(job["id"]), "done", result={"ok": True})
    store.finish(int(job2["id"]), "done", result=None)

    row = store.get(int(job["id"]))
    assert row["status"] == "done"
    assert row["lease_owner"] is None
    assert '"ok": true' in row["result_json"]

    # 리스가 풀려 다른 실행기도 시도 가능(큐 비어 None)
    assert store.acquire("pc-b") is None


def test_finish_failed_and_cancel_open(conn):
    store = JobStore(conn)
    store.enqueue(TaskSpec(task="status"), "k1")
    job = store.acquire("pc-a")
    store.finish(int(job["id"]), "failed", None, "오류 발생")
    assert store.get(int(job["id"]))["error"] == "오류 발생"

    store.enqueue(TaskSpec(task="stop"), "k2")
    store.enqueue(TaskSpec(task="status"), "k3")
    store.acquire("pc-a")
    assert store.cancel_open("pc-a") == 2
    assert all(j["status"] != "queued" for j in store.recent())

    with pytest.raises(ValueError):
        store.finish(1, "이상한상태")


def test_publications_exists_includes_uncertain(conn):
    pub = PublicationStore(conn)
    assert pub.exists("일상글목록", 3, "h1") is False

    pub.mark("일상글목록", 3, "h1", "uncertain", "daily_submitting", account="abc01")
    assert pub.exists("일상글목록", 3, "h1") is True
    assert len(pub.list_uncertain()) == 1

    pub.mark("일상글목록", 3, "h1", "done", "done", url="https://x/1", source_id="s-1")
    assert pub.exists("일상글목록", 3, "h1") is True
    assert pub.list_uncertain() == []
    assert pub.count_done("일상글목록") == 1
    assert pub.by_source_id("s-1")["url"] == "https://x/1"

    pub.mark("일상글목록", 4, "h2", "skipped")
    assert pub.exists("일상글목록", 4, "h2") is False

    with pytest.raises(ValueError):
        pub.mark("일상글목록", 5, "h3", "done", None, 없는열="x")


def test_account_state_restrict_and_lru(conn):
    acc = AccountStateStore(conn)
    now = datetime(2026, 9, 19, 12, 0, tzinfo=KST)
    acc.touch_used("abc01", now - timedelta(days=2))
    acc.touch_used("abc02", now)

    assert acc.is_restricted("abc01", now) is False
    acc.restrict("abc01", now + timedelta(days=30), "27000", "계정 제한")
    assert acc.is_restricted("abc01", now) is True
    assert acc.is_restricted("abc01", now + timedelta(days=31)) is False
    assert acc.is_restricted("없는계정", now) is False

    used = acc.last_used_map()
    assert set(used) == {"abc01", "abc02"}
    assert used["abc01"] < used["abc02"]


def test_source_cache(conn):
    cache = SourceCache(conn)
    assert cache.get("일상글목록") is None
    cache.put("일상글목록", {"rows": [{"a": 1}]})
    assert cache.get("일상글목록") == {"rows": [{"a": 1}]}
    cache.put("일상글목록", {"rows": []}, status="stale")
    assert cache.meta("일상글목록")["status"] == "stale"
    assert cache.get("일상글목록") == {"rows": []}


def test_event_log(conn):
    store = JobStore(conn)
    job_id = store.enqueue(TaskSpec(task="status"), "k1")
    log = EventLog(conn)
    log.log(job_id, "info", "시작")
    log.log(job_id, "error", "실패")
    log.log(None, "info", "전역")
    assert len(log.recent(job_id)) == 2
    assert len(log.recent()) == 3
    assert log.recent(job_id)[0]["message"] == "실패"
