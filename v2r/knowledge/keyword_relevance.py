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
- 원고 대상 = relevance_llm 0에서 3(2026-09-24부터: 0 직접/1 근접/2 확장/3 당위성).
  4(무관)만 제외한다. 척도가 0-3(무관 포함) → 0-4(당위성/무관 분리)로 넓어졌다
  (사용자 지시 2026-09-24, docs/reports/relevance-split-2026-09-24.md).

원고 대상 판정은 반드시 `is_manuscript_target(row)`를 통해서만 한다(하드코딩 금지).
`row`는 최소 `relevance_llm`(또는 `relevance`)·`relevance_codex`·`needs_review` 키를
가진 dict 또는 sqlite3.Row다. 다른 모듈(sheets_writer.py, brand_queue.py,
exposure_priority.py, keyword_exposure.py)은 이 함수를 import해서 쓴다.
keyword_fill_loop.py는 이번 변경 대상이 아니다(다른 작업자가 시드 다양화 작업
중) — 그 파일 담당자가 나중에 `_relevance_eligible_keywords` 류 로직을
`is_manuscript_target`로 갈아끼우면 된다: 시그니처는
`is_manuscript_target(row: Mapping[str, Any]) -> bool`이고, sqlite Row/딕셔너리
어느 쪽이든 `row["relevance_llm"]`, `row["relevance_codex"]`, `row["needs_review"]`
키(또는 속성)로 값을 얻을 수 있으면 된다.
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

#: 라벨 (M/N열 반영용 — 본문 분류). 2026-09-24: 0-3(무관 포함) → 0-4로 확장,
#: 3(당위성)과 4(무관)을 분리했다(사용자 지시).
RELEVANCE_LABELS: dict[int, str] = {0: "직접", 1: "근접", 2: "확장", 3: "당위성", 4: "무관"}

#: 원고 대상 상한 (이 값 이하만 원고 후보, 4(무관)만 제외)
MANUSCRIPT_MAX_RELEVANCE = 3

#: 당위성 등급(3) — bridge 필드가 채워져야 하는 등급
RELEVANCE_BRIDGE = 3
#: 무관 등급(4) — rationale이 비어야 하는 등급
RELEVANCE_UNRELATED = 4

#: 한 번에 모델에 넣는 키워드 수 (2026-09-24: 100 → 50, JSON 잘림 방지)
DEFAULT_BATCH_SIZE = 50

#: 실패 묶음 재시도 횟수
RETRY_COUNT = 2

#: 모델 응답 최대 토큰 (2026-09-24: 4000 → 8000, "JSON이 끝나지 않았습니다" 잘림 방지)
DEFAULT_MAX_TOKENS = 8000

