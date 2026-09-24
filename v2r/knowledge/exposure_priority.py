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

import json
import logging
import os
import threading
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
    #
    # 2026-09-24 4차(실측 8, 6-5·6-6절) — 120초였을 때 실측해 보니 작업자가
    # 매번 브랜드를 바꿔(5개 순환) 같은 브랜드로 돌아오기까지 평균 150초 이상
    # 걸려 TTL 안에 캐시가 거의 재사용되지 못했다(원인 (a), 로그 실측으로
    # 확정 — 4차 절 참고). 정확성(중복 재검사 방지)은 이제 `next_priority_batch`
    # 의 후보별 DB 직전 확인과 `is_due_now`가 캐시와 무관하게 항상 지키므로,
    # 이 캐시는 "정렬 순서·최근 발행 집합의 신선도"에만 영향을 준다 — 600초
    # (10분)로 늘려도 안전하다.
    "universe_cache_sec": 600,
    # 4차 — universe 번들(시트 CSV 포함)을 작업자 프로세스 간에도 공유하는
    # 파일 캐시 TTL(초). 기본은 universe_cache_sec과 같지만 따로 조정 가능.
    # `data/exposure_universe_cache/<브랜드>.json`에 저장되며, 어느 작업자든
    # 이 파일이 신선하면(mtime 기준) 네트워크 없이 읽는다.
    "universe_file_cache_sec": 600,
    # 러너가 한 번에 뽑아 작업자 메모리 큐에 쌓아 둘 후보 수(1건씩 매번
    # 조회하던 것을 배치로 바꿈 — exposure_runner.WorkerQueue 참고).
    "batch_size": 10,
    # 2026-09-24 2차 재실측(exposure-speed-2026-09-24.md 6-2절) — universe
    # 캐시 후에도 건당 9.9초로 개선이 없어, 매번 다시 읽던 `latest_by_keyword`
    # (전체 이력 쿼리)와 매번 다시 하던 등급 매기기·정렬(1만 개 이상 순회)도
    # 캐시한다. last_checked는 이 초만큼 캐시하되, 러너가 검사 결과를 저장한
    # 직후 `mark_checked`로 즉시 갱신해 정확도를 지킨다.
    "last_checked_cache_sec": 20,
    # 2026-09-24 5차(실측 9, 6-7·6-8절) — 4차의 프로세스별 정렬 캐시는 작업자
    # 5개가 각자 계산한 목록의 앞쪽을 동시에 다퉈 중복 재검사가 19.4%로
    # 늘었다. 공유 큐(`exposure_queue` 표)로 바꿔 정렬은 브랜드당 이
    # 초(기본은 universe_cache_sec와 동일)마다 한 프로세스만 갱신하고,
    # 선점은 sqlite 원자적 UPDATE 한 번으로 한다.
    "queue_refresh_sec": 600,
    # 공유 큐 선점 TTL(초) — 이 시간 넘게 완료(done_at)되지 않은 선점은
    # 죽은 작업자의 것으로 보고 다시 선점 가능하다(기존
    # `exposure_runner.INFLIGHT_TTL_SECONDS`와 같은 값 유지).
    "claim_ttl_sec": 180,
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


#: 2026-09-24 4차 — universe 번들을 작업자 프로세스 간에도 공유하는 파일 캐시.
#: `data/exposure_universe_cache/<브랜드>.json`, mtime 기준으로 신선도 판단.
_UNIVERSE_FILE_CACHE_DIR = "data/exposure_universe_cache"

#: (repo_root, brand) -> {"source": "process_cache"|"file_cache"|"live",
#:                          "fetch_sec": float} — 직전 `_universe_bundle` 호출
#: 진단 정보(로그용, 함수 시그니처는 안 바꾸려고 곁가지 딕셔너리로 둠).
_UNIVERSE_BUNDLE_META: dict[tuple[str, str], dict[str, Any]] = {}


def _universe_file_cache_path(rt: Any, brand: str) -> Path:
    return Path(rt.settings.repo_root) / _UNIVERSE_FILE_CACHE_DIR / f"{brand}.json"


