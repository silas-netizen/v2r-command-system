"""브랜드 5개 동시 병렬 키워드 발굴 (2026-09-23). 브라우저·실제 프로세스는 열지 않는다."""

from __future__ import annotations

import datetime as _dt
import json

from v2r.command.parser import BRAND_NAMES
from v2r.knowledge import keyword_discovery_parallel as kdp


def test_brands_matches_parser():
    assert kdp.BRANDS == BRAND_NAMES


def test_clone_profile_name():
    assert kdp.clone_profile_name("우아덤") == "browser-profile-naver-kw-우아덤"


def test_clone_profile_copies_but_skips_lock_files(tmp_path):
    src = tmp_path / "browser-profile-naver"
    src.mkdir()
    (src / "Cookies").write_text("cookie-data", encoding="utf-8")
    (src / "lockfile").write_text("locked", encoding="utf-8")
    (src / "SingletonLock").write_text("x", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    dst = kdp.clone_profile("우아덤", src, data_dir)

    assert dst == data_dir / "browser-profile-naver-kw-우아덤"
    assert (dst / "Cookies").read_text(encoding="utf-8") == "cookie-data"
    assert not (dst / "lockfile").exists()
    assert not (dst / "SingletonLock").exists()
    # 원본은 그대로(안 건드림)
    assert (src / "lockfile").exists()


def test_clone_profile_overwrites_stale_clone(tmp_path):
    src = tmp_path / "browser-profile-naver"
    src.mkdir()
    (src / "Cookies").write_text("new", encoding="utf-8")
    data_dir = tmp_path / "data"
    stale = data_dir / "browser-profile-naver-kw-우아덤"
    stale.mkdir(parents=True)
    (stale / "Cookies").write_text("old", encoding="utf-8")
    (stale / "junk.txt").write_text("stale", encoding="utf-8")

    dst = kdp.clone_profile("우아덤", src, data_dir)

    assert (dst / "Cookies").read_text(encoding="utf-8") == "new"
    assert not (dst / "junk.txt").exists()


def test_update_progress_merges_per_brand(tmp_path):
    kdp.update_progress("우아덤", {"collected": 100, "status": "running"}, data_dir=tmp_path)
    kdp.update_progress("장으뜸", {"collected": 5, "status": "starting"}, data_dir=tmp_path)
    kdp.update_progress("우아덤", {"collected": 250}, data_dir=tmp_path)

    data = json.loads(kdp.progress_path(tmp_path).read_text(encoding="utf-8"))
    assert data["우아덤"]["collected"] == 250
    assert data["우아덤"]["status"] == "running"  # 안 준 필드는 유지
    assert data["장으뜸"]["collected"] == 5


def test_worker_main_parses_headless_offscreen_flags(tmp_path, monkeypatch):
    calls = {}

    def _fake_worker_run(brand, profile_dir, target, deadline, data_dir="data", **kw):
        calls["headless"] = kw.get("headless")
        calls["offscreen"] = kw.get("offscreen")
        return {"ok": True}

    monkeypatch.setattr(kdp, "worker_run", _fake_worker_run)
    rc = kdp._worker_main(
        ["우아덤", str(tmp_path / "p"), "10000", _dt.datetime.now().isoformat(), str(tmp_path), "0", "1"]
    )
    assert rc == 0
    assert calls == {"headless": False, "offscreen": True}


def test_worker_run_stops_at_deadline_without_error(tmp_path, monkeypatch):
    """`run_for_brand`가 deadline 지남을 이유로 TimeoutError를 던지면
    `worker_run`이 이를 정상 종료(ok=True)로 흡수하고 progress를 done으로 남긴다."""
    from v2r.knowledge import naver_keyword_tool as kt_mod

    def _fake_run_for_brand(rt, brand, **kwargs):
        progress_cb = kwargs.get("progress_cb")
        if progress_cb:
            progress_cb({"brand": brand, "collected": 3, "queries": 1})
        raise TimeoutError("08:20 상한 도달 — 이 브랜드 조회를 멈춥니다")

    class _FakeRuntime:
        @staticmethod
        def open():
            return _FakeRuntime()

        def close(self):
            pass

    monkeypatch.setattr(kt_mod, "run_for_brand", _fake_run_for_brand)
    monkeypatch.setattr("v2r.engine.context.Runtime", _FakeRuntime)

    out = kdp.worker_run(
        "우아덤",
        tmp_path / "profile",
        target=10_000,
        deadline=_dt.datetime.now() - _dt.timedelta(seconds=1),
        data_dir=tmp_path,
    )
    assert out["ok"] is True
    data = json.loads(kdp.progress_path(tmp_path).read_text(encoding="utf-8"))
    assert data["우아덤"]["status"] == "done"