#: keyword 대조 시 허용하는 편집 거리 비율(기대 키워드 길이 대비) — 오타 교정 응답 수용
KEYWORD_FUZZY_RATIO = 0.3
#: 편집 거리 허용 최소값(짧은 키워드도 최소 이만큼은 봐준다)
KEYWORD_FUZZY_MIN = 2

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
    #: relevance==3(당위성)일 때의 연결 논리 한 줄 (사용자 지시 2026-09-24)
    "bridge_rationale": "TEXT NOT NULL DEFAULT ''",
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

    # 1단계: 브랜드/제품/타깃 정체성이 담긴 메타 줄을 (파일 안 위치와 무관하게)
    # 전부 먼저 모은다. 예전에는 "[절대 규칙]" 같은 작성 지침 블록이 이 줄들보다
    # 앞에 오면 300자 한도에서 정체성 줄이 아예 밀려나(장으뜸·뉴더미스 사례),
    # 모델이 "브랜드 정보가 없다"며 JSON 대신 거부 응답을 내는 원인이 됐다
    # (2026-09-23 연관도 재산정 실패 조사).
    identity: list[str] = []
    identity_keywords = ("브랜드", "제품", "타겟", "타깃")
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("```"):
            continue
        if stripped.startswith("-") and any(k in stripped[:12] for k in identity_keywords):
            identity.append(stripped.lstrip("- "))

    # 2단계: 정체성 줄만으로 부족하면 [역할]/[절대 규칙] 블록으로 채운다(예전 동작).
    picked: list[str] = list(identity)
    if len(" ".join(picked)) < max_chars:
        in_role_block = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#") and not picked:
                continue  # 제목 줄은 건너뛴다
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
        " 우리 브랜드 논리로 자연스럽게 이어지는지 판단해 relevance(0에서 4)를 매긴다.\n"
        "- 0 = 직접: 브랜드/제품/증상을 바로 가리키는 핵심어\n"
        "- 1 = 근접: 같은 타깃·같은 문제의식이지만 브랜드를 직접 가리키진 않음\n"
        "- 2 = 확장: 관련은 있으나 거리가 있음(주변 정보 탐색 수준)\n"
        "- 3 = 당위성: 브랜드 논리로 자연스럽게 '이어 붙일 수 있는' 키워드. 직접·근접·확장만큼"
        " 밀접하진 않지만, 한 문장짜리 다리(bridge)를 놓으면 억지스럽지 않게 브랜드로 연결된다."
        " 원고(콘텐츠) 대상에 포함한다.\n"
        "- 4 = 무관: 브랜드 논리로 이어지지 않는 일반어. 다리를 놓으려면 억지스럽거나 두 문장 이상"
        " 설명을 짜내야 한다. 원고 대상에서 제외한다.\n\n"
        "3(당위성)과 4(무관)의 경계가 이번 척도의 핵심이다. 예시(우아덤 기준, 산부인과 여성 건강"
        " 브랜드):\n"
        "  - 3(당위성)으로 매기는 예: 대상포진, 독감, 위고비, 근처피부과, 사마귀"
        " — 면역·호르몬·피부 컨디션 등 브랜드 논리와 한 문장으로 이어 붙일 다리가 있다"
        "(예: '독감으로 몸살을 앓으면 면역이 떨어져 여성 건강 관리가 더 중요해진다').\n"
        "  - 4(무관)로 매기는 예: 피부과, 안과, 치과, 마운자로, 독감예방접종"
        " — 진료과 이름이거나 비만·안과·치과처럼 브랜드 타깃·논리와 맞닿는 지점이 없어,"
        " 다리를 놓으려면 근거 없이 우기는 것이 된다.\n"
        "브랜드마다 이 경계가 다르다: 위 예시는 우아덤 기준이고, 다른 브랜드는 위 요약의 제품·"
        "타깃·핵심 논리에 맞춰 판단한다(예시를 다른 브랜드에 그대로 옮기지 않는다).\n\n"
        "판단 기준(중요): 이비인후과·내과·피부과 같은 병원 진료과 이름, 지역명, 일반 생활어는"
        " 그 자체로는 형식적 채우기 키워드다. 브랜드 논리로 이어진다는 당위성이"
        " 두 문장 이상 설명을 짜내야 할 만큼 억지스럽다면 4로 매긴다."
        " 병원 진료과·지역명·일반 생활어는 브랜드 논리에 **직접** 연결되는 근거가 뚜렷할 때만 2 이하로 내린다.\n"
        "질병명·증상명·의료행위(예: 대상포진, 독감, 매독, 헤르페스, 다낭성난소증후군, 예방접종,"
        " 건강검진 같은 것)는 **우리 브랜드 제품이 그 질병/증상을 직접 다루거나 개선을 표방할 때** 0~2를,"
        " 직접 다루진 않지만 한 문장 다리로 자연스럽게 이어지면 3을,"
        " 그렇지도 않으면(브랜드 제품과 무관한 질병·증상이면) 4를 매긴다."
        " '건강 관리에 도움이 된다'처럼 막연히 관련짓지 않는다.\n\n"
        "각 키워드마다 rationale(이 검색자가 우리 논리로 이어지는 당위성 한 줄, 4면 빈 문자열)을 함께 준다.\n"
        "relevance가 3(당위성)인 항목은 반드시 bridge(브랜드로 자연스럽게 이어 붙이는 한 문장 논리)도"
        " 함께 준다. 3이 아니면 bridge는 빈 문자열로 둔다.\n"
        "출력은 오직 JSON 배열 하나. 형식:\n"
        '[{"keyword": "...", "relevance": 0, "rationale": "...", "bridge": "..."}, ...]\n'
        "입력 키워드 수와 출력 배열 길이가 반드시 같아야 하고, keyword 값은 입력 그대로여야 한다."
        " JSON 밖의 다른 텍스트는 쓰지 않는다."
    )


def build_user_prompt(keywords: list[str]) -> str:
    lines = [f"{i + 1}. {kw}" for i, kw in enumerate(keywords)]
    return "\n".join(lines)


# --- 응답 파싱 --------------------------------------------------------