def _read_universe_file_cache(rt: Any, brand: str, ttl: float) -> dict[str, Any] | None:
    p = _universe_file_cache_path(rt, brand)
    try:
        if not p.exists():
            return None
        age = time.time() - p.stat().st_mtime
        if age > ttl:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        # 2026-09-24 8차 — recent_norm이 집합(set[str])에서 매핑
        # (dict[키워드, 발행시각]) 으로 바뀌었다. 옛 파일 캐시(리스트 형태)가
        # 남아 있어도 조용히 빈 매핑으로 취급한다(다음 갱신이 채운다).
        raw_recent = data.get("recent_norm")
        recent_norm: dict[str, datetime] = {}
        if isinstance(raw_recent, dict):
            for k, v in raw_recent.items():
                dt = _parse_iso(str(v or ""))
                if dt is not None:
                    recent_norm[k] = dt
        return {
            "universe": data.get("universe") or [],
            "cafes": set(data.get("cafes") or []),
            "recent_norm": recent_norm,
            "vol_threshold": float(data.get("vol_threshold") or 0.0),
        }
    except Exception as exc:  # pragma: no cover - 방어용
        log.warning("universe 파일 캐시 읽기 실패(%s): %s", brand, exc)
        return None


def _write_universe_file_cache(rt: Any, brand: str, bundle: dict[str, Any]) -> None:
    p = _universe_file_cache_path(rt, brand)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "universe": bundle["universe"],
            "cafes": sorted(bundle["cafes"]),
            "recent_norm": {k: v.isoformat() for k, v in bundle["recent_norm"].items()},
            "vol_threshold": bundle["vol_threshold"],
        }
        # 여러 작업자 프로세스가 동시에 쓸 수 있으니 pid로 고유한 임시 이름을
        # 쓰고 os.replace로 원자적으로 바꾼다(exposure_runner의 상태 파일
        # 쓰기와 같은 패턴 — 이름 충돌로 크래시하던 d9849f2 교훈).
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as exc:  # pragma: no cover - 방어용
        log.warning("universe 파일 캐시 쓰기 실패(%s): %s", brand, exc)


def _universe_bundle(rt: Any, brand: str, cfg: dict, now: datetime) -> dict[str, Any]:
    """`keyword_universe` + 최근 발행 집합 + 검색량 임계값을 TTL 캐시에서 꺼낸다.

    2026-09-24 4차 — 캐시를 2단으로 뒀다: (1) 프로세스 메모리(`_UNIVERSE_CACHE`,
    이 프로세스 안에서는 공짜), (2) 작업자 프로세스 간 공유 파일
    (`data/exposure_universe_cache/<브랜드>.json`, mtime 기준) — 한 작업자가
    이미 받아 둔 시트 CSV를 다른 작업자들이 네트워크 없이 재사용한다(실측
    8에서 5개 작업자가 독립적으로 같은 시트를 반복 요청하던 걸 줄임). 그래도
    둘 다 없으면 실제로 `keyword_universe`(시트 CSV)를 읽는다. 어느 경로든
    이 함수를 부른 브랜드에 대해 `_UNIVERSE_BUNDLE_META`에 소스·소요 시간을
    남겨 `next_priority_batch`가 로그에 찍는다.

    같은 브랜드를 여러 작업자 프로세스가 부르더라도 실제로 같은 키워드를
    두 번 검사하는 일은 (이 캐시가 아니라) 공유 큐(`exposure_queue` 표,
    2026-09-24 5차)의 원자적 선점과 `is_due_now`의 DB 직전 확인이 막는다 —
    이 캐시가 얼마나 낡아도(최대 파일 TTL) 정확성에는 영향이 없다. 이
    번들(universe·최근 발행 집합)은 `_do_refresh_queue`가 공유 큐를 갱신할
    재료로만 쓴다.
    """
    from v2r.knowledge.keyword_exposure import keyword_universe

    ttl = float(cfg.get("universe_cache_sec", 600))
    file_ttl = float(cfg.get("universe_file_cache_sec", ttl))
    key = _cache_key(rt, brand)
    cached = _UNIVERSE_CACHE.get(key)
    now_epoch = time.time()
    if cached is not None and cached[0] > now_epoch:
        _UNIVERSE_BUNDLE_META[key] = {"source": "process_cache", "fetch_sec": 0.0}
        return cached[1]

    t0 = time.perf_counter()
    file_bundle = _read_universe_file_cache(rt, brand, file_ttl)
    if file_bundle is not None:
        _UNIVERSE_CACHE[key] = (now_epoch + ttl, file_bundle)
        _UNIVERSE_BUNDLE_META[key] = {"source": "file_cache", "fetch_sec": time.perf_counter() - t0}
        return file_bundle

    universe = keyword_universe(rt, brand)
    cafes = {i.get("cafe") for i in universe if i.get("cafe")}
    recent_norm = _recent_publish_keywords(
        rt, brand, cafes, now, [float(h) for h in cfg.get("recent_publish_hours", [4, 24, 72])]
    )
    vol_threshold = _volume_threshold(universe, float(cfg.get("top_volume_percentile", 0.7)))
    bundle = {"universe": universe, "cafes": cafes, "recent_norm": recent_norm, "vol_threshold": vol_threshold}
    _UNIVERSE_CACHE[key] = (now_epoch + ttl, bundle)
    _write_universe_file_cache(rt, brand, bundle)
    _UNIVERSE_BUNDLE_META[key] = {"source": "live", "fetch_sec": time.perf_counter() - t0}
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


