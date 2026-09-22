"""새 모양 현황판·일일 보고·중간 보고 생성기 테스트 (2026-09-22).

외부 자원은 건드리지 않는다. 보고서 파일은 전부 임시 폴더에만 쓴다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from v2r.command.parser import parse_korean_command
from v2r.command.spec import ALLOWED_TASKS, TaskSpec
from v2r.engine import worker
from v2r.engine.dashboard import (
    SECTION_TITLES,
    build_daily_report,
    build_dashboard,
    build_progress_report,
    collect,
    limited_sql,
    manuscript_stock,
    mask_account,
)
from v2r.store.db import KST, now_iso
from tests.test_engine import make_runtime


def _mark(rt, source, row, hashv, status, *, cafe, account, menu, at, stage=""):
    """publications 한 줄을 시각까지 정해서 넣는다."""
    rt.publications.mark(
        source, row, hashv, status, cafe=cafe, account=account, menu_id=menu, stage=stage
    )
    rt.conn.execute(
        "UPDATE publications SET created_at = ?, updated_at = ?"
        " WHERE source_key = ? AND row_number = ? AND content_hash = ?",
        (at, at, source, row, hashv),
    )


def _seed_day(rt, date: str) -> None:
    """그날 카페 두 곳에 발행 기록을 넣는다."""
    # 고요한 아침: 3건 성공(게시판 2곳, 한 번 연속), 계정 2개
    _mark(rt, "시트A", 1, "h1", "done", cafe="고요한 아침", account="user01",
          menu="11", at=f"{date}T09:05:00+09:00")
    _mark(rt, "시트A", 2, "h2", "done", cafe="고요한 아침", account="user02",
          menu="11", at=f"{date}T09:20:00+09:00")  # 같은 게시판 연속 1
    _mark(rt, "시트A", 3, "h3", "done", cafe="고요한 아침", account="user01",
          menu="12", at=f"{date}T10:40:00+09:00")
    # 송도포털: 성공 1 · 제한 1 · 미확정 1
    _mark(rt, "시트B", 1, "h4", "done", cafe="송도포털", account="user03",
          menu="30", at=f"{date}T09:10:00+09:00")
    _mark(rt, "시트B", 2, "h5", "failed", cafe="송도포털", account="user03",
          menu="30", at=f"{date}T18:37:00+09:00", stage="등록 제한(ID/IP)")
    _mark(rt, "시트B", 3, "h6", "uncertain", cafe="송도포털", account="user04",
          menu="31", at=f"{date}T18:40:00+09:00")


def _seed_target(rt, date: str, count: int = 3) -> None:
    """그날 '카페별 N건' 발행 작업을 넣어 목표를 만든다."""
    spec = TaskSpec(task="publish_daily", count=count, per_cafe=True, cafe="고요한 아침")
    ts = f"{date}T09:00:00+09:00"
    rt.conn.execute(
        "INSERT INTO jobs (idem_key, task, spec_json, status, created_at, updated_at)"
        " VALUES (?, 'publish_daily', ?, 'running', ?, ?)",
        (f"goal-{date}", spec.to_json(), ts, ts),
    )


# --------------------------------------------------------------------
# 1) 현황판(오늘 기준) — data/dashboard.html
# --------------------------------------------------------------------
def test_dashboard_has_new_sections(tmp_path):
    rt = make_runtime(tmp_path)
    try:
        today = now_iso()[:10]
        _seed_day(rt, today)
        path = build_dashboard(rt)
        assert path.name == "dashboard.html"
        assert path.parent == rt.settings.data_dir
        html = path.read_text(encoding="utf-8")
    finally:
        rt.close()

    for title in SECTION_TITLES.values():
        assert title in html, title
    # 바깥 자원을 부르지 않는 자급자족 한 장
    assert "<script" not in html
    assert "cdn" not in html
    assert "prefers-color-scheme: dark" in html
    assert "viewport" in html
    # 6칸 카드
    for card in ("발행 성공", "사용 계정", "같은 게시판 연속", "V2R 로그인", "원고 비용", "실행기"):
        assert card in html, card
    # 카페별 표의 칸
    for head in ("제한 글", "미확정", "계정 수", "게시판 수", "게시판 연속", "상태"):
        assert head in html, head
    assert "고요한 아침" in html and "송도포털" in html
    # 시간대별 막대 24칸
    assert html.count("<span>0") + html.count("<span>1") + html.count("<span>2") >= 24


def test_dashboard_브랜드_키워드_노출_섹션(tmp_path):
    """collect()가 keyword_exposure.summary(rt)를 그대로 담고, 현황판에 렌더된다."""
    from v2r.knowledge.keyword_exposure import ExposureRow
    from v2r.store import keyword_exposure_store as store

    rt = make_runtime(tmp_path)
    try:
        store.save(
            rt.conn,
            ExposureRow(
                "우아덤", "다이어트", "씨씨앙",
                "https://cafe.naver.com/mycafe/1", 3, "exposed",
                "2026-09-21T08:00:00+09:00",
            ).as_row(),
        )
        store.save(
            rt.conn,
            ExposureRow(
                "우아덤", "홈트", "씨씨앙", "", None, "unpublished",
                "2026-09-22T08:00:00+09:00",
            ).as_row(),
        )
        data = collect(rt, kind="live")
        assert data.exposure["우아덤"]["exposed"] == 1
        assert data.exposure["우아덤"]["unpublished"] == 1

        path = build_dashboard(rt)
        html = path.read_text(encoding="utf-8")
    finally:
        rt.close()

    assert "브랜드 키워드 노출" in html
    assert "우아덤" in html


def test_dashboard_empty_db(tmp_path):
    rt = make_runtime(tmp_path)
    try:
        path = build_dashboard(rt)
        html = path.read_text(encoding="utf-8")
    finally:
        rt.close()
    assert SECTION_TITLES["header"] in html
    assert "발행 기록이 없습니다." in html
    # 빈 DB에서도 모든 섹션이 그려진다
    for title in SECTION_TITLES.values():
        assert title in html, title


# --------------------------------------------------------------------
# 2) 실측 수치
# --------------------------------------------------------------------
def test_collect_counts_are_measured(tmp_path):
    rt = make_runtime(tmp_path)
    try:
        date = "2026-09-21"
        _seed_day(rt, date)
        data = collect(rt, kind="daily", date=date)
    finally:
        rt.close()

    rows = {r["cafe"]: r for r in data.cafes}
    assert rows["고요한 아침"]["done"] == 3
    assert rows["고요한 아침"]["accounts"] == 2
    assert rows["고요한 아침"]["boards"] == 2
    assert rows["고요한 아침"]["board_run"] == 1  # 09:05·09:20 이 같은 게시판
    assert rows["송도포털"]["done"] == 1
    assert rows["송도포털"]["limited"] == 1     # stage 에 '제한' 표시
    assert rows["송도포털"]["failed"] == 0      # 제한은 실패 칸에서 뺀다
    assert rows["송도포털"]["uncertain"] == 1
    assert data.totals["done"] == 4
    assert data.totals["limited"] == 1
    # 시간대별: 09시 3건, 10시 1건
    assert data.hourly[9] == 3
    assert data.hourly[10] == 1
    assert sum(data.hourly) == 4


def test_limited_is_loose_and_zero_without_marks(tmp_path):
    """'제한' 표시가 없으면 0이 된다(다른 일꾼이 정리 중이라 느슨하게 본다)."""
    rt = make_runtime(tmp_path)
    try:
        date = "2026-09-20"
        _mark(rt, "시트C", 1, "z1", "failed", cafe="러브 인썸", account="u",
              menu="9", at=f"{date}T11:00:00+09:00", stage="등록 응답 없음")
        data = collect(rt, kind="daily", date=date)
        sql = limited_sql(rt)
    finally:
        rt.close()
    assert "제한" in sql and "status = 'failed'" in sql
    assert data.totals["limited"] == 0
    assert data.totals["failed"] == 1


# --------------------------------------------------------------------
# 3) 일일 보고 (어제 기준) 파일 두 장
# --------------------------------------------------------------------
def test_build_daily_report_files(tmp_path):
    rt = make_runtime(tmp_path)
    out = tmp_path / "reports"
    try:
        date = "2026-09-23"  # 손으로 만든 1안(2026-09-21)과 안 겹치는 날짜
        _seed_day(rt, date)
        html_path, md_path = build_daily_report(rt, date=date, out_dir=out)
    finally:
        rt.close()
    assert html_path.name == "dashboard-2026-09-23.html"
    assert md_path.name == "dashboard-2026-09-23.md"
    html = html_path.read_text(encoding="utf-8")
    md = md_path.read_text(encoding="utf-8")
    for title in SECTION_TITLES.values():
        assert title in html and title in md, title
    assert "2026-09-23" in html and "(어제)" in html
    assert "| 고요한 아침 |" in md
    assert "**합계**" in md
    # 일일 보고에는 진행률 칸이 없다
    assert "목표 대비 진행률" not in html


def test_build_daily_report_never_overwrites_hand_made_1안(tmp_path):
    """2026-09-21 은 손으로 만든 1안 — 자동 생성기는 반드시 '-auto' 꼬리표로 비켜 간다.

    2026-09-22 사고: 예약이 같은 날짜 파일명과 겹쳐 1안을 실수로 덮어썼다.
    """
    rt = make_runtime(tmp_path)
    out = tmp_path / "reports"
    hand_made_html = out / "dashboard-2026-09-21.html"
    hand_made_md = out / "dashboard-2026-09-21.md"
    out.mkdir(parents=True, exist_ok=True)
    hand_made_html.write_text("손으로 만든 1안 HTML", encoding="utf-8")
    hand_made_md.write_text("손으로 만든 1안 MD", encoding="utf-8")
    try:
        date = "2026-09-21"
        _seed_day(rt, date)
        html_path, md_path = build_daily_report(rt, date=date, out_dir=out)
    finally:
        rt.close()
    assert html_path.name == "dashboard-2026-09-21-auto.html"
    assert md_path.name == "dashboard-2026-09-21-auto.md"
    # 1안은 그대로다
    assert hand_made_html.read_text(encoding="utf-8") == "손으로 만든 1안 HTML"
    assert hand_made_md.read_text(encoding="utf-8") == "손으로 만든 1안 MD"


def test_daily_report_defaults_to_yesterday(tmp_path):
    rt = make_runtime(tmp_path)
    out = tmp_path / "reports"
    try:
        now = datetime.now(KST)
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        _seed_day(rt, yesterday)
        html_path, _ = build_daily_report(rt, out_dir=out)
    finally:
        rt.close()
    # 손으로 만든 1안(2026-09-21)과 겹치는 날이면 '-auto' 꼬리표가 붙는다
    from v2r.engine.dashboard import PROTECTED_STEMS

    stem = f"dashboard-{yesterday}"
    expect = f"{stem}-auto" if stem in PROTECTED_STEMS else stem
    assert html_path.name == f"{expect}.html"


def test_build_daily_report_empty_db(tmp_path):
    rt = make_runtime(tmp_path)
    out = tmp_path / "reports"
    try:
        html_path, md_path = build_daily_report(rt, date="2026-01-01", out_dir=out)
    finally:
        rt.close()
    assert html_path.is_file() and md_path.is_file()
    assert "발행 기록이 없습니다." in html_path.read_text(encoding="utf-8")
    assert SECTION_TITLES["overview"] in md_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------
# 4) 중간 보고 (오늘 기준) — 진행률·남은 건수·예상 종료
# --------------------------------------------------------------------
def test_build_progress_report(tmp_path):
    rt = make_runtime(tmp_path)
    out = tmp_path / "reports"
    try:
        today = now_iso()[:10]
        _seed_day(rt, today)
        _seed_target(rt, today, count=10)
        now = datetime.now(KST).replace(hour=12, minute=0, second=0, microsecond=0)
        html_path, md_path = build_progress_report(rt, now=now, out_dir=out)
    finally:
        rt.close()
    assert html_path.name == f"progress-{today}-1200.html"
    assert md_path.name == f"progress-{today}-1200.md"
    html = html_path.read_text(encoding="utf-8")
    md = md_path.read_text(encoding="utf-8")
    # 한눈에 카드에 진행률이 더 붙는다
    assert "목표 대비 진행률" in html and "목표 대비 진행률" in md
    # 카페별 표에 남은 건수·예상 종료 시각
    assert "남은 건수" in html and "예상 종료" in html
    assert "남은 건수" in md and "예상 종료" in md
    # 고요한 아침 목표 10건 중 3건 → 남은 7건
    assert "| 7 |" in md


def test_progress_targets_from_jobs(tmp_path):
    rt = make_runtime(tmp_path)
    try:
        today = now_iso()[:10]
        _seed_day(rt, today)
        _seed_target(rt, today, count=10)
        data = collect(rt, kind="progress")
    finally:
        rt.close()
    assert data.targets == {"고요한 아침": 10}
    assert data.target_total == 10
    assert data.remaining_total == 7
    assert 0.3 <= data.rate <= 0.45


# --------------------------------------------------------------------
# 5) 원고 잔량·계정 가리기(기존 기능 유지)
# --------------------------------------------------------------------
def test_manuscript_stock_counts(tmp_path):
    rt = make_runtime(tmp_path)
    try:
        pool = rt.settings.warehouse_dir / "manuscripts" / "affiliate_daily_pool.jsonl"
        pool.parent.mkdir(parents=True, exist_ok=True)
        pool.write_text(
            "\n".join(
                json.dumps(d, ensure_ascii=False)
                for d in [
                    {"title": "가", "body": "본문", "cafe": "씨씨앙", "content_hash": "h1"},
                    {"title": "나", "body": "본문", "cafe": "씨씨앙", "content_hash": "h9"},
                    {"title": "다", "body": "본문", "cafe": "양평맘", "content_hash": "h8"},
                ]
            ),
            encoding="utf-8",
        )
        sheets = rt.settings.warehouse_dir / "inbox" / "sheets"
        sheets.mkdir(parents=True, exist_ok=True)
        (sheets / "각색1.xlsx").write_bytes(b"x")
        (sheets / "각색2.xlsx").write_bytes(b"x")
        rt.publications.mark("시트A", 1, "h1", "done", cafe="씨씨앙", account="user01")

        stock = manuscript_stock(rt)
        assert stock["pool_total"] == 3
        assert stock["pool_unused"] == 2  # h1은 이미 발행됨
        assert stock["xlsx"] == 2
        assert {r["cafe"]: r["unused"] for r in stock["pool"]} == {"씨씨앙": 1, "양평맘": 1}
    finally:
        rt.close()


def test_mask_account():
    assert mask_account("testuser01") == "tes…"
    assert mask_account("ab") == "ab…"
    assert mask_account("") == "-"
    assert mask_account(None) == "-"


# --------------------------------------------------------------------
# 6) 명령·작업 연결
# --------------------------------------------------------------------
def test_parser_routes_dashboard():
    assert "dashboard" in ALLOWED_TASKS
    for text in ("현황판", "현황판 갱신해줘", "대시보드 보여줘", "대시 보드"):
        spec = parse_korean_command(text)
        assert spec is not None and spec.task == "dashboard", text
    # 기존 상태 조회는 그대로
    for text in ("현황 알려줘", "상태 알려줘"):
        spec = parse_korean_command(text)
        assert spec is not None and spec.task == "status", text


def test_parser_routes_reports():
    assert "daily_report" in ALLOWED_TASKS
    assert "progress_report" in ALLOWED_TASKS
    for text in ("일일 보고", "어제 보고서 보내줘", "하루 보고"):
        spec = parse_korean_command(text)
        assert spec is not None and spec.task == "daily_report", text
    for text in ("중간 보고", "중간 보고서 보내줘", "진행 보고"):
        spec = parse_korean_command(text)
        assert spec is not None and spec.task == "progress_report", text


def test_worker_dashboard_task(tmp_path):
    rt = make_runtime(tmp_path)
    try:
        accepted = worker.handle_text(rt, "현황판 갱신")
        assert accepted["ok"]
        out = worker.run_once(rt)
        assert out is not None and out["status"] == "done"
        path = out["result"]["path"]
        assert path.endswith("dashboard.html")
        assert (rt.settings.data_dir / "dashboard.html").exists()
        assert "현황판" in out["result"]["message"]
    finally:
        rt.close()


def test_worker_report_tasks_send_files(tmp_path, monkeypatch):
    """보고서는 미처리 보고와 같은 길(파일 전송)로 나간다."""
    rt = make_runtime(tmp_path)
    out_dir = tmp_path / "reports"
    sent: list[tuple[str, str]] = []

    def fake_send(channels, path, caption):
        del channels
        sent.append((str(path), caption))
        return 1

    monkeypatch.setattr("v2r.channels.notify_document_all", fake_send)
    monkeypatch.setattr(
        "v2r.engine.dashboard.build_daily_report",
        lambda r, **kw: _orig_daily(r, out_dir=out_dir),
    )
    monkeypatch.setattr(
        "v2r.engine.dashboard.build_progress_report",
        lambda r, **kw: _orig_progress(r, out_dir=out_dir),
    )
    try:
        for text, stem in (("일일 보고", "dashboard-"), ("중간 보고", "progress-")):
            sent.clear()
            assert worker.handle_text(rt, text)["ok"]
            res = worker.run_once(rt)
            assert res is not None and res["status"] == "done", text
            assert res["result"]["sent"] == 2  # MD + HTML 두 장
            assert len(sent) == 2
            assert all(stem in p for p, _ in sent), text
    finally:
        rt.close()


_orig_daily = build_daily_report
_orig_progress = build_progress_report


def test_schedule_yaml_has_report_entries():
    """예약표에 02:30 일일 보고 + 12:00·15:00·18:00 중간 보고가 있다."""
    from v2r.config import load_yaml

    entries = {e["name"]: e for e in (load_yaml("schedule") or {}).get("entries", [])}
    assert entries["일일 보고"]["time"] == "02:30"
    assert entries["일일 보고"]["command"] == "일일 보고"
    for i, when in enumerate(("12:00", "15:00", "18:00"), start=1):
        entry = entries[f"중간 보고-{i}"]
        assert entry["time"] == when
        assert entry["command"] == "중간 보고"
        assert entry["enabled"] is True
    # 미처리 보고는 사용자 지시(2026-09-23)로 하루 5회 → 18:00 1회로 축소됐다
    assert "pending-1" in entries
    assert entries["pending-1"]["time"] == "18:00"
    for i in range(2, 6):
        assert f"pending-{i}" not in entries


def test_reports_are_light_tasks():
    from v2r.engine import sidecar as sidecar_mod

    assert "daily_report" in sidecar_mod.LIGHT_TASKS
    assert "progress_report" in sidecar_mod.LIGHT_TASKS
