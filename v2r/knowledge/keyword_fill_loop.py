"""브랜드별 원고 대상(연관도 0~2) 키워드를 1만 개까지 채우는 순환.

배경: `data/keywords/<브랜드>.sqlite`는 발굴(BFS)이 끝나 있어도 무관(3) 키워드가
대부분이라 원고 대상(= `relevance_llm` 0~2 AND `relevance_codex` 0~2 AND
`needs_review` 아님)이 목표(1만 개)에 한참 못 미친다. 이 모듈은

  (a) 시드 선택 — 아직 시드로 안 쓴 원고 대상 키워드 중 검색량 상위 N개
      (부족하면 브랜드 정리본에서 LLM으로 제품·증상·타깃 용어를 뽑아 보충)
  (b) 키워드 도구로 연관 키워드 수집(새 키워드만)
  (c) 새 키워드만 `keyword_relevance.score_brand` → `crosscheck_brand`
  (d) `data/keywords/fill_progress.json` 갱신

를 한 회차(`run_cycle`)로 묶고, `fill_until_target`이 목표에 닿거나 시드
고갈(3회 연속 채택률 5% 미만)로 멈출 때까지 반복한다.

브라우저 호출(`fetch_fn`)과 LLM 라우터는 인자로 주입한다 — 이 모듈 자체는
네트워크를 직접 만들지 않아 가짜 도구로 테스트할 수 있다.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: 브랜드당 원고 대상 목표
DEFAULT_TARGET = 10_000
#: 한 회차 시드 개수(부족분은 정리본 용어로 보충)
DEFAULT_SEED_LIMIT = 50
#: 정리본에서 LLM으로 뽑을 보충 시드 개수
DEFAULT_GUIDE_SEED_N = 30
#: 네이버 키워드 도구 한 번 조회 최대 씨앗 수(기존 값 그대로)
SEED_BATCH_SIZE = 5
#: 브랜드당 DB 총 행수 상한(무관 키워드 폭증 방지)
DEFAULT_CAP = 50_000
#: 이 회차 수만큼 연속 채택률이 문턱 아래면 "시드 고갈"로 멈춘다
LOW_ADOPTION_STREAK_LIMIT = 3
#: 채택률 문턱(이 미만이면 그 회차는 "낮음"으로 센다)
LOW_ADOPTION_THRESHOLD = 0.05

PROGRESS_FILENAME = "fill_progress.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _kd_store():
    from v2r.store import keyword_discovery_store as store

    return store


def _kr_mod():
    from v2r.knowledge import keyword_relevance as kr

    return kr


def _kt_mod():
    from v2r.knowledge import naver_keyword_tool as kt

    return kt


# --- 마이그레이션(seeded_at) --------------------------------------------


def migrate_fill_columns(conn: sqlite3.Connection) -> list[str]:
    """`keywords`에 `seeded_at`(시드로 쓴 시각, 안 썼으면 '')를 더한다."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(keywords)")}
    added: list[str] = []
    if "seeded_at" not in existing:
        conn.execute("ALTER TABLE keywords ADD COLUMN seeded_at TEXT NOT NULL DEFAULT ''")
        added.append("seeded_at")
        conn.commit()
    return added


# --- 원고 대상 판정 -------------------------------------------------------

#: 사용자 지시(2026-09-24): relevance_llm 0~2 AND relevance_codex 0~2 AND
#: needs_review 아님 = 원고 대상.
ELIGIBLE_SQL = (
    "relevance_llm IS NOT NULL AND relevance_llm <= 2"
    " AND relevance_codex IS NOT NULL AND relevance_codex <= 2"
    " AND (needs_review = 0 OR needs_review IS NULL)"
)


def eligible_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM keywords WHERE {ELIGIBLE_SQL}").fetchone()[0])


# --- 시드 선택 ------------------------------------------------------------


def select_seed_keywords(conn: sqlite3.Connection, limit: int = DEFAULT_SEED_LIMIT) -> list[str]:
    """아직 시드로 안 쓴 원고 대상 키워드 중 검색량(total) 상위 `limit`개."""
    sql = (
        f"SELECT keyword FROM keywords WHERE {ELIGIBLE_SQL}"
        " AND (seeded_at = '' OR seeded_at IS NULL) ORDER BY total DESC LIMIT ?"
    )
    return [row[0] for row in conn.execute(sql, (int(limit),))]


