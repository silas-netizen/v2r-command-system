"""노출 확인 우선순위 큐 — `exposure_runner.py` 전용.

설계: `docs/reports/exposure-speed-plan-2026-09-23.md` §3, `docs/reports/reply-priority-recheck.md`.
판정 규칙은 건드리지 않는다 — 이 모듈은 "다음에 어떤 키워드를 검사할지"만
정하고, 실제 검사·판정은 `keyword_exposure.judge_keyword_exposure`(호출만)가 한다.

등급(숫자가 작을수록 먼저) — 2026-09-24 사용자 재정의:
    1. 노출완 — 마지막 판정이 exposed고 재검사 주기(기본 6시간) 지남 (빠르게 확인)
    2. 최근 발행 — 같은 카페에 우리 글이 최근(설정한 시각 근방) 발행된 키워드
    3. 밀려남·미확인 — 검색량 높은 순. 한 번 검사한 키워드는 최소 간격
       (기본 12시간)만 지나면 다시 대상. 48시간/7일 같은 긴 주기는 없다.

연관도 3(무관)은 `keyword_exposure.keyword_universe`가 애초에 뽑지 않으므로
여기서 따로 걸러낼 필요가 없다(연결 3 규칙 그대로 재사용).

같은 등급 안에서는 마지막 검사가 오래된 순(미확인은 항상 맨 앞, 순서는
`keyword_universe`가 준 순서 그대로 안정 정렬).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

CONFIG_PATH = "config/exposure.yaml"

_DEFAULT_PRIORITY = {
    "recent_publish_hours": [2, 6, 24],
    "exposed_recheck_hours": 6,
    "pushed_min_gap_hours": 12,
    # 구 설정 호환(더 이상 등급을 가르지 않음)
    "pushed_recheck_hours": 12,
    "pushed_low_recheck_hours": 12,
    "top_volume_percentile": 0.7,
}

#: 발행 시각이 이 창(±시간) 안이면 "최근 발행"으로 본다
_RECENT_PUBLISH_WINDOW_HOURS = 2.0


def load_config(repo_root: str | Path) -> dict[str, Any]:
    p = Path(repo_root) / CONFIG_PATH
    if not p.exists():
        return {"workers": 2, "priority": dict(_DEFAULT_PRIORITY)}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover - 방어용
        log.warning("exposure.yaml 읽기 실패, 기본값 사용: %s", exc)
        data = {}
    data.setdefault("priority", {})
    for k, v in _DEFAULT_PRIORITY.items():
        data["priority"].setdefault(k, v)
    return data


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _hours_since(checked_at: str, now: datetime) -> float | None:
    dt = _parse_iso(checked_at)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 3600.0


def _recent_publish_keywords(rt: Any, brand: str, cafes: set[str], now: datetime, window_hours: list[float]) -> set[str]:
    """`article_index`에서 최근(설정한 시각 근방) 발행된 우리 글의 제목 맨 앞 키워드."""
    article_index = getattr(rt, "article_index", None)
    if article_index is None or not cafes:
        return set()
    from v2r.knowledge.keyword_exposure import _title_lead_keyword, _norm

    out: set[str] = set()
    for cafe in cafes:
        try:
            rows = article_index.rows_for_cafe(cafe)
        except Exception:
            rows = []
        for row in rows or []:
            ts = row.get("published_at") or row.get("synced_at") or ""
            dt = _parse_iso(str(ts))
            if dt is None:
                continue
            age_h = (now - dt).total_seconds() / 3600.0
            if age_h < 0:
                continue
            for target in window_hours:
                if abs(age_h - float(target)) <= _RECENT_PUBLISH_WINDOW_HOURS:
                    kw = _title_lead_keyword(row.get("title") or "")
                    if kw:
                        out.add(_norm(kw))
                    break
    return out


def priority_tier(
    item: dict,
    last_checked: dict[str, dict],
    recent_publish_norm: set[str],
    cfg: dict,
    volume_threshold: float,
    now: datetime,
) -> tuple[int, float]:
    """이 키워드의 (등급, 정렬용 보조키). 보조키는 클수록(오래될수록) 우선.

    반환하는 두 번째 값은 "마지막 검사 이후 경과 시간(시간)" — 미확인은 무한대로
    취급해 항상 그 등급 안에서 맨 앞에 온다.
    """
    from v2r.knowledge.keyword_exposure import _norm

    key = _norm(item.get("keyword", ""))
    last = last_checked.get(key)
    # "기존 노출완" = 시트 G열이 노출완인 키워드(아직 러너가 제대로 안 본 것 포함) 또는
    # 러너 마지막 판정이 exposed — 2026-09-24 사용자: DB 판정만 보면 안 됨(아직 노출 검사가
    # 제대로 안 됐으므로 시트 기준이 우선).
    sheet_exposed = str(item.get("t0_status") or "").replace(" ", "") in ("노출완", "exposed")
    if last is None:
        if sheet_exposed:
            return (1, float("inf"))
        # 미확인 — 3등급(밀려남과 같이 검색량 순), 경과 시간은 무한대
        return (3, float("inf"))

    age_h = _hours_since(last.get("checked_at", ""), now)
    age_h = age_h if age_h is not None else float("inf")
    status = last.get("status", "")

    if status == "exposed" or sheet_exposed:
        due = float(cfg.get("exposed_recheck_hours", 6))
        return (1, age_h) if age_h >= due else (99, age_h)

    if key in recent_publish_norm:
        return (2, age_h)

    # 밀려남·미확인·unknown — 최소 간격만 지나면 대상(검색량 순 정렬은 배치 쪽)
    due = float(cfg.get("pushed_min_gap_hours", 12))
    return (3, age_h) if age_h >= due else (99, age_h)


def _volume_threshold(universe: list[dict], percentile: float) -> float:
    vols = sorted(float(i.get("volume") or 0) for i in universe)
    if not vols:
        return 0.0
    idx = min(len(vols) - 1, max(0, int(len(vols) * percentile)))
    return vols[idx]


def next_priority_batch(rt: Any, brand: str, n: int, now: datetime | None = None) -> list[dict]:
    """다음에 검사할 키워드 n개 — 위 등급 순, 같은 등급 안에서는 오래된 순.

    99등급(주기가 아직 안 돎)은 절대 뽑히지 않는다 — n개를 못 채우면 그만큼만
    돌려준다(빈 목록도 정상, 순환이 다음 틱에 다시 부른다).
    """
    from v2r.knowledge.keyword_exposure import keyword_universe, _norm
    from v2r.store import keyword_exposure_store as store

    now = now or datetime.now(timezone.utc)
    cfg = load_config(rt.settings.repo_root).get("priority", dict(_DEFAULT_PRIORITY))

    universe = keyword_universe(rt, brand)
    if not universe:
        return []

    last_checked = {
        _norm(r["keyword"]): {"checked_at": str(r["checked_at"] or ""), "status": str(r["status"] or "")}
        for r in store.latest_by_keyword(rt.conn, brand)
    }
    cafes = {i.get("cafe") for i in universe if i.get("cafe")}
    recent_norm = _recent_publish_keywords(
        rt, brand, cafes, now, [float(h) for h in cfg.get("recent_publish_hours", [4, 24, 72])]
    )
    vol_threshold = _volume_threshold(universe, float(cfg.get("top_volume_percentile", 0.7)))

    scored = []
    for item in universe:
        tier, age = priority_tier(item, last_checked, recent_norm, cfg, vol_threshold, now)
        if tier >= 99:
            continue
        vol = float(item.get("volume") or 0)
        # 1·2등급: 오래된 순. 3등급(밀려남·미확인): 검색량 높은 순 → 오래된 순
        sub = (-vol, -age) if tier == 3 else (0.0, -age)
        scored.append((tier, sub, item))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [item for _, _, item in scored[: max(0, n)]]


def queue_counts(rt: Any, brand: str, now: datetime | None = None) -> dict[str, int]:
    """브랜드별 등급별 대기 수(보고서용)."""
    from v2r.knowledge.keyword_exposure import keyword_universe, _norm
    from v2r.store import keyword_exposure_store as store

    now = now or datetime.now(timezone.utc)
    cfg = load_config(rt.settings.repo_root).get("priority", dict(_DEFAULT_PRIORITY))
    universe = keyword_universe(rt, brand)
    if not universe:
        return {}
    last_checked = {
        _norm(r["keyword"]): {"checked_at": str(r["checked_at"] or ""), "status": str(r["status"] or "")}
        for r in store.latest_by_keyword(rt.conn, brand)
    }
    cafes = {i.get("cafe") for i in universe if i.get("cafe")}
    recent_norm = _recent_publish_keywords(
        rt, brand, cafes, now, [float(h) for h in cfg.get("recent_publish_hours", [4, 24, 72])]
    )
    vol_threshold = _volume_threshold(universe, float(cfg.get("top_volume_percentile", 0.7)))
    counts: dict[str, int] = {}
    for item in universe:
        tier, _ = priority_tier(item, last_checked, recent_norm, cfg, vol_threshold, now)
        counts[str(tier)] = counts.get(str(tier), 0) + 1
    return counts


__all__ = [
    "CONFIG_PATH",
    "load_config",
    "priority_tier",
    "next_priority_batch",
    "queue_counts",
]
