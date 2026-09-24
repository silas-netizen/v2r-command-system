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
#: 네이버 키워드 도구 한 번 조회 최대 씨앗 수(기존 값 그대로)
SEED_BATCH_SIZE = 5
#: 브랜드당 DB 총 행수 상한(무관 키워드 폭증 방지)
DEFAULT_CAP = 50_000
#: 이 회차 수만큼 연속 채택률이 문턱 아래면 "시드 고갈"로 멈춘다
LOW_ADOPTION_STREAK_LIMIT = 3
#: 채택률 문턱(이 미만이면 그 회차는 "낮음"으로 센다)
LOW_ADOPTION_THRESHOLD = 0.05

PROGRESS_FILENAME = "fill_progress.json"
#: 순환 정지 파일 — 이 파일이 있으면(`data/keywords/fill_STOP`) 모든 브랜드 워커가
#: 현재 묶음을 마치는 대로 스스로 멈춘다(강제 종료 대신 쓰는 정지 수단, 2026-09-24).
STOP_FILENAME = "fill_STOP"
#: 다른 프로세스(실행기 재채점·시트 반영)가 같은 sqlite를 쓰는 동안 "database is locked"가
#: 나면 이만큼 기다렸다 다시 시도한다(실측 2026-09-24 03:35: 장으뜸·팥순이 워커가 이 예외로 죽음).
DB_LOCK_RETRY = 20
DB_LOCK_WAIT_SEC = 60.0


class FillStopped(RuntimeError):
    """정지 파일이 감지돼 순환을 멈춘다."""


def stop_path(data_dir: str | Path = "data") -> Path:
    return Path(data_dir) / "keywords" / STOP_FILENAME


def stop_requested(data_dir: str | Path = "data") -> bool:
    return stop_path(data_dir).exists()


def _retry_db_locked(fn: Callable[[], Any], what: str, sleep_fn: Callable[[float], None] | None = None) -> Any:
    import time as _time

    sleep = sleep_fn or _time.sleep
    for attempt in range(1, DB_LOCK_RETRY + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt >= DB_LOCK_RETRY:
                raise
            log.warning("%s: DB 잠김(%d/%d) — %.0f초 뒤 재시도: %s", what, attempt, DB_LOCK_RETRY, DB_LOCK_WAIT_SEC, exc)
            sleep(DB_LOCK_WAIT_SEC)
    return None


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


# --- 마이그레이션(seeded_at·seed_source_type·fill_seeds) --------------------

#: 시드 출처 이름(사용자 승인 2026-09-24 00:50 — 4가지 + 기존 원고 대상)
SOURCE_ELIGIBLE = "원고대상"
SOURCE_GUIDE = "정리본"
SOURCE_COMPETITOR = "경쟁"
SOURCE_AUTOCOMPLETE = "자동완성"
SOURCE_RELATED = "연관검색"
SOURCE_TYPES = (SOURCE_ELIGIBLE, SOURCE_GUIDE, SOURCE_COMPETITOR, SOURCE_AUTOCOMPLETE, SOURCE_RELATED)

#: 출처별 한 회차 시드 상한
DEFAULT_GUIDE_SEED_N = 60
DEFAULT_COMPETITOR_SEED_N = 20
#: 자동완성·연관검색을 뽑을 원고 대상 키워드 수(검색량 상위)
DEFAULT_EXPAND_TOP_N = 30
#: 자동완성·연관검색 출처 각각의 회차당 시드 상한(조회 횟수 폭증 방지)
DEFAULT_EXPAND_SEED_CAP = 100

_FILL_SEEDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fill_seeds (
    seed TEXT PRIMARY KEY,
    source_type TEXT NOT NULL DEFAULT '',
    used_at TEXT NOT NULL DEFAULT ''
);
"""


def migrate_fill_columns(conn: sqlite3.Connection) -> list[str]:
    """`keywords`에 `seeded_at`(시드로 쓴 시각)·`seed_source_type`(어느 출처 시드에서
    수집됐는지)을 더하고 `fill_seeds`(시드 사용 기록) 표를 만든다."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(keywords)")}
    added: list[str] = []
    if "seeded_at" not in existing:
        conn.execute("ALTER TABLE keywords ADD COLUMN seeded_at TEXT NOT NULL DEFAULT ''")
        added.append("seeded_at")
    if "seed_source_type" not in existing:
        conn.execute("ALTER TABLE keywords ADD COLUMN seed_source_type TEXT NOT NULL DEFAULT ''")
        added.append("seed_source_type")
    conn.executescript(_FILL_SEEDS_SCHEMA)
    conn.commit()
    return added