# =======================================================================
# 2026-09-24 5차 — 공유 큐(`exposure_queue` 표, `exposure_queue_store.py`).
#
# 재실측(exposure-speed-2026-09-24.md 6-7·6-8절): 4차로 큐 조회는 3.2초로
# 빨라졌지만 중복 재검사가 19.4%로 늘었다 — 작업자 5개가 **각자 독립적으로**
# 계산한 정렬 목록(`_SORTED_CACHE`, 프로세스별)의 앞쪽을 동시에 다퉈, 같은
# 키워드를 여럿이 함께 고르는 경쟁이 됐기 때문(DB 직전 확인이 있어도, 확인과
# 실제 저장 사이 수 초의 경쟁 구간은 못 막는다). 근본 해결책은 정렬·선점을
# **공유 상태**(sqlite 표)로 옮기는 것 — 등급·정렬은 브랜드당 한 프로세스가
# TTL(기본 600초)마다 한 번만 계산해 `exposure_queue` 표에 갱신하고, 작업자는
# 그 표에서 원자적 `UPDATE`(한 트랜잭션) 한 번으로 배치를 선점한다. 같은
# rowid를 두 프로세스가 동시에 못 고르므로(sqlite 쓰기 트랜잭션은 직렬화)
# 애초에 경쟁이 없다.
# =======================================================================


def _sort_key_for_tier(tier: int, vol: float, age_h: float) -> float:
    """`(tier, 보조키)` 다중 정렬을, `exposure_queue.sort_key` 한 칸에 담을
    단일 실수로 접는다 — 같은 tier 안에서는 `ORDER BY sort_key ASC`가 기존
    `scored.sort(key=(tier, sub))`(1·2등급: 오래된 순, 3등급: 검색량 → 오래된
    순)와 같은 순서가 되도록 만든다. 나이(시간)는 상한(약 114년)으로 잘라
    `float("inf")`가 그대로 sqlite REAL로 들어가는 걸 피한다."""
    age_h = min(age_h, 1_000_000.0) if age_h != float("inf") else 1_000_000.0
    if tier == 3:
        # 검색량 차이 1만 나도 나이 상한(1e6)보다 훨씬 크게 갈리도록 충분히
        # 큰 배수(1e7)를 곱한다 — 검색량이 먼저, 그다음 나이 순.
        return -(vol * 1e7 + age_h)
    return -age_h


