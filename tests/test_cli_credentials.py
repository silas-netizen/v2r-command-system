"""Claude Code CLI 자격증명 백업·복원 (사용자 허용 2026-09-21). 실제 CLI·네트워크 없음."""

from __future__ import annotations

from v2r.llm import cli_credentials as cc
from v2r.llm import plan_session as ps


def test_backup_and_restore_roundtrip(tmp_path):
    src = tmp_path / "home" / ".claude" / ".credentials.json"
    src.parent.mkdir(parents=True)
    src.write_bytes(b'{"token": "abc"}')
    data = tmp_path / "data"
    latest = cc.backup(data, src)
    assert latest and latest.read_bytes() == b'{"token": "abc"}'
    # 같은 내용이면 새 사본을 만들지 않는다
    cc.backup(data, src)
    assert len(list((data / cc.BACKUP_DIRNAME).glob("credentials-*.json"))) == 1
    # 깨졌다 → 복원
    src.write_bytes(b"")
    assert cc.restore(data, src)
    assert src.read_bytes() == b'{"token": "abc"}'


def test_backup_skips_missing_or_empty(tmp_path):
    assert cc.backup(tmp_path, tmp_path / "none.json") is None
    empty = tmp_path / "e.json"; empty.write_bytes(b"")
    assert cc.backup(tmp_path, empty) is None
    assert cc.restore(tmp_path, tmp_path / "x.json") is False


def test_check_session_restores_when_logged_out(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_status(exe):
        calls["n"] += 1
        return {"loggedIn": calls["n"] > 1, "authMethod": "claude.ai"}

    class _B:
        exe = "claude.exe"
        def available(self): return True

    monkeypatch.setattr(ps, "auth_status", fake_status)
    monkeypatch.setattr(cc, "restore", lambda data_dir, dst=None: True)
    backed = []
    monkeypatch.setattr(cc, "backup", lambda data_dir, src=None: backed.append(data_dir) or True)
    out = ps.check_session(tmp_path, backend=_B())
    assert out["ok"] and out["logged_in"] and out.get("restored_from_backup")
    assert not out["notice"]  # 복원됐으면 사람에게 알리지 않는다
    assert backed  # 성공 시 백업 갱신


def test_check_session_notifies_when_restore_fails(tmp_path, monkeypatch):
    class _B:
        exe = "claude.exe"
        def available(self): return True

    monkeypatch.setattr(ps, "auth_status", lambda exe: {"loggedIn": False})
    monkeypatch.setattr(cc, "restore", lambda data_dir, dst=None: False)
    out = ps.check_session(tmp_path, backend=_B())
    assert not out["logged_in"] and out["notice"]
