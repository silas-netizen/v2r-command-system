"""네이버 로그인 세션 유지 (2026-09-21). 브라우저는 열지 않는다."""

from __future__ import annotations

import json

import pytest

from v2r.command.parser import parse_korean_command
from v2r.engine import worker
from v2r.engine.sidecar import is_light
from v2r.warehouse import naver_session as ns


def _c(name, domain=".naver.com", expires=-1):
    return {"name": name, "value": "x", "domain": domain, "path": "/", "expires": expires}


@pytest.mark.parametrize("text", ["네이버 세션 점검", "네이버 로그인 유지 점검", "네이버 로그인 유지"])
def test_naver_keepalive_patterns(text):
    spec = parse_korean_command(text)
    assert spec is not None and spec.task == "naver_keepalive", text
    assert is_light("naver_keepalive")  # 긴 발행 중에도 사이드카가 돌린다


def test_login_cookie_detection_only_by_name():
    assert ns.has_login_cookies([_c("NID_AUT"), _c("NID_SES", ".naver.com")])
    assert not ns.has_login_cookies([_c("NID_AUT")])
    assert not ns.has_login_cookies([_c("NID_AUT", ".google.com"), _c("NID_SES", ".google.com")])


def test_export_only_naver_cookies(tmp_path):
    cookies = [_c("NID_AUT", ".naver.com", 1e10), _c("NID_SES", "nid.naver.com"), _c("sid", ".google.com")]
    extra = tmp_path / "web-crawler" / "output" / "cafe.naver.com" / "cookies.json"
    n = ns.export_cookies(cookies, tmp_path / "data" / "naver_cookies.json", [extra])
    assert n == 2
    saved = json.loads((tmp_path / "data" / "naver_cookies.json").read_text(encoding="utf-8"))
    assert {c["name"] for c in saved["cookies"]} == {"NID_AUT", "NID_SES"}
    assert extra.exists()  # web-crawler 자리에도 복사
    assert ns.login_cookie_expiry(cookies) == 1e10


class _Ch:
    def __init__(self):
        self.sent = []

    def send(self, text, **kw):
        self.sent.append(text)


class _RT:
    channels = []


def test_naver_keepalive_notifies_when_logged_out(monkeypatch):
    sent = []
    monkeypatch.setattr(ns, "check_naver_session", lambda: {"ok": True, "logged_in": False, "note": "풀림"})
    monkeypatch.setattr(worker, "notify_all", lambda channels, text, **kw: sent.append(text))
    out = worker._naver_keepalive(_RT(), None)
    assert out["notified"] is True and sent and "naver-login.cmd" in sent[0]


def test_naver_keepalive_quiet_when_logged_in(monkeypatch):
    sent = []
    monkeypatch.setattr(ns, "check_naver_session", lambda: {"ok": True, "logged_in": True, "note": "연장"})
    monkeypatch.setattr(worker, "notify_all", lambda channels, text, **kw: sent.append(text))
    out = worker._naver_keepalive(_RT(), None)
    assert out["logged_in"] and not sent


def test_backup_and_restore_profile(tmp_path):
    prof = tmp_path / "browser-profile-naver"
    (prof / "Default" / "Cache").mkdir(parents=True)
    (prof / "Default" / "Cookies").write_text("c", encoding="utf-8")
    (prof / "Default" / "Cache" / "big").write_text("x" * 100, encoding="utf-8")
    assert ns.backup_profile(prof)
    bak = ns.backup_dir(prof)
    assert (bak / "Default" / "Cookies").exists()
    assert not (bak / "Default" / "Cache").exists()  # 캐시는 뺀다
    (prof / "Default" / "Cookies").write_text("broken", encoding="utf-8")
    assert ns.restore_profile(prof)
    assert (prof / "Default" / "Cookies").read_text(encoding="utf-8") == "c"
    assert (tmp_path / "browser-profile-naver.broken").exists()


