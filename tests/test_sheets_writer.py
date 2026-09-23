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
    format_calls = []
    monkeypatch.setattr(
        sw, "copy_row_format", lambda *a, **k: format_calls.append((a, k))
    )

    result = sw.append_rows(
        "sid", "탭", [{"H": "kw2", "I": "url2"}], header=["H", "I"], repo_root=tmp_path
    )
    assert result["mode"] == "sheets"
    assert captured["cell"] == "A3"  # 헤더 1행 + 데이터 1행 다음 = 3행부터
    assert captured["rows"] == [["kw2", "url2"]]
    # copy_format 기본 True: 2행(기존 드롭다운 행) 서식을 새 행(3행)에 복사한다.
    assert format_calls == [(("sid", 0, 2, 3, 3), {"n_cols": 2})]


def test_append_rows_skips_format_copy_when_disabled(monkeypatch, tmp_path):
    table = [["H", "I"], ["kw1", "url1"]]
    monkeypatch.setattr(sw, "_read_export_csv", lambda sid, gid, timeout=15.0: table)
    monkeypatch.setattr(
        sw, "_write_verified", lambda sid, gid, cell, rows, repo_root=".": {"written": len(rows), "mode": "sheets", "cell": cell}
    )
    called = []
    monkeypatch.setattr(sw, "copy_row_format", lambda *a, **k: called.append(1))
    sw.append_rows(
        "sid", "탭", [{"H": "kw2", "I": "url2"}], header=["H", "I"], repo_root=tmp_path, copy_format=False
    )
    assert called == []


def test_paste_special_finds_submenu_item(monkeypatch):
    """`_paste_special`이 부모 메뉴 hover 후 하위 메뉴에서 대상 텍스트를 찾아 클릭하는지."""

    class FakeItem:
        def __init__(self, text):
            self._text = text
            self.clicked = False
            self.hovered = False

        def inner_text(self):
            return self._text

        def hover(self):
            self.hovered = True

        def click(self, force=False):
            self.clicked = True

    class FakeLocator:
        def __init__(self, items):
            self._items = items

        def count(self):
            return len(self._items)

        def nth(self, i):
            return self._items[i]

    class FakePage:
        def __init__(self):
            self.round = 0
            self.parent = FakeItem("선택하여 붙여넣기")
            self.target = FakeItem("데이터 확인만 붙여넣기")
            self.escaped = False

        def locator(self, sel):
            self.round += 1
            if self.round == 1:
                return FakeLocator([self.parent])  # baseline(부모 메뉴 찾기)
            if self.round in (2, 3):
                return FakeLocator([self.target])  # 하위 메뉴에서 대상 항목 찾기·클릭
            return FakeLocator([])  # 클릭 뒤 메뉴가 닫혔는지 확인 -> 빈 목록 = 닫힘

        class _KB:
            def __init__(self, outer):
                self.outer = outer

            def press(self, key):
                if key == "Escape":
                    self.outer.escaped = True

        @property
        def keyboard(self):
            return FakePage._KB(self)

        def wait_for_timeout(self, ms):
            pass

    page = FakePage()
    sw._paste_special(page, "데이터 확인만 붙여넣기")
    assert page.parent.clicked  # hover가 아니라 click으로 하위 메뉴를 연다(2026-09-24 실측)
    assert page.target.clicked
    assert not page.escaped


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
            ("당위성키워드", 60, "당위성 논리", 3, 3, 0),  # 2026-09-24: 3=당위성 -> 원고 대상 포함
            ("무관키워드", 50, "", 4, 4, 0),  # relevance 4 = 무관 -> 제외
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
    # SQL 단계에서 무관키워드(relevance 4)·검토대기(needs_review) 제외 -> 3건 picked
    assert res["picked"] == 3
    # 그중 이미시트에있음은 시트 H열에 이미 있어 append에서 다시 제외 -> 2건만 appended
    assert res["appended"] == 2
    kws = [r["키워드"] for r in captured["rows"]]
    assert kws == ["직접키워드", "당위성키워드"]
    # 당위성 등급은 본문 분류가 "당위성"이어야 한다
    labels = {r["키워드"]: r["본문 분류"] for r in captured["rows"]}
    assert labels["당위성키워드"] == "당위성"


