"""sheets_writer: TSV 조립, 셀 파싱, CSV 검증 로직(가짜 페이지/가짜 CSV로 테스트).

실제 Playwright 브라우저나 네트워크는 쓰지 않는다 — `_write_tsv_at`와
`_read_export_csv`를 monkeypatch해서 순수 로직만 검증한다.
"""

from __future__ import annotations

import pytest

from v2r.sources import sheets_writer as sw


def test_col_letter():
    assert sw._col_letter(0) == "A"
    assert sw._col_letter(7) == "H"
    assert sw._col_letter(25) == "Z"
    assert sw._col_letter(26) == "AA"


def test_parse_cell():
    assert sw._parse_cell("H2") == ("H", 2)
    assert sw._parse_cell("Z1") == ("Z", 1)
    assert sw._parse_cell("AA10") == ("AA", 10)


def test_has_credentials_always_true():
    assert sw.has_credentials(".") is True


def test_tsv_assembly_in_write_verified(monkeypatch, tmp_path):
    """`_write_verified`가 넘기는 TSV가 탭/개행으로 올바르게 조립되는지."""
    captured = {}

    def fake_write(sid, gid, cell, tsv):
        captured["cell"] = cell
        captured["tsv"] = tsv

    fake_table = [["H", "I"], ["a1", "b1"]]

    def fake_read(sid, gid, timeout=15.0):
        return fake_table

    monkeypatch.setattr(sw, "_write_tsv_at", fake_write)
    monkeypatch.setattr(sw, "_read_export_csv", fake_read)

    result = sw._write_verified(
        "sid", 0, "A2", [["x", "y"], ["p", "q"]], repo_root=tmp_path
    )
    assert captured["cell"] == "A2"
    assert captured["tsv"] == "x\ty\np\tq"
    # fake_table doesn't actually change, so verify should fail -> csv_only
    assert result["mode"] == "csv_only"


def test_verify_matches_when_csv_reflects_write(monkeypatch, tmp_path):
    written_state = {"table": [["H", "I"], ["old", "old"]]}

    def fake_write(sid, gid, cell, tsv):
        col_letter, row1 = sw._parse_cell(cell)
        rows = [r.split("\t") for r in tsv.split("\n")]
        col0 = ord(col_letter) - ord("A")
        table = written_state["table"]
        for r, row in enumerate(rows):
            idx = row1 - 1 + r
            while len(table) <= idx:
                table.append([])
            for c, val in enumerate(row):
                cidx = col0 + c
                while len(table[idx]) <= cidx:
                    table[idx].append("")
                table[idx][cidx] = val

    def fake_read(sid, gid, timeout=15.0):
        return written_state["table"]

    monkeypatch.setattr(sw, "_write_tsv_at", fake_write)
    monkeypatch.setattr(sw, "_read_export_csv", fake_read)

    result = sw._write_verified("sid", 0, "A2", [["new1", "new2"]], repo_root=tmp_path)
    assert result["mode"] == "sheets"
    assert result["written"] == 1
    assert written_state["table"][1] == ["new1", "new2"]


def test_retries_then_gives_up(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_write(sid, gid, cell, tsv):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(sw, "_write_tsv_at", fake_write)
    monkeypatch.setattr(sw, "_RETRIES", 2)
    monkeypatch.setattr(sw.time, "sleep", lambda *_: None)

    result = sw._write_verified("sid", 0, "A1", [["v"]], repo_root=tmp_path)
    assert result["mode"] == "csv_only"
    assert calls["n"] == 2


def test_brand_lock_blocks_and_releases(tmp_path):
    with sw._BrandLock("sid1", tmp_path):
        lock_file = tmp_path / "data" / "locks" / "sheet-sid1.lock"
        assert lock_file.exists()
    assert not lock_file.exists()


def test_brand_lock_stale_is_reclaimed(tmp_path, monkeypatch):
    lock_dir = tmp_path / "data" / "locks"
    lock_dir.mkdir(parents=True)
    stale = lock_dir / "sheet-sid2.lock"
    stale.write_text("999999")
    import os as _os
    import time as _time

    old_mtime = _time.time() - 1000
    _os.utime(stale, (old_mtime, old_mtime))
    monkeypatch.setattr(sw, "_LOCK_STALE", 300.0)

    with sw._BrandLock("sid2", tmp_path):
        assert stale.exists()  # 재생성된 새 잠금


def test_update_by_key_finds_row_and_writes(monkeypatch, tmp_path):
    table = [
        ["A", "B", "C", "D", "E", "F", "G", "H", "I"],
        ["", "", "", "", "", "", "미확인", "kw1", "url1"],
        ["", "", "", "", "", "", "미확인", "kw2", "url2"],
    ]

    def fake_read(sid, gid, timeout=15.0):
        return table

    written = {}

    def fake_write_verified(sid, gid, cell, rows, repo_root="."):
        written[cell] = rows[0][0]
        col_letter, row1 = sw._parse_cell(cell)
        col0 = ord(col_letter) - ord("A")
        table[row1 - 1][col0] = rows[0][0]
        return {"written": 1, "mode": "sheets", "cell": cell}

    monkeypatch.setattr(sw, "_read_export_csv", fake_read)
    monkeypatch.setattr(sw, "_write_verified", fake_write_verified)

    result = sw.update_by_key(
        "sid", "탭", "kw2", {"G": "확인", "I": "url2-new"}, key_column="H", repo_root=tmp_path
    )
    assert result["mode"] == "sheets"
    assert result["row"] == 3
    assert written["G3"] == "확인"
    assert written["I3"] == "url2-new"


def test_append_rows_starts_after_last_row(monkeypatch, tmp_path):
    table = [["H", "I"], ["kw1", "url1"]]

    def fake_read(sid, gid, timeout=15.0):
        return table

    captured = {}

    def fake_write_verified(sid, gid, cell, rows, repo_root="."):
        captured["cell"] = cell
        captured["rows"] = rows
        return {"written": len(rows), "mode": "sheets", "cell": cell}

    monkeypatch.setattr(sw, "_read_export_csv", fake_read)
    monkeypatch.setattr(sw, "_write_verified", fake_write_verified)

    result = sw.append_rows(
        "sid", "탭", [{"H": "kw2", "I": "url2"}], header=["H", "I"], repo_root=tmp_path
    )
    assert result["mode"] == "sheets"
    assert captured["cell"] == "A3"  # 헤더 1행 + 데이터 1행 다음 = 3행부터
    assert captured["rows"] == [["kw2", "url2"]]
