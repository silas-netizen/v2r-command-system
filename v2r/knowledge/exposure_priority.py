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
import time
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
    # 2026-09-24 큐 캐시 — keyword_universe(시트 CSV·키워드DB 전량 읽기)와
    # 최근 발행 집합을 이 초만큼 브랜드별로 프로세스 메모리에 유지한다.
    # `store.latest_by_keyword`(DB 조회 1건)는 가벼워서 캐시하지 않고 매번
    # 새로 읽는다 — 그래서 방금 이 작업자가 저장한 검사 결과는 캐시와 무관하게
    # 항상 바로 반영된다.
    "universe_cache_sec": 120,
    # 러너가 한 번에 뽑아 작업자 메모리 큐에 쌓아 둘 후보 수(1건씩 매번
    # 조회하던 것을 배치로 바꿈 — exposure_runner.WorkerQueue 참고).
    "batch_size": 10,
    # 2026-09-24 2차 재실측(exposure-speed-2026-09-24.md 6-2절) — universe
    # 캐시 후에도 건당 9.9초로 개선이 없어, 매번 다시 읽던 `latest_by_keyword`
    # (전체 이력 쿼리)와 매번 다시 하던 등급 매기기·정렬(1만 개 이상 순회)도
    # 캐시한다. last_checked는 이 초만큼 캐시하되, 러너가 검사 결과를 저장한
    # 직후 `mark_checked`로 즉시 갱신해 정확도를 지킨다.
    "last_checked_cache_sec": 20,
}


# =======================================================================
# 브랜드별 universe 캐시 — keyword_universe + 최근 발행 집합 + 검색량 임계값을
# 한 묶음(번들)으로 TTL 동안 프로세스 메모리에 유지한다.
# =======================================================================

#: (repo_root, brand) -> (expires_at_epoch, bundle)
_UNIVERSE_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _cache_key(rt: Any, brand: str) -> tuple[str, str]:
    return (str(rt.settings.repo_root), brand)


def invalidate_universe_cache(rt: Any, brand: str | None = None) -> None:
    """이 브랜드(또는 전체, brand=None)의 universe 캐시를 즉시 비운다.

    캐시 자체는 판정 상태(last_checked)를 담지 않으므로(그건 매번 DB에서
    새로 읽음) 꼭 필요하진 않지만, 발행·키워드 시트가 방금 바뀐 걸 알 때
    (예: 새 키워드 발굴 직후) 다음 조회에서 바로 반영하고 싶으면 부른다."""
    if brand is None:
        _UNIVERSE_CACHE.clear()
        return
    key = (str(rt.settings.repo_root), brand)
    _UNIVERSE_CACHE.pop(key, None)


def _universe_bundle(rt: Any, brand: str, cfg: dict, now: datetime) -> dict[str, Any]:
    """`keyword_universe` + 최근 발행 집합 + 검색량 임계값을 TTL 캐시에서 꺼낸다.

    같은 브랜드를 여러 작업자 프로세스가 부르더라도 캐시는 프로세스별
    메모리라 서로 섞이지 않는다 — 작업자 간 중복 선점 방지는 이미
    `exposure_runner.claim_inflight`(파일 기반)가 맡고 있으므로, 여기서
    같은 번들을 여러 작업자가 동시에 읽어도(같은 순서로 정렬돼도) 실제로
    같은 키워드를 두 번 검사하는 일은 claim_inflight가 막는다.
    """
    from v2r.knowledge.keyword_exposure import keyword_universe

    ttl = float(cfg.get("universe_cache_sec", 120))
    key = _cache_key(rt, brand)
    cached = _UNIVERSE_CACHE.get(key)
    now_epoch = time.time()
    if cached is not None and cached[0] > now_epoch:
        return cached[1]

    universe = keyword_universe(rt, brand)
    cafes = {i.get("cafe") for i in universe if i.get("cafe")}
    recent_norm = _recent_publish_keywords(
        rt, brand, cafes, now, [float(h) for h in cfg.get("recent_publish_hours", [4, 24, 72])]
    )
    vol_threshold = _volume_threshold(universe, float(cfg.get("top_volume_percentile", 0.7)))
    bundle = {"universe": universe, "cafes": cafes, "recent_norm": recent_norm, "vol_threshold": vol_threshold}
    _UNIVERSE_CACHE[key] = (now_epoch + ttl, bundle)
    return bundle