def mark_seeded(conn: sqlite3.Connection, keywords: list[str]) -> None:
    if not keywords:
        return
    stamp = _now_iso()
    conn.executemany(
        "UPDATE keywords SET seeded_at = ? WHERE keyword = ?",
        [(stamp, kw) for kw in keywords],
    )
    conn.commit()


#: 정리본에서 시드 후보를 뽑는 프롬프트(제품·증상·타깃 용어, 검색어로 쓸 수 있는 짧은 낱말만)
_GUIDE_SEED_PROMPT = (
    "아래는 브랜드 정리본 원문이다. 이 안에서 네이버 검색광고 키워드 도구에 '시드'로"
    " 넣을 만한 제품명·증상명·타깃 용어를 최대 {n}개, 짧은 명사(2~8자)로 뽑아라."
    " 문장·설명·중복은 빼고 검색어로 자연스러운 낱말만.\n"
    "출력은 오직 JSON 배열: [\"용어1\", \"용어2\", ...]. 다른 텍스트는 쓰지 않는다.\n\n"
    "정리본:\n{text}"
)


def guide_seed_terms(
    brand: str,
    guides_dir: str | Path,
    router: Any = None,
    n: int = DEFAULT_GUIDE_SEED_N,
    purpose: str = "keyword_fill_seed",
) -> list[str]:
    """정리본에서 시드 후보 낱말을 최대 `n`개 뽑는다.

    `router`가 있으면 클로드(요금제 길)로 뽑고, 없거나 실패하면
    `naver_keyword_tool.extract_guide_keywords`(정규식 추출)로 대체한다.
    """
    kt = _kt_mod()
    fallback = kt.extract_guide_keywords(brand, guides_dir)[:n]
    if router is None:
        return fallback
    try:
        guides_dir = Path(guides_dir)
        candidates = list(guides_dir.glob(f"{brand}*.md"))
        if not candidates:
            return fallback
        text = candidates[0].read_text(encoding="utf-8")[:4000]
        prompt = _GUIDE_SEED_PROMPT.format(n=n, text=text)
        raw = router.complete(purpose, "정리본에서 키워드 시드를 뽑는 도우미다.", prompt, max_tokens=1000)
        from ..llm.router import extract_json

        data = extract_json(raw)
        if not isinstance(data, list):
            return fallback
        terms = [str(t).strip() for t in data if str(t or "").strip()]
        terms = [t for t in terms if 1 < len(t) <= 12][:n]
        return terms or fallback
    except Exception as exc:  # noqa: BLE001 - LLM 실패는 정규식 대체로 흡수
        log.warning("정리본 LLM 시드 추출 실패(%s) — 정규식 대체 사용: %s", brand, exc)
        return fallback


# --- 진행 파일 -------------------------------------------------------------


