"""scripts/supervisor.py 의 순수 판정 로직 단위 테스트."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("v2r_supervisor", ROOT / "scripts" / "supervisor.py")
supervisor = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("v2r_supervisor", supervisor)
SPEC.loader.exec_module(supervisor)  # type: ignore[union-attr]


def test_decide_action_fresh_heartbeat_is_ok_even_without_process():
    assert supervisor.decide_action(heartbeat_age=10.0, stale_seconds=420.0, process_alive=False) == "ok"


def test_decide_action_fresh_heartbeat_is_ok_with_process():
    assert supervisor.decide_action(heartbeat_age=10.0, stale_seconds=420.0, process_alive=True) == "ok"


def test_decide_action_stale_heartbeat_no_process_restarts():
    assert supervisor.decide_action(heartbeat_age=500.0, stale_seconds=420.0, process_alive=False) == "restart"


def test_decide_action_stale_heartbeat_with_process_is_stalled():
    assert supervisor.decide_action(heartbeat_age=500.0, stale_seconds=420.0, process_alive=True) == "stalled"


def test_decide_action_missing_heartbeat_no_process_restarts():
    assert supervisor.decide_action(heartbeat_age=None, stale_seconds=420.0, process_alive=False) == "restart"


def test_decide_action_missing_heartbeat_with_process_is_stalled():
    # 최초 실행 등으로 기록이 없을 때 프로세스가 있으면 아직 재기동은 보류한다.
    assert supervisor.decide_action(heartbeat_age=None, stale_seconds=420.0, process_alive=True) == "stalled"


def test_can_restart_no_history_allows_restart():
    assert supervisor.can_restart("x", {}, 900.0, now_epoch=1_000_000.0) is True


def test_can_restart_within_cooldown_blocks():
    last_map = {"x": {"last_restart_epoch": 1_000_000.0}}
    assert supervisor.can_restart("x", last_map, 900.0, now_epoch=1_000_500.0) is False


def test_can_restart_after_cooldown_allows():
    last_map = {"x": {"last_restart_epoch": 1_000_000.0}}
    assert supervisor.can_restart("x", last_map, 900.0, now_epoch=1_000_901.0) is True


def test_to_epoch_parses_iso_and_handles_z_suffix():
    a = supervisor.to_epoch("2026-09-27T00:00:00+00:00")
    b = supervisor.to_epoch("2026-09-27T00:00:00Z")
    assert a == b
    assert a is not None


def test_to_epoch_returns_none_for_missing_or_bad_value():
    assert supervisor.to_epoch(None) is None
    assert supervisor.to_epoch("") is None
    assert supervisor.to_epoch("not-a-date") is None


def test_fill_and_rescore_brand_lists_match_existing_scripts():
    # scripts/rescore.cmd(6개, 갱년기 포함)·scripts/keyword-fill-hidden.cmd(5개)와 동일해야 한다.
    assert supervisor.RESCORE_BRANDS == ["우아덤", "코숨핏", "뉴더미스", "장으뜸", "팥순이", "갱년기"]
    assert supervisor.FILL_BRANDS == ["우아덤", "코숨핏", "뉴더미스", "장으뜸", "팥순이"]
