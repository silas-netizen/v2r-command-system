"""인증(PoW 챌린지·로그인) 테스트."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from v2r.api import auth
from v2r.api.errors import V2RApiError

BASE = "https://api-test.example"


@pytest.fixture()
def device(tmp_path) -> auth.DeviceProfile:
    return auth.DeviceProfile.load(tmp_path / "device.json")


@pytest.fixture()
def session(tmp_path, device) -> auth.AuthSession:
    return auth.AuthSession(device=device, session_path=tmp_path / "session.json")


@pytest.fixture()
def client():
    with httpx.Client(base_url=BASE) as c:
        yield c


def test_device_profile_keys_and_stability(tmp_path) -> None:
    path = tmp_path / "device.json"
    first = auth.DeviceProfile.load(path)
    assert sorted(first.fingerprint) == sorted(auth.FINGERPRINT_KEYS)
    assert first.fingerprint["webdriver"] is False
    assert first.fingerprint["platform"] == "Win32"
    assert first.fingerprint["timezone"] == "Asia/Seoul"
    assert first.fingerprint["version"] == "v1"
    second = auth.DeviceProfile.load(path)
    assert second.device_id == first.device_id
    assert second.fp_hash() == first.fp_hash()


def test_fp_hash_matches_sorted_json(device) -> None:
    expected = hashlib.sha256(
        json.dumps(
            {k: device.fingerprint[k] for k in sorted(device.fingerprint)},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    assert device.fp_hash() == expected


def test_signal_headers(device) -> None:
    headers = device.signal_headers()
    assert headers["X-Device-Id"] == device.device_id
    assert headers["X-Browser-Signal-Status"] == "ok"
    assert json.loads(headers["X-Browser-Signal"])["platform"] == "Win32"


@pytest.mark.parametrize("difficulty", [1, 2])
def test_solve_pow(difficulty: int) -> None:
    challenge = {
        "version": "v1",
        "nonce": "abc",
        "timestamp": 1700000000,
        "difficulty": difficulty,
        "signature": "sig",
    }
    fp = "f" * 64
    n = auth.solve_pow(challenge, auth.LOGIN_PATH, fp)
    msg = f"v1:{auth.LOGIN_PATH}:{fp}:abc:1700000000:{n}"
    assert hashlib.sha256(msg.encode()).hexdigest().startswith("0" * difficulty)
    # 최소값인지 확인
    for smaller in range(n):
        m = f"v1:{auth.LOGIN_PATH}:{fp}:abc:1700000000:{smaller}"
        assert not hashlib.sha256(m.encode()).hexdigest().startswith("0" * difficulty)


def test_build_proof_keys() -> None:
    challenge = {
        "version": "v1",
        "nonce": "abc",
        "timestamp": 1,
        "difficulty": 1,
        "signature": "sig",
    }
    proof = json.loads(auth.build_proof(challenge, auth.LOGIN_PATH, "fp", 7))
    assert set(proof) == {
        "version",
        "nonce",
        "timestamp",
        "difficulty",
        "signature",
        "path",
        "fp_hash",
        "proof_nonce",
    }
    assert proof["path"] == auth.LOGIN_PATH
    assert proof["proof_nonce"] == 7


def test_login_plain_success(httpx_mock, session, client) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/auths/login",
        json={"token": {"access_token": "TOK-1"}},
    )
    assert session.login(client, "a@b.c", "pw") == "TOK-1"
    assert session.token == "TOK-1"
    saved = json.loads(session.session_path.read_text(encoding="utf-8"))
    assert saved["access_token"] == "TOK-1"
    request = httpx_mock.get_requests()[0]
    assert "multipart/form-data" in request.headers["content-type"]
    assert request.headers["X-Device-Id"] == session.device.device_id


def test_login_challenge_flow(httpx_mock, session, client) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/auths/login",
        status_code=401,
        json={"error": {"code": "CHALLENGE_REQUIRED", "reason": "x", "extra": {}}},
    )
    httpx_mock.add_response(
        url=f"{BASE}/auths/challenge",
        json={
            "challenge": {
                "version": "v1",
                "nonce": "n1",
                "timestamp": 123,
                "difficulty": 1,
                "signature": "sig",
            }
        },
    )
    httpx_mock.add_response(
        url=f"{BASE}/auths/login",
        json={"token": {"access_token": "TOK-2"}},
    )
    assert session.login(client, "a@b.c", "pw") == "TOK-2"
    last = httpx_mock.get_requests()[-1]
    proof = json.loads(last.headers["X-Challenge-Proof"])
    assert proof["fp_hash"] == session.device.fp_hash()
    assert proof["signature"] == "sig"


def test_login_rate_limited(httpx_mock, session, client) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/auths/login",
        status_code=429,
        json={"error": {"code": "TOO_MANY", "extra": {"retry_after": 120}}},
    )
    with pytest.raises(V2RApiError) as exc:
        session.login(client, "a@b.c", "pw")
    assert exc.value.kind == "rate_limited"
    assert exc.value.retry_after == 120


def test_invalidate_removes_file(httpx_mock, session, client) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/auths/login", json={"token": {"access_token": "T"}}
    )
    session.login(client, "a@b.c", "pw")
    session.invalidate()
    assert session.token is None
    assert not session.session_path.exists()
