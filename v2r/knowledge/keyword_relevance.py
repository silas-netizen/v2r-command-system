"""키워드-브랜드 연관도(0~3)를 LLM으로 재산정한다.

문제: `data/keywords/<브랜드>.sqlite`의 `relevance`(0~3)는 낱말 겹침 기준이라
변별력이 없다(예: 장으뜸 9,996개가 전부 2). 상위 검색량이 브랜드와 무관한
일반어(이비인후과·내과·피부과)로 채워지는 원인이다.

이 모듈은 브랜드 정리본(`warehouse/guides/정리본/<브랜드>.md`)의 논리 요약을
system 프롬프트로, 키워드 묶음(최대 100개)을 user 프롬프트로 넣어 요금제 길
(`LLMRouter`, purpose=`keyword_relevance`, 0원)로 각 키워드에
`relevance 0(직접)~3(무관)` + `rationale`(3이면 빈칸)을 JSON 배열로 받는다.

- DB에는 `rationale`, `relevance_llm`, `scored_at`, `primary_brand` 열을 더한다
  (기존 `relevance`—낱말겹침 값—은 손대지 않는다).
- 여러 브랜드에 겹치는 키워드는 relevance_llm이 더 낮은(가까운) 브랜드에
  `primary_brand`를 표시한다.
- 원고 대상 = relevance_llm 0~2. 3(무관)은 제외.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Codex 교차 검증 기본 모델(2026-09-23 사용자 지시: Astra 계열 우선, 없으면 대체)
CODEX_DEFAULT_MODEL = "gpt-6-astra"
CODEX_FALLBACK_MODEL = "gpt-5.6-sol"
CODEX_DEFAULT_EFFORT = "low"
CODEX_DEFAULT_TIMEOUT = 300
#: 두 모델 점수 차이가 이 값 이상이면 재검토 표시
NEEDS_REVIEW_GAP = 2

#: 라벨 (M/N열 반영용 — 본문 분류)
RELEVANCE_LABELS: dict[int, str] = {0: "직접", 1: "근접", 2: "확장", 3: "무관"}

#: 원고 대상 상한 (이 값 이하만 원고 후보, 3은 제외)
MANUSCRIPT_MAX_RELEVANCE = 2

#: 한 번에 모델에 넣는 키워드 수
DEFAULT_BATCH_SIZE = 100

#: 실패 묶음 재시도 횟수
RETRY_COUNT = 2

PURPOSE = "keyword_relevance"

MIGRATION_COLUMNS: dict[str, str] = {
    "rationale": "TEXT NOT NULL DEFAULT ''",
    "relevance_llm": "INTEGER",
    "scored_at": "TEXT NOT NULL DEFAULT ''",
    "primary_brand": "TEXT NOT NULL DEFAULT ''",
    #: Codex(GPT) 교차 검증 점수·재검토 표시 (사용자 지시 2026-09-23)
    "relevance_codex": "INTEGER",
    "needs_review": "INTEGER NOT NULL DEFAULT 0",
    "codex_checked_at": "TEXT NOT NULL DEFAULT ''",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --- 마이그레이션 ---------------------------------------------------


def migrate(conn: sqlite3.Connection) -> list[str]:
    """`keywords` 테이블에 없는 열을 더한다. 이미 있으면 건드리지 않는다.

    돌려주는 값은 이번에 새로 더한 열 이름 목록(로그·테스트용).
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(keywords)")}
    added: list[str] = []
    for col, ddl in MIGRATION_COLUMNS.items():
        if col in existing:
            continue
        conn.execute(f"ALTER TABLE keywords ADD COLUMN {col} {ddl}")
        added.append(col)
    if added:
        conn.commit()
    return added


def migrate_path(db_path: str | Path) -> list[str]:
    """파일 경로로 마이그레이션 (연결을 열고 닫는다)."""
    conn = sqlite3.connect(str(db_path))
    try:
        return migrate(conn)
    finally:
        conn.close()


# --- 브랜드 요약 -----------------------------------------------------