def test_apply_exposure_maps_columns_and_writes_totals(tmp_path, monkeypatch):
    """2026-09-23 사용자 최종 지시 — A·G·J·K·L만 바꾸고 B~F(E=비밀번호 포함)는
    절대 안 건드린다. I는 비어 있을 때만 채운다."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "brands.yaml").write_text(
        "brands:\n  테스트브랜드:\n    spreadsheet_id: sid1\n", encoding="utf-8"
    )
    monkeypatch.setattr(sw, "_second_tab_gid", lambda sid: 999)
    # 시트 export 흉내 — A~L, kw1은 I가 이미 채워짐, kw2는 비어 있음.
    header = ["카페", "url", "발행시간", "작성자", "비번", "발행URL", "노출 상태", "키워드", "통합검색", "최종편집", "검색량", "노출량"]
    monkeypatch.setattr(sw, "_read_export_csv", lambda sid, gid: [
        header,
        ["", "", "", "", "", "", "미확인", "kw1", "https://already", "", "", ""],
        ["", "", "", "", "", "", "미확인", "kw2", "", "", "", ""],
    ])

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
            {"keyword": "kw1", "status": "노출완", "final_url": "https://x", "edited_at": "2026-09-23 12:00:00", "cafe": "씨씨앙", "volume": 100},
            {"keyword": "kw2", "status": "밀려남", "final_url": "https://y", "edited_at": "2026-09-23 12:01:00", "volume": 50},
        ],
        totals={"P1": 10, "Q1": 20},
        repo_root=tmp_path,
    )
    assert calls[0][0] == "kw1"
    # kw1: 노출완 + 카페 있음 + I 이미 있음(안 건드림) + 검색량 있음
    assert calls[0][1] == {"G": "노출완", "J": "2026-09-23 12:00:00", "A": "씨씨앙", "K": "100", "L": "100"}
    # kw2: 밀려남 + 카페 없음(A 안 건드림) + I 비어 있어서 채움 + L=0
    assert calls[1][0] == "kw2"
    assert calls[1][1] == {"G": "밀려남", "J": "2026-09-23 12:01:00", "I": "https://y", "K": "50", "L": "0"}
    # B~F, H, E는 어떤 updates 딕셔너리에도 등장하지 않는다.
    for _, updates in calls:
        assert set(updates) <= {"A", "G", "I", "J", "K", "L"}
    assert ("P1", 10) in cell_calls
    assert ("Q1", 20) in cell_calls


def test_build_exposure_column_updates_노출완_카페_있음():
    updates = sw.build_exposure_column_updates(
        status="exposed", checked_at_kst="2026-09-23 10:00:00", cafe="양평맘", volume=200,
        existing_i="", integrated_search_url_fn=lambda k: f"https://s/{k}", keyword="kw",
    )
    assert updates == {
        "G": "노출완", "J": "2026-09-23 10:00:00", "A": "양평맘",
        "I": "https://s/kw", "K": "200", "L": "200",
    }


def test_build_exposure_column_updates_밀려남_카페_안바뀜():
    updates = sw.build_exposure_column_updates(
        status="pushed", checked_at_kst="2026-09-23 10:00:00", cafe=None, volume=80,
        existing_i="https://이미있음", integrated_search_url_fn=lambda k: "무시됨", keyword="kw",
    )
    assert "A" not in updates  # 밀려남이면 카페(A)를 바꾸지 않는다
    assert "I" not in updates  # 이미 있으면 I도 안 바꾼다
    assert updates["G"] == "밀려남"
    assert updates["K"] == "80"
    assert updates["L"] == "0"


def test_normalize_kst_timestamp_iso_to_kst():
    # +09:00 오프셋 ISO는 그대로 같은 시각이므로 T만 빠지고 그대로.
    assert sw.normalize_kst_timestamp("2026-09-23T23:18:27+09:00") == "2026-09-23 23:18:27"


def test_normalize_kst_timestamp_다른_시간대는_KST로_변환():
    # UTC 23:18:27 -> KST(+9) 익일 08:18:27
    assert sw.normalize_kst_timestamp("2026-09-23T23:18:27+00:00") == "2026-09-24 08:18:27"


def test_normalize_kst_timestamp_이미_공백형식이면_그대로():
    assert sw.normalize_kst_timestamp("2026-09-23 23:18:27") == "2026-09-23 23:18:27"


def test_normalize_kst_timestamp_빈값_그대로():
    assert sw.normalize_kst_timestamp("") == ""


def test_build_exposure_column_updates_J는_항상_정규화():
    updates = sw.build_exposure_column_updates(
        status="pushed", checked_at_kst="2026-09-23T23:18:27+09:00", cafe=None, volume=None,
        existing_i="x", integrated_search_url_fn=None, keyword="kw",
    )
    assert updates["J"] == "2026-09-23 23:18:27"


def test_last_data_row_ignores_trailing_blank_rows():
    from v2r.sources.sheets_writer import _last_data_row

    table = [["카페", "url"], ["", "", "", "", "", "", "", "k1"], [""] * 12, ["", ""], []]
    assert _last_data_row(table) == 2
    assert _last_data_row([["h"]]) == 1
    assert _last_data_row([]) == 0


def test_append_rows_starts_after_last_data_row_not_grid_end(monkeypatch):
    from v2r.sources import sheets_writer as sw

    table = [["카페"] + [""] * 7 + ["키워드"]] + [[""] * 7 + ["k%d" % i] for i in range(3)] + [[""] * 12] * 50
    monkeypatch.setattr(sw, "_read_export_csv", lambda *a, **k: table)
    calls = []

    def fake_write(sid, gid, cell, rows, repo_root):
        calls.append(cell)
        return {"written": len(rows), "mode": "sheets"}

    monkeypatch.setattr(sw, "_write_verified", fake_write)
    monkeypatch.setattr(sw, "copy_row_format", lambda *a, **k: {"ok": True}, raising=False)
    sw.append_rows("sid", "노출 현황", [["", "", "", "", "", "", "", "new"]], gid="1", copy_format=False)
    assert calls == ["A5"]


def test_norm_cell_date_without_leading_zero():
    from v2r.sources.sheets_writer import _norm_cell

    assert _norm_cell("2026-09-24 0:31:50") == _norm_cell("2026-09-24 00:31:50")
    assert _norm_cell("2026-09-23 8:39:17") == "2026-09-23 08:39:17"
