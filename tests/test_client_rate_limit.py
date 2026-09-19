"""장기 레이트 제한(429)은 재시도하지 않고 바로 올린다 (장애 2026-09-19).

로그인 횟수 제한(`RATE_LIMIT_LOGIN`, 하루 20회/지문)은 몇 시간짜리 제한이라
그 자리에서 10초·30초 재시도를 돌리면 남은 슬롯만 줄줄이 태운다.
"""

from __future__ import annotations

import httpx
import pytest

from v2r.api.auth import AuthSession, DeviceProfile
from v2r.api.client import V2RClient, clear_cache
from v2r.api.errors import V2RApiError

BASE = "https://api-test.example"

RATE_LIMIT_LOGIN_BODY = {
    "error": {
        "code": 160,
        "reason": "RATE_LIMIT_LOGIN",
        "extra": {
            "scope": "daily_fingerprint",
            "retry_after": 43459,
            "max_attempts": 20,
        },
    }
}


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """재시도가 일어나면 테스트가 멈추지 않도록 잠을 없앤다."""
    monkeypatch.setattr("v2r.api.client.time.sleep", lambda *_: None)


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
    http = httpx.Client(base_url=BASE, timeout=60.0)
    c = V2RClient(base_url=BASE, auth=auth, client=http)
    yield c
    c.close()


def test_login_rate_limit_raises_immediately(httpx_mock, api) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/navers/accounts", status_code=429, json=RATE_LIMIT_LOGIN_BODY
    )
    with pytest.raises(V2RApiError) as exc:
        api.get("/navers/accounts")
    assert exc.value.kind == "rate_limited_long"
    assert exc.value.retry_after == 43459
    assert len(httpx_mock.get_requests()) == 1  # 재시도 없음


def test_long_retry_after_without_reason_is_long(httpx_mock, api) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/some/path",
        status_code=429,
        json={"error": {"code": "X", "extra": {"retry_after": 3600}}},
    )
    with pytest.raises(V2RApiError) as exc:
        api.get("/some/path")
    assert exc.value.kind == "rate_limited_long"
    assert len(httpx_mock.get_requests()) == 1


def test_short_rate_limit_still_retries(httpx_mock, api) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/some/path",
        status_code=429,
        json={"error": {"code": "X", "extra": {"retry_after": 5}}},
    )
    httpx_mock.add_response(url=f"{BASE}/some/path", json={"ok": True})
    assert api.get("/some/path") == {"ok": True}
    assert len(httpx_mock.get_requests()) == 2


def test_post_long_limit_not_marked_ambiguous(httpx_mock, api) -> None:
    """비멱등 POST도 429 장기 제한은 '처리됐을 수도'가 아니라 확실한 거부다."""
    httpx_mock.add_response(
        url=f"{BASE}/naver_cafe_articles", status_code=429, json=RATE_LIMIT_LOGIN_BODY
    )
    with pytest.raises(V2RApiError) as exc:
        api.post("/naver_cafe_articles", json={"a": 1})
    assert exc.value.kind == "rate_limited_long"
    assert len(httpx_mock.get_requests()) == 1
