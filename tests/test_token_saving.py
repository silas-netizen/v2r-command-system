"""토큰 절약 조치 검증 (2026-09-22).

- 본문·댓글·부분 재시도의 system 프롬프트가 **글자 하나까지 같은가** (캐시 조건)
- 정리본 지침 압축률이 목표(30%)를 넘는가 + 지워서는 안 될 내용이 남았는가
- 묶음 생성이 브랜드끼리 묶이고 같은 브랜드 안에서는 순차인가
- 사용량 장부와 이벤트 정리가 실제로 도는가
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from v2r.content import brand_batch
from v2r.content import brand_writer as bw
from v2r.content.guide_compress import compress_guide_text, compression_ratio
from v2r.engine import maintenance
from v2r.llm import usage_ledger
from v2r.store.db import connect, init_schema

GUIDE_DIR = Path(__file__).resolve().parents[1] / "warehouse" / "guides" / "정리본"
BUNDLES = [
    ("우아덤", "질문형"),
    ("장으뜸", "질문형"),
    ("코숨핏", "질문형"),
    ("뉴더미스", "질문형"),
    ("팥순이", "질문형"),
    ("팥순이", "후기형"),
]


# --- 1. 브랜드당 system 프롬프트 1개 --------------------------------
@pytest.mark.parametrize("brand,mtype", BUNDLES)
def test_three_prompts_share_one_system(brand: str, mtype: str) -> None:
    guide = "지침 본문\n[브랜드 정보]\n- 제품: 시험용"
    examples = ["예시 하나", "예시 둘"]
    body_sys, body_user = bw.build_body_prompt(
        brand, "시험키워드", "카페", guide, mtype, examples
    )
    cmt_sys, cmt_user = bw.build_comments_prompt(
        brand, "시험키워드", "제목", "본문", mtype, guide, examples
    )
    fix_sys, fix_user = bw.build_partial_retry_prompt(
        brand, "시험키워드", ["댓글2"], ["무언가 어김"], mtype, guide, examples
    )
    # 세 호출의 system이 같아야 2·3번째가 cache_read 로 잡힌다
    assert body_sys == cmt_sys == fix_sys
    assert body_sys == bw.build_shared_system(brand, mtype, guide, examples)
    # 할 일과 출력 형식은 user 쪽에 있다
    assert "이번에는 본문만 쓴다" in body_user
    assert "이번에는 댓글만 쓴다" in cmt_user
    assert "걸린 자리만 고쳐 쓴다" in fix_user
    for user in (body_user, cmt_user, fix_user):
        assert "출력" in user
    assert "이번에는 본문만" not in body_sys


def test_shared_system_does_not_change_with_keyword_or_cafe() -> None:
    a, _ = bw.build_body_prompt("우아덤", "키워드가", "카페하나", "지침")
    b, _ = bw.build_body_prompt("우아덤", "키워드나", "카페둘", "지침")
    assert a == b


def test_guide_and_examples_go_in_only_once() -> None:
    mark = "표식문장-중복확인용"
    system = bw.build_shared_system("우아덤", "질문형", f"[브랜드 정보]\n{mark}", [mark])
    assert system.count(mark) == 2  # 예시 1번 + 지침 1번 (예전에는 각각 두 번씩)


def test_sheet_examples_are_capped_at_three() -> None:
    lines = bw.examples_block([f"예시 {i}" for i in range(10)])
    assert "[예시 3]" in "\n".join(lines)
    assert "[예시 4]" not in "\n".join(lines)
    assert bw.MAX_PROMPT_EXAMPLES == 3


# --- 2. 묶음 순서와 동시 실행 ---------------------------------------
def test_order_bundles_groups_same_brand_together() -> None:
    mixed = [("우아덤", "질문형"), ("팥순이", "질문형"), ("우아덤", "후기형")]
    assert brand_batch.order_bundles(mixed) == [
        ("우아덤", "질문형"),
        ("우아덤", "후기형"),
        ("팥순이", "질문형"),
    ]


def test_run_bundles_is_sequential_inside_a_brand_and_keeps_order() -> None:
    running: dict[str, int] = {}
    overlap: list[str] = []
    lock = threading.Lock()

    def run_one(brand: str, mtype: str, index: int) -> str:
        with lock:
            running[brand] = running.get(brand, 0) + 1
            if running[brand] > 1:
                overlap.append(brand)
        time.sleep(0.01)
        with lock:
            running[brand] -= 1
        return f"{index}:{brand}/{mtype}"

    items = brand_batch.order_bundles(BUNDLES)
    out = brand_batch.run_bundles(items, run_one, max_brands=2)
    assert not overlap  # 같은 브랜드는 절대 동시에 돌지 않는다
    assert out == [f"{i}:{b}/{t}" for i, (b, t) in enumerate(items)]


def test_run_bundles_keeps_going_when_one_fails() -> None:
    def run_one(brand: str, mtype: str, index: int) -> dict:
        if brand == "장으뜸":
            raise RuntimeError("일부러 실패")
        return {"brand": brand, "ok": True}

    out = brand_batch.run_bundles(
        [("우아덤", "질문형"), ("장으뜸", "질문형")], run_one, max_brands=2
    )
    assert out[0]["ok"] is True
    assert "일부러 실패" in out[1]["error"]


# --- 3. 지침 압축 ----------------------------------------------------
@pytest.mark.parametrize(
    "name", [p.name for p in sorted(GUIDE_DIR.glob("*.md")) if p.name != "INDEX.md"]
)
def test_guide_compression_hits_the_target(name: str) -> None:
    text = (GUIDE_DIR / name).read_text(encoding="utf-8")
    small = compress_guide_text(text)
    assert compression_ratio(text, small) >= 0.30  # 목표 30% 이상 축소
    # 코드 검증기가 담당하는 규칙은 프롬프트에서 빠진다
    assert "Make에 그대로 붙여넣기" not in small
    assert "## 변경 로그" not in small
    assert "{키워드}" not in small
    # 모델만 할 수 있는 판단(브랜드·설득 논리)은 남는다
    assert "[브랜드 정보" in small or "브랜드 정보" in small


def test_compression_keeps_no_duplicate_lines() -> None:
    text = "[역할]\n같은 문장이다\n\n[브랜드 정보]\n같은 문장이다\n다른 문장이다"
    assert compress_guide_text(text).count("같은 문장이다") == 1


def test_original_guide_files_are_untouched() -> None:
    # 원본 md는 건드리지 않는다 — 압축은 프롬프트에 실을 때만 한다
    text = (GUIDE_DIR / "우아덤.md").read_text(encoding="utf-8")
    assert "Make에 그대로 붙여넣기" in text
    compress_guide_text(text)
    assert (GUIDE_DIR / "우아덤.md").read_text(encoding="utf-8") == text


def test_compressed_guide_is_what_goes_into_the_prompt() -> None:
    text = (GUIDE_DIR / "우아덤.md").read_text(encoding="utf-8")
    system = bw.build_shared_system("우아덤", "질문형", text)
    assert "Make에 그대로 붙여넣기" not in system
    assert compress_guide_text(text)[:40] in system


# --- 4. 사용량 장부 --------------------------------------------------
def test_usage_ledger_splits_by_month_and_counts_cache(tmp_path: Path) -> None:
    when = datetime(2026, 9, 22, 3, 0)
    path = usage_ledger.append_call(
        tmp_path,
        "plan",
        "brand_comments",
        "claude-opus-5",
        {
            "input_tokens": 100,
            "output_tokens": 900,
            "cache_read_input_tokens": 18000,
            "cache_creation_input_tokens": 1900,
        },
        prompt_sha256="abc",
        when=when,
    )
    assert path is not None and path.name == "llm_usage-2026-09.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["backend"] == "plan"
    assert row["cache_read_input_tokens"] == 18000
    assert row["cache_hit_ratio"] == pytest.approx(18000 / 20000)
    assert usage_ledger.read_month(tmp_path, when)[0]["purpose"] == "brand_comments"


def test_cache_hit_ratio_is_zero_without_tokens() -> None:
    assert usage_ledger.cache_hit_ratio({}) == 0.0
    assert usage_ledger.cache_hit_ratio(None) == 0.0


# --- 5. 누적 속도 조치 -----------------------------------------------
def _rt(tmp_path: Path) -> SimpleNamespace:
    conn = connect(tmp_path / "v2r.sqlite")
    init_schema(conn)
    return SimpleNamespace(
        conn=conn,
        settings=SimpleNamespace(data_dir=tmp_path, db_path=tmp_path / "v2r.sqlite"),
    )


def test_purge_old_events_keeps_recent_ones(tmp_path: Path) -> None:
    rt = _rt(tmp_path)
    old = (datetime.now().astimezone() - timedelta(days=40)).isoformat()
    new = datetime.now().astimezone().isoformat()
    for stamp in (old, old, new):
        rt.conn.execute(
            "INSERT INTO events (job_id, level, message, created_at) VALUES (?,?,?,?)",
            (None, "info", "시험", stamp),
        )
    out = maintenance.purge_old_events(rt)
    assert out["deleted"] == 2
    left = rt.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert left == 1


def test_vacuum_runs(tmp_path: Path) -> None:
    rt = _rt(tmp_path)
    assert maintenance.vacuum_db(rt)["ok"] is True


def test_publications_index_exists(tmp_path: Path) -> None:
    rt = _rt(tmp_path)
    names = {
        r["name"]
        for r in rt.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert "idx_pub_cafe_created" in names


def test_clean_plan_work_removes_only_stale_files(tmp_path: Path) -> None:
    folder = tmp_path / "plan-work"
    folder.mkdir()
    fresh = folder / "v2r-sys-new.txt"
    stale = folder / "v2r-sys-old.txt"
    fresh.write_text("새것", encoding="utf-8")
    stale.write_text("헌것", encoding="utf-8")
    old = time.time() - 7200
    import os

    os.utime(stale, (old, old))
    out = maintenance.clean_plan_work(tmp_path)
    assert out["removed"] == 1
    assert fresh.exists() and not stale.exists()


def test_browser_cache_sweep_never_touches_logins(tmp_path: Path) -> None:
    profile = tmp_path / "browser-profile-naver" / "Default"
    (profile / "Cache" / "Cache_Data").mkdir(parents=True)
    (profile / "Cache" / "Cache_Data" / "chunk").write_text("쓰레기", encoding="utf-8")
    (profile / "Code Cache").mkdir()
    (profile / "Code Cache" / "js").write_text("쓰레기", encoding="utf-8")
    (profile / "Network").mkdir()
    (profile / "Network" / "Cookies").write_text("로그인쿠키", encoding="utf-8")
    (profile / "Local Storage").mkdir()
    (profile / "Local Storage" / "leveldb").write_text("세션", encoding="utf-8")

    out = maintenance.sweep_browser_caches(tmp_path)
    assert out["ok"] is True
    # 캐시는 비었고 로그인 자료는 그대로다
    assert not list((profile / "Cache").iterdir())
    assert not list((profile / "Code Cache").iterdir())
    assert (profile / "Network" / "Cookies").read_text(encoding="utf-8") == "로그인쿠키"
    assert (profile / "Local Storage" / "leveldb").exists()


def test_maintenance_task_is_light_and_registered() -> None:
    from v2r.command.parser import parse_korean_command
    from v2r.command.spec import ALLOWED_TASKS
    from v2r.engine.sidecar import is_light

    assert "maintenance" in ALLOWED_TASKS
    assert is_light("maintenance") is True
    assert parse_korean_command("정기 정비").task == "maintenance"


def test_schedule_has_sunday_maintenance() -> None:
    import yaml

    root = Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / "config" / "schedule.yaml").read_text(encoding="utf-8"))
    entry = [e for e in data["entries"] if e.get("command") == "정기 정비"]
    assert entry and entry[0]["time"] == "03:00"
    assert entry[0]["days"] == ["sun"] and entry[0]["enabled"] is True


def test_ledger_never_lands_in_the_real_data_dir(tmp_path: Path) -> None:
    """`data_dir` 없이 만든 라우터도 진짜 장부를 건드리지 못한다 (2026-09-22).

    시험 121줄이 `data/llm_usage-2026-09.jsonl` 에 섞여 들어간 일이 있었다.
    `tests/conftest.py` 가 `V2R_USAGE_LEDGER_DIR` 을 임시 폴더로 돌려 막는다.
    """
    from v2r.llm import usage_ledger

    real = Path(__file__).resolve().parents[1] / "data"
    path = usage_ledger.append_call(real, "api", "brand_body", "claude-sonnet-5", {})
    assert path is not None
    assert real not in path.parents, f"진짜 data 폴더에 적혔습니다: {path}"


def test_plan_lock_writes_an_alert_file(tmp_path: Path) -> None:
    from v2r.llm.router import LLMRouter

    # 알림은 data 폴더의 윗 폴더에 떨어진다 (저장소에서는 docs/reports/alerts)
    router = LLMRouter(api_key="", data_dir=tmp_path / "data")
    router.lock_plan("한도 도달")
    alerts = list((tmp_path / "docs" / "reports" / "alerts").glob("plan-lock-*.md"))
    assert alerts, "요금제 잠금 알림 파일이 없습니다"
    assert "한도 도달" in alerts[0].read_text(encoding="utf-8")


def test_sqlite_connection_closes_cleanly(tmp_path: Path) -> None:
    rt = _rt(tmp_path)
    assert isinstance(rt.conn, sqlite3.Connection)
    rt.conn.close()
