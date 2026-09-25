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
    table = [["카페", "I"], ["kw1", "url1"]]

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
    table = [["카페", "I"], ["kw1", "url1"]]
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

    header_row = ["카페", "B", "C", "D", "E", "F", "G", "H"]
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


def test_append_rows_starts_after_last_data_row_not_grid_end(monkeypatch, tmp_path):
    from v2r.sources import sheets_writer as sw

    # 시트 API가 비활성(기본 config가 없는 tmp_path)일 때만 이 브라우저 경로가 쓰인다.
    table = [["카페"] + [""] * 7 + ["키워드"]] + [[""] * 7 + ["k%d" % i] for i in range(3)] + [[""] * 12] * 50
    monkeypatch.setattr(sw, "_read_export_csv", lambda *a, **k: table)
    calls = []

    def fake_write(sid, gid, cell, rows, repo_root):
        calls.append(cell)
        return {"written": len(rows), "mode": "sheets"}

    monkeypatch.setattr(sw, "_write_verified", fake_write)
    monkeypatch.setattr(sw, "copy_row_format", lambda *a, **k: {"ok": True}, raising=False)
    sw.append_rows(
        "sid", "노출 현황", [["", "", "", "", "", "", "", "new"]], gid="1", copy_format=False, repo_root=tmp_path
    )
    assert calls == ["A5"]


def test_norm_cell_date_without_leading_zero():
    from v2r.sources.sheets_writer import _norm_cell

    assert _norm_cell("2026-09-24 0:31:50") == _norm_cell("2026-09-24 00:31:50")
    assert _norm_cell("2026-09-23 8:39:17") == "2026-09-23 08:39:17"


# --------------------------------------------------------------------------
# 2026-09-24: 공백·대소문자 차이로 같은 키워드가 새 행으로 중복 추가되던 사고
# (5개 브랜드 시트에서 811개 중복 발생) 재발 방지 시험
# --------------------------------------------------------------------------


def test_update_by_key_matches_ignoring_space_and_case(monkeypatch, tmp_path):
    """H열에 "수면 테이프"(공백 포함)가 있는데 "수면테이프"로 조회해도 같은 키로 본다."""
    table = [
        ["A", "B", "C", "D", "E", "F", "G", "H", "I"],
        ["", "", "", "", "", "", "미확인", "수면 테이프", "url1"],
    ]

    def fake_read(sid, gid, timeout=15.0):
        return table

    written = {}

    def fake_write_verified(sid, gid, cell, rows, repo_root="."):
        written[cell] = rows[0][0]
        return {"written": 1, "mode": "sheets", "cell": cell}

    monkeypatch.setattr(sw, "_read_export_csv", fake_read)
    monkeypatch.setattr(sw, "_write_verified", fake_write_verified)

    result = sw.update_by_key(
        "sid", "탭", "수면테이프", {"G": "확인"}, key_column="H", repo_root=tmp_path
    )
    assert result["mode"] == "sheets"
    assert result["row"] == 2
    assert written["G2"] == "확인"


def test_update_by_key_updates_all_rows_when_multiple_normalize_the_same(monkeypatch, tmp_path):
    """이미 시트에 정규화 중복 행이 남아 있으면(예: 다른 원인으로 재발) 첫 행만이 아니라
    전부 갱신해야 한 행만 갱신하고 다른 행은 낡은 값으로 남는 일이 없다."""
    table = [
        ["A", "B", "C", "D", "E", "F", "G", "H", "I"],
        ["", "", "", "", "", "", "미확인", "수면테이프", "url1"],
        ["", "", "", "", "", "", "미확인", "수면 테이프", "url2"],
    ]

    def fake_read(sid, gid, timeout=15.0):
        return table

    written = []

    def fake_write_verified(sid, gid, cell, rows, repo_root="."):
        written.append(cell)
        return {"written": 1, "mode": "sheets", "cell": cell}

    monkeypatch.setattr(sw, "_read_export_csv", fake_read)
    monkeypatch.setattr(sw, "_write_verified", fake_write_verified)

    result = sw.update_by_key(
        "sid", "탭", "수면 테이프", {"G": "확인"}, key_column="H", repo_root=tmp_path
    )
    assert result["rows"] == [2, 3]
    assert set(written) == {"G2", "G3"}


