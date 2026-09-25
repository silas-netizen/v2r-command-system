"""sheets_api: Apps Script 웹앱 클라이언트 — httpx.post를 가짜 응답으로 바꿔
재시도·타임아웃·오류 응답 처리만 검증한다(실제 네트워크 호출 없음)."""

from __future__ import annotations

import pytest

from v2r.sources import sheets_api as sa


def _write_config(tmp_path, *, enabled=True, url="https://example.invalid/exec", retries=3, timeout_sec=120):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "sheets_api.yaml").write_text(
        f"enabled: {str(enabled).lower()}\nurl: {url}\ntimeout_sec: {timeout_sec}\nretries: {retries}\n",
        encoding="utf-8",
    )


class FakeResp:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._json


def test_load_config_missing_file_disabled(tmp_path):
    cfg = sa.load_sheets_api_config(tmp_path)
    assert cfg.enabled is False


def test_load_config_reads_yaml(tmp_path):
    _write_config(tmp_path)
    cfg = sa.load_sheets_api_config(tmp_path)
    assert cfg.enabled is True
    assert cfg.url == "https://example.invalid/exec"
    assert cfg.retries == 3
    assert cfg.timeout_sec == 120


def test_load_config_enabled_but_no_url_is_disabled(tmp_path):
    _write_config(tmp_path, url="")
    cfg = sa.load_sheets_api_config(tmp_path)
    assert cfg.enabled is False


def test_call_sheets_api_rejects_unknown_action(tmp_path):
    _write_config(tmp_path)
    with pytest.raises(sa.SheetsApiError):
        sa.call_sheets_api("sid", "delete_everything", {}, repo_root=tmp_path)


def test_call_sheets_api_raises_when_disabled(tmp_path):
    with pytest.raises(sa.SheetsApiError):
        sa.call_sheets_api("sid", "snapshot", {}, repo_root=tmp_path)


def test_call_sheets_api_success(monkeypatch, tmp_path):
    _write_config(tmp_path)
    captured = {}

    def fake_post(url, json, timeout, follow_redirects=True):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResp({"ok": True, "result": {"added": 2, "skipped": 0}})

    monkeypatch.setattr(sa.httpx, "post", fake_post)
    out = sa.api_append("sid1", [{"keyword": "k1"}], repo_root=tmp_path)
    assert out == {"added": 2, "skipped": 0}
    assert captured["json"]["action"] == "append"
    assert captured["json"]["spreadsheet_id"] == "sid1"
    assert captured["timeout"] == 120


def test_call_sheets_api_ok_false_retries_then_raises(monkeypatch, tmp_path):
    _write_config(tmp_path, retries=2)
    calls = []

    def fake_post(url, json, timeout, follow_redirects=True):
        calls.append(1)
        return FakeResp({"ok": False, "error": "허용되지 않은 시트"})

    sleeps = []
    monkeypatch.setattr(sa.httpx, "post", fake_post)
    monkeypatch.setattr(sa.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(sa.SheetsApiError):
        sa.api_snapshot("bad-sid", repo_root=tmp_path)
    assert len(calls) == 2  # retries=2회 모두 시도
    assert len(sleeps) == 1  # 마지막 시도 뒤에는 안 잔다


def test_call_sheets_api_network_error_retries_with_backoff(monkeypatch, tmp_path):
    _write_config(tmp_path, retries=3)
    calls = []

    def fake_post(url, json, timeout, follow_redirects=True):
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("network down")
        return FakeResp({"ok": True, "result": {"deleted": 1}})

    sleeps = []
    monkeypatch.setattr(sa.httpx, "post", fake_post)
    monkeypatch.setattr(sa.time, "sleep", lambda s: sleeps.append(s))

    out = sa.api_delete_by_key("sid1", ["kw1"], repo_root=tmp_path)
    assert out == {"deleted": 1}
    assert len(calls) == 3
    assert sleeps == [1, 2]  # 지수 백오프: 2^0, 2^1


def test_call_sheets_api_uses_passed_config_over_file(monkeypatch, tmp_path):
    """설정 파일이 없어도 `config=`를 직접 넘기면 그걸 쓴다(파일 재읽기 없음)."""
    captured = {}

    def fake_post(url, json, timeout, follow_redirects=True):
        captured["url"] = url
        return FakeResp({"ok": True, "result": {}})

    monkeypatch.setattr(sa.httpx, "post", fake_post)
    cfg = sa.SheetsApiConfig(enabled=True, url="https://direct.invalid/exec", timeout_sec=5, retries=1)
    sa.api_snapshot("sid", repo_root=tmp_path, config=cfg)
    assert captured["url"] == "https://direct.invalid/exec"