class RelevanceParseError(ValueError):
    """모델 응답이 기대한 형식이 아니다."""


def _levenshtein(a: str, b: str) -> int:
    """편집 거리(삽입/삭제/치환 1회씩). 짧은 문자열용 표준 DP."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def _keyword_matches(expected: str, got: str) -> bool:
    """정규화(공백 제거·소문자) 후 같거나, 오타 수준(편집거리 <= 길이의 30%, 최소 2)이면 허용."""
    norm_expected = expected.replace(" ", "").lower()
    norm_got = got.replace(" ", "").lower()
    if norm_expected == norm_got:
        return True
    limit = max(KEYWORD_FUZZY_MIN, int(len(norm_expected) * KEYWORD_FUZZY_RATIO))
    return _levenshtein(norm_expected, norm_got) <= limit


def parse_response(raw_text: str, keywords: list[str]) -> list[dict[str, Any]]:
    """모델 응답 텍스트에서 키워드별 relevance/rationale을 뽑는다.

    응답 항목은 **위치(index)로 기대 키워드에 대응**시킨다(모델이 이따금
    오타를 "교정"해 돌려주는 탓에 keyword 문자열 그대로 비교하면 묶음 전체가
    실패하는 문제 — 2026-09-24). 정규화 후 같거나 편집 거리가 작으면 기대
    키워드 그대로 저장한다(응답의 철자는 버린다). 응답에 keyword 필드가 아예
    없어도 위치로만 대응한다. 개수가 다르거나 대응이 크게 어긋나면
    `RelevanceParseError`.
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
        raw_kw = item.get("keyword")
        if raw_kw is not None:
            got_kw = str(raw_kw).strip()
            if not _keyword_matches(expected_kw, got_kw):
                raise RelevanceParseError(
                    f"{idx}번째 keyword 크게 어긋남: 기대 '{expected_kw}', 응답 '{got_kw}'"
                )
        kw = expected_kw
        try:
            rel = int(item.get("relevance"))
        except (TypeError, ValueError) as exc:
            raise RelevanceParseError(f"{idx}번째 relevance가 정수가 아닙니다") from exc
        if rel not in RELEVANCE_LABELS:
            raise RelevanceParseError(f"{idx}번째 relevance 범위 밖: {rel}")
        rationale = str(item.get("rationale", "") or "").strip()
        bridge = str(item.get("bridge", "") or "").strip()
        if rel == RELEVANCE_UNRELATED:
            rationale = ""
        if rel != RELEVANCE_BRIDGE:
            bridge = ""
        out.append(
            {"keyword": kw, "relevance": rel, "rationale": rationale, "bridge_rationale": bridge}
        )
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
            text = router.complete(PURPOSE, system, user, max_tokens=DEFAULT_MAX_TOKENS)
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