def _do_refresh_queue(rt: Any, brand: str, cfg: dict, now: datetime) -> int:
    """브랜드의 등급·정렬을 다시 계산해 `exposure_queue` 표에 갱신한다.

    등급 규칙(`priority_tier`)은 그대로 재사용 — 이 함수는 그 결과를 어디에
    보관하느냐만 바꾼다(프로세스 메모리 → 공유 표). universe·최근 발행
    집합은 여전히 `_universe_bundle`(프로세스+파일 캐시, 4차)에서 가져와
    시트 CSV 재읽기를 줄인다.

    2026-09-24 6차(코디네이터 지시 — "done 항목이 다시 미완료로 되살아나는지"
    확인) — `last_checked`는 여기서 캐시를 건너뛰고 항상 방금 커밋된 값을
    읽는다. 갱신은 드물게(기본 600초마다) 일어나므로 `last_checked_cache_sec`
    (기본 20초) 캐시를 그대로 쓰면 이론상 아주 좁은 창에서 "방금 완료됐는데
    아직 캐시에 안 보여" 상태로 등급을 잘못 계산할 여지가 있다 — 갱신
    빈도가 낮아 매번 새로 읽어도 비용이 크지 않으므로 정확성을 우선한다."""
    from v2r.knowledge.keyword_exposure import _norm
    from v2r.store import exposure_queue_store as qstore

    bundle = _universe_bundle(rt, brand, cfg, now)
    universe = bundle["universe"]
    if not universe:
        return 0
    invalidate_last_checked_cache(rt, brand)
    last_checked = _last_checked_map(rt, brand, cfg)
    recent_norm = bundle["recent_norm"]
    vol_threshold = bundle["vol_threshold"]

    candidates: list[tuple[int, float, str, str, dict]] = []
    for item in universe:
        tier, age = priority_tier(item, last_checked, recent_norm, cfg, vol_threshold, now)
        if tier >= 99:
            continue
        vol = float(item.get("volume") or 0)
        sort_key = _sort_key_for_tier(tier, vol, age)
        keyword = str(item.get("keyword", ""))
        candidates.append((tier, sort_key, _norm(keyword), keyword, item))

    qstore.upsert_candidates(rt.conn, brand, candidates, now_epoch=time.time())
    return len(candidates)


#: 브랜드별 갱신 중복 방지용 mkdir 락(2026-09-24 5차) — "작업자 중 한
#: 프로세스만" 갱신하게 한다. 갱신 자체는 보통 수 초 안에 끝나므로, 이보다
#: 오래된 락은 죽은 작업자의 것으로 보고 무시한다.
_REFRESH_LOCK_STALE_SEC = 60.0


def _refresh_lock_dir(rt: Any, brand: str) -> Path:
    return Path(rt.settings.repo_root) / "data" / "exposure_queue_refresh.lock" / brand


def _try_refresh_lock(rt: Any, brand: str) -> bool:
    d = _refresh_lock_dir(rt, brand)
    d.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(d)
        return True
    except FileExistsError:
        try:
            age = time.time() - d.stat().st_mtime
        except OSError:
            return False
        if age <= _REFRESH_LOCK_STALE_SEC:
            return False
        try:
            os.rmdir(d)
            os.mkdir(d)
            return True
        except OSError:
            return False


def _release_refresh_lock(rt: Any, brand: str) -> None:
    _release_refresh_lock_by_root(rt.settings.repo_root, brand)


def _release_refresh_lock_by_root(repo_root: str | Path, brand: str) -> None:
    d = Path(repo_root) / "data" / "exposure_queue_refresh.lock" / brand
    try:
        os.rmdir(d)
    except OSError:
        pass


def _open_refresh_runtime() -> Any:
    """`_refresh_queue_in_background` 전용 — 별도 함수로 빼서 시험에서
    (실제 프로덕션 설정 대신) 같은 임시 DB의 `rt`를 그대로 돌려주도록
    바꿔치기할 수 있게 한다."""
    from v2r.engine.context import Runtime

    return Runtime.open()