# =======================================================================
# 2026-09-24 2차 — last_checked(DB) 캐시 + 정렬 결과 캐시.
#
# 재실측(exposure-speed-2026-09-24.md 6-2절): universe 캐시 후에도 건당
# 9.9초로 개선이 없었다. 원인은 `next_priority_batch`가 매 호출마다
# (1) `store.latest_by_keyword`(브랜드 전체 이력 쿼리)를 다시 읽고
# (2) 1만 개 이상 universe를 순회하며 `priority_tier`·정렬을 다시 하기 때문.
#
# last_checked는 20초(기본) TTL로 캐시하되, 러너가 결과를 저장한 직후
# `mark_checked`를 불러 그 키워드만 즉시 갱신한다 — TTL이 남아 있어도
# 방금 이 작업자가 검사한 키워드는 바로 반영된다(같은 키워드 연속 재선택
# 방지). 정렬된 후보 목록은 universe와 같은 TTL로 캐시하고, 배치 요청마다
# 캐시를 다시 정렬하지 않고 앞에서부터 아직 유효한(재검사 주기 안 지났거나
# 다른 작업자가 선점한) 항목만 걸러 자른다 — 전체 재정렬은 캐시 갱신
# 시점에만 한다.
# =======================================================================

#: (repo_root, brand) -> (expires_at_epoch, {norm_keyword: {"checked_at","status"}})
_LAST_CHECKED_CACHE: dict[tuple[str, str], tuple[float, dict[str, dict]]] = {}

#: (repo_root, brand) -> (expires_at_epoch, [(tier, item), ...] 정렬됨)
_SORTED_CACHE: dict[tuple[str, str], tuple[float, list[tuple[int, dict]]]] = {}


def _last_checked_map(rt: Any, brand: str, cfg: dict) -> dict[str, dict]:
    from v2r.knowledge.keyword_exposure import _norm
    from v2r.store import keyword_exposure_store as store

    ttl = float(cfg.get("last_checked_cache_sec", 20))
    key = _cache_key(rt, brand)
    now_epoch = time.time()
    cached = _LAST_CHECKED_CACHE.get(key)
    if cached is not None and cached[0] > now_epoch:
        return cached[1]

    rows = store.latest_by_keyword(rt.conn, brand)
    mapping = {
        _norm(r["keyword"]): {"checked_at": str(r["checked_at"] or ""), "status": str(r["status"] or "")}
        for r in rows
    }
    _LAST_CHECKED_CACHE[key] = (now_epoch + ttl, mapping)
    return mapping


def mark_checked(rt: Any, brand: str, keyword: str, status: str, checked_at: str) -> None:
    """러너가 검사 결과를 저장한 직후 부른다 — last_checked 캐시(TTL 안이라도)를
    이 키워드만 바로 갱신해, 다음 배치 조회에서 같은 키워드가 곧바로 다시
    뽑히지 않게 한다. 캐시가 아직 없으면(TTL 만료 후 첫 검사 등) 이 한
    항목만으로 새 캐시를 시작한다(다음 조회 때 `_last_checked_map`이 마저
    채운다 — 그 전까지는 이 키워드에 대해서만 정확하면 충분)."""
    from v2r.knowledge.keyword_exposure import _norm

    key = _cache_key(rt, brand)
    entry = {"checked_at": str(checked_at or ""), "status": str(status or "")}
    cached = _LAST_CHECKED_CACHE.get(key)
    if cached is None or cached[0] <= time.time():
        cfg = load_config(rt.settings.repo_root).get("priority", dict(_DEFAULT_PRIORITY))
        ttl = float(cfg.get("last_checked_cache_sec", 20))
        _LAST_CHECKED_CACHE[key] = (time.time() + ttl, {_norm(keyword): entry})
        return
    cached[1][_norm(keyword)] = entry


def invalidate_last_checked_cache(rt: Any, brand: str | None = None) -> None:
    if brand is None:
        _LAST_CHECKED_CACHE.clear()
        return
    _LAST_CHECKED_CACHE.pop((str(rt.settings.repo_root), brand), None)