def brand_summary(brand: str, guides_dir: str | Path, max_chars: int = 300) -> str:
    """정리본 md에서 제품·타깃·핵심 논리를 뽑아 `max_chars`자로 자른다.

    정리본은 시스템 프롬프트(Make용) 원문을 통째로 담고 있어 전부 넣으면
    너무 길다. `[역할]`/`[절대 규칙]`처럼 논리가 드러나는 대목과 머리말의
    "브랜드/제품", "타겟"류 메타 줄을 우선으로 모아 자른다.
    """
    guides_dir = Path(guides_dir)
    candidates = list(guides_dir.glob(f"{brand}*.md"))
    if not candidates:
        raise FileNotFoundError(f"정리본을 찾지 못했습니다: {guides_dir}/{brand}*.md")
    text = candidates[0].read_text(encoding="utf-8")

    lines = text.splitlines()
    picked: list[str] = []
    in_role_block = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and not picked:
            continue  # 제목 줄은 건너뛴다
        if stripped.startswith(("- 브랜드", "- 제품", "- 타겟", "- 타깃")):
            picked.append(stripped.lstrip("- "))
            continue
        if stripped in ("[역할]", "[절대 규칙]"):
            in_role_block = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_role_block = False
            continue
        if stripped.startswith("```"):
            in_role_block = False
            continue
        if in_role_block:
            picked.append(stripped)
        joined = " ".join(picked)
        if len(joined) >= max_chars:
            break

    summary = " ".join(picked).strip()
    if not summary:
        summary = " ".join(text.split())
    return summary[:max_chars]


# --- 프롬프트 --------------------------------------------------------


def build_system_prompt(brand: str, summary: str) -> str:
    return (
        f"너는 {brand} 브랜드의 키워드 담당자다. 아래는 이 브랜드의 제품·타깃·핵심 논리 요약이다.\n\n"
        f"{summary}\n\n"
        "이제 사용자가 보내는 키워드 목록 각각에 대해, 이 검색어를 친 사람이"
        " 우리 브랜드 논리로 자연스럽게 이어지는지 판단해 relevance(0~3)를 매긴다.\n"
        "- 0 = 직접: 브랜드/제품/증상을 바로 가리키는 핵심어\n"
        "- 1 = 근접: 같은 타깃·같은 문제의식이지만 브랜드를 직접 가리키진 않음\n"
        "- 2 = 확장: 관련은 있으나 거리가 있음(주변 정보 탐색 수준)\n"
        "- 3 = 무관: 브랜드 논리로 이어지지 않는 일반어(예: 진료과 이름, 무관한 질병)\n\n"
        "판단 기준(중요): 이비인후과·내과·피부과 같은 병원 진료과 이름, 지역명, 일반 생활어는"
        " 그 자체로는 형식적 채우기 키워드다. 이 검색자가 우리 브랜드 논리로 이어진다는 당위성이"
        " 억지스럽거나 두 문장 이상 설명을 짜내야 한다면 반드시 3으로 매긴다."
        " 병원 진료과·지역명·일반 생활어는 브랜드 논리에 **직접** 연결되는 근거가 뚜렷할 때만 2 이하로 내린다.\n\n"
        "각 키워드마다 rationale(이 검색자가 우리 논리로 이어지는 당위성 한 줄, 3이면 빈 문자열)을 함께 준다.\n"
        "출력은 오직 JSON 배열 하나. 형식:\n"
        '[{"keyword": "...", "relevance": 0, "rationale": "..."}, ...]\n'
        "입력 키워드 수와 출력 배열 길이가 반드시 같아야 하고, keyword 값은 입력 그대로여야 한다."
        " JSON 밖의 다른 텍스트는 쓰지 않는다."
    )


def build_user_prompt(keywords: list[str]) -> str:
    lines = [f"{i + 1}. {kw}" for i, kw in enumerate(keywords)]
    return "\n".join(lines)


# --- 응답 파싱 --------------------------------------------------------


class RelevanceParseError(ValueError):
    """모델 응답이 기대한 형식이 아니다."""


def parse_response(raw_text: str, keywords: list[str]) -> list[dict[str, Any]]:
    """모델 응답 텍스트에서 키워드별 relevance/rationale을 뽑는다.

    입력 `keywords` 순서·개수와 어긋나면 `RelevanceParseError`.
    """
    from ..llm.router import extract_json

    try:
        data = extract_json(raw_text)
    except ValueError as exc:
        raise RelevanceParseError(str(exc)) from exc
    if not isinstance(data, list):
        raise RelevanceParseError("응답이 JSON 배열이 아닙니다")
    if len(data) != len(keywords):
        raise RelevanceParseError(
            f"응답 개수({len(data)})가 입력 키워드 수({len(keywords)})와 다릅니다"
        )

    out: list[dict[str, Any]] = []
    for idx, (item, expected_kw) in enumerate(zip(data, keywords)):
        if not isinstance(item, dict):
            raise RelevanceParseError(f"{idx}번째 항목이 객체가 아닙니다")
        kw = str(item.get("keyword", "")).strip()
        if kw != expected_kw:
            raise RelevanceParseError(
                f"{idx}번째 keyword 불일치: 기대 '{expected_kw}', 응답 '{kw}'"
            )
        try:
            rel = int(item.get("relevance"))
        except (TypeError, ValueError) as exc:
            raise RelevanceParseError(f"{idx}번째 relevance가 정수가 아닙니다") from exc
        if rel not in RELEVANCE_LABELS:
            raise RelevanceParseError(f"{idx}번째 relevance 범위 밖: {rel}")
        rationale = str(item.get("rationale", "") or "").strip()
        if rel == 3:
            rationale = ""
        out.append({"keyword": kw, "relevance": rel, "rationale": rationale})
    return out


