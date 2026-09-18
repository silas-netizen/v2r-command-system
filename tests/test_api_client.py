"""클라이언트 레이트 게이트·캐시·재시도 테스트."""

from __future__ import annotations

import time

import httpx
import pytest

from v2r.api import client as client_mod
from v2r.api.auth import AuthSession, DeviceProfile
from v2r.api.client import V2RClient, clear_cache, field, walk_dicts
from v2r.api.errors import V2RApiError

BASE = "https://api-test.example"


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture()
def auth(tmp_path) -> AuthSession:
    session = AuthSession(
        device=DeviceProfile.load(tmp_path / "device.json"),
        session_path=tmp_path / "session.json",
    )
    session.token = "TOK"
    return session


@pytest.fixture()
def api(auth):
    http = httpx.Client(base_url=BASE, timeout=60.0, headers={"Accept": "application/json"})
    c = V2RClient(base_url=BASE, auth=auth, client=http)
    yield c
    c.close()


def test_walk_dicts_and_field() -> None:
    data = {"a": [{"cafe_id": 1}, {"x": {"cafeId": 2}}]}
    ids = [field(d, "cafe_id", "cafeId") for d in walk_dicts(data)]
    assert [i for i in ids if i] == [1, 2]
    assert field({"b": None, "c": 3}, "b", "c") == 3


def test_rate_gate_enforces_interval(httpx_mock, api, monkeypatch) -> None:
    monkeypatch.setattr(client_mod, "RATE_INTERVAL", 0.2)
    httpx_mock.add_response(url=f"{BASE}/x", json={"ok": 1}, is_reusable=True)
    start = time.monotonic()
    api.get("/x")
    api.get("/x")
    assert time.monotonic() - start >= 0.15


def test_get_cache_for_listed_paths(httpx_mock, api) -> None:
    httpx_mock.add_response(url=f"{BASE}/navers/accounts", json={"n": 1})
    assert api.get("/navers/accounts") == {"n": 1}
    assert api.get("/navers/accounts") == {"n": 1}
    assert len(httpx_mock.get_requests()) == 1


def test_uncached_path_hits_server_twice(httpx_mock, api) -> None:
    httpx_mock.add_response(url=f"{BASE}/other", json={"n": 1}, is_reusable=True)
    api.get("/other")
    api.get("/other")
    assert len(httpx_mock.get_requests()) == 2


def test_token_expired_relogin_once(httpx_mock, api, monkeypatch) -> None:
    monkeypatch.setenv("V2R_EMAIL", "a@b.c")
    monkeypatch.setenv("V2R_PASSWORD", "pw")
    monkeypatch.setattr(
        "v2r.api.auth.credentials", lambda: ("a@b.c", "pw")
    )
    httpx_mock.add_response(
        url=f"{BASE}/x", status_code=403, text='{"error":{"code":"TOKEN_ERROR"}}'
    )
    httpx_mock.add_response(
        url=f"{BASE}/auths/login", json={"token": {"access_token": "TOK2"}}
    )
    httpx_mock.add_response(url=f"{BASE}/x", json={"ok": True})
    assert api.get("/x") == {"ok": True}
    assert api.auth.token == "TOK2"
    last = httpx_mock.get_requests()[-1]
    assert last.headers["Authorization"] == "Bearer TOK2"


def test_token_expired_only_once(httpx_mock, api, monkeypatch) -> None:
    monkeypatch.setattr("v2r.api.auth.credentials", lambda: ("a@b.c", "pw"))
    httpx_mock.add_response(
        url=f"{BASE}/x",
        status_code=403,
        text='{"error":{"code":"TOKEN_ERROR"}}',
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=f"{BASE}/auths/login",
        json={"token": {"access_token": "TOK2"}},
        is_reusable=True,
    )
    with pytest.raises(V2RApiError) as exc:
        api.get("/x")
    assert exc.value.kind == "token_expired"


def test_retry_after_header_preferred(httpx_mock, api, monkeypatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: waits.append(s))
    httpx_mock.add_response(
        url=f"{BASE}/x", status_code=429, headers={"Retry-After": "3"}, json={}
    )
    httpx_mock.add_response(url=f"{BASE}/x", json={"ok": 1})
    assert api.get("/x") == {"ok": 1}
    assert 3.0 in waits


def test_retry_after_capped(httpx_mock, api, monkeypatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: waits.append(s))
    httpx_mock.add_response(
        url=f"{BASE}/x", status_code=503, headers={"Retry-After": "5000"}, json={}
    )
    httpx_mock.add_response(url=f"{BASE}/x", json={"ok": 1})
    api.get("/x")
    assert max(waits) == 900.0


def test_server_error_gives_up_after_three_attempts(httpx_mock, api, monkeypatch) -> None:
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    httpx_mock.add_response(url=f"{BASE}/x", status_code=500, json={}, is_reusable=True)
    with pytest.raises(V2RApiError) as exc:
        api.get("/x")
    assert exc.value.kind == "server"
    assert len([r for r in httpx_mock.get_requests() if r.url.path == "/x"]) == 3


def test_post_is_not_retried_on_server_error(httpx_mock, api, monkeypatch) -> None:
    """비멱등 요청은 한 번만 보내고 kind='ambiguous'로 올린다."""
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    httpx_mock.add_response(url=f"{BASE}/y", status_code=503, json={}, is_reusable=True)
    with pytest.raises(V2RApiError) as exc:
        api.post("/y", json={"a": 1})
    assert exc.value.kind == "ambiguous"
    assert len([r for r in httpx_mock.get_requests() if r.url.path == "/y"]) == 1


def test_post_can_opt_into_retry(httpx_mock, api, monkeypatch) -> None:
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    httpx_mock.add_response(url=f"{BASE}/y", status_code=503, json={})
    httpx_mock.add_response(url=f"{BASE}/y", json={"ok": 1})
    assert api.post("/y", json={"a": 1}, idempotent=True) == {"ok": 1}


def test_post_rate_limited_is_not_retried(httpx_mock, api, monkeypatch) -> None:
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    httpx_mock.add_response(url=f"{BASE}/y", status_code=429, json={}, is_reusable=True)
    with pytest.raises(V2RApiError) as exc:
        api.post("/y", json={"a": 1})
    assert exc.value.kind == "rate_limited"
    assert len([r for r in httpx_mock.get_requests() if r.url.path == "/y"]) == 1


def test_cache_returns_copy(httpx_mock, api) -> None:
    httpx_mock.add_response(url=f"{BASE}/navers/accounts", json={"n": {"deep": 1}})
    first = api.get("/navers/accounts")
    first["n"]["deep"] = 999
    first["extra"] = "오염"
    assert api.get("/navers/accounts") == {"n": {"deep": 1}}


def test_custom_headers_survive_retry(httpx_mock, api, monkeypatch) -> None:
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    httpx_mock.add_response(url=f"{BASE}/x", status_code=500, json={})
    httpx_mock.add_response(url=f"{BASE}/x", json={"ok": 1})
    api.get("/x", headers={"X-Custom": "keep"})
    assert all(
        r.headers.get("X-Custom") == "keep"
        for r in httpx_mock.get_requests()
        if r.url.path == "/x"
    )


def test_non_get_adds_signal_headers(httpx_mock, api) -> None:
    httpx_mock.add_response(url=f"{BASE}/y", json={"ok": 1})
    api.post("/y", json={"a": 1})
    req = httpx_mock.get_requests()[-1]
    assert req.headers["X-Device-Id"] == api.auth.device.device_id
    assert req.headers["X-Browser-Signal-Status"] == "ok"