def _sorted_candidates(rt: Any, brand: str, cfg: dict, now: datetime) -> list[tuple[int, dict]]:
    """등급·정렬을 한 번만 계산해 TTL(universe와 동일) 동안 캐시한다.

    반환값은 `(tier, item)` 튜플의 정렬된 리스트 — 등급이 낮을수록,
    같은 등급 안에서는 오래된/검색량 높은 순으로 이미 정렬돼 있다.
    99등급(주기 전)은 여기서 이미 제외돼 있다."""
    ttl = float(cfg.get("universe_cache_sec", 120))
    key = _cache_key(rt, brand)
    now_epoch = time.time()
    cached = _SORTED_CACHE.get(key)
    if cached is not None and cached[0] > now_epoch:
        return cached[1]

    bundle = _universe_bundle(rt, brand, cfg, now)
    universe = bundle["universe"]
    last_checked = _last_checked_map(rt, brand, cfg)
    recent_norm = bundle["recent_norm"]
    vol_threshold = bundle["vol_threshold"]

    scored = []
    for item in universe:
        tier, age = priority_tier(item, last_checked, recent_norm, cfg, vol_threshold, now)
        if tier >= 99:
            continue
        vol = float(item.get("volume") or 0)
        sub = (-vol, -age) if tier == 3 else (0.0, -age)
        scored.append((tier, sub, item))
    scored.sort(key=lambda t: (t[0], t[1]))
    result = [(t[0], t[2]) for t in scored]
    _SORTED_CACHE[key] = (now_epoch + ttl, result)
    return result


def invalidate_sorted_cache(rt: Any, brand: str | None = None) -> None:
    if brand is None:
        _SORTED_CACHE.clear()
        return
    _SORTED_CACHE.pop((str(rt.settings.repo_root), brand), None)


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
    """최근(설정한 시각 근방) 발행된 우리 글의 키워드 — 두 갈래의 합집합.

    (a) `article_index`에서 제목 맨 앞 키워드(기존 방식).
    (b) `publications` 표(DB)에서 성공(uncertain/done) 발행의 URL·시각을 읽어,
        같은 창 안이면 그 URL을 브랜드 시트 F열(발행 URL)과 글 번호로 대조해
        같은 행 H열(키워드)을 더한다(`_recent_publish_keywords_from_db`).
    """
    from v2r.knowledge.keyword_exposure import _title_lead_keyword, _norm

    out: set[str] = set()

    article_index = getattr(rt, "article_index", None)
    if article_index is not None and cafes:
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

    out |= _recent_publish_keywords_from_db(rt, brand, now, window_hours)
    return out


def _publications_recent_article_ids(conn: Any, now: datetime, window_hours: list[float]) -> set[str]:
    """`publications` 표에서 성공(uncertain/done) 발행 중 `created_at`이 각
    `window_hours` 시점 ±2시간 창 안인 행의 글 번호(정규화) 집합.

    URL의 쿼리스트링·끝 슬래시 차이는 `_article_id`(글 번호만 뽑음)가 이미
    무시한다. E열(비밀번호)은 이 표에 없으므로 접근하지 않는다.
    """
    from v2r.knowledge.keyword_exposure import _article_id
    from v2r.store.publications import BLOCKING_STATUSES

    out: set[str] = set()
    if conn is None:
        return out
    try:
        placeholders = ", ".join("?" for _ in BLOCKING_STATUSES)
        rows = conn.execute(
            f"SELECT url, created_at FROM publications WHERE status IN ({placeholders})",
            tuple(BLOCKING_STATUSES),
        ).fetchall()
    except Exception:
        return out
    for row in rows or []:
        try:
            url = row["url"] if hasattr(row, "keys") else row[0]
            created_at = row["created_at"] if hasattr(row, "keys") else row[1]
        except Exception:
            continue
        dt = _parse_iso(str(created_at or ""))
        if dt is None:
            continue
        age_h = (now - dt).total_seconds() / 3600.0
        if age_h < 0:
            continue
        for target in window_hours:
            if abs(age_h - float(target)) <= _RECENT_PUBLISH_WINDOW_HOURS:
                aid = _article_id(str(url or ""))
                if aid:
                    out.add(aid)
                break
    return out


#: 브랜드 시트 두 번째 탭(노출 현황) F열 헤더 — "발행 URL"
_PUBLISH_URL_HEADERS = ("발행url", "발행 url")


