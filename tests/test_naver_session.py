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