def test_update_rows_treats_space_case_variants_as_same_key(monkeypatch, tmp_path):
    """update_rows: 기존 "수면 테이프" 행을 "수면테이프" 키로 넣으면 새 행 추가가 아니라
    그 행을 갱신해야 한다."""
    table = [["키워드", "값"], ["수면 테이프", "old"]]
    monkeypatch.setattr(sw, "_read_export_csv", lambda sid, gid, timeout=15.0: table)

    updated = {}
    appended = {}

    def fake_write_verified(sid, gid, cell, rows, repo_root="."):
        updated[cell] = rows[0]
        return {"written": 1, "mode": "sheets", "cell": cell}

    def fake_append_rows(sid, sheet, rows, *, header=None, gid=0, repo_root="."):
        appended["rows"] = rows
        return {"written": len(rows), "mode": "sheets"}

    monkeypatch.setattr(sw, "_write_verified", fake_write_verified)
    monkeypatch.setattr(sw, "append_rows", fake_append_rows)

    result = sw.update_rows(
        "sid", "탭", [{"키워드": "수면테이프", "값": "new"}], key_column="키워드", repo_root=tmp_path
    )
    assert result["written"] == 1
    assert updated["A2"] == ["수면테이프", "new"]
    assert "rows" not in appended  # 새 행으로 추가되지 않았다


def test_update_rows_dedupes_normalized_duplicates_within_append_batch(monkeypatch, tmp_path):
    """새로 넣을 목록 안에 공백·대소문자만 다른 중복이 있으면 하나만 append한다."""
    table = [["키워드", "값"]]
    monkeypatch.setattr(sw, "_read_export_csv", lambda sid, gid, timeout=15.0: table)
    monkeypatch.setattr(
        sw, "_write_verified",
        lambda sid, gid, cell, rows, repo_root=".": {"written": 1, "mode": "sheets", "cell": cell},
    )
    captured = {}

    def fake_append_rows(sid, sheet, rows, *, header=None, gid=0, repo_root="."):
        captured["rows"] = rows
        return {"written": len(rows), "mode": "sheets"}

    monkeypatch.setattr(sw, "append_rows", fake_append_rows)

    sw.update_rows(
        "sid", "탭",
        [
            {"키워드": "수면테이프", "값": "a"},
            {"키워드": "수면 테이프", "값": "b"},
            {"키워드": "Sleep Tape", "값": "c"},
        ],
        key_column="키워드", repo_root=tmp_path,
    )
    assert len(captured["rows"]) == 2
    assert [r[0] for r in captured["rows"]] == ["수면테이프", "Sleep Tape"]


def test_sync_keywords_to_sheet_dedupes_space_case_variants(tmp_path, monkeypatch):
    """이미 시트에 "수면 테이프"가 있으면 DB의 "수면테이프"는 다시 안 붙이고,
    새로 붙일 후보 안에 정규화 중복이 있어도 하나만 남긴다."""
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
            ("수면테이프", 100, "", 0, 0, 0),  # 시트에 이미 "수면 테이프"로 있음 -> 제외
            ("코숨 편해요", 90, "", 0, 0, 0),
            ("코숨편해요", 80, "", 0, 0, 0),  # 위와 정규화 중복 -> 하나만
        ],
    )
    con.commit()
    con.close()

    header_row = ["카페", "B", "C", "D", "E", "F", "G", "H"]
    existing_row = ["", "", "", "", "", "", "", "수면 테이프"]
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
    assert res["appended"] == 1
    assert [r["키워드"] for r in captured["rows"]] == ["코숨 편해요"]


# --------------------------------------------------------------------------
# 2026-09-25 사고(실행기 재시작 중 시트 훼손) 재발 방지 가드 시험
# --------------------------------------------------------------------------


def test_check_a1_ok_accepts_expected_header():
    assert sw._check_a1_ok([["카페", "url"], ["", ""]]) == ""


def test_check_a1_ok_rejects_corrupted_a1():
    # 2026-09-25 실측: 이름 상자 이동이 씹혀 A1에 "A1265" 같은 셀 주소 문자열이 박혔다.
    err = sw._check_a1_ok([["A1265", "url"], ["", ""]])
    assert "A1265" in err
    assert "카페" in err


