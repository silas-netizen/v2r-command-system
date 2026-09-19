"""토큰 파일 공유·갱신·로그인 예산 (장애 2026-09-19 대응).

서버는 기기 지문당 하루 20회만 로그인시켜 준다. 여기서는
1) 토큰이 파일 하나로 공유·재사용되는지,
2) 만료 임박이면 로그인 대신 갱신하는지,
3) 자체 한도(15회)에서 스스로 멈추는지를 지킨다.
"""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from v2r.api import auth
from v2r.api.errors import V2RApiError

BASE = "https://api-test.example"


def make_token(exp_in: float, name: str = "T") -> str:
    """`exp`만 담은 가짜 JWT(서명 검증은 하지 않는다)."""

    def seg(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{seg({'alg': 'HS256'})}.{seg({'exp': time.time() + exp_in, 'n': name})}.sig"


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


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    """실제 .env 값을 쓰지 않도록 고정한다."""
    monkeypatch.setattr(auth, "credentials", lambda: ("a@b.c", "pw"))


# ---- 만료 해독 ----
def test_decode_exp_reads_jwt_exp() -> None:
    token = make_token(3600)
    exp = auth.decode_exp(token)
    assert exp is not None and 3500 < exp - time.time() < 3700


@pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.b", "a.!!!.c"])
def test_decode_exp_returns_none_for_non_jwt(bad: str) -> None:
    assert auth.decode_exp(bad) is None


def test_expiring_soon_unknown_expiry_is_reused(session) -> None:
    session.token = "OPAQUE"
    session.expires_at = None
    assert session.expiring_soon() is False


def test_expiring_soon_within_skew(session) -> None:
    session.expires_at = time.time() + 60
    assert session.expiring_soon() is True
    session.expires_at = time.time() + 3600
    assert session.expiring_soon() is False


# ---- 파일 저장·재적재 ----
def test_login_writes_shared_file_with_expiry_and_cookie(
    httpx_mock, session, client, device, tmp_path
) -> None:
    token = make_token(3600)
    httpx_mock.add_response(
        url=f"{BASE}/auths/login",
        json={"token": {"access_token": token}},
        headers={"set-cookie": "refresh_token=RCOOKIE; Path=/auths; HttpOnly"},
    )
    assert session.login(client, "a@b.c", "pw") == token

    saved = json.loads(session.session_path.read_text(encoding="utf-8"))
    assert saved["access_token"] == token
    assert saved["refresh_token"] == "RCOOKIE"
    assert saved["expires_at"] == pytest.approx(auth.decode_exp(token))
    assert saved["obtained_at"] > 0

    # 다른 프로세스 흉내: 같은 경로로 새 세션 → 로그인 없이 그대로 재사용
    other = auth.AuthSession(device=device, session_path=session.session_path)
    assert other.token == token
    assert other.refresh_token == "RCOOKIE"
    assert other.ensure_token(client) == token
    assert len(httpx_mock.get_requests()) == 1  # 추가 로그인 없음


def test_save_file_is_atomic_and_leaves_no_temp(session) -> None:
    session._save_file(make_token(3600), refresh_token="R")
    session._save_file(make_token(7200), refresh_token="R2")
    leftovers = [p.name for p in session.session_path.parent.glob("session.json.*")]
    assert leftovers == []
    assert session.session_path.exists()
    assert json.loads(session.session_path.read_text(encoding="utf-8"))["refresh_token"] == "R2"


def test_load_file_ignores_garbage(session) -> None:
    session.session_path.write_text("{not json", encoding="utf-8")
    assert session._load_file() is None


# ---- 갱신 ----
def test_ensure_token_refreshes_instead_of_login(httpx_mock, session, client) -> None:
    old = make_token(60, "old")  # 만료 5분 이내 → 갱신 대상
    new = make_token(3600, "new")
    session._save_file(old, refresh_token="RCOOKIE")
    httpx_mock.add_response(
        url=f"{BASE}/auths/refresh_token",
        json={"token": {"access_token": new}},
    )

    assert session.ensure_token(client) == new
    requests = httpx_mock.get_requests()
    assert [str(r.url) for r in requests] == [f"{BASE}/auths/refresh_token"]
    body = json.loads(requests[0].content)
    assert body == {"access_token": old, "refresh_token": None}
    assert "refresh_token=RCOOKIE" in requests[0].headers.get("cookie", "")
    # 새 토큰이 파일에도 반영된다
    assert json.loads(session.session_path.read_text(encoding="utf-8"))["access_token"] == new


def test_refresh_4xx_falls_back_to_login(httpx_mock, session, client) -> None:
    old = make_token(60, "old")
    fresh = make_token(3600, "fresh")
    session._save_file(old, refresh_token="RCOOKIE")
    httpx_mock.add_response(url=f"{BASE}/auths/refresh_token", status_code=401, json={})
    httpx_mock.add_response(
        url=f"{BASE}/auths/login", json={"token": {"access_token": fresh}}
    )

    assert session.ensure_token(client) == fresh
    assert session.login_count_today() == 1


def test_refresh_without_access_token_in_body_falls_back(httpx_mock, session, client) -> None:
    session._save_file(make_token(60), refresh_token="R")
    httpx_mock.add_response(url=f"{BASE}/auths/refresh_token", json={"ok": True})
    httpx_mock.add_response(
        url=f"{BASE}/auths/login", json={"token": {"access_token": "PLAIN"}}
    )
    assert session.ensure_token(client) == "PLAIN"


def test_refresh_does_not_log_secrets(httpx_mock, session, client, caplog) -> None:
    session._save_file(make_token(60), refresh_token="SECRET-COOKIE")
    new = make_token(3600, "new")
    httpx_mock.add_response(
        url=f"{BASE}/auths/refresh_token", json={"token": {"access_token": new}}
    )
    with caplog.at_level("INFO", logger="v2r.api.auth"):
        session.refresh(client)
    text = caplog.text
    assert "SECRET-COOKIE" not in text and new not in text
    assert "token.access_token" in text  # 형태(키)는 남는다


# ---- 로그인 예산 ----
def test_login_budget_refuses_over_limit(httpx_mock, session, client) -> None:
    now = time.time()
    session.login_log_path.write_text(
        json.dumps({"logins": [now - 60 * i for i in range(auth.LOGIN_BUDGET_PER_DAY)]}),
        encoding="utf-8",
    )
    assert session.login_count_today() == auth.LOGIN_BUDGET_PER_DAY
    with pytest.raises(V2RApiError) as exc:
        session.login(client, "a@b.c", "pw")
    assert exc.value.kind == "login_budget"
    assert "로그인" in str(exc.value)
    assert httpx_mock.get_requests() == []  # 서버를 아예 부르지 않았다


def test_login_budget_ignores_old_entries(httpx_mock, session, client) -> None:
    stale = time.time() - auth.LOGIN_WINDOW_S - 10
    session.login_log_path.write_text(
        json.dumps({"logins": [stale] * 50}), encoding="utf-8"
    )
    assert session.login_count_today() == 0
    httpx_mock.add_response(url=f"{BASE}/auths/login", json={"token": {"access_token": "T"}})
    assert session.login(client, "a@b.c", "pw") == "T"
    assert session.login_count_today() == 1


# ---- 24시간 유지: 하루 로그인 1회 ----
class FakeClock:
    """테스트용 가짜 시계."""

    def __init__(self, start: float | None = None) -> None:
        # httpx 쿠키 항아리는 진짜 시계로 Max-Age를 만료로 바꾼다 → 같은 지점에서 출발한다
        self.now = time.time() if start is None else start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_26시간_틱을_돌려도_로그인은_한_번뿐(httpx_mock, session, client, monkeypatch):
    """30분 틱 × 26시간. 액세스 토큰은 갱신으로 잇고, 로그인은 쿠키 만료 직전 1회."""
    clock = FakeClock()
    monkeypatch.setattr(auth, "_now", clock)

    def token_for(exp_at: float) -> str:
        def seg(data: dict) -> str:
            raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

        return f"{seg({'alg': 'HS256'})}.{seg({'exp': exp_at})}.sig"

    logins: list[float] = []
    refreshes: list[float] = []

    def on_login(request):
        logins.append(clock.now)
        return httpx.Response(
            200,
            json={"token": {"access_token": token_for(clock.now + 3600)}},
            headers={
                "set-cookie": (
                    f"refresh_token=RC{len(logins)}; Path=/auths; "
                    f"Max-Age={int(auth.REFRESH_COOKIE_TTL_S)}; HttpOnly"
                )
            },
        )

    def on_refresh(request):
        refreshes.append(clock.now)
        return httpx.Response(
            200, json={"token": {"access_token": token_for(clock.now + 3600)}}
        )

    httpx_mock.add_callback(on_login, url=f"{BASE}/auths/login", is_reusable=True)
    httpx_mock.add_callback(on_refresh, url=f"{BASE}/auths/refresh_token", is_reusable=True)

    # 첫 기동: 토큰이 없으니 한 번 로그인한다
    assert session.maintain(client) == "login"
    assert len(logins) == 1
    start = clock.now

    # 30분마다 26시간치 점검
    actions: list[str] = []
    for _ in range(int(26 * 3600 / auth.MAINTAIN_TICK_S)):
        clock.advance(auth.MAINTAIN_TICK_S)
        actions.append(session.maintain(client))

    assert actions.count("login") == 1, actions  # 24시간 안에 딱 한 번
    assert refreshes, "액세스 토큰은 갱신으로 이어져야 한다"
    # 그 한 번은 쿠키 만료(로그인 + 24h) 1시간 전쯤이다
    second = logins[1]
    assert (start + auth.REFRESH_COOKIE_TTL_S) - second <= auth.REFRESH_COOKIE_RENEW_S
    assert session.login_count_today() <= 2

    # 언제나 쓸 수 있는 토큰이 남아 있다
    assert session.token and not session.expiring_soon()


def test_maintain_keeps_quiet_when_nothing_is_due(httpx_mock, session, client, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(auth, "_now", clock)
    session._save_file(
        make_token(9999),  # make_token은 진짜 시계를 쓰므로 아래에서 덮어쓴다
        refresh_token="RC",
        refresh_expires_at=clock.now + 20 * 3600,
    )
    session.expires_at = clock.now + 10 * 3600
    session._save_file(
        session.token,
        refresh_token="RC",
        expires_at=clock.now + 10 * 3600,
        refresh_expires_at=clock.now + 20 * 3600,
    )
    assert session.maintain(client) == "keep"
    assert httpx_mock.get_requests() == []


def test_report_has_no_secrets(session) -> None:
    session._save_file(make_token(3600), refresh_token="SECRET", refresh_expires_at=1.0)
    info = session.report()
    assert info["has_token"] is True and info["has_refresh_cookie"] is True
    assert "SECRET" not in json.dumps(info)
    assert session.token not in json.dumps(info)
    assert info["login_budget"] == auth.LOGIN_BUDGET_PER_DAY
    assert info["logins_24h"] == 0


def test_session_report_text_is_korean_and_secret_free(session) -> None:
    from v2r.__main__ import session_report_text

    token = make_token(3600)
    session._save_file(token, refresh_token="SECRET", refresh_expires_at=time.time() + 20 * 3600)
    text = session_report_text(session.report())
    assert "액세스 토큰: 있음" in text and "분 남음" in text
    assert "갱신 쿠키: 있음" in text and ("19시간" in text or "20시간" in text)
    assert "최근 24시간 로그인: 0회 / 자체 한도 15회" in text
    assert "SECRET" not in text and token not in text


def test_maintain_session_never_raises(tmp_path, monkeypatch) -> None:
    """serve 루프는 세션 점검이 터져도 계속 돌아야 한다."""
    from v2r.engine import worker

    class Boom:
        @property
        def client(self):
            raise RuntimeError("네트워크 없음")

    assert worker.maintain_session(Boom()) == "error"


def test_login_attempts_are_counted(httpx_mock, session, client) -> None:
    httpx_mock.add_response(url=f"{BASE}/auths/login", json={"token": {"access_token": "T"}})
    httpx_mock.add_response(url=f"{BASE}/auths/login", json={"token": {"access_token": "T"}})
    session.login(client, "a@b.c", "pw")
    session.login(client, "a@b.c", "pw")
    assert session.login_count_today() == 2
    # 다른 프로세스도 같은 카운터를 본다
    assert auth.AuthSession(
        device=session.device, session_path=session.session_path
    ).login_count_today() == 2