def _refresh_queue_in_background(repo_root: str | Path, brand: str, cfg: dict, now: datetime) -> None:
    """백그라운드 스레드에서 실행 — 그 사이 호출한 작업자(와 다른 작업자들)는
    기존(약간 오래된) 큐로 계속 선점을 진행한다(2026-09-24 6차, 코디네이터
    지시). `rt.conn`을 다른 스레드와 공유하면 sqlite3 커넥션 동시 접근이
    안전하지 않으므로, 이 스레드 전용의 새 `Runtime`(= 새 커넥션)을 연다."""
    thread_rt = None
    try:
        thread_rt = _open_refresh_runtime()
        _do_refresh_queue(thread_rt, brand, cfg, now)
    except Exception as exc:  # pragma: no cover - 방어용(백그라운드라 예외를 삼킴)
        log.warning("노출 큐 백그라운드 갱신 실패(%s): %s", brand, exc)
    finally:
        if thread_rt is not None:
            try:
                thread_rt.close()
            except Exception:
                pass
        _release_refresh_lock_by_root(repo_root, brand)


def maybe_refresh_queue(rt: Any, brand: str, cfg: dict, now: datetime) -> bool:
    """이 브랜드 큐가 TTL(`priority.queue_refresh_sec`, 기본
    `universe_cache_sec`와 동일)보다 오래됐으면 갱신한다.

    2026-09-24 6차(코디네이터 지시) — **이미 큐에 뭔가 있으면**(콜드 스타트가
    아니면) 백그라운드 스레드에서 갱신하고 바로 돌아간다. 이전엔 락을 잡은
    작업자가 갱신이 끝날 때까지(평균 13.5초, 최대 36.5초) 제자리에서
    기다려 그 작업자만 그동안 아무 검사도 못 했다 — 이제는 갱신을 던져
    놓고, 호출자도(그리고 락을 못 잡은 다른 작업자들도 원래부터 그랬듯)
    갱신이 끝나기 전까지는 기존(약간 오래된) 큐로 계속 선점한다.

    **큐가 아직 한 번도 채워진 적 없으면**(`last_refreshed_at == 0`, 콜드
    스타트) 대체할 기존 큐가 없으므로 예외적으로 **동기** 갱신한다 — 안
    그러면 첫 조회가 빈 배치를 돌려줘 그 브랜드는 영영 검사를 못 시작한다
    (`tests/test_exposure_runner.py`의 콜드 스타트 시험들이 이 경로를 검증).

    락을 못 잡으면(다른 작업자가 이미 갱신 중) 아무것도 안 하고 `False`."""
    from v2r.store import exposure_queue_store as qstore

    ttl = float(cfg.get("queue_refresh_sec", cfg.get("universe_cache_sec", 600)))
    now_epoch = time.time()
    last_refreshed = qstore.last_refreshed_at(rt.conn, brand)
    if now_epoch - last_refreshed < ttl:
        return False
    cold_start = last_refreshed <= 0.0
    if not _try_refresh_lock(rt, brand):
        return False
    # 락을 잡는 사이 다른 프로세스가 이미 갱신했을 수 있으니 다시 확인.
    if now_epoch - qstore.last_refreshed_at(rt.conn, brand) < ttl:
        _release_refresh_lock(rt, brand)
        return False
    if cold_start:
        try:
            _do_refresh_queue(rt, brand, cfg, now)
            return True
        finally:
            _release_refresh_lock(rt, brand)

    threading.Thread(
        target=_refresh_queue_in_background,
        args=(rt.settings.repo_root, brand, cfg, now),
        daemon=True,
        name=f"exposure-queue-refresh-{brand}",
    ).start()
    return False


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


def _recent_publish_keywords(
    rt: Any, brand: str, cafes: set[str], now: datetime, window_hours: list[float]
) -> dict[str, datetime]:
    """최근 발행된 **브랜드 원고**의 키워드 -> 발행 시각(`publications.
    created_at`) 매핑.

    2026-09-24 8차(코디네이터 지시, 7차 실측 후속) — 예전엔 여기서
    (a) `article_index`(카페의 모든 글 — V2R이 발행한 것이든 자사 카페
    일상 글이든, 심지어 이 시스템이 생기기 전 수동 글까지 전부 섞여
    있고 브랜드 태그가 아예 없다, `v2r/store/article_index.py` 참고)에서
    "제목 맨 앞 낱말"을 키워드로 간주하는 휴리스틱도 같이 합쳤는데, 이게
    바로 오탐 원천이었다 — 오늘 발행된 자사 카페 일상 글(브랜드 키워드와
    무관)의 제목 앞 낱말이 우연히 브랜드 universe의 어떤 키워드와 겹치면
    그 키워드가 "최근 발행"으로 잘못 잡혀 2등급(원래 재검사 간격 없음)으로
    분류됐다. 실측(7차): 초과 검사 31건 중 27건이 2등급이었는데, 대조해보니
    이 article_index 휴리스틱이 `article_index`에 브랜드 구분이 아예 없어
    발행 종류를 못 가려 생긴 오탐이었다.

    이제 **`publications` 표(source_key=이 브랜드)의 실제 발행 기록을 브랜드
    시트 F열(발행 URL)과 대조한 결과만** 쓴다 — `_recent_publish_keywords_from_db`.
    "제목 앞 낱말" 휴리스틱은 완전히 제거했다.
    """
    return _recent_publish_keywords_from_db(rt, brand, now, window_hours)