def test_check_a1_ok_rejects_empty_table():
    assert sw._check_a1_ok([]) != ""
    assert sw._check_a1_ok([[]]) != ""


def test_append_rows_refuses_to_write_when_a1_corrupted(monkeypatch, tmp_path):
    """A1이 "카페"가 아니면 append_rows는 아무 것도 쓰지 않고 즉시 중단한다."""
    table = [["A1265", "I"], ["kw1", "url1"]]
    monkeypatch.setattr(sw, "_read_export_csv", lambda sid, gid, timeout=15.0: table)
    write_calls = []
    monkeypatch.setattr(
        sw, "_write_verified",
        lambda sid, gid, cell, rows, repo_root=".": write_calls.append(cell) or {"written": 0, "mode": "sheets"},
    )
    result = sw.append_rows(
        "sid", "탭", [{"H": "kw2", "I": "url2"}], header=["H", "I"], repo_root=tmp_path
    )
    assert result["mode"] == "csv_only"
    assert result["written"] == 0
    assert "카페" in result["error"]
    assert write_calls == []  # 어떤 셀에도 쓰기 시도조차 하지 않는다


def test_apply_exposure_refuses_to_write_when_a1_corrupted(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "brands.yaml").write_text(
        "brands:\n  테스트브랜드:\n    spreadsheet_id: sid1\n", encoding="utf-8"
    )
    monkeypatch.setattr(sw, "_second_tab_gid", lambda sid: 999)
    monkeypatch.setattr(
        sw, "_read_export_csv",
        lambda sid, gid: [["A1265", "url", "", "", "", "", "G", "H", "I"], ["", "", "", "", "", "", "미확인", "kw1", ""]],
    )
    calls = []
    monkeypatch.setattr(
        sw, "update_by_key",
        lambda *a, **k: calls.append(a) or {"written": 1, "mode": "sheets"},
    )
    res = sw.apply_exposure("테스트브랜드", [{"keyword": "kw1", "status": "노출완"}], repo_root=tmp_path)
    assert res["written"] == 0
    assert "카페" in res["error"]
    assert calls == []


def test_sync_keywords_to_sheet_refuses_when_a1_corrupted(tmp_path, monkeypatch):
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
    con.execute("insert into keywords values ('키워드1', 10, '', 0, 0, 0)")
    con.commit()
    con.close()

    monkeypatch.setattr(sw, "_second_tab_gid", lambda sid: 999)
    monkeypatch.setattr(
        sw, "_read_export_csv", lambda sid, gid, timeout=15.0: [["A1265"] + [""] * 7]
    )
    append_calls = []
    monkeypatch.setattr(sw, "append_rows", lambda *a, **k: append_calls.append(1))

    res = sw.sync_keywords_to_sheet("테스트브랜드", repo_root=tmp_path)
    assert res["skipped"] is True
    assert "카페" in res["reason"]
    assert append_calls == []


def test_append_rows_stops_chunk_loop_on_first_verify_failure(monkeypatch, tmp_path):
    """200행씩 나눠 붙이다 한 chunk가 검증 실패하면 다음 chunk를 진행하지 않는다
    (2026-09-25 사고: 계속 진행해 어긋난 자리 위에 훼손이 쌓였다)."""
    table = [["카페"] + [""] * 7] + [[""] * 8 for _ in range(1)]
    monkeypatch.setattr(sw, "_read_export_csv", lambda sid, gid, timeout=15.0: table)
    calls = []

    def fake_write_verified(sid, gid, cell, rows, repo_root="."):
        calls.append(cell)
        if len(calls) == 1:
            return {"written": 0, "mode": "csv_only", "error": "검증 불일치"}
        return {"written": len(rows), "mode": "sheets"}

    monkeypatch.setattr(sw, "_write_verified", fake_write_verified)
    monkeypatch.setattr(sw, "copy_row_format", lambda *a, **k: None)

    rows = [["kw%d" % i] + [""] * 7 for i in range(250)]  # CHUNK=200 -> 2 chunks
    result = sw.append_rows("sid", "탭", rows, header=["H"] + [""] * 7, repo_root=tmp_path, copy_format=False)
    assert calls == ["A2"]  # 두 번째 chunk("A202")는 시도조차 안 함
    assert result["mode"] == "csv_only"
    assert "중단" in result["error"]