# --- 모델 호출(재시도 포함) -------------------------------------------


def score_batch(
    router: Any,
    brand: str,
    keywords: list[str],
    summary: str,
    retries: int = RETRY_COUNT,
) -> list[dict[str, Any]]:
    """묶음 하나를 모델로 채점한다. 실패하면 `retries`번까지 다시 시도."""
    system = build_system_prompt(brand, summary)
    user = build_user_prompt(keywords)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            text = router.complete(PURPOSE, system, user, max_tokens=4000)
            return parse_response(text, keywords)
        except Exception as exc:  # noqa: BLE001 - 재시도 대상이면 전부 잡는다
            last_error = exc
            log.warning(
                "키워드 연관도 채점 실패(%d/%d) 브랜드=%s 묶음크기=%d: %s",
                attempt + 1,
                retries + 1,
                brand,
                len(keywords),
                exc,
            )
    raise RelevanceParseError(
        f"{retries + 1}회 시도 모두 실패({brand}, {len(keywords)}개): {last_error}"
    )


# --- 진행 상황 파일 ----------------------------------------------------


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
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def update_progress(path: str | Path, brand: str, **fields: Any) -> dict[str, Any]:
    data = load_progress(path)
    entry = data.setdefault(brand, {})
    entry.update(fields)
    entry["updated_at"] = _now_iso()
    save_progress(path, data)
    return data


# --- 브랜드 전체 채점 --------------------------------------------------


def pending_keywords(conn: sqlite3.Connection, limit: int = 0) -> list[str]:
    """아직 `scored_at`이 없는 키워드 목록(검색량 내림차순)."""
    sql = "SELECT keyword FROM keywords WHERE scored_at = '' OR scored_at IS NULL ORDER BY total DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [row[0] for row in conn.execute(sql)]


