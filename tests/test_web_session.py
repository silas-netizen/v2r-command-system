"""Claude 플랫폼·Make 로그인 유지 (2026-09-21). 브라우저는 열지 않는다."""

from __future__ import annotations

import pytest

from v2r.command.parser import parse_korean_command
from v2r.engine import worker
from v2r.engine.sidecar import is_light
from v2r.warehouse import web_session as ws


@pytest.mark.parametrize("text", ["웹 세션 점검", "Make 로그인 유지 점검", "클로드 세션 점검"])
def test_web_keepalive_patterns(text):
    spec = parse_korean_command(text)
    assert spec is not None and spec.task == "web_keepalive", text
    assert is_light("web_keepalive")


def test_sites_table():
    assert set(ws.SITES) == {"claude", "make"}
    for site in ws.SITES.values():
        assert site.login_url.startswith("https://") and site.touch_url.startswith("https://")
        assert site.logged_in_markers and site.logged_out_markers


class _Page:
    def __init__(self, url, body):
        self.url, self._body = url, body

    def inner_text(self, sel):
        return self._body


def test_logged_out_detection():
    site = ws.SITES["make"]
    assert ws._logged_out(_Page("https://us2.make.com/login", ""), site)
    assert ws._logged_out(_Page("https://us2.make.com/495291/organization/dashboard", "아무것도"), site)
    assert not ws._logged_out(_Page("https://us2.make.com/495291/organization/dashboard", "Operations 1,200 / 10,000"), site)


class _RT:
    channels = []


def test_worker_notifies_only_logged_out_sites(monkeypatch):
    sent = []
    monkeypatch.setattr(ws, "check_all", lambda: {"ok": True, "sites": {
        "claude": {"logged_in": True, "note": "ok"},
        "make": {"logged_in": False, "note": "풀림"},
    }})
    monkeypatch.setattr(worker, "notify_all", lambda channels, text, **kw: sent.append(text))
    out = worker._web_keepalive(_RT(), None)
    assert len(sent) == 1 and "Make" in sent[0] and "web-login.cmd make" in sent[0]
    assert out["sites"]["make"]["notified"] is True


def test_worker_quiet_when_never_logged_in(monkeypatch):
    sent = []
    monkeypatch.setattr(ws, "check_all", lambda: {"ok": True, "sites": {"claude": {"logged_in": False, "note": "프로필 없음(아직 로그인 안 함)"}}})
    monkeypatch.setattr(worker, "notify_all", lambda channels, text, **kw: sent.append(text))
    worker._web_keepalive(_RT(), None)
    assert not sent