def test_ensure_grid_rows_aborts_when_boundary_reverify_fails(monkeypatch):
    """`current`(격자 끝으로 믿은 값)를 삽입 직전 다시 확인했을 때 실제로 끝이
    아니면(A{current+1} 이동도 성공함) 삽입하지 않고 즉시 중단한다.

    2026-09-25 사고 의심 경로: 이진 탐색(`_grid_row_count`)이 실제보다 훨씬
    작은 값에 잘못 수렴하면 그 값 아래에 빈 행이 삽입된다(뉴더미스 865행부터
    빈 행 1,465개). 이 가드는 삽입 직전 `current`가 진짜 격자 끝인지
    A{current+1} 이동이 실패(모달)하는지로 재확인한다.
    """
    page = object()  # _nav_to를 완전히 대체하므로 page 속성은 쓰이지 않는다

    def fake_nav_to(pg, cell):
        # A{last_row}(=A10, probe)만 실패시켜 이진 탐색 경로로 들어가게 하고,
        # 그 밖의 모든 이동(A{current}, A{current+1} 포함)은 "성공"으로 응답해
        # 경계 오판(사실은 끝이 아닌데 끝이라고 믿음)을 흉내낸다.
        if cell == "A10":
            return ""
        return cell.upper()

    monkeypatch.setattr(sw, "_nav_to", fake_nav_to)
    monkeypatch.setattr(sw, "_grid_row_count", lambda pg, hi_hint=1000: 5)

    with pytest.raises(sw.SheetsWriteError, match="격자 끝이 아님"):
        sw._ensure_grid_rows(page, 10)


def test_sync_keywords_to_sheet_backs_up_before_writing(tmp_path, monkeypatch):
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
    con.execute("insert into keywords values ('키워드1', 10, '', 0, 0, 0)")
    con.commit()
    con.close()

    header_row = ["카페", "B", "C", "D", "E", "F", "G", "키워드"]
    monkeypatch.setattr(sw, "_second_tab_gid", lambda sid: 999)
    monkeypatch.setattr(sw, "_read_export_csv", lambda sid, gid, timeout=15.0: [header_row])
    monkeypatch.setattr(sw, "append_rows", lambda *a, **k: {"written": 1, "mode": "sheets"})

    res = sw.sync_keywords_to_sheet("테스트브랜드", repo_root=tmp_path)
    assert res["appended"] == 1
    backups = list((tmp_path / "data" / "sheet_backups").glob("테스트브랜드_노출현황_before_sync_*.csv"))
    assert len(backups) == 1
    with open(backups[0], encoding="utf-8-sig") as f:
        content = f.read()
    assert "카페" in content


def test_sync_keywords_to_sheet_caps_rows_per_run(tmp_path, monkeypatch):
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
        "insert into keywords values (?, ?, '', 0, 0, 0)",
        [(f"키워드{i}", 10, ) for i in range(5)],
    )
    con.commit()
    con.close()

    header_row = ["카페", "B", "C", "D", "E", "F", "G", "키워드"]
    monkeypatch.setattr(sw, "_second_tab_gid", lambda sid: 999)
    monkeypatch.setattr(sw, "_read_export_csv", lambda sid, gid, timeout=15.0: [header_row])
    monkeypatch.setattr(sw, "MAX_ROWS_PER_SYNC", 3)
    captured = {}

    def fake_append_rows(sid, sheet, rows, *, header=None, gid=0, repo_root="."):
        captured["rows"] = rows
        return {"written": len(rows), "mode": "sheets"}

    monkeypatch.setattr(sw, "append_rows", fake_append_rows)

    res = sw.sync_keywords_to_sheet("테스트브랜드", repo_root=tmp_path)
    assert res["appended"] == 3
    assert res["capped_at"] == 3
    assert len(captured["rows"]) == 3


# --------------------------------------------------------------------------
# 시트 API(config/sheets_api.yaml enabled=true) 경로 — 브라우저 경로가 전혀
# 호출되지 않는지, snapshot 가드가 작동하는지 검증한다(2026-09-25).
# --------------------------------------------------------------------------


def _enable_sheets_api(tmp_path, *, retries=3):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "sheets_api.yaml").write_text(
        f"enabled: true\nurl: https://example.invalid/exec\ntimeout_sec: 30\nretries: {retries}\n",
        encoding="utf-8",
    )


