"""저장소 테스트."""

import sqlite3
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


def test_publications_exists_hash_is_global(conn):
    """본문 해시 전역 검사 — 다른 파일·다른 행이라도 본문이 같으면 이미 발행됨."""
    pub = PublicationStore(conn)
    assert pub.exists_hash("hash-a") is False
    assert pub.exists_hash("") is False

    pub.mark("각색_전체_1", 2, "hash-a", "done", "done")
    assert pub.exists_hash("hash-a") is True
    # 같은 본문이 다른 파일 다른 행으로 들어와도 (파일, 행) 검사는 통과한다
    assert pub.exists("각색_전체_2", 77, "hash-a") is False
    assert pub.exists_hash("hash-a") is True

    pub.mark("각색_전체_2", 5, "hash-b", "uncertain", "submitting")
    assert pub.exists_hash("hash-b") is True

    pub.mark("각색_전체_2", 6, "hash-c", "skipped")
    assert pub.exists_hash("hash-c") is False  # 건너뛴 글은 다시 쓸 수 있다


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


# --------------------------------------------------------------------
# DB 잠금 재시도 (사고 2026-09-26: publish_daily가 database is locked 한 번에
# 통째로 failed 됐다 — publications.mark()가 지수 백오프로 다시 쓰게 한다)
# --------------------------------------------------------------------
def test_retry_on_lock_retries_then_succeeds():
    from v2r.store import publications as pub_mod

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    out = pub_mod.retry_on_lock(flaky, base_delay=0)
    assert out == "ok"
    assert calls["n"] == 3


def test_retry_on_lock_gives_up_after_max_retries():
    from v2r.store import publications as pub_mod

    calls = {"n": 0}

    def always_locked():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        pub_mod.retry_on_lock(always_locked, max_retries=3, base_delay=0)
    assert calls["n"] == 3


def test_retry_on_lock_does_not_retry_other_operational_errors():
    from v2r.store import publications as pub_mod

    calls = {"n": 0}

    def bad_sql():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: bogus")

    with pytest.raises(sqlite3.OperationalError):
        pub_mod.retry_on_lock(bad_sql, base_delay=0)
    assert calls["n"] == 1  # 잠금이 아니면 다시 시도하지 않고 바로 올린다


class _FlakyConn:
    """`conn.execute`를 감싸 `INSERT INTO publications` 첫 시도만 잠금으로 실패시킨다.

    `sqlite3.Connection`은 C 확장 타입이라 인스턴스 속성을 monkeypatch로 못 바꾼다
    (읽기 전용) — 그래서 얇은 프록시로 감싼다.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.calls = 0

    def execute(self, sql, params=()):
        if "INSERT INTO publications" in sql:
            self.calls += 1
            if self.calls < 2:
                raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_mark_retries_on_lock_then_writes(conn):
    """`mark()`가 잠금 오류를 겪어도(모의) 재시도 뒤 결국 기록한다."""
    flaky = _FlakyConn(conn)
    pub = PublicationStore(flaky)
    # 첫 시도만 잠금이므로 백오프 대기는 한 번(기본 0.2초)뿐이다 — 그대로 둔다.
    pub.mark("일상글목록", 9, "h9", "done", "done")
    assert flaky.calls == 2
    assert pub.exists("일상글목록", 9, "h9") is True


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


def test_account_state_naive_datetime(conn):
    """naive datetime은 KST로 간주하며 TypeError로 죽지 않는다."""
    acc = AccountStateStore(conn)
    acc.restrict("abc03", datetime(2030, 1, 1))
    assert acc.is_restricted("abc03") is True
    assert acc.is_restricted("abc03", datetime(2030, 1, 2)) is False
    assert acc.is_restricted("abc03", datetime(2029, 12, 31, tzinfo=KST)) is True
    # 해석 불가 값은 보수적으로 "제한 중"
    conn.execute(
        "UPDATE account_state SET restricted_until = '언젠가' WHERE login_id = 'abc03'"
    )
    assert acc.is_restricted("abc03") is True


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