def _publications_recent_pub_times(
    conn: Any, brand: str, now: datetime, max_age_hours: float
) -> dict[str, datetime]:
    """이 **브랜드**(`source_key = brand`)의 성공(uncertain/done) 발행 중
    `created_at`이 지금부터 `max_age_hours` 안인 행의 글 번호(정규화) ->
    발행 시각 매핑.

    2026-09-24 8차 — `source_key = ?`(브랜드) 조건을 추가했다. 예전엔 이
    조건이 없어 **다른 브랜드는 물론 자사 카페 일상 글(`source_key`가
    브랜드 시트 이름이 아닌 발행 — `v2r.engine.publish.brand_source_keys`가
    "일상 글이 아닌 source_key 집합"으로 이미 정의해 둔 구분과 같은 기준)
    까지 전부 "최근 발행"으로 셌다** — 오탐의 또 다른 경로. 이제 이 브랜드
    이름과 정확히 같은 `source_key`(브랜드 키워드 원고 발행만 이 값을
    쓴다)만 본다.

    URL의 쿼리스트링·끝 슬래시 차이는 `_article_id`(글 번호만 뽑음)가 이미
    무시한다. E열(비밀번호)은 이 표에 없으므로 접근하지 않는다."""
    from v2r.knowledge.keyword_exposure import _article_id
    from v2r.store.publications import BLOCKING_STATUSES

    out: dict[str, datetime] = {}
    if conn is None:
        return out
    try:
        placeholders = ", ".join("?" for _ in BLOCKING_STATUSES)
        rows = conn.execute(
            f"SELECT url, created_at FROM publications WHERE source_key = ? AND status IN ({placeholders})",
            (brand, *BLOCKING_STATUSES),
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
        if age_h < 0 or age_h > max_age_hours:
            continue
        aid = _article_id(str(url or ""))
        if not aid:
            continue
        # 같은 글 번호가 여러 행으로(재시도 등) 있으면 가장 최근 발행 시각을 쓴다.
        prev = out.get(aid)
        if prev is None or dt > prev:
            out[aid] = dt
    return out


#: 브랜드 시트 두 번째 탭(노출 현황) F열 헤더 — "발행 URL"
_PUBLISH_URL_HEADERS = ("발행url", "발행 url")


def _recent_publish_keywords_from_db(rt: Any, brand: str, now: datetime, window_hours: list[float]) -> dict[str, datetime]:
    """이 브랜드의 DB `publications` 최근 발행 URL(`source_key = brand`)을
    시트 F열(발행 URL)과 글 번호로 대조해 같은 행 H열(키워드) -> 발행 시각을
    뽑는다. F열 값은 대조에만 쓰고 어디에도 기록하지 않는다 — E열(비밀번호)은
    `_sheet_rows`가 이미 버린 뒤라 아예 접근하지 못한다.

    창(2h/6h/24h) 판정 자체는 여기서 하지 않는다 — 발행 시각만 돌려주고,
    "지금 어느 창이 활성인지·그 창에서 이미 검사했는지·최소 간격(90분)"은
    `priority_tier`가 호출 시점의 `now`로 매번 새로 계산한다(2026-09-24
    8차). 그래서 넉넉히(가장 긴 창 + 여유) 훑는다."""
    from v2r.knowledge.keyword_exposure import _article_id, _sheet_rows
    from v2r.sources.keyword_list import _norm as _norm_kw
    from v2r.sources.keyword_list import _pick

    max_age_hours = (max(window_hours) if window_hours else 24.0) + _RECENT_PUBLISH_WINDOW_HOURS
    pub_times = _publications_recent_pub_times(getattr(rt, "conn", None), brand, now, max_age_hours)
    if not pub_times:
        return {}

    try:
        cfg = getattr(rt, "sources_cfg", None)
        xlsx = Path(rt.settings.repo_root) / "data" / f"brand_sheet_{brand}.xlsx"
        rows = _sheet_rows(brand, cfg, str(xlsx) if xlsx.exists() else None)
    except Exception:
        return {}

    out: dict[str, datetime] = {}
    for row in rows or []:
        url = _pick(row, _PUBLISH_URL_HEADERS)
        if not url:
            continue
        aid = _article_id(url)
        if not aid or aid not in pub_times:
            continue
        kw = _pick(row, ("키워드",))
        if kw:
            out[_norm_kw(kw)] = pub_times[aid]
    return out


#: 2026-09-24 8차(코디네이터 지시) — 2등급(최근 발행)도 최소 간격을 둔다.
#: 창(2h/6h/24h)과 무관하게 이보다 자주는 절대 재검사하지 않는다.
_RECENT_PUBLISH_MIN_GAP_HOURS = 1.5


def priority_tier(
    item: dict,
    last_checked: dict[str, dict],
    recent_publish_norm: dict[str, datetime],
    cfg: dict,
    volume_threshold: float,
    now: datetime,
) -> tuple[int, float]:
    """이 키워드의 (등급, 정렬용 보조키). 보조키는 클수록(오래될수록) 우선.

    반환하는 두 번째 값은 "마지막 검사 이후 경과 시간(시간)" — 미확인은 무한대로
    취급해 항상 그 등급 안에서 맨 앞에 온다.

    `recent_publish_norm`은 2026-09-24 8차부터 `{정규화 키워드: 발행 시각}`
    매핑이다(예전엔 단순 `set[str]`) — 2등급 판정에 "지금이 몇 번째 창인지",
    "그 창에서 이미 검사했는지"를 매번 `now` 기준으로 새로 계산하기 위해서다.
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

    pub_dt = recent_publish_norm.get(key)
    if pub_dt is not None:
        # 2026-09-24 8차 — "발행 후 2·6·24시간 근방에 한 번씩"이 취지였는데,
        # 예전엔 창 안이면 재검사 간격 없이 매번 대상이었다(실측 7차: 초과
        # 검사의 87%가 이 경로). 이제 (a) 같은 창에서는 최대 1회만
        # (last가 이미 같은 창 범위에 들어 있으면 99등급), (b) 어떤 경우든
        # 최소 90분은 지나야 한다.
        window_targets = [float(h) for h in cfg.get("recent_publish_hours", [2, 6, 24])]
        window = float(cfg.get("recent_publish_window_hours", _RECENT_PUBLISH_WINDOW_HOURS))
        age_since_pub_h = (now - pub_dt).total_seconds() / 3600.0
        active_target = next(
            (t for t in window_targets if abs(age_since_pub_h - t) <= window), None
        )
        if active_target is not None:
            min_gap = float(cfg.get("recent_publish_min_gap_hours", _RECENT_PUBLISH_MIN_GAP_HOURS))
            if age_h < min_gap:
                return (99, age_h)
            last_dt = _parse_iso(str(last.get("checked_at", "")))
            if last_dt is not None:
                last_age_since_pub_h = (last_dt - pub_dt).total_seconds() / 3600.0
                if abs(last_age_since_pub_h - active_target) <= window:
                    # 이 창에서는 이미 한 번 검사했다 — 다음 창(또는 3등급
                    # 규칙)까지는 다시 안 뽑는다.
                    return (99, age_h)
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


def next_priority_batch(
    rt: Any, brand: str, n: int, worker_id: str = "0", now: datetime | None = None
) -> list[dict]:
    """다음에 검사할 키워드 n개를 공유 큐(`exposure_queue` 표)에서 원자적으로
    선점해 돌려준다 — 위 등급 순, 같은 등급 안에서는 오래된/검색량 순.

    99등급(주기가 아직 안 돎)은 절대 뽑히지 않는다 — n개를 못 채우면 그만큼만
    돌려준다(빈 목록도 정상, 순환이 다음 틱에 다시 부른다).

    2026-09-24 5차(실측 9, 6-7·6-8절) — 4차까지는 작업자 프로세스마다 따로
    정렬해 뒀다가 저장 순서로 경쟁했는데(중복 19.4%), 이제 등급·정렬은
    `maybe_refresh_queue`가 TTL(기본 600초)마다 브랜드당 한 번만 계산해
    공유 표에 쓰고, 이 함수는 `exposure_queue_store.claim_batch`의 원자적
    `UPDATE`(한 sqlite 트랜잭션) 한 번으로 n개를 선점한다 — 같은 rowid를
    두 작업자가 동시에 못 고르므로 근본적으로 중복이 안 난다. 선점 자체가
    이제 "누가 먼저 검사했는지"의 최신 정보이므로, 파일 기반
    `claim_inflight`/`complete_inflight`는 더 이상 쓰지 않는다(제거).
    """
    from v2r.store import exposure_queue_store as qstore

    now = now or datetime.now(timezone.utc)
    cfg = load_config(rt.settings.repo_root).get("priority", dict(_DEFAULT_PRIORITY))
    key = _cache_key(rt, brand)

    t0 = time.perf_counter()
    refreshed = maybe_refresh_queue(rt, brand, cfg, now)
    t1 = time.perf_counter()
    universe_meta = _UNIVERSE_BUNDLE_META.get(key, {})

    claim_ttl = float(cfg.get("claim_ttl_sec", 180.0))
    out = qstore.claim_batch(rt.conn, brand, str(worker_id), n, now_epoch=time.time(), claim_ttl_sec=claim_ttl)
    t2 = time.perf_counter()
    log.info(
        "타이밍 공유큐조회 브랜드=%s round_trip=%.3fs 갱신=%s(%.3fs, universe=%s %.3fs) 선점=%.3fs 후보=%s",
        brand, t2 - t0, refreshed, t1 - t0, universe_meta.get("source"), universe_meta.get("fetch_sec", 0.0),
        t2 - t1, len(out),
    )
    return out


def is_due_now(rt: Any, brand: str, item: dict, now: datetime | None = None) -> bool:
    """작업자가 이 후보를 실제로 검사하기 직전(브라우저를 열기 바로 전) 마지막
    으로 확인한다 — 캐시(universe·정렬·last_checked 전부)와 무관하게 항상 DB
    단건 조회(인덱스, 밀리초)로 최소 간격 규칙(노출완 6시간, 밀려남·미확인
    12시간)을 다시 본다. `True`면 검사해도 된다(주기가 지났거나 최근 발행
    예외), `False`면 다른 작업자가 그 사이 이미 검사한 것이니 건너뛰어야
    한다. 2단계 확인 대기(pending_confirm) 재확인은 이 함수를 거치지 않는다
    (`exposure_runner.run_worker`가 `due_pending` 경로로 별도 처리, 의도된
    예외이므로)."""
    from v2r.knowledge.keyword_exposure import _norm
    from v2r.store import keyword_exposure_store as store

    now = now or datetime.now(timezone.utc)
    cfg = load_config(rt.settings.repo_root).get("priority", dict(_DEFAULT_PRIORITY))
    keyword = item.get("keyword", "")
    last_row = store.latest_for_keyword(rt.conn, brand, keyword)
    last_checked = (
        {_norm(keyword): {"checked_at": str(last_row["checked_at"] or ""), "status": str(last_row["status"] or "")}}
        if last_row is not None
        else {}
    )
    bundle = _universe_bundle(rt, brand, cfg, now)
    tier, _age = priority_tier(item, last_checked, bundle["recent_norm"], cfg, bundle["vol_threshold"], now)
    return tier < 99


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
    "is_due_now",
    "maybe_refresh_queue",
]