def _forbid_browser_paths(monkeypatch, sw):
    def _boom(*a, **k):
        raise AssertionError("브라우저 경로가 호출됨(API 모드에서는 안 됨)")

    monkeypatch.setattr(sw, "_write_tsv_at", _boom)
    monkeypatch.setattr(sw, "_ensure_grid_rows", _boom)
    monkeypatch.setattr(sw, "_read_export_csv", _boom)
    monkeypatch.setattr(sw, "_second_tab_gid", _boom)


def test_append_rows_uses_api_and_skips_browser(monkeypatch, tmp_path):
    _enable_sheets_api(tmp_path)
    _forbid_browser_paths(monkeypatch, sw)

    calls = []

    def fake_post(url, json, timeout, follow_redirects=True):
        calls.append(json["action"])
        if json["action"] == "snapshot":
            n = 1 if len(calls) == 1 else 3  # append 전 1행(헤더), append 후 3행
            rows = [["카페"] + [""] * 13] + [[""] * 14] * (n - 1)
            return _FakeHttpResp({"ok": True, "result": {"rows": rows, "last_row": n}})
        if json["action"] == "append":
            return _FakeHttpResp({"ok": True, "result": {"added": 2, "skipped": 0, "first_row": 2, "last_row": 3}})
        raise AssertionError(f"예상 못한 action: {json['action']}")

    monkeypatch.setattr(sw._sheets_api.httpx, "post", fake_post)

    # 실제 시트 스키마처럼 위치로 열을 맞춘다(0=A=카페 ... 7=H=키워드, 8=I=통합검색).
    hdr = ["카페", "B", "C", "D", "E", "F", "G", "키워드", "통합검색"]
    res = sw.append_rows(
        "sid1",
        "탭",
        [{"키워드": "kw1", "통합검색": "url1"}, {"키워드": "kw2", "통합검색": "url2"}],
        header=hdr,
        repo_root=tmp_path,
    )
    assert res["mode"] == "sheets"
    assert res["written"] == 2
    assert calls == ["snapshot", "append", "snapshot"]


def test_append_rows_api_aborts_when_a1_not_cafe(monkeypatch, tmp_path):
    _enable_sheets_api(tmp_path)
    _forbid_browser_paths(monkeypatch, sw)

    def fake_post(url, json, timeout, follow_redirects=True):
        assert json["action"] == "snapshot"
        rows = [["A1265"] + [""] * 13]
        return _FakeHttpResp({"ok": True, "result": {"rows": rows, "last_row": 1}})

    monkeypatch.setattr(sw._sheets_api.httpx, "post", fake_post)

    hdr = ["카페", "B", "C", "D", "E", "F", "G", "키워드"]
    res = sw.append_rows("sid1", "탭", [{"키워드": "kw1"}], header=hdr, repo_root=tmp_path)
    assert res["written"] == 0
    assert res["mode"] == "csv_only"
    assert "A1" in res["error"]


def test_append_rows_api_aborts_on_row_count_mismatch(monkeypatch, tmp_path):
    _enable_sheets_api(tmp_path)
    _forbid_browser_paths(monkeypatch, sw)
    calls = []

    def fake_post(url, json, timeout, follow_redirects=True):
        calls.append(json["action"])
        if json["action"] == "snapshot":
            n = 1 if calls.count("snapshot") == 1 else 2  # added=2라고 응답했는데 실제로는 1행만 늚
            rows = [["카페"] + [""] * 13] + [[""] * 14] * (n - 1)
            return _FakeHttpResp({"ok": True, "result": {"rows": rows, "last_row": n}})
        return _FakeHttpResp({"ok": True, "result": {"added": 2, "skipped": 0}})

    monkeypatch.setattr(sw._sheets_api.httpx, "post", fake_post)

    hdr = ["카페", "B", "C", "D", "E", "F", "G", "키워드"]
    res = sw.append_rows("sid1", "탭", [{"키워드": "kw1"}, {"키워드": "kw2"}], header=hdr, repo_root=tmp_path)
    assert res["mode"] == "csv_only"
    assert "불일치" in res["error"]