# --- 원고 대상 판정 -------------------------------------------------------

#: 원고 대상 = 클로드·Codex 둘 다 `keyword_relevance.MANUSCRIPT_MAX_RELEVANCE` 이하
#: AND needs_review 아님. 상한을 하드코딩하지 않고 keyword_relevance 쪽 값을 따른다
#: (2026-09-24 relevance-split: 0–2 → 0–3으로 넓어져도 이 모듈은 그대로 맞춰 간다).
#: dict 한 건 판정은 `keyword_relevance.is_manuscript_target`(있으면)을 쓴다.
#: 2026-09-24 01:12 코디네이터 지시: 연관도 0–4 척도 커밋 후 원고 대상 판정은 전부
#: `keyword_relevance.is_manuscript_target`(둘 다 0에서 3, needs_review 아님)을 따른다.
#: SQL 집계도 같은 상한(`MANUSCRIPT_MAX_RELEVANCE`)을 그대로 쓴다 — 하드코딩 금지.
def manuscript_max_relevance() -> int:
    return int(getattr(_kr_mod(), "MANUSCRIPT_MAX_RELEVANCE", 3))


def eligible_sql() -> str:
    mx = manuscript_max_relevance()
    bridge = int(getattr(_kr_mod(), "RELEVANCE_BRIDGE", 3))
    # is_manuscript_target과 정확히 동일해야 한다(2026-09-24 엄격화 — 우회 SQL 금지 지시):
    # 클로드·Codex 둘 다 채점 완료(NULL 아님), 0에서 mx, 단 mx(=당위성 3)이면
    # bridge_rationale이 채워져 있을 때만 인정. relevance_codex IS NULL을 통과시키던
    # 옛 조건이 GPT 미검증 키워드를 시트로 새게 한 사고 원인이었다(작업 186).
    # scored_at이 비어 있으면(예: 옛 3=무관 점수를 재채점 대기로 돌려놓은 행) 아직
    # 판정이 확정되지 않은 것이므로 세지 않는다 — 실측(01:15): 이 행이 브랜드당 약
    # 6,000개라 그대로 세면 원고 대상이 9,000대로 부풀려진다.
    return (
        "scored_at != '' AND scored_at IS NOT NULL"
        f" AND relevance_llm IS NOT NULL AND relevance_llm >= 0 AND relevance_llm <= {mx}"
        f" AND relevance_codex IS NOT NULL AND relevance_codex >= 0 AND relevance_codex <= {mx}"
        f" AND (relevance_llm != {bridge} OR (bridge_rationale IS NOT NULL AND bridge_rationale != ''))"
        f" AND (relevance_codex != {bridge} OR (bridge_rationale IS NOT NULL AND bridge_rationale != ''))"
        " AND (needs_review = 0 OR needs_review IS NULL)"
    )


def is_eligible_row(row: Any) -> bool:
    """dict/sqlite Row 한 건의 원고 대상 판정 = `keyword_relevance.is_manuscript_target`."""
    try:
        scored = row["scored_at"]
    except (KeyError, IndexError):
        scored = "x"
    if not scored:
        return False
    return bool(_kr_mod().is_manuscript_target(row))


def eligible_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM keywords WHERE {eligible_sql()}").fetchone()[0])