def test_check_restores_from_backup_when_logged_out(tmp_path, monkeypatch):
    prof = tmp_path / "browser-profile-naver"
    prof.mkdir()
    ns.backup_dir(prof).mkdir()
    calls = []

    def fake_once(path):
        calls.append(1)
        return {"ok": True, "logged_in": len(calls) > 1, "note": "n", "expires_in_days": 30.0}

    monkeypatch.setattr(ns, "_check_once", fake_once)
    monkeypatch.setattr(ns, "backup_profile", lambda p: True)
    out = ns.check_naver_session(prof)
    assert out["logged_in"] and out["restored_from_backup"] and len(calls) == 2


def test_check_warns_when_extension_not_working(tmp_path, monkeypatch):
    prof = tmp_path / "browser-profile-naver"
    prof.mkdir()
    monkeypatch.setattr(ns, "_check_once", lambda p: {"ok": True, "logged_in": True, "note": "n", "expires_in_days": 1.5})
    monkeypatch.setattr(ns, "backup_profile", lambda p: True)
    out = ns.check_naver_session(prof)
    assert out["warnings"] and "naver-login.cmd" in out["warnings"][0]


def test_worker_notifies_warning_and_restore(monkeypatch):
    sent = []
    monkeypatch.setattr(ns, "check_naver_session", lambda: {"ok": True, "logged_in": True, "restored_from_backup": True, "warnings": ["w1"]})
    monkeypatch.setattr(worker, "notify_all", lambda channels, text, **kw: sent.append(text))
    worker._naver_keepalive(_RT(), None)
    assert any("백업" in t for t in sent) and any("w1" in t for t in sent)


# --- 프로세스 간 파일 잠금 (docs/reports/keyword-discovery-2026-09-22.md 3절) ----
def test_lock_acquire_and_release(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "_data_dir", lambda: tmp_path)
    lock = ns.acquire_profile_lock("test-purpose")
    path = ns.lock_file_path()
    assert path.exists()
    info = json.loads(path.read_text(encoding="utf-8"))
    assert info["purpose"] == "test-purpose"
    import os

    assert info["pid"] == os.getpid()
    lock.release()
    assert not path.exists()  # 자기 소유면 놓을 때 파일도 치운다


def test_lock_blocks_second_holder_until_release(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "_data_dir", lambda: tmp_path)
    first = ns.acquire_profile_lock("holder-1")
    with pytest.raises(ns.ProfileLockTimeout):
        ns.acquire_profile_lock("holder-2", max_wait=0.5)
    first.release()
    second = ns.acquire_profile_lock("holder-2", max_wait=5)
    second.release()


def test_lock_reclaims_dead_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "_data_dir", lambda: tmp_path)
    path = ns.lock_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 절대 쓰이지 않을 만큼 큰 가짜 pid를 '죽은 프로세스'로 기록해 둔다
    path.write_text(json.dumps({"pid": 999_999_999, "started_at": "x", "purpose": "dead"}), encoding="utf-8")
    lock = ns.acquire_profile_lock("new-holder", max_wait=5)
    info = json.loads(path.read_text(encoding="utf-8"))
    assert info["purpose"] == "new-holder"
    lock.release()


def test_launch_and_close_hold_lock(tmp_path, monkeypatch):
    """`_launch`/`_close`가 잠금을 자동으로 잡고 놓는지 실제 브라우저 없이 확인."""
    monkeypatch.setattr(ns, "_data_dir", lambda: tmp_path)

    class _FakeContext:
        pages: list = []

        def new_page(self):
            return object()

    class _FakePlaywright:
        class chromium:
            @staticmethod
            def launch_persistent_context(*a, **kw):
                return _FakeContext()

        def stop(self):
            pass

    class _FakeSyncAPI:
        @staticmethod
        def sync_playwright():
            class _S:
                def start(self_inner):
                    return _FakePlaywright()

            return _S()

    monkeypatch.setattr("playwright.sync_api.sync_playwright", _FakeSyncAPI.sync_playwright)
    path = tmp_path / "browser-profile-naver"
    playwright, context, page = ns._launch(path, headless=True, purpose="test-launch")
    assert ns.lock_file_path().exists()
    ns._close(playwright, context, page)
    assert not ns.lock_file_path().exists()  # close가 잠금을 놓았다
