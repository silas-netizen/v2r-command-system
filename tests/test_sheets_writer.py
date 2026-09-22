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


# --------------------------------------------------------------------------
# 2026-09-23 추가분: 브랜드 설정 조회, 탭 gid 조회, sync_keywords_*, apply_exposure
# --------------------------------------------------------------------------


def test_get_spreadsheet_id_prefers_explicit_key(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "brands.yaml").write_text(
        "brands:\n"
        "  브랜드A:\n"
        "    spreadsheet_id: sid-explicit\n"
        "    sheets: [sid-from-sheets-list]\n"
        "  브랜드B:\n"
        "    sheets: [sid-only-list]\n",
        encoding="utf-8",
    )
    assert sw.get_spreadsheet_id("브랜드A", tmp_path) == "sid-explicit"
    assert sw.get_spreadsheet_id("브랜드B", tmp_path) == "sid-only-list"
    assert sw.get_spreadsheet_id("없는브랜드", tmp_path) is None


def test_list_configured_brands(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "brands.yaml").write_text(
        "brands:\n  가:\n    sheets: [x]\n  나:\n    sheets: [y]\n", encoding="utf-8"
    )
    assert sorted(sw.list_configured_brands(tmp_path)) == ["가", "나"]


def test_sync_keywords_to_sheet_skips_when_columns_missing(tmp_path, monkeypatch):
    import sqlite3

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "brands.yaml").write_text(
        "brands:\n  테스트브랜드:\n    spreadsheet_id: sid1\n", encoding="utf-8"
    )
    kw_dir = tmp_path / "data" / "keywords"
    kw_dir.mkdir(parents=True)
    con = sqlite3.connect(str(kw_dir / "테스트브랜드.sqlite"))
    con.execute("create table keywords (keyword text, total integer)")
    con.commit()
    con.close()

    res = sw.sync_keywords_to_sheet("테스트브랜드", repo_root=tmp_path)
    assert res["skipped"] is True
    assert "미산정" in res["reason"]


def test_sync_keywords_to_sheet_picks_only_target_rows(tmp_path, monkeypatch):
    import sqlite3

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "brands.yaml").write_text(
        "brands:\n  테스트브랜드:\n    spreadsheet_id: sid1\n", encoding="utf-8"
    )
    kw_dir = tmp_path / "data" / "keywords"
    kw_dir.mkdir(parents=True)
    con = sqlite3.connect(str(kw_dir / "테스트브랜드.sqlite"))
    con.execute(
        "create table keywords (keyword text, total integer, rationale text, "
        "relevance_llm integer, relevance_codex integer, needs_review integer)"
    )
    con.executemany(
        "insert into keywords values (?, ?, ?, ?, ?, ?)",
        [
            ("직접키워드", 100, "딱맞음", 0, 0, 0),
            ("무관키워드", 50, "", 3, 3, 0),  # relevance 3 = 무관 -> 제외
            ("검토대기", 30, "", 1, 1, 1),  # needs_review -> 제외
            ("이미시트에있음", 20, "", 2, 2, 0),  # 시트에 이미 있음 -> 제외
        ],
    )
    con.commit()
    con.close()

    header_row = ["A", "B", "C", "D", "E", "F", "G", "H"]
    existing_row = ["", "", "", "", "", "", "", "이미시트에있음"]
    monkeypatch.setattr(sw, "_second_tab_gid", lambda sid: 999)
    monkeypatch.setattr(
        sw, "_read_export_csv", lambda sid, gid, timeout=15.0: [header_row, existing_row]
    )
    captured = {}

    def fake_append_rows(sid, sheet, rows, *, header=None, gid=0, repo_root="."):
        captured["rows"] = rows
        return {"written": len(rows), "mode": "sheets"}

    monkeypatch.setattr(sw, "append_rows", fake_append_rows)

    res = sw.sync_keywords_to_sheet("테스트브랜드", repo_root=tmp_path)
    # SQL 단계에서 무관키워드(relevance 3)·검토대기(needs_review) 제외 -> 2건 picked
    assert res["picked"] == 2
    # 그중 이미시트에있음은 시트 H열에 이미 있어 append에서 다시 제외 -> 1건만 appended
    assert res["appended"] == 1
    kws = [r["키워드"] for r in captured["rows"]]
    assert kws == ["직접키워드"]


def test_apply_exposure_maps_columns_and_writes_totals(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "brands.yaml").write_text(
        "brands:\n  테스트브랜드:\n    spreadsheet_id: sid1\n", encoding="utf-8"
    )
    monkeypatch.setattr(sw, "_second_tab_gid", lambda sid: 999)

    calls = []

    def fake_update_by_key(sid, sheet, key_value, updates, *, key_column="H", gid=0, repo_root="."):
        calls.append((key_value, updates))
        return {"written": len(updates), "mode": "sheets"}

    cell_calls = []

    def fake_set_cell(sid, sheet, cell, value, *, gid=0, repo_root="."):
        cell_calls.append((cell, value))
        return {"written": True, "mode": "sheets"}

    monkeypatch.setattr(sw, "update_by_key", fake_update_by_key)
    monkeypatch.setattr(sw, "set_cell", fake_set_cell)

    res = sw.apply_exposure(
        "테스트브랜드",
        [
            {"keyword": "kw1", "status": "노출", "final_url": "https://x", "rank": 2},
            {"keyword": "kw2", "exposed_total": "1,234"},
        ],
        totals={"P1": 10, "Q1": 20},
        repo_root=tmp_path,
    )
    assert res["written"] == 4  # kw1: 3개(G,I,O) + kw2: 1개(L)
    assert calls[0][0] == "kw1"
    assert calls[0][1] == {"G": "노출", "I": "https://x", "O": 2}
    assert calls[1][1] == {"L": "1,234"}
    assert ("P1", 10) in cell_calls
    assert ("Q1", 20) in cell_calls