def top_eligible_keywords(conn: sqlite3.Connection, limit: int = DEFAULT_EXPAND_TOP_N) -> list[str]:
    """검색량 상위 원고 대상 키워드(자동완성·연관검색 확장의 출발점)."""
    sql = f"SELECT keyword FROM keywords WHERE {eligible_sql()} ORDER BY total DESC LIMIT ?"
    return [row[0] for row in conn.execute(sql, (int(limit),))]


# --- 시드 선택 ------------------------------------------------------------


def select_seed_keywords(conn: sqlite3.Connection, limit: int = DEFAULT_SEED_LIMIT) -> list[str]:
    """아직 시드로 안 쓴 원고 대상 키워드 중 검색량(total) 상위 `limit`개."""
    sql = (
        f"SELECT keyword FROM keywords WHERE {eligible_sql()}"
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


def used_seeds(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT seed FROM fill_seeds")}


def record_seeds(conn: sqlite3.Connection, seeds: list[tuple[str, str]]) -> None:
    """`[(seed, source_type)]`를 `fill_seeds`에 기록(이미 있으면 유지)."""
    if not seeds:
        return
    stamp = _now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO fill_seeds (seed, source_type, used_at) VALUES (?, ?, ?)",
        [(s, t, stamp) for s, t in seeds],
    )
    conn.commit()


def _guide_text(brand: str, guides_dir: str | Path, max_chars: int = 6000) -> str:
    guides_dir = Path(guides_dir)
    candidates = list(guides_dir.glob(f"{brand}*.md"))
    if not candidates:
        return ""
    return candidates[0].read_text(encoding="utf-8")[:max_chars]


def _llm_string_list(router: Any, purpose: str, system: str, user: str, n: int) -> list[str]:
    from ..llm.router import extract_json

    raw = router.complete(purpose, system, user, max_tokens=1500)
    data = extract_json(raw)
    if not isinstance(data, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in data:
        t = str(t or "").strip()
        if 1 < len(t) <= 20 and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:n]


#: (1) 정리본 → 검색창에 칠 법한 증상·상황·타깃·고민 표현
_GUIDE_SEED_PROMPT = (
    "아래는 브랜드 정리본 원문이다. 이 브랜드의 타깃이 **네이버 검색창에 실제로 칠 법한**"
    " 증상·상황·타깃·고민 표현을 최대 {n}개 뽑아라. 제품명·브랜드명은 빼고, 사람들이 검색하는"
    " 말투(예: '아기 밤에 자주 깸', '코막힘 잠 못잘때')로 2에서 15자 짜리 검색어만. 중복·설명 금지.\n"
    "출력은 오직 JSON 배열: [\"검색어1\", \"검색어2\", ...].\n\n정리본:\n{text}"
)
#: (2) 경쟁·대체 제품명
_COMPETITOR_SEED_PROMPT = (
    "아래 브랜드 정리본과 이 브랜드의 원고 대상 키워드 일부를 보고, 같은 고민을 가진 사람이"
    " 대신 검색할 **경쟁 제품·대체 제품·대체 방법의 이름**을 최대 {n}개 뽑아라(일반 명사형,"
    " 2에서 15자, 브랜드 자기 제품명은 제외). 중복·설명 금지.\n"
    "출력은 오직 JSON 배열: [\"이름1\", \"이름2\", ...].\n\n정리본:\n{text}\n\n원고 대상 키워드 예:\n{keywords}"
)


def guide_seed_terms(
    brand: str,
    guides_dir: str | Path,
    router: Any = None,
    n: int = DEFAULT_GUIDE_SEED_N,
    purpose: str = "keyword_fill_seed",
) -> list[str]:
    """(1) 정리본에서 검색 표현 시드를 최대 `n`개. LLM 실패 시 정규식 추출로 대체."""
    kt = _kt_mod()
    fallback = kt.extract_guide_keywords(brand, guides_dir)[:n]
    if router is None:
        return fallback
    try:
        text = _guide_text(brand, guides_dir)
        if not text:
            return fallback
        terms = _llm_string_list(
            router, purpose, "정리본에서 검색어 시드를 뽑는 도우미다.",
            _GUIDE_SEED_PROMPT.format(n=n, text=text), n,
        )
        return terms or fallback
    except Exception as exc:  # noqa: BLE001
        log.warning("정리본 LLM 시드 추출 실패(%s) — 정규식 대체 사용: %s", brand, exc)
        return fallback


def competitor_seed_terms(
    brand: str,
    guides_dir: str | Path,
    router: Any,
    sample_keywords: list[str],
    n: int = DEFAULT_COMPETITOR_SEED_N,
    purpose: str = "keyword_fill_seed",
) -> list[str]:
    """(2) 경쟁·대체 제품명 시드 최대 `n`개(LLM 없거나 실패하면 빈 목록)."""
    if router is None:
        return []
    try:
        text = _guide_text(brand, guides_dir)
        terms = _llm_string_list(
            router, purpose, "경쟁·대체 제품명을 뽑는 도우미다.",
            _COMPETITOR_SEED_PROMPT.format(n=n, text=text, keywords=", ".join(sample_keywords[:40])), n,
        )
        if not terms:
            log.warning("경쟁 제품 LLM 시드가 비었습니다(%s)", brand)
        return terms
    except Exception as exc:  # noqa: BLE001
        log.warning("경쟁 제품 LLM 시드 추출 실패(%s): %s", brand, exc)
        return []


def expand_seed_terms(
    base_keywords: list[str],
    expand_fn: Callable[[str], list[str]],
    cap: int = DEFAULT_EXPAND_SEED_CAP,
) -> list[str]:
    """(3)(4) 기준 키워드마다 `expand_fn`(자동완성/연관검색)을 불러 후보를 모은다.
    기준 키워드 자신과 중복은 뺀다. 기준 키워드를 돌아가며 고르게 담아 `cap`까지."""
    per: list[list[str]] = []
    base_set = set(base_keywords)
    for kw in base_keywords:
        try:
            items = [str(t).strip() for t in expand_fn(kw) if str(t or "").strip()]
        except Exception as exc:  # noqa: BLE001
            log.warning("시드 확장 실패(%s): %s", kw, exc)
            items = []
        per.append([t for t in items if t not in base_set])
    out: list[str] = []
    seen: set[str] = set()
    while len(out) < cap and any(per):
        for lst in per:
            if not lst:
                continue
            t = lst.pop(0)
            if t not in seen:
                seen.add(t)
                out.append(t)
            if len(out) >= cap:
                break
    return out


def gather_seeds(
    conn: sqlite3.Connection,
    brand: str,
    guides_dir: str | Path,
    router: Any,
    autocomplete_fn: Callable[[str], list[str]] | None = None,
    related_fn: Callable[[str], list[str]] | None = None,
    seed_limit: int = DEFAULT_SEED_LIMIT,
    guide_seed_n: int = DEFAULT_GUIDE_SEED_N,
    competitor_seed_n: int = DEFAULT_COMPETITOR_SEED_N,
    expand_top_n: int = DEFAULT_EXPAND_TOP_N,
    expand_cap: int = DEFAULT_EXPAND_SEED_CAP,
) -> list[tuple[str, str]]:
    """이번 회차 시드 `[(seed, source_type)]` — 5가지 출처, 중복·DB 기존 키워드·이미 쓴 시드 제외."""
    already_kw = {row[0] for row in conn.execute("SELECT keyword FROM keywords")}
    used = used_seeds(conn)
    picked: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(terms: list[str], source: str, allow_in_db: bool = False) -> None:
        for t in terms:
            t = str(t or "").strip()
            if not t or t in seen or t in used:
                continue
            if not allow_in_db and t in already_kw:
                continue
            seen.add(t)
            picked.append((t, source))

    _add(select_seed_keywords(conn, seed_limit), SOURCE_ELIGIBLE, allow_in_db=True)
    _add(guide_seed_terms(brand, guides_dir, router, guide_seed_n), SOURCE_GUIDE)
    top = top_eligible_keywords(conn, expand_top_n)
    _add(competitor_seed_terms(brand, guides_dir, router, top, competitor_seed_n), SOURCE_COMPETITOR)
    if autocomplete_fn is not None and top:
        _add(expand_seed_terms(top, autocomplete_fn, expand_cap), SOURCE_AUTOCOMPLETE)
    if related_fn is not None and top:
        _add(expand_seed_terms(top, related_fn, expand_cap), SOURCE_RELATED)
    counts: dict[str, int] = {}
    for _s, t in picked:
        counts[t] = counts.get(t, 0) + 1
    log.info("%s: 이번 회차 시드 출처별 개수 %s", brand, counts)
    return picked


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


# --- 출처별 채택률 ---------------------------------------------------------


def source_stats(conn: sqlite3.Connection, since_iso: str) -> dict[str, dict[str, Any]]:
    """`since_iso` 이후 수집된 키워드를 출처별로 신규·채택·채택률로 묶는다."""
    out: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT seed_source_type, COUNT(*),"
        f" SUM(CASE WHEN {eligible_sql()} THEN 1 ELSE 0 END)"
        " FROM keywords WHERE collected_at >= ? GROUP BY seed_source_type",
        (since_iso,),
    ).fetchall()
    for src, new, adopted in rows:
        new = int(new or 0)
        adopted = int(adopted or 0)
        out[src or "(없음)"] = {
            "new": new,
            "adopted": adopted,
            "adoption_rate": round(adopted / new, 4) if new else 0.0,
        }
    return out


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
    autocomplete_fn: Callable[[str], list[str]] | None = None,
    related_fn: Callable[[str], list[str]] | None = None,
    competitor_seed_n: int = DEFAULT_COMPETITOR_SEED_N,
    expand_top_n: int = DEFAULT_EXPAND_TOP_N,
    expand_cap: int = DEFAULT_EXPAND_SEED_CAP,
) -> dict[str, Any]:
    """시드 수집(5출처) → 조회 → 새 키워드 채점/교차검증 → 출처별 채택률까지 한 회차.

    `fetch_fn(seeds, depth) -> list[KeywordRow-like]`; `autocomplete_fn`/`related_fn`
    (키워드 → 후보 목록)은 없으면 그 출처를 건너뛴다(테스트·오프라인).
    """
    kd_store = _kd_store()
    kr = _kr_mod()
    from v2r.store.db import now_iso as _store_now

    conn = kd_store.open_db(db_path)
    try:
        kr.migrate(conn)
        migrate_fill_columns(conn)

        total_before = kd_store.count(conn)
        if total_before >= cap:
            return {
                "brand": brand, "capped": True, "total": total_before,
                "eligible": eligible_count(conn), "new_collected": 0, "adopted": 0,
                "adoption_rate": 0.0, "seed_exhausted": False, "by_source": {},
            }

        eligible_before = eligible_count(conn)
        seeds = gather_seeds(
            conn, brand, guides_dir, router, autocomplete_fn, related_fn,
            seed_limit=seed_limit, guide_seed_n=guide_seed_n,
            competitor_seed_n=competitor_seed_n, expand_top_n=expand_top_n, expand_cap=expand_cap,
        )
        seed_counts: dict[str, int] = {}
        for _s, t in seeds:
            seed_counts[t] = seed_counts.get(t, 0) + 1
        seed_exhausted = len(seeds) == 0
        mark_seeded(conn, [s for s, t in seeds if t == SOURCE_ELIGIBLE])
        record_seeds(conn, seeds)

        round_start = _store_now()
        new_saved = 0
        # 같은 출처끼리 5개씩 묶어 조회(출처별 채택률을 정확히 나누기 위해)
        by_type: dict[str, list[str]] = {}
        for s, t in seeds:
            by_type.setdefault(t, []).append(s)
        stop = False
        for source_type, terms in by_type.items():
            if stop:
                break
            for start in range(0, len(terms), batch_size):
                batch = terms[start : start + batch_size]
                try:
                    related = fetch_fn(batch, 1)
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s: 조회 실패(출처=%s, 씨앗=%s): %s", brand, source_type, batch, exc)
                    stop = True
                    break
                rows_to_save: list[dict[str, Any]] = []
                source_seed = ",".join(batch)
                for kr_row in related:
                    is_dict = isinstance(kr_row, dict)
                    kw = kr_row.get("keyword") if is_dict else getattr(kr_row, "keyword", None)
                    if not kw:
                        continue
                    pc = int((kr_row.get("pc") if is_dict else getattr(kr_row, "pc", 0)) or 0)
                    mobile = int((kr_row.get("mobile") if is_dict else getattr(kr_row, "mobile", 0)) or 0)
                    rows_to_save.append(
                        {"keyword": str(kw).strip(), "pc": pc, "mobile": mobile, "total": pc + mobile,
                         "source_seed": source_seed, "depth": 1, "relevance": 0}
                    )
                if rows_to_save:
                    before = kd_store.count(conn)
                    kd_store.save_many(conn, rows_to_save)
                    inserted = kd_store.count(conn) - before
                    new_saved += inserted
                    if inserted:
                        conn.execute(
                            "UPDATE keywords SET seed_source_type = ? WHERE collected_at >= ?"
                            " AND (seed_source_type = '' OR seed_source_type IS NULL)",
                            (source_type, round_start),
                        )
                        conn.commit()
    finally:
        conn.close()

    score_result = {"scored": 0, "failed_batches": 0}
    cross_result = {"checked": 0, "failed_batches": 0}
    if new_saved:
        data_dir_guess = Path(db_path).resolve().parent.parent
        if not stop_requested(data_dir_guess):
            score_result = _retry_db_locked(
                lambda: kr.score_brand(router, brand, db_path, guides_dir), f"{brand} 채점"
            )
        if not stop_requested(data_dir_guess):
            cross_result = _retry_db_locked(
                lambda: kr.crosscheck_brand(brand, db_path, guides_dir, codex_exe=codex_exe), f"{brand} 교차검증"
            )

    conn = kd_store.open_db(db_path)
    try:
        eligible_after = eligible_count(conn)
        total_after = kd_store.count(conn)
        by_source = source_stats(conn, round_start)
    finally:
        conn.close()

    adopted = max(0, eligible_after - eligible_before)
    adoption_rate = round(adopted / new_saved, 4) if new_saved else 0.0

    return {
        "brand": brand, "capped": False, "total": total_after, "eligible": eligible_after,
        "new_collected": new_saved, "adopted": adopted, "adoption_rate": adoption_rate,
        "seed_exhausted": seed_exhausted, "seeds_used": len(seeds), "seed_counts": seed_counts,
        "by_source": by_source, "score_result": score_result, "cross_result": cross_result,
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
    autocomplete_fn: Callable[[str], list[str]] | None = None,
    related_fn: Callable[[str], list[str]] | None = None,
) -> dict[str, Any]:
    """`eligible >= target`이거나 시드 고갈로 멈출 때까지 `run_cycle`을 반복한다."""
    ppath = progress_path(data_dir)
    low_streak = 0
    rounds = 0
    last: dict[str, Any] = {}
    while rounds < max_rounds:
        if stop_requested(data_dir):
            update_fill_progress(ppath, brand, status="정지(STOP 파일)", eligible=last.get("eligible"))
            log.info("%s: 정지 파일 감지 — 순환을 멈춥니다", brand)
            break
        rounds += 1
        result = run_cycle(
            brand, db_path, guides_dir, router, fetch_fn,
            codex_exe=codex_exe, seed_limit=seed_limit, guide_seed_n=guide_seed_n, cap=cap,
            autocomplete_fn=autocomplete_fn, related_fn=related_fn,
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
            seed_counts=result.get("seed_counts", {}),
            by_source=result.get("by_source", {}),
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

    url = kt.KEYWORD_PLANNER_URL.format(account_id=account_id)

    def _fetch(seeds: list[str], depth: int) -> list[Any]:
        if stop_requested(download_dir.parent.parent):
            raise FillStopped("정지 파일 감지")
        try:
            return kt.fetch_related_keywords(page, seeds, account_id, download_dir=download_dir)
        except RuntimeError as exc:
            # 시드 수집(LLM·자동완성·연관검색)에 몇 분이 걸리는 동안 페이지가 바뀌어
            # 입력창/버튼을 못 찾을 수 있다(실측 2026-09-24) — 도구 페이지를 다시 열고 1회 재시도.
            if "찾지 못했습니다" not in str(exc) and "비활성" not in str(exc):
                raise
            log.warning("키워드 도구 페이지 재진입 후 재시도: %s", exc)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
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
    import time as _time

    from v2r.warehouse.naver_session import ProfileLockTimeout

    stop_file = stop_path(data_dir)
    if stop_file.exists():
        log.info("%s: 시작 전에 정지 파일이 있어 시작하지 않습니다(%s)", brand, stop_file)
        return 0
    #: 같은 브랜드의 옛 워커가 아직 프로필 잠금을 쥐고 있으면 끝날 때까지 기다린다(최대 24시간).
    wait_deadline = _time.monotonic() + 24 * 3600
    while True:
        try:
            playwright, context, page, logged_in = kt.open_keyword_tool_page(profile_dir=profile_dir, headless=True)
            break
        except ProfileLockTimeout as exc:
            if stop_file.exists() or _time.monotonic() >= wait_deadline:
                log.error("%s: 프로필 잠금 대기 중단: %s", brand, exc)
                return 1
            log.info("%s: 옛 워커가 프로필을 쓰는 중 — 120초 뒤 다시 시도(%s)", brand, exc)
            _time.sleep(120)

    class _StoppableRouter:
        """정지 파일이 생기면 채점 호출을 즉시 실패시켜 묶음 반복을 빨리 빠져나오게 한다."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def complete(self, *args: Any, **kwargs: Any) -> str:
            if stop_file.exists():
                raise FillStopped("정지 파일 감지")
            return self._inner.complete(*args, **kwargs)

    kr = _kr_mod()
    _orig_codex = kr.score_batch_codex

    def _stoppable_codex(*args: Any, **kwargs: Any) -> Any:
        if stop_file.exists():
            raise kr.RelevanceParseError("정지 파일 감지")
        return _orig_codex(*args, **kwargs)

    kr.score_batch_codex = _stoppable_codex  # 이 워커 프로세스 안에서만
    try:
        if not logged_in:
            log.error("%s: 네이버 로그인이 풀려 있어 채우기를 시작하지 않습니다", brand)
            return 1
        router = _StoppableRouter(LLMRouter.from_settings(settings))
        fetch_fn = _real_fetch_fn(page, account_id="685753", download_dir=data_dir / "keywords" / "_tmp")
        guides_dir = repo / "warehouse" / "guides" / "정리본"
        db_path = _kt_mod().db_path_for_brand(brand, data_dir)
        from v2r.knowledge import keyword_exposure as ke

        result = fill_until_target(
            brand, db_path, guides_dir, router, fetch_fn,
            target=target, data_dir=data_dir,
            autocomplete_fn=ke.naver_autocomplete_all,
            related_fn=ke.naver_related_searches,
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
    "eligible_sql",
    "is_eligible_row",
    "manuscript_max_relevance",
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
    "STOP_FILENAME",
    "stop_path",
    "stop_requested",
    "FillStopped",
    "SOURCE_TYPES",
    "gather_seeds",
    "expand_seed_terms",
    "competitor_seed_terms",
    "source_stats",
    "record_seeds",
    "used_seeds",
    "top_eligible_keywords",
]