def pending_codex_rows(
    conn: sqlite3.Connection, limit: int = 0
) -> list[tuple[str, int, str, str]]:
    """클로드는 채점됐지만 아직 Codex 교차 검증이 안 된 (키워드, relevance, rationale, bridge_rationale)."""
    sql = (
        "SELECT keyword, relevance_llm, rationale, bridge_rationale FROM keywords"
        " WHERE scored_at != '' AND relevance_codex IS NULL ORDER BY total DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def write_scores(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    stamp = _now_iso()
    conn.executemany(
        "UPDATE keywords SET relevance_llm = ?, rationale = ?, bridge_rationale = ?,"
        " scored_at = ? WHERE keyword = ?",
        [
            (r["relevance"], r["rationale"], r.get("bridge_rationale", ""), stamp, r["keyword"])
            for r in rows
        ],
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


def crosscheck_brand(
    brand: str,
    db_path: str | Path,
    guides_dir: str | Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int = 0,
    progress_path: str | Path | None = None,
    codex_exe: str = "",
) -> dict[str, int]:
    """클로드 채점이 끝난 키워드 중 Codex 교차 검증이 안 된 것을 전부 처리한다."""
    conn = sqlite3.connect(str(db_path))
    migrate(conn)
    summary = brand_summary(brand, guides_dir)
    checked = 0
    failed_batches = 0
    try:
        rows = pending_codex_rows(conn, limit=limit)
        for start in range(0, len(rows), batch_size):
            chunk = rows[start : start + batch_size]
            claude_rows = [
                {
                    "keyword": kw,
                    "relevance": int(rel),
                    "rationale": ra or "",
                    "bridge_rationale": br or "",
                }
                for kw, rel, ra, br in chunk
            ]
            keywords = [r["keyword"] for r in claude_rows]
            try:
                codex_rows, _model = score_batch_codex(
                    brand, keywords, summary, exe=codex_exe
                )
            except RelevanceParseError as exc:
                failed_batches += 1
                log.error("codex 묶음 포기(%s, %d개): %s", brand, len(chunk), exc)
                continue
            merged = crosscheck_rows(claude_rows, codex_rows)
            write_crosscheck(conn, merged)
            checked += len(merged)
            if progress_path is not None:
                update_progress(
                    progress_path,
                    brand,
                    codex_checked=checked,
                    codex_failed_batches=failed_batches,
                    codex_status="running",
                )
    finally:
        conn.close()
    if progress_path is not None:
        update_progress(progress_path, brand, codex_status="done", codex_checked=checked)
    return {"checked": checked, "failed_batches": failed_batches}


def score_and_crosscheck_brand(
    router: Any,
    brand: str,
    db_path: str | Path,
    guides_dir: str | Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress_path: str | Path | None = None,
    codex_exe: str = "",
) -> dict[str, int]:
    """브랜드 전량을 클로드 채점 → Codex 교차 검증까지 한 번에 끝낸다(명령용 진입점)."""
    claude_result = score_brand(
        router, brand, db_path, guides_dir, batch_size=batch_size, progress_path=progress_path
    )
    codex_result = crosscheck_brand(
        brand, db_path, guides_dir, batch_size=batch_size, progress_path=progress_path, codex_exe=codex_exe
    )
    return {
        "scored": claude_result["scored"],
        "score_failed_batches": claude_result["failed_batches"],
        "checked": codex_result["checked"],
        "codex_failed_batches": codex_result["failed_batches"],
    }


# --- 구 척도(0-3) 재채점: 3(무관) → 3(당위성)/4(무관) 분리 ------------------


def pending_legacy_unrelated_rows(conn: sqlite3.Connection, limit: int = 0) -> list[str]:
    """구 척도(0-3, 3=무관)로 채점된 뒤 아직 새 척도로 재채점되지 않은 키워드.

    판정: relevance_llm == 3(구 무관)이고 relevance_codex도 3 이하(구 척도에는 4가
    없었으므로)이며, 아직 bridge_rationale/재채점 흔적이 없는 것(needs_review로
    걸려있던 것도 포함해 전부 다시 본다). 검색량(total) 내림차순.
    """
    sql = (
        "SELECT keyword FROM keywords"
        " WHERE relevance_llm = 3"
        " AND (relevance_codex IS NULL OR relevance_codex <= 3)"
        " ORDER BY total DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [row[0] for row in conn.execute(sql)]


def rescore_legacy_unrelated_brand(
    router: Any,
    brand: str,
    db_path: str | Path,
    guides_dir: str | Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int = 0,
    progress_path: str | Path | None = None,
    codex_exe: str = "",
) -> dict[str, int]:
    """구 척도 3(무관)이던 키워드만 새 프롬프트로 재채점해 3(당위성)/4(무관)로 나눈다.

    `score_and_crosscheck_brand`와 같은 패턴(클로드 100개 배치 → Codex 교차검증)을
    쓰되, 대상을 `pending_legacy_unrelated_rows`로 좁힌다. 대량(5개 브랜드
    약 3만 7천개) 실행은 이 함수를 부르는 CLI에서 브랜드별로 명시적으로
    실행해야 한다(이 모듈은 자동으로 전량을 돌리지 않는다).

    돌려주는 값은 `score_and_crosscheck_brand`와 같은 형태.
    """
    conn = sqlite3.connect(str(db_path))
    added = migrate(conn)
    if added:
        log.info("마이그레이션 %s: %s", db_path, added)
    try:
        targets = pending_legacy_unrelated_rows(conn, limit=limit)
        if not targets:
            return {"scored": 0, "score_failed_batches": 0, "checked": 0, "codex_failed_batches": 0}
        # 재채점 대상만 다시 미채점 상태로 되돌려 score_brand/crosscheck_brand의
        # 기존 pending 로직을 그대로 재사용한다.
        conn.executemany(
            "UPDATE keywords SET scored_at = '', relevance_codex = NULL,"
            " needs_review = 0, codex_checked_at = '' WHERE keyword = ?",
            [(kw,) for kw in targets],
        )
        conn.commit()
    finally:
        conn.close()

    return score_and_crosscheck_brand(
        router,
        brand,
        db_path,
        guides_dir,
        batch_size=batch_size,
        progress_path=progress_path,
        codex_exe=codex_exe,
    )


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
        dist = dict.fromkeys(range(5), 0)
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
        " bridge_rationale = ?, codex_checked_at = ? WHERE keyword = ?",
        [
            (
                r["final_relevance"],
                r["relevance_codex"],
                1 if r["needs_review"] else 0,
                r.get("bridge_rationale", ""),
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
    """원고 대상인가: 클로드·codex 둘 다 0~2 (사용자 지시 2026-09-23 §3, 구 버전).

    2026-09-24부터는 `is_manuscript_target`을 쓴다. 이 함수는 옛 호출부 호환용으로
    남겨둔다.
    """
    claude_rel = row.get("relevance")
    codex_rel = row.get("relevance_codex")
    if claude_rel is None or claude_rel > MANUSCRIPT_MAX_RELEVANCE:
        return False
    if codex_rel is not None and codex_rel > MANUSCRIPT_MAX_RELEVANCE:
        return False
    return True


def is_manuscript_target(row: Any) -> bool:
    """원고(콘텐츠) 대상인가 (사용자 지시 2026-09-24, 엄격판).

    규칙: relevance_llm·relevance_codex 둘 다 **채점 완료**(None 아님)이고,
    needs_review가 아니며, 각각 0에서 3 범위 안이되 **3은 bridge_rationale이
    채워져 있을 때만** 인정한다(당위성 논리 없는 3은 무관 취급).

    - relevance_llm이 None이면 즉시 탈락(과거에는 여기서 `relevance`로
      대체하거나 relevance_codex가 None이면 그냥 통과시켰는데, 그 틈으로
      GPT 미검증·재채점 전 키워드가 시트에 섞여 들어갔다 — 2026-09-24 사고).
    - relevance_codex가 None이어도 즉시 탈락(교차 검증 전이면 원고 대상 아님).
    - relevance_codex == 3인 경우, 이 값이 구 척도(재채점 전, 논리 없는 "3=무관")가
      아니라 새 척도(당위성)에서 나온 값인지는 이 스키마에 별도 열
      (crosscheck_at/rescored_at 같은)이 없어 직접 구분할 수 없다. 대신
      `rescore_legacy_unrelated_brand`가 재채점 시 `relevance_codex`를 NULL로
      되돌려 교차 검증을 다시 받게 만들어 두었으므로, relevance_codex 값이
      존재한다는 사실 자체가 "그 재채점 이후에 다시 채점됐다"는 증거가 된다.
      즉 bridge_rationale 유무 검사와 결합하면 구 3(논리 없음)은 이 함수를
      통과하지 못한다.

    `row`는 dict 또는 sqlite3.Row(둘 다 매핑처럼 `row["key"]`로 접근 가능)다.
    이것이 sheets_writer.py/keyword_fill_loop.py/keyword_exposure.py/
    brand_queue.py가 공통으로 써야 하는 원고 대상 판정 함수다(우회 SQL 조건 금지).
    """

    def _get(key: str) -> Any:
        try:
            val = row[key]
        except (KeyError, IndexError):
            return None
        return val

    def _rel_ok(rel: Any) -> bool:
        if rel is None:
            return False
        rel = int(rel)
        if rel < 0 or rel > MANUSCRIPT_MAX_RELEVANCE:
            return False
        if rel == RELEVANCE_BRIDGE:
            bridge = str(_get("bridge_rationale") or "").strip()
            if not bridge:
                return False
        return True

    llm_rel = _get("relevance_llm")
    if not _rel_ok(llm_rel):
        return False

    codex_rel = _get("relevance_codex")
    if not _rel_ok(codex_rel):
        return False

    needs_review = _get("needs_review")
    if needs_review:
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
    "is_manuscript_target",
    "RELEVANCE_BRIDGE",
    "RELEVANCE_UNRELATED",
    "pending_legacy_unrelated_rows",
    "rescore_legacy_unrelated_brand",
    "CODEX_DEFAULT_MODEL",
    "CODEX_FALLBACK_MODEL",
    "NEEDS_REVIEW_GAP",
    "pending_codex_rows",
    "crosscheck_brand",
    "score_and_crosscheck_brand",
]