def write_scores(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    stamp = _now_iso()
    conn.executemany(
        "UPDATE keywords SET relevance_llm = ?, rationale = ?, scored_at = ? WHERE keyword = ?",
        [(r["relevance"], r["rationale"], stamp, r["keyword"]) for r in rows],
    )
    conn.commit()


def score_brand(
    router: Any,
    brand: str,
    db_path: str | Path,
    guides_dir: str | Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int = 0,
    progress_path: str | Path | None = None,
) -> dict[str, int]:
    """브랜드 하나를 처음부터 끝까지(또는 `limit`개까지) 재산정한다.

    돌려주는 값: {"scored": 성공 개수, "failed_batches": 실패 묶음 수}.
    """
    conn = sqlite3.connect(str(db_path))
    added = migrate(conn)
    if added:
        log.info("마이그레이션 %s: %s", db_path, added)
    summary = brand_summary(brand, guides_dir)

    scored = 0
    failed_batches = 0
    try:
        keywords = pending_keywords(conn, limit=limit)
        for start in range(0, len(keywords), batch_size):
            chunk = keywords[start : start + batch_size]
            try:
                rows = score_batch(router, brand, chunk, summary)
            except RelevanceParseError as exc:
                failed_batches += 1
                log.error("묶음 포기(%s, %d개): %s", brand, len(chunk), exc)
                continue
            write_scores(conn, rows)
            scored += len(rows)
            if progress_path is not None:
                update_progress(
                    progress_path,
                    brand,
                    scored=scored,
                    total_pending_at_start=len(keywords),
                    failed_batches=failed_batches,
                    status="running",
                )
    finally:
        conn.close()

    if progress_path is not None:
        update_progress(progress_path, brand, status="done", scored=scored, failed_batches=failed_batches)
    return {"scored": scored, "failed_batches": failed_batches}


# --- 브랜드 간 중복 배정 -----------------------------------------------


def assign_primary_brand(brand_db_paths: dict[str, str | Path]) -> dict[str, int]:
    """여러 브랜드 DB에서 겹치는 키워드를 찾아, relevance_llm이 더 낮은(가까운)
    브랜드에 `primary_brand`를 표시한다. 동률이면 먼저 나온 브랜드.

    돌려주는 값: {브랜드: primary로 배정된 키워드 수}.
    """
    per_kw: dict[str, list[tuple[str, int]]] = {}
    conns: dict[str, sqlite3.Connection] = {}
    try:
        for brand, path in brand_db_paths.items():
            conn = sqlite3.connect(str(path))
            conns[brand] = conn
            for kw, rel in conn.execute(
                "SELECT keyword, relevance_llm FROM keywords WHERE relevance_llm IS NOT NULL"
            ):
                per_kw.setdefault(kw, []).append((brand, int(rel)))

        counts: dict[str, int] = {b: 0 for b in brand_db_paths}
        for kw, entries in per_kw.items():
            if len(entries) < 2:
                brand, _ = entries[0]
                conns[brand].execute(
                    "UPDATE keywords SET primary_brand = ? WHERE keyword = ?", (brand, kw)
                )
                counts[brand] += 1
                continue
            entries.sort(key=lambda e: e[1])
            winner = entries[0][0]
            for brand, _ in entries:
                conns[brand].execute(
                    "UPDATE keywords SET primary_brand = ? WHERE keyword = ?",
                    (winner if brand == winner else "", kw),
                )
            counts[winner] += 1
        for conn in conns.values():
            conn.commit()
        return counts
    finally:
        for conn in conns.values():
            conn.close()


# --- 현황 --------------------------------------------------------------


def brand_status(db_path: str | Path) -> dict[str, Any]:
    """브랜드 하나의 0/1/2/3 분포와 미산정 수."""
    conn = sqlite3.connect(str(db_path))
    try:
        migrate(conn)
        dist = dict.fromkeys(range(4), 0)
        for rel, cnt in conn.execute(
            "SELECT relevance_llm, COUNT(*) FROM keywords WHERE relevance_llm IS NOT NULL GROUP BY relevance_llm"
        ):
            dist[int(rel)] = cnt
        unscored = conn.execute(
            "SELECT COUNT(*) FROM keywords WHERE scored_at = '' OR scored_at IS NULL"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]
        return {"total": total, "unscored": unscored, "distribution": dist}
    finally:
        conn.close()


def status_report(data_dir: str | Path, brands: list[str] | None = None) -> dict[str, dict[str, Any]]:
    data_dir = Path(data_dir)
    if brands is None:
        brands = [p.stem for p in data_dir.glob("*.sqlite")]
    return {brand: brand_status(data_dir / f"{brand}.sqlite") for brand in brands}


# --- Codex(GPT) 교차 검증 -----------------------------------------------


def find_codex() -> str:
    """`codex.exe` 자리를 찾는다 (기존 `gpt_crosscheck.find_codex` 재사용)."""
    from ..content.gpt_crosscheck import find_codex as _find

    return _find()


def run_codex(
    prompt: str,
    exe: str = "",
    model: str = CODEX_DEFAULT_MODEL,
    effort: str = CODEX_DEFAULT_EFFORT,
    timeout: int = CODEX_DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
) -> tuple[str, str]:
    """codex를 읽기 전용으로 한 번 부른다. `(출력, 오류설명)`.

    지정한 모델(기본 `gpt-6-astra`)이 안 되면 `CODEX_FALLBACK_MODEL`로 한 번
    더 시도한다(호출측이 `codex_model_used`로 실제 쓴 모델을 알 수 있다).
    """
    exe = exe or find_codex()
    if not exe:
        return "", "codex 실행 파일을 찾지 못했습니다"
    cmd = [
        exe,
        "exec",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "--color",
        "never",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-",
    ]
    try:
        done = subprocess.run(  # noqa: S603 - 고정된 명령이다
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired:
        return "", f"{timeout}초 안에 답이 오지 않았습니다"
    except Exception as exc:  # noqa: BLE001 - 실행 자체가 안 될 때
        return "", f"codex 실행 실패: {exc}"
    out = done.stdout or ""
    err = (done.stderr or "").strip()
    if not out.strip():
        return "", err[:300] or "빈 응답"
    return out, ""


def score_batch_codex(
    brand: str,
    keywords: list[str],
    summary: str,
    exe: str = "",
    cwd: str | Path | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """codex로 같은 묶음을 같은 기준으로 채점한다.

    돌려주는 값: `(rows, model_used)`. 지정 모델이 전부 실패하면 `RelevanceParseError`.
    """
    prompt = build_system_prompt(brand, summary) + "\n\n" + build_user_prompt(keywords)
    exe = exe or find_codex()
    last_error = ""
    for model in (CODEX_DEFAULT_MODEL, CODEX_FALLBACK_MODEL):
        out, err = run_codex(prompt, exe=exe, model=model, cwd=cwd)
        if err:
            last_error = f"{model}: {err}"
            log.warning("codex 모델 %s 실패 — 다음 모델로: %s", model, err)
            continue
        try:
            rows = parse_response(out, keywords)
        except RelevanceParseError as exc:
            last_error = f"{model}: {exc}"
            log.warning("codex 응답 파싱 실패(%s) — 다음 모델로: %s", model, exc)
            continue
        return rows, model
    raise RelevanceParseError(f"codex 교차 검증 실패({brand}): {last_error}")


def crosscheck_rows(
    claude_rows: list[dict[str, Any]], codex_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """클로드·codex 점수를 합쳐 `needs_review`와 최종 점수를 정한다.

    2점 이상 차이 나면 `needs_review=True`이고, 보수적으로(무관 쪽인) 큰 값을
    최종 relevance로 채택한다. 차이가 작으면 클로드 점수를 그대로 쓴다.
    """
    codex_by_kw = {r["keyword"]: r for r in codex_rows}
    merged: list[dict[str, Any]] = []
    for row in claude_rows:
        kw = row["keyword"]
        codex_row = codex_by_kw.get(kw)
        if codex_row is None:
            merged.append(
                {**row, "relevance_codex": None, "needs_review": False, "final_relevance": row["relevance"]}
            )
            continue
        c_rel = int(codex_row["relevance"])
        gap = abs(row["relevance"] - c_rel)
        needs_review = gap >= NEEDS_REVIEW_GAP
        final = max(row["relevance"], c_rel) if needs_review else row["relevance"]
        merged.append(
            {**row, "relevance_codex": c_rel, "needs_review": needs_review, "final_relevance": final}
        )
    return merged


def write_crosscheck(conn: sqlite3.Connection, merged: list[dict[str, Any]]) -> None:
    stamp = _now_iso()
    conn.executemany(
        "UPDATE keywords SET relevance_llm = ?, relevance_codex = ?, needs_review = ?,"
        " codex_checked_at = ? WHERE keyword = ?",
        [
            (
                r["final_relevance"],
                r["relevance_codex"],
                1 if r["needs_review"] else 0,
                stamp,
                r["keyword"],
            )
            for r in merged
        ],
    )
    conn.commit()


def agreement_rate(merged: list[dict[str, Any]]) -> float:
    """±1 이내로 일치하는 비율 (codex 점수가 있는 항목만 대상)."""
    checked = [r for r in merged if r.get("relevance_codex") is not None]
    if not checked:
        return 0.0
    agree = sum(1 for r in checked if abs(r["relevance"] - r["relevance_codex"]) <= 1)
    return round(agree / len(checked), 4)


def manuscript_eligible(row: dict[str, Any]) -> bool:
    """원고 대상인가: 클로드·codex 둘 다 0~2 (사용자 지시 2026-09-23 §3)."""
    claude_rel = row.get("relevance")
    codex_rel = row.get("relevance_codex")
    if claude_rel is None or claude_rel > MANUSCRIPT_MAX_RELEVANCE:
        return False
    if codex_rel is not None and codex_rel > MANUSCRIPT_MAX_RELEVANCE:
        return False
    return True


__all__ = [
    "RELEVANCE_LABELS",
    "MANUSCRIPT_MAX_RELEVANCE",
    "RelevanceParseError",
    "migrate",
    "migrate_path",
    "brand_summary",
    "build_system_prompt",
    "build_user_prompt",
    "parse_response",
    "score_batch",
    "score_brand",
    "pending_keywords",
    "write_scores",
    "assign_primary_brand",
    "brand_status",
    "status_report",
    "load_progress",
    "save_progress",
    "update_progress",
    "find_codex",
    "run_codex",
    "score_batch_codex",
    "crosscheck_rows",
    "write_crosscheck",
    "agreement_rate",
    "manuscript_eligible",
    "CODEX_DEFAULT_MODEL",
    "CODEX_FALLBACK_MODEL",
    "NEEDS_REVIEW_GAP",
]