def progress_path(data_dir: str | Path = "data") -> Path:
    p = Path(data_dir) / "keywords" / PROGRESS_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_progress(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_progress(path: str | Path, data: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def update_fill_progress(path: str | Path, brand: str, **fields: Any) -> dict[str, Any]:
    data = load_progress(path)
    entry = dict(data.get(brand) or {})
    entry.update(fields)
    entry["updated_at"] = _now_iso()
    data[brand] = entry
    save_progress(path, data)
    return data


# --- 한 회차 --------------------------------------------------------------


def run_cycle(
    brand: str,
    db_path: str | Path,
    guides_dir: str | Path,
    router: Any,
    fetch_fn: Callable[[list[str], int], list[Any]],
    codex_exe: str = "",
    seed_limit: int = DEFAULT_SEED_LIMIT,
    guide_seed_n: int = DEFAULT_GUIDE_SEED_N,
    cap: int = DEFAULT_CAP,
    batch_size: int = SEED_BATCH_SIZE,
) -> dict[str, Any]:
    """시드 선택 → 조회 → 새 키워드 채점/교차검증까지 한 회차.

    `fetch_fn(seeds, depth) -> list[KeywordRow-like]` — 실전에서는
    `naver_keyword_tool.fetch_related_keywords`를 얇게 감싼 함수, 테스트에서는
    가짜 함수를 준다(`.keyword/.pc/.mobile` 속성 또는 dict).
    """
    kd_store = _kd_store()
    kr = _kr_mod()

    conn = kd_store.open_db(db_path)
    try:
        kr.migrate(conn)
        migrate_fill_columns(conn)

        total_before = kd_store.count(conn)
        if total_before >= cap:
            eligible_now = eligible_count(conn)
            return {
                "brand": brand,
                "capped": True,
                "total": total_before,
                "eligible": eligible_now,
                "new_collected": 0,
                "adopted": 0,
                "adoption_rate": 0.0,
                "seed_exhausted": False,
            }

        eligible_before = eligible_count(conn)
        seeds = select_seed_keywords(conn, seed_limit)
        used_eligible_seeds = list(seeds)
        guide_used = False
        if len(seeds) < seed_limit:
            guide_terms = guide_seed_terms(brand, guides_dir, router, guide_seed_n)
            already = {row[0] for row in conn.execute("SELECT keyword FROM keywords")}
            need = seed_limit - len(seeds)
            extra = [g for g in guide_terms if g not in already and g not in seeds][:need]
            if extra:
                guide_used = True
            seeds = seeds + extra

        seed_exhausted = len(seeds) == 0
        mark_seeded(conn, used_eligible_seeds)

        new_saved = 0
        for start in range(0, len(seeds), batch_size):
            batch = seeds[start : start + batch_size]
            try:
                related = fetch_fn(batch, 1)
            except Exception as exc:  # noqa: BLE001 - 이 브랜드 회차만 중단
                log.warning("%s: 조회 실패(씨앗=%s): %s", brand, batch, exc)
                break
            rows_to_save: list[dict[str, Any]] = []
            source_seed = ",".join(batch)
            for kr_row in related:
                kw = getattr(kr_row, "keyword", None) if not isinstance(kr_row, dict) else kr_row.get("keyword")
                if not kw:
                    continue
                pc = getattr(kr_row, "pc", None) if not isinstance(kr_row, dict) else kr_row.get("pc")
                mobile = getattr(kr_row, "mobile", None) if not isinstance(kr_row, dict) else kr_row.get("mobile")
                pc = int(pc or 0)
                mobile = int(mobile or 0)
                rows_to_save.append(
                    {
                        "keyword": str(kw).strip(),
                        "pc": pc,
                        "mobile": mobile,
                        "total": pc + mobile,
                        "source_seed": source_seed,
                        "depth": 1,
                        "relevance": 0,
                    }
                )
            if rows_to_save:
                new_saved += kd_store.save_many(conn, rows_to_save)
    finally:
        conn.close()

    score_result = {"scored": 0, "failed_batches": 0}
    cross_result = {"checked": 0, "failed_batches": 0}
    if new_saved:
        score_result = kr.score_brand(router, brand, db_path, guides_dir)
        cross_result = kr.crosscheck_brand(brand, db_path, guides_dir, codex_exe=codex_exe)

    conn = kd_store.open_db(db_path)
    try:
        eligible_after = eligible_count(conn)
        total_after = kd_store.count(conn)
    finally:
        conn.close()

    adopted = max(0, eligible_after - eligible_before)
    adoption_rate = round(adopted / new_saved, 4) if new_saved else 0.0

    return {
        "brand": brand,
        "capped": False,
        "total": total_after,
        "eligible": eligible_after,
        "new_collected": new_saved,
        "adopted": adopted,
        "adoption_rate": adoption_rate,
        "seed_exhausted": seed_exhausted,
        "guide_seed_used": guide_used,
        "seeds_used": len(seeds),
        "score_result": score_result,
        "cross_result": cross_result,
    }


# --- 목표까지 반복 ----------------------------------------------------------


def fill_until_target(
    brand: str,
    db_path: str | Path,
    guides_dir: str | Path,
    router: Any,
    fetch_fn: Callable[[list[str], int], list[Any]],
    target: int = DEFAULT_TARGET,
    data_dir: str | Path = "data",
    codex_exe: str = "",
    seed_limit: int = DEFAULT_SEED_LIMIT,
    guide_seed_n: int = DEFAULT_GUIDE_SEED_N,
    cap: int = DEFAULT_CAP,
    max_rounds: int = 100_000,
) -> dict[str, Any]:
    """`eligible >= target`이거나 시드 고갈로 멈출 때까지 `run_cycle`을 반복한다."""
    ppath = progress_path(data_dir)
    low_streak = 0
    rounds = 0
    last: dict[str, Any] = {}
    while rounds < max_rounds:
        rounds += 1
        result = run_cycle(
            brand, db_path, guides_dir, router, fetch_fn,
            codex_exe=codex_exe, seed_limit=seed_limit, guide_seed_n=guide_seed_n, cap=cap,
        )
        last = result
        eligible = result["eligible"]

        if result.get("capped"):
            update_fill_progress(
                ppath, brand, status="용량상한", round=rounds, eligible=eligible, total=result["total"],
            )
            break

        rate = result["adoption_rate"]
        if result["new_collected"] > 0 and rate < LOW_ADOPTION_THRESHOLD:
            low_streak += 1
        else:
            low_streak = 0

        status = "running"
        if eligible >= target:
            status = "done"
        elif low_streak >= LOW_ADOPTION_STREAK_LIMIT:
            status = "시드고갈"

        update_fill_progress(
            ppath,
            brand,
            status=status,
            round=rounds,
            eligible=eligible,
            total=result["total"],
            new_this_round=result["new_collected"],
            adopted_this_round=result["adopted"],
            adoption_rate=rate,
            low_adoption_streak=low_streak,
            seed_exhausted=result["seed_exhausted"],
        )

        if status in ("done", "시드고갈"):
            break
        if result["seed_exhausted"] and result["new_collected"] == 0:
            update_fill_progress(ppath, brand, status="시드고갈(신규없음)", round=rounds, eligible=eligible)
            break
    return last


# --- 실행기(숨김 프로세스) 진입점 ------------------------------------------


def _real_fetch_fn(page: Any, account_id: str, download_dir: Path) -> Callable[[list[str], int], list[Any]]:
    kt = _kt_mod()

    def _fetch(seeds: list[str], depth: int) -> list[Any]:
        return kt.fetch_related_keywords(page, seeds, account_id, download_dir=download_dir)

    return _fetch


def worker_main(argv: list[str]) -> int:
    """`python -m v2r.knowledge.keyword_fill_loop --worker <브랜드> [target]`.

    브랜드 전용 복제 프로필(`keyword_discovery_parallel.clone_profile_name`)로
    네이버 키워드 도구를 헤드리스로 열고, `LLMRouter.from_settings`(요금제 길
    우선)로 `fill_until_target`을 돈다. 로그아웃 상태면 재로그인 없이 즉시
    실패로 남긴다(철칙).
    """
    import logging as _logging

    from v2r.config import get_settings
    from v2r.knowledge import keyword_discovery_parallel as kdp
    from v2r.llm.router import LLMRouter
    from v2r.warehouse import naver_session

    brand = argv[0]
    target = int(argv[1]) if len(argv) > 1 else DEFAULT_TARGET
    settings = get_settings()
    repo = Path(settings.repo_root)
    log_file = repo / "logs" / f"keyword-fill-{brand}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    _logging.basicConfig(filename=str(log_file), level=_logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    data_dir = repo / "data"
    profile_dir = data_dir / kdp.clone_profile_name(brand)
    if not profile_dir.is_dir():
        # 발굴 병렬 실행에서 만든 복제 프로필을 그대로 쓴다(없으면 새로 복제).
        kdp.clone_all_profiles(data_dir, [brand])

    kt = _kt_mod()
    playwright, context, page, logged_in = kt.open_keyword_tool_page(profile_dir=profile_dir, headless=True)
    try:
        if not logged_in:
            log.error("%s: 네이버 로그인이 풀려 있어 채우기를 시작하지 않습니다", brand)
            return 1
        router = LLMRouter.from_settings(settings)
        fetch_fn = _real_fetch_fn(page, account_id="685753", download_dir=data_dir / "keywords" / "_tmp")
        guides_dir = repo / "warehouse" / "guides" / "정리본"
        db_path = _kt_mod().db_path_for_brand(brand, data_dir)
        result = fill_until_target(
            brand, db_path, guides_dir, router, fetch_fn,
            target=target, data_dir=data_dir,
        )
        log.info("%s: 채우기 종료 %s", brand, result)
        return 0
    finally:
        naver_session._close(playwright, context, page)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 2 and sys.argv[1] == "--worker":
        raise SystemExit(worker_main(sys.argv[2:]))
    print("사용법: python -m v2r.knowledge.keyword_fill_loop --worker <브랜드> [목표개수]")
    raise SystemExit(2)


__all__ = [
    "DEFAULT_TARGET",
    "DEFAULT_SEED_LIMIT",
    "DEFAULT_GUIDE_SEED_N",
    "SEED_BATCH_SIZE",
    "DEFAULT_CAP",
    "LOW_ADOPTION_STREAK_LIMIT",
    "LOW_ADOPTION_THRESHOLD",
    "ELIGIBLE_SQL",
    "migrate_fill_columns",
    "eligible_count",
    "select_seed_keywords",
    "mark_seeded",
    "guide_seed_terms",
    "progress_path",
    "load_progress",
    "save_progress",
    "update_fill_progress",
    "run_cycle",
    "fill_until_target",
    "worker_main",
]