def _recent_publish_keywords_from_db(rt: Any, brand: str, now: datetime, window_hours: list[float]) -> set[str]:
    """DB `publications` 최근 발행 URL을 시트 F열(발행 URL)과 글 번호로 대조해
    같은 행 H열(키워드)을 뽑는다. F열 값은 대조에만 쓰고 어디에도 기록하지
    않는다 — E열(비밀번호)은 `_sheet_rows`가 이미 버린 뒤라 아예 접근하지 못한다.
    """
    from v2r.knowledge.keyword_exposure import _article_id, _sheet_rows
    from v2r.sources.keyword_list import _norm as _norm_kw
    from v2r.sources.keyword_list import _pick

    article_ids = _publications_recent_article_ids(getattr(rt, "conn", None), now, window_hours)
    if not article_ids:
        return set()

    try:
        cfg = getattr(rt, "sources_cfg", None)
        xlsx = Path(rt.settings.repo_root) / "data" / f"brand_sheet_{brand}.xlsx"
        rows = _sheet_rows(brand, cfg, str(xlsx) if xlsx.exists() else None)
    except Exception:
        return set()

    out: set[str] = set()
    for row in rows or []:
        url = _pick(row, _PUBLISH_URL_HEADERS)
        if not url:
            continue
        aid = _article_id(url)
        if not aid or aid not in article_ids:
            continue
        kw = _pick(row, ("키워드",))
        if kw:
            out.add(_norm_kw(kw))
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
    sheet_exposed = any(
        str(item.get(k) or "").replace(" ", "") in ("노출완", "exposed") for k in ("sheet_status", "t0_status")
    )
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

    2026-09-24 2차 개선 — 등급 매기기·정렬은 `_sorted_candidates`가 TTL 동안
    캐시한 결과를 쓰고(전체 1만 개 재순회는 캐시 갱신 시점에만), 이 함수는
    캐시된 정렬 목록 앞에서부터 (a) 캐시 이후 이미 검사돼 주기가 안 지난
    항목, (b) 다른 작업자가 선점(claim_inflight) 중인 항목만 걸러 n개를
    자른다."""
    from v2r.knowledge.exposure_runner import is_inflight
    from v2r.knowledge.keyword_exposure import _norm

    now = now or datetime.now(timezone.utc)
    cfg = load_config(rt.settings.repo_root).get("priority", dict(_DEFAULT_PRIORITY))

    t0 = time.perf_counter()
    candidates = _sorted_candidates(rt, brand, cfg, now)
    t1 = time.perf_counter()
    if not candidates:
        return []

    last_checked = _last_checked_map(rt, brand, cfg)
    bundle = _universe_bundle(rt, brand, cfg, now)
    recent_norm = bundle["recent_norm"]
    vol_threshold = bundle["vol_threshold"]

    out: list[dict] = []
    skipped_due, skipped_inflight = 0, 0
    for _tier, item in candidates:
        # 정렬 캐시 시점 이후 이 키워드가 검사됐을 수 있으니(같은 작업자의
        # 직전 검사, mark_checked로 즉시 반영됨) 가벼운 last_checked만으로
        # 다시 등급을 확인한다 — 전체 universe 재순회가 아니라 이 후보
        # 하나만 보므로 비용이 거의 없다.
        tier2, _age2 = priority_tier(item, last_checked, recent_norm, cfg, vol_threshold, now)
        if tier2 >= 99:
            skipped_due += 1
            continue
        if is_inflight(rt.settings.repo_root, brand, item["keyword"]):
            skipped_inflight += 1
            continue
        out.append(item)
        if len(out) >= max(0, n):
            break
    t2 = time.perf_counter()
    log.info(
        "타이밍 우선순위조회 브랜드=%s 정렬캐시=%.3fs 필터=%.3fs 후보=%s 주기전제외=%s 선점제외=%s",
        brand, t1 - t0, t2 - t1, len(out), skipped_due, skipped_inflight,
    )
    return out


def queue_counts(rt: Any, brand: str, now: datetime | None = None) -> dict[str, int]:
    """브랜드별 등급별 대기 수(보고서용)."""
    from v2r.knowledge.keyword_exposure import _norm
    from v2r.store import keyword_exposure_store as store

    now = now or datetime.now(timezone.utc)
    cfg = load_config(rt.settings.repo_root).get("priority", dict(_DEFAULT_PRIORITY))
    bundle = _universe_bundle(rt, brand, cfg, now)
    universe = bundle["universe"]
    if not universe:
        return {}
    last_checked = {
        _norm(r["keyword"]): {"checked_at": str(r["checked_at"] or ""), "status": str(r["status"] or "")}
        for r in store.latest_by_keyword(rt.conn, brand)
    }
    recent_norm = bundle["recent_norm"]
    vol_threshold = bundle["vol_threshold"]
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
    "invalidate_universe_cache",
    "mark_checked",
    "invalidate_last_checked_cache",
    "invalidate_sorted_cache",
]
