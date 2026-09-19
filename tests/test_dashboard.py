"""운영 현황판(HTML) 생성기 테스트. 외부 자원은 건드리지 않는다."""

from __future__ import annotations

import json

from v2r.command.parser import parse_korean_command
from v2r.command.spec import ALLOWED_TASKS, TaskSpec
from v2r.engine import worker
from v2r.engine.dashboard import SECTION_TITLES, build_dashboard, mask_account
from v2r.store.db import now_iso
from tests.test_engine import make_runtime


def _seed(rt) -> None:
    """작업·발행·이벤트를 조금 넣어 둔다."""
    ts = now_iso()
    today = ts[:10]

    rt.jobs.enqueue(TaskSpec(task="publish_daily", notes="고요한 아침에 일상 글 3개"), "k1")
    job2 = rt.jobs.enqueue(TaskSpec(task="status", notes="현황 알려줘"), "k2")
    rt.conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job2,))
    rt.events.log(job2, "info", "2번째 글 등록 중")
    rt.events.log(job2, "warn", "계정 하나가 제한 상태입니다")
    rt.events.log(None, "error", "네트워크 오류로 재시도")

    rt.conn.execute(
        "INSERT INTO jobs (idem_key, task, spec_json, status, result_json, created_at, updated_at)"
        " VALUES ('k3', 'reconcile', '{\"task\": \"reconcile\"}', 'done', ?, ?, ?)",
        (
            json.dumps({"checked": 4, "done": 3, "failed": 1, "unresolved": ["a"]}),
            ts,
            ts,
        ),
    )

    rt.publications.mark(
        "시트A", 1, "h1", "done",
        cafe="고요한 아침", account="testuser01", url="https://example.com/a1", menu_id="101",
    )
    rt.publications.mark(
        "시트A", 2, "h2", "uncertain", cafe="고요한 아침", account="testuser02",
    )
    rt.publications.mark(
        "시트B", 1, "h3", "failed", cafe="씨씨앙", account="ab",
    )
    assert today  # 오늘 날짜 기준으로 기록된다


def test_build_dashboard_has_sections(tmp_path):
    rt = make_runtime(tmp_path)
    try:
        _seed(rt)
        path = build_dashboard(rt)
        assert path.name == "dashboard.html"
        assert path.parent == rt.settings.data_dir
        html = path.read_text(encoding="utf-8")
    finally:
        rt.close()

    for title in SECTION_TITLES.values():
        assert title in html
    # 바깥 자원을 부르지 않는 자급자족 파일
    assert "<script" not in html
    assert "cdn" not in html
    assert 'prefers-color-scheme: dark' in html
    assert "viewport" in html
    # 카페 행·가린 계정·링크
    assert "고요한 아침" in html
    assert "씨씨앙" in html
    assert "tes…" in html
    assert "testuser01" not in html
    assert 'href="https://example.com/a1"' in html
    # 점검 요약
    assert "확인 4건" in html
    assert "계정 하나가 제한 상태입니다" in html


def test_build_dashboard_empty_db(tmp_path):
    rt = make_runtime(tmp_path)
    try:
        path = build_dashboard(rt)
        html = path.read_text(encoding="utf-8")
    finally:
        rt.close()
    assert SECTION_TITLES["header"] in html
    assert "발행 기록이 없습니다." in html


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

        from v2r.engine.dashboard import manuscript_stock

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


def test_parser_routes_dashboard():
    assert "dashboard" in ALLOWED_TASKS
    for text in ("현황판", "현황판 갱신해줘", "대시보드 보여줘", "대시 보드"):
        spec = parse_korean_command(text)
        assert spec is not None and spec.task == "dashboard", text
    # 기존 상태 조회는 그대로
    for text in ("현황 알려줘", "상태 알려줘"):
        spec = parse_korean_command(text)
        assert spec is not None and spec.task == "status", text


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