def test_update_by_key_uses_api_when_enabled(monkeypatch, tmp_path):
    _enable_sheets_api(tmp_path)
    _forbid_browser_paths(monkeypatch, sw)
    captured = {}

    def fake_post(url, json, timeout, follow_redirects=True):
        assert json["action"] == "update_by_key"
        captured["updates"] = json["updates"]
        return _FakeHttpResp({"ok": True, "result": {"updated": 1, "missing": []}})

    monkeypatch.setattr(sw._sheets_api.httpx, "post", fake_post)

    res = sw.update_by_key("sid1", "탭", "수면테이프", {"G": "확인", "J": "2026-09-25 10:00:00"}, key_column="H", repo_root=tmp_path)
    assert res["mode"] == "sheets"
    assert captured["updates"] == [{"keyword": "수면테이프", "G": "확인", "J": "2026-09-25 10:00:00"}]


def test_update_by_key_api_missing_key_returns_error(monkeypatch, tmp_path):
    _enable_sheets_api(tmp_path)
    _forbid_browser_paths(monkeypatch, sw)

    def fake_post(url, json, timeout, follow_redirects=True):
        return _FakeHttpResp({"ok": True, "result": {"updated": 0, "missing": ["없는키워드"]}})

    monkeypatch.setattr(sw._sheets_api.httpx, "post", fake_post)
    res = sw.update_by_key("sid1", "탭", "없는키워드", {"G": "확인"}, key_column="H", repo_root=tmp_path)
    assert res["mode"] == "csv_only"
    assert "찾지 못함" in res["error"]


def test_apply_exposure_batches_updates_via_api(monkeypatch, tmp_path):
    """러너 노출 순환 갱신은 API 모드에서 50개씩 묶어 update_by_key를 부른다."""
    _enable_sheets_api(tmp_path)
    cfg_dir = tmp_path / "config"
    (cfg_dir / "brands.yaml").write_text(
        "brands:\n  테스트브랜드:\n    spreadsheet_id: sid1\n", encoding="utf-8"
    )
    monkeypatch.setattr(sw, "_write_tsv_at", lambda *a, **k: (_ for _ in ()).throw(AssertionError("browser 호출됨")))
    monkeypatch.setattr(sw, "_read_export_csv", lambda *a, **k: (_ for _ in ()).throw(AssertionError("csv 호출됨")))

    calls = []

    def fake_post(url, json, timeout, follow_redirects=True):
        calls.append(json["action"])
        if json["action"] == "snapshot":
            rows = [["카페"] + [""] * 13, [""] * 7 + ["kw1"] + [""] * 6]
            return _FakeHttpResp({"ok": True, "result": {"rows": rows, "last_row": 2}})
        if json["action"] == "update_by_key":
            return _FakeHttpResp({"ok": True, "result": {"updated": len(json["updates"]), "missing": []}})
        raise AssertionError(json["action"])

    monkeypatch.setattr(sw._sheets_api.httpx, "post", fake_post)

    rows = [{"keyword": f"kw{i}", "status": "노출완", "edited_at": "2026-09-25 10:00:00", "volume": 10, "cafe": "카페A"} for i in range(120)]
    res = sw.apply_exposure("테스트브랜드", rows, repo_root=tmp_path, config_path="config/brands.yaml")
    assert res["written"] == 120
    # 120개를 50개씩 3묶음(50+50+20)으로 나눠 보냈는지
    assert calls.count("update_by_key") == 3


def test_delete_rows_by_key_requires_api(tmp_path):
    res = sw.delete_rows_by_key("sid1", ["kw1"], repo_root=tmp_path)
    assert res["mode"] == "csv_only"
    assert "API" in res["error"]
    assert res["deleted"] == 0


def test_delete_rows_by_key_uses_api(monkeypatch, tmp_path):
    _enable_sheets_api(tmp_path)
    captured = {}

    def fake_post(url, json, timeout, follow_redirects=True):
        assert json["action"] == "delete_by_key"
        captured["keywords"] = json["keywords"]
        return _FakeHttpResp({"ok": True, "result": {"deleted": 2}})

    monkeypatch.setattr(sw._sheets_api.httpx, "post", fake_post)
    res = sw.delete_rows_by_key("sid1", ["kw1", "kw2"], repo_root=tmp_path)
    assert res == {"deleted": 2, "mode": "sheets"}
    assert captured["keywords"] == ["kw1", "kw2"]


class _FakeHttpResp:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json
