"""발행 실행기: 원고 준비 → 슬롯 계획 → 슬롯 실행. DESIGN §5, §6 기준."""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from v2r.accounts.assign import AssignError, assign, rotate
from v2r.accounts.loader import Account, load_from_rows
from v2r.accounts.rules import eligible, find_affiliate, is_staff_level, work_type_for
from v2r.api import articles as api_articles
from v2r.api.errors import V2RApiError, classify
from v2r.command.spec import TaskSpec
from v2r.content import comments as comment_mod
from v2r.content import duplicate, sanitize as sanitize_mod, seone
from v2r.content.manuscript import Manuscript
from v2r.engine import article_sync
from v2r.engine.context import Runtime
from v2r.engine.scheduler import KST, plan_slots, revision_at

#: 같은 카페 연속 허용 상한(이 수 이상 몰리면 다른 카페 원고를 끌어와 섞는다, 사용자 규칙 2026-09-20)
MAX_SAME_CAFE_RUN = 3
from v2r.sources import sheets

log = logging.getLogger(__name__)

DAILY_POOL_SOURCE = "랜덤일상"
DEFAULT_CAFE = "고요한 아침"
#: 모의 실행에서 카탈로그(가입 카페·게시판 권한)를 조회하지 않고 계정 결정을 미룰 때 쓰는 표시
DEFERRED_ACCOUNT = "(실행 시 결정)"
#: 명령이 카페를 지정했는데 그 카페 원고가 안 나올 때, 이만큼 훑고 나면 아무 원고나 쓴다
CAFE_SCAN_LIMIT = 500
RESTRICT_DAYS = 30
RESTRICT_CODE = "27000"
WAIT_WRITTEN_MAX_S = 120.0

# 서버가 요청 자체를 거부한 경우(글이 생기지 않음) → 다른 계정으로 재시도 가능하게 failed로 내린다
DEFINITIVE_REJECTIONS = {
    "account_restricted",
    "grade",
    "no_membership",
    "consecutive_limit",
    "not_login",
}


#: 서버가 "지금은 안 된다"고 한 것뿐인 오류들. 글이 만들어지지 않았고,
#: 제한이 풀리면 같은 원고로 그대로 다시 시도할 수 있다.
RATE_KINDS = {"rate_limited", "rate_limited_long", "login_budget"}


class PublishError(RuntimeError):
    """발행 실패(복구 불가).

    `kind`/`retry_after`는 상위(worker)가 레이트 제한 대기를 결정할 때 쓴다.
    """

    def __init__(
        self,
        message: str = "",
        *,
        kind: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class RetryWithOtherAccount(RuntimeError):
    """계정 제한 등으로 다른 계정으로 다시 시도해야 함."""


@dataclass
class Slot:
    """발행 슬롯 한 건."""

    manuscript: Manuscript
    account: str
    cafe: str
    board: str
    head: str = ""
    scheduled_at: datetime | None = None
    revision_at: datetime | None = None
    workflow: str = "self"
    images: list[Path] = field(default_factory=list)


# --------------------------------------------------------------------
# 원본(시트) 선택과 적재
# --------------------------------------------------------------------
def _norm(name: str) -> str:
    return re.sub(r"\s+", "", name or "").casefold()


#: 로컬 각색 xlsx(자사 카페 일상 글) 원본 종류
XLSX_DAILY_KIND = "xlsx_daily"
#: 일상 글 풀로 쓰는 원본 종류 (xlsx 행을 먼저 쓴다)
DAILY_KINDS = (XLSX_DAILY_KIND, "daily_pool")


def xlsx_dir(rt: Runtime) -> Path | None:
    """각색 xlsx를 찾는 폴더. `sources.yaml: local_xlsx_dir`가 없으면 None."""
    raw = str(rt.sources_cfg.get("local_xlsx_dir") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else rt.settings.repo_root / path


def discovered_xlsx_entries(rt: Runtime) -> list[dict]:
    """`local_xlsx_dir`의 `*각색*.xlsx` 원본 항목 목록 (결정 3·7)."""
    from v2r.sources.local_files import discover_xlsx

    base = xlsx_dir(rt)
    if base is None:
        return []
    try:
        return discover_xlsx(base)
    except Exception:
        return []


def brand_sheet_entries(rt: Runtime, brand: str = "") -> list[dict]:
    """`sources.yaml: brand_sheets`의 브랜드 원고 시트 목록 (A~J 제휴 배치).

    항목 이름은 브랜드명 그대로다 (`pick_images`가 `m.source`로 브랜드를 찾는다).
    """
    out: list[dict] = []
    for name, cfg in (rt.sources_cfg.get("brand_sheets") or {}).items():
        if not isinstance(cfg, dict) or not cfg.get("spreadsheet_id"):
            continue
        if brand and _norm(brand) != _norm(name):
            continue
        out.append(
            {
                "name": str(name),
                "kind": "brand",
                "brand": str(name),
                "spreadsheet_id": str(cfg.get("spreadsheet_id")),
                "gid": cfg.get("gid", 0),
                "sheet": str(cfg.get("sheet") or ""),
                # 브랜드 시트는 전부 문자열 열이라 gviz의 머리글 추측이 통째로
                # 어긋난다 (sheets.gviz_csv_url 주석) → 항상 머리글 1줄로 못박는다.
                "headers": cfg.get("headers", 0),
            }
        )
    return out


def _sheet_entries(rt: Runtime) -> list[dict]:
    """원고 원본 목록: sources.yaml의 시트/브랜드시트/일상풀/xlsx + 폴더에서 찾은 xlsx."""
    entries = [
        e
        for e in (rt.sources_cfg.get("sources") or [])
        if isinstance(e, dict)
        and (e.get("kind") or "sheet") in ("sheet", "brand", XLSX_DAILY_KIND, "daily_pool")
    ]
    names = {_norm(e.get("name", "")) for e in entries}
    for entry in brand_sheet_entries(rt) + discovered_xlsx_entries(rt):
        if _norm(entry.get("name", "")) not in names:
            names.add(_norm(entry.get("name", "")))
            entries.append(entry)
    return entries


def _entry_by_name(rt: Runtime, name: str) -> dict | None:
    key = _norm(name)
    if not key:
        return None
    for entry in _sheet_entries(rt):
        cand = _norm(entry.get("name", ""))
        if cand == key or key in cand or cand in key:
            return entry
    return None


def select_source_entries(rt: Runtime, spec: TaskSpec) -> list[dict]:
    """명령과 작업 종류로 사용할 시트 목록을 정한다."""
    if spec.source:
        entry = _entry_by_name(rt, spec.source)
        if entry is None:
            raise PublishError(f"원본 시트를 찾지 못했습니다: {spec.source}")
        return [entry]

    entries = _sheet_entries(rt)
    if spec.task == "publish_daily":
        # 일상 글 원본: 인박스 각색 xlsx(먼저) + 생성한 짧은 일상 글 풀
        daily = [e for e in entries if (e.get("kind") or "sheet") in DAILY_KINDS]
        # 각색 xlsx 먼저(파일 이름 오름차순 = 오래된 것부터), 그다음 일상 글 풀
        daily.sort(
            key=lambda e: (
                0 if (e.get("kind") or "") == XLSX_DAILY_KIND else 1,
                str(e.get("name") or ""),
            )
        )
        if daily:
            return daily
        for wanted in ("일상글목록", DAILY_POOL_SOURCE):
            entry = _entry_by_name(rt, wanted)
            if entry is not None:
                return [entry]
        return entries[:1]
    if spec.task == "publish_brand":
        # 브랜드 시트가 있으면 그것만 쓴다 (spec.brand가 있으면 그 브랜드만)
        brand_entries = brand_sheet_entries(rt, spec.brand)
        if brand_entries:
            return brand_entries
        picked = [
            e
            for e in entries
            if _norm(e.get("name", "")) != _norm(DAILY_POOL_SOURCE)
            and (e.get("kind") or "sheet") not in DAILY_KINDS
            and ("각색" in (e.get("name") or "") or "제휴" in (e.get("name") or ""))
        ]
        return picked or [
            e
            for e in entries
            if _norm(e.get("name", "")) != _norm(DAILY_POOL_SOURCE)
            and (e.get("kind") or "sheet") not in DAILY_KINDS
        ]
    if spec.task == "publish_info":
        entry = _entry_by_name(rt, "정보성")
        if entry is None:
            raise PublishError("정보성 글 시트가 sources.yaml에 없습니다")
        return [entry]
    # publish_batch — 일상 글 풀은 제휴 일상 글 전용이라 일괄 발행에서 제외한다
    return [e for e in entries if (e.get("kind") or "sheet") != "daily_pool"]


def _cache_hooks(rt: Runtime):
    def cache_get(key: str) -> list[dict] | None:
        payload = rt.sources_cache.get(key)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        return rows if isinstance(rows, list) else None

    def cache_put(key: str, rows: list[dict]) -> None:
        rt.sources_cache.put(key, {"rows": list(rows or [])})

    return cache_get, cache_put


def _parse_rows(rows: list[dict], name: str, cafes_cfg: dict) -> list[Manuscript]:
    """시트 형태를 보고 알맞은 파서를 고른다."""
    if not rows:
        return []
    have = {sheets._norm_key(k) for k in rows[0].keys()}
    if {sheets._norm_key("각색제목"), sheets._norm_key("각색본문")} <= have:
        return sheets.parse_adapted_rows(rows, cafes_cfg, source=name)
    if sheets._norm_key("완료링크") in have or sheets._norm_key("키워드") in have:
        return sheets.parse_affiliate_rows(rows, source=name)
    return sheets.parse_daily_rows(rows, source=name)


def _expand_article(m: Manuscript) -> Manuscript:
    """B열 본문이 `제목 :` / `본문 :` / `댓글N :` 블록이면 펼쳐 넣는다.

    브랜드 시트는 A열 키워드를 제목으로 쓰지만 실제 제목·댓글은 B열 안에 있다.
    """
    if "제목" not in (m.body or ""):
        return m
    from v2r.content.manuscript import content_hash, parse_article

    parsed = parse_article(m.body)
    if not parsed.title and not parsed.comments:
        return m
    m.title = parsed.title or m.title
    m.body = parsed.body or m.body
    m.comments = parsed.comments
    m.content_hash = content_hash(m.title, m.body)
    return m


def load_manuscripts(rt: Runtime, entry: dict, prefer_cache: bool = False) -> list[Manuscript]:
    """원본 1건을 원고 목록으로. `prefer_cache`면 네트워크를 쓰지 않는다.

    `kind`: `sheet`(구글 시트) / `xlsx`(로컬 각색 엑셀) / `daily_pool`(창고 일상 글 풀).
    """
    name = str(entry.get("name") or "")
    kind = entry.get("kind") or "sheet"
    if kind == "daily_pool":
        from v2r.sources.daily_pool import load_pool

        return load_pool(rt.settings.warehouse_dir)
    if kind == XLSX_DAILY_KIND:
        from v2r.sources.local_files import parse_xlsx_entry

        cache: dict = rt.scratch.setdefault("xlsx_cache", {})
        path = str(entry.get("path") or "")
        if path not in cache:
            cache[path] = parse_xlsx_entry(entry, rt.cafes_cfg)
        return list(cache[path])
    cache_get, cache_put = _cache_hooks(rt)
    rows = sheets.load_source(
        entry, cache_get=cache_get, cache_put=cache_put, prefer_cache=prefer_cache
    )
    if kind == "brand":
        # 브랜드 시트는 A~J 제휴 배치 고정. 완료 링크가 있는 행은 파서가 건너뛴다.
        items = sheets.parse_affiliate_rows(rows, source=name)
        return [_expand_article(m) for m in items]
    return _parse_rows(rows, name, rt.cafes_cfg)


def refresh_source(rt: Runtime, entry: dict) -> int:
    """원본 1건을 새로 읽어 캐시에 저장. 행 수 반환."""
    kind = entry.get("kind") or "sheet"
    if kind == "daily_pool":
        from v2r.sources.daily_pool import load_pool

        return len(load_pool(rt.settings.warehouse_dir))
    if kind == XLSX_DAILY_KIND:
        from v2r.sources.local_files import load_xlsx_rows

        rows = load_xlsx_rows(entry.get("path") or "")
        _cache_hooks(rt)[1](str(entry.get("name") or ""), rows)
        return len(rows)
    cache_get, cache_put = _cache_hooks(rt)
    rows = sheets.load_source(entry, cache_get=cache_get, cache_put=cache_put)
    return len(rows)


def _done_history(rt: Runtime, source_key: str) -> list[Manuscript]:
    """이미 발행된(done) 원고 목록을 캐시에서 되살린다."""
    rows = rt.conn.execute(
        "SELECT row_number FROM publications WHERE source_key = ? AND status = 'done'",
        (source_key,),
    ).fetchall()
    if not rows:
        return []
    payload = rt.sources_cache.get(source_key)
    cached = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(cached, list):
        return []
    by_row = {m.source_row: m for m in _parse_rows(cached, source_key, rt.cafes_cfg)}
    out: list[Manuscript] = []
    for r in rows:
        m = by_row.get(int(r["row_number"]))
        if m is not None:
            out.append(m)
    return out


def self_cafe_names(rt: Runtime, include_excluded: bool = False) -> list[str]:
    """자사 카페(`self_owned`) 중 `excluded`가 아닌 카페 이름 목록 (규칙 §1)."""
    out: list[str] = []
    for entry in (rt.cafes_cfg or {}).get("self_owned") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if name and (include_excluded or not entry.get("excluded")) and name not in out:
            out.append(name)
    return out


def brand_source_keys(rt: Runtime) -> set[str]:
    """브랜드 원고 시트 이름 집합(= 일상 글이 아닌 `source_key`).

    `sources.yaml: brand_sheets` 키 + `config/brands.yaml: brands` 키.
    오늘 올린 **일상 글**만 세기 위해 쓴다 (규칙 §7).
    """
    out = {str(name) for name in (rt.sources_cfg.get("brand_sheets") or {})}
    try:
        from v2r.warehouse.store import load_brands_config

        out |= {str(name) for name in (load_brands_config().get("brands") or {})}
    except Exception:  # 설정이 없어도 세는 일은 계속한다
        pass
    return out


def count_today_for_cafe(rt: Runtime, cafe: str, kst_date: str = "") -> int:
    """오늘(KST) 그 카페에 이미 올라간 **일상 글** 수 (규칙 §7)."""
    date = kst_date or datetime.now(KST).date().isoformat()
    return rt.publications.count_today(cafe, date, exclude_sources=brand_source_keys(rt))


def prepare_per_cafe(
    rt: Runtime, spec: TaskSpec, skipped: list[dict] | None = None
) -> list[Manuscript]:
    """`카페별 N건` — 자사 카페마다 **오늘 N건이 되게** 모자란 만큼 고른다 (규칙 §1~§3, §7).

    `spec.count`는 "오늘 이 카페의 일상 글 총량"이다. 오늘 이미 올린 만큼을 빼고
    (`목표 = max(0, N - 오늘 올린 수)`) 고른다. `spec.per_cafe_mode == "추가로"`면
    예전처럼 오늘 올린 수와 무관하게 N건을 더 고른다.

    **순서 절대 규칙(사용자 지시)**: 각색 xlsx는 카페가 행마다 번갈아 오도록 일부러
    그렇게 짜 둔 것이다. 그러니 파일 이름 오름차순(오래된 날짜 먼저) → 그 파일의
    위 행부터 아래로, **카페를 가로질러 한 줄로** 고른다. 카페별로 묶거나 라운드로빈으로
    다시 섞지 않는다. xlsx 행을 다 쓴 뒤에야 일상 글 풀을 풀 순서대로 이어 쓴다.

    건너뛰는 행(이미 발행/중복/제외 카페/대상 아님/그 카페는 목표 달성)은 그냥 넘어가고
    **나머지 행의 순서는 그대로** 이어진다. 이미 발행한 (파일, 행)뿐 아니라
    **본문 해시 전역 검사**로도 건너뛴다.

    `rt.scratch["per_cafe_sequence"]`에 `순서/파일/행/카페/제목`을 남겨 보고서가
    사용자에게 실제 발행 순서를 그대로 보여줄 수 있게 한다.
    """
    skipped = skipped if skipped is not None else []
    targets = [spec.cafe] if spec.cafe else self_cafe_names(rt)
    targets = [c for c in targets if not is_excluded_cafe(rt, c)]
    if not targets:
        raise PublishError("발행할 자사 카페가 없습니다 (cafes.yaml self_owned 확인)")

    loaded: list[tuple[str, list[Manuscript]]] = []
    attempted = failed = 0
    for entry in select_source_entries(rt, spec):
        name = str(entry.get("name") or "")
        attempted += 1
        try:
            items = load_manuscripts(rt, entry, prefer_cache=bool(spec.dry_run))
        except Exception as exc:
            failed += 1
            skipped.append({"source": name, "reason": f"원본 적재 실패: {exc}"})
            continue
        loaded.append((name, items))
    rt.scratch["source_load"] = {"attempted": attempted, "failed": failed}

    requested = int(spec.count or 0)
    add_mode = str(getattr(spec, "per_cafe_mode", "") or "") == "추가로"
    today = datetime.now(KST).date().isoformat()
    # 카페마다 목표를 먼저 정한다: 오늘 이미 올린 일상 글을 뺀 나머지 (규칙 §7)
    targets_left: dict[str, int] = {}
    report: dict[str, dict[str, int]] = {}
    for cafe in targets:
        already = 0 if add_mode else count_today_for_cafe(rt, cafe, today)
        want_cafe = requested if (add_mode or not requested) else max(0, requested - already)
        targets_left[cafe] = want_cafe
        report[cafe] = {"requested": requested, "already": already, "planned": 0}
    rt.scratch["per_cafe_plan"] = report

    used_keys: set[tuple[str, int]] = set()
    used_hashes: set[str] = set()
    picked: list[Manuscript] = []
    sequence: list[dict] = []
    rt.scratch["per_cafe_sequence"] = sequence

    def reached(cafe: str) -> bool:
        """그 카페가 오늘 목표를 채웠는가 (`requested`가 0이면 상한 없음)."""
        return bool(requested) and targets_left.get(cafe, 0) <= 0

    def open_cafe() -> str:
        """카페가 비어 있는 원고(일상 글 풀)를 줄 카페 — 아직 덜 채운 곳부터."""
        left = [c for c in targets if not reached(c)]
        if not left:
            return ""
        return min(left, key=lambda c: (report[c]["planned"], targets.index(c)))

    # 이미 목표를 채운 카페는 먼저 알려 둔다 (규칙 §7)
    for cafe in targets:
        if reached(cafe):
            skipped.append(
                {"source": cafe, "reason": f"오늘 이미 {report[cafe]['already']}건 — 목표 달성"}
            )

    # 파일 순서 → 행 순서 그대로 한 줄로 훑는다 (카페별로 묶지 않는다)
    for name, items in loaded:
        if all(reached(c) for c in targets):
            break
        for m in items:
            if all(reached(c) for c in targets):
                break
            if (m.source, m.source_row) in used_keys:
                continue
            if m.cafe:
                # 원고에 카페가 적혀 있으면 그 카페 글로만 쓴다
                if is_excluded_cafe(rt, m.cafe):
                    skipped.append(
                        {"source": name, "row": m.source_row, "reason": "발행 제외 카페"}
                    )
                    continue
                cafe = next((c for c in targets if cafe_matches(c, m.cafe)), "")
                if not cafe:
                    skipped.append(
                        {
                            "source": name,
                            "row": m.source_row,
                            "reason": f"대상 카페 아님({m.cafe})",
                        }
                    )
                    continue
                if reached(cafe):
                    # 그 카페만 목표를 채웠다 → 이 행만 건너뛰고 순서는 이어간다
                    skipped.append(
                        {"source": name, "row": m.source_row, "reason": f"{cafe} 목표 달성"}
                    )
                    continue
            else:
                cafe = open_cafe()
                if not cafe:
                    break
            if m.content_hash and m.content_hash in used_hashes:
                skipped.append(
                    {"source": name, "row": m.source_row, "reason": "중복(본문 해시)"}
                )
                continue
            if rt.publications.exists(name, m.source_row, m.content_hash):
                skipped.append({"source": name, "row": m.source_row, "reason": "이미 발행됨"})
                continue
            if rt.publications.exists_hash(m.content_hash):
                skipped.append(
                    {"source": name, "row": m.source_row, "reason": "중복(본문 해시 전역)"}
                )
                continue
            # V2R 글 목록 색인 대조 (사용자 절대 규칙) — 서버에 이미 있는 글이면 건너뛴다
            dup, why = duplicate.is_duplicate_against_index(rt, m, m.cafe or cafe)
            if dup:
                skipped.append(
                    {
                        "source": name,
                        "row": m.source_row,
                        "reason": duplicate.index_skip_reason(why),
                    }
                )
                continue
            used_keys.add((m.source, m.source_row))
            if m.content_hash:
                used_hashes.add(m.content_hash)
            if not m.cafe:
                m.cafe = cafe
            picked.append(m)
            sequence.append(
                {
                    "seq": len(picked),
                    "source": name,
                    "row": m.source_row,
                    "cafe": cafe,
                    "title": m.title,
                }
            )
            report[cafe]["planned"] += 1
            if requested:
                targets_left[cafe] -= 1
    # 사용자 규칙(2026-09-20): 같은 카페가 몰린 구간이 있으면 섞는다.
    # 카페 안 순서(파일→행)는 그대로 두고, 카페끼리만 번갈아 나오게 재배열한다.
    picked, sequence = declump_by_cafe(picked, sequence)
    for i, entry in enumerate(sequence, start=1):
        entry["seq"] = i
    rt.scratch["per_cafe_sequence"] = sequence
    return picked


def declump_by_cafe(items: list, sequence: list[dict], max_run: int = MAX_SAME_CAFE_RUN) -> tuple[list, list[dict]]:
    """같은 카페가 `max_run`건 이상 연속되는 '몰림'만 푼다. 시트 순서는 그 밖에서 그대로.

    앞에서부터 훑다가 같은 카페가 `max_run`번째 연속될 자리가 오면, 뒤쪽에서 가장 가까운
    다른 카페 원고를 끌어와 끼운다(그 원고의 카페 내부 순서는 보존). 끌어올 게 없으면 허용.
    """
    n = len(items)
    if n != len(sequence) or n == 0:
        return items, sequence
    idx = list(range(n))
    out: list[int] = []
    run_cafe, run_len = None, 0
    while idx:
        cur = idx[0]
        cafe = str(sequence[cur].get("cafe") or "")
        if cafe == run_cafe and run_len >= max_run - 1:
            alt = next((j for j in idx if str(sequence[j].get("cafe") or "") != cafe), None)
            if alt is not None:
                idx.remove(alt)
                out.append(alt)
                run_cafe, run_len = str(sequence[alt].get("cafe") or ""), 1
                continue
        idx.pop(0)
        out.append(cur)
        if cafe == run_cafe:
            run_len += 1
        else:
            run_cafe, run_len = cafe, 1
    return [items[i] for i in out], [sequence[i] for i in out]


def prepare_manuscripts(
    rt: Runtime, spec: TaskSpec, skipped: list[dict] | None = None
) -> list[Manuscript]:
    """발행할 원고를 고른다. 이미 발행됐거나 중복인 행은 건너뛴다."""
    skipped = skipped if skipped is not None else []
    if getattr(spec, "per_cafe", False) and not spec.manuscripts:
        return prepare_per_cafe(rt, spec, skipped)
    if spec.cafe and is_excluded_cafe(rt, spec.cafe):
        entry = find_cafe_entry(spec.cafe, rt.cafes_cfg) or {}
        label = str(entry.get("name") or spec.cafe)
        raise PublishError(f"발행 제외 카페입니다: {label} (합류 지시 전까지 제외)")
    picked: list[Manuscript] = []
    # 명령이 카페를 지정하면 그 카페 원고를 먼저 쓴다(게시판이 카페와 맞아야 하므로).
    # 한 건도 없으면 other의 원고로 되돌아간다.
    other: list[Manuscript] = []
    want_cafe = spec.cafe or ""
    scanned = 0
    attempted = failed = 0

    if spec.manuscripts:
        for i, raw in enumerate(spec.manuscripts, start=1):
            data = dict(raw)
            data.setdefault("source", spec.source or "명령첨부")
            data.setdefault("source_row", i)
            m = Manuscript.model_validate(data)
            if not m.content_hash:
                from v2r.content.manuscript import content_hash as _h

                m.content_hash = _h(m.title, m.body)
            picked.append(m)
    else:
        for entry in select_source_entries(rt, spec):
            name = str(entry.get("name") or "")
            attempted += 1
            try:
                items = load_manuscripts(rt, entry, prefer_cache=bool(spec.dry_run))
            except Exception as exc:
                failed += 1
                skipped.append({"source": name, "reason": f"원본 적재 실패: {exc}"})
                continue
            # 미리 만들어 둔 엑셀 일상 글은 사용자가 이미 중복 정리함 → 중복 검사 생략
            skip_dup = str(entry.get("kind") or "") == "xlsx_daily"
            history = [] if skip_dup else _done_history(rt, name)
            seen_hashes = {h.content_hash for h in history if h.content_hash}
            for m in items:
                if spec.count and spec.count > 0 and len(picked) >= spec.count:
                    break  # 필요한 수만 고르면 중단 (수천 행 전수 비교 방지)
                if want_cafe and other and scanned >= CAFE_SCAN_LIMIT:
                    break  # 지정 카페 원고가 안 보인다 → 모아둔 다른 카페 원고를 쓴다
                scanned += 1
                if m.content_hash in seen_hashes:
                    skipped.append({"source": name, "row": m.source_row, "reason": "중복(완전일치)"})
                    continue
                if rt.publications.exists(name, m.source_row, m.content_hash):
                    skipped.append(
                        {"source": name, "row": m.source_row, "reason": "이미 발행됨"}
                    )
                    continue
                verdict = None if skip_dup else duplicate.check_against_history(m, history)
                if verdict:
                    reason = "중복(완전일치)" if verdict == "exact" else "중복(유사)"
                    if not spec.dry_run:  # 모의 실행은 DB를 바꾸지 않는다
                        rt.publications.mark(
                            name, m.source_row, m.content_hash, "skipped", stage=verdict
                        )
                    skipped.append({"source": name, "row": m.source_row, "reason": reason})
                    continue
                if not comment_mod.manuscript_type_matches(
                    spec.manuscript_type, m.manuscript_type
                ):
                    skipped.append(
                        {
                            "source": name,
                            "row": m.source_row,
                            "reason": f"원고유형 불일치({m.manuscript_type or '없음'})",
                        }
                    )
                    continue
                if is_excluded_cafe(rt, m.cafe):
                    skipped.append(
                        {"source": name, "row": m.source_row, "reason": "발행 제외 카페"}
                    )
                    continue
                # V2R 글 목록 색인 대조 (사용자 절대 규칙) — 서버에 이미 있는 글이면 건너뛴다
                dup, why = duplicate.is_duplicate_against_index(
                    rt, m, m.cafe or want_cafe
                )
                if dup:
                    skipped.append(
                        {
                            "source": name,
                            "row": m.source_row,
                            "reason": duplicate.index_skip_reason(why),
                        }
                    )
                    continue
                if want_cafe and not cafe_matches(want_cafe, m.cafe):
                    other.append(m)
                    continue
                picked.append(m)
                history.append(m)  # 같은 실행 안에서의 중복도 잡는다
                seen_hashes.add(m.content_hash)
            if spec.count and spec.count > 0 and len(picked) >= spec.count:
                break
            if want_cafe and other and scanned >= CAFE_SCAN_LIMIT:
                break

    if not picked and other:
        picked = other  # 지정 카페 원고가 없으면 아무 원고나 쓴다(게시판은 카페 기본값)

    rt.scratch["source_load"] = {"attempted": attempted, "failed": failed}
    if spec.count and spec.count > 0:
        picked = picked[: spec.count]
    return picked


# --------------------------------------------------------------------
# 계정
# --------------------------------------------------------------------
def load_accounts(rt: Runtime, prefer_cache: bool = False) -> list[Account]:
    """계정 시트에서 계정 목록을 읽는다(실패 시 빈 목록)."""
    entry = None
    for e in rt.sources_cfg.get("sources") or []:
        if isinstance(e, dict) and e.get("kind") == "accounts":
            entry = e
            break
    if entry is None and rt.accounts_cfg.get("spreadsheet_id"):
        entry = {
            "name": "계정시트",
            "spreadsheet_id": rt.accounts_cfg.get("spreadsheet_id"),
            "gid": rt.accounts_cfg.get("gid", 0),
        }
    if entry is None:
        return []
    cache_get, cache_put = _cache_hooks(rt)
    try:
        rows = sheets.load_source(
            entry, cache_get=cache_get, cache_put=cache_put, prefer_cache=prefer_cache
        )
    except Exception:
        return []
    accounts, _stats = load_from_rows(rows)
    return accounts


def comment_pool(rt: Runtime, workflow: str) -> list[str]:
    """댓글 전용 계정 목록."""
    pools = (rt.cafes_cfg.get("comment_accounts") or {})
    return list(pools.get(workflow) or [])


def all_comment_accounts(rt: Runtime) -> set[str]:
    """모든 댓글 전용 계정(본문 작성에서 제외)."""
    pools = (rt.cafes_cfg.get("comment_accounts") or {})
    out: set[str] = set()
    for group in pools.values():
        out.update(group or [])
    return out


def restricted_accounts(rt: Runtime) -> set[str]:
    """지금 제한 중인 계정 집합."""
    return {
        login
        for login in rt.account_state.last_used_map()
        if rt.account_state.is_restricted(login)
    }


# --------------------------------------------------------------------
# 카페·게시판 해석
# --------------------------------------------------------------------
def _test_cafes(rt: Runtime) -> set[str]:
    return {_norm(k) for k in (rt.cafes_cfg.get("test") or {})}


def cafe_matches(query: str, name: str) -> bool:
    """`catalog.match_name`과 같은 규칙으로 카페 이름 두 개가 같은 카페인지 본다.

    정규화(이모지·공백·기호 제거) 완전일치 → 한글만 완전일치 → 한쪽이 다른 쪽에
    포함(줄임말) 순. 예: `태극` ↔ `태극마케팅센터`, `러브 인썸` ↔ `러브 인썸 (Love in Some)`.
    """
    from v2r.api.catalog import korean_only, normalize_name

    q, n = normalize_name(query), normalize_name(name)
    if not q or not n:
        return False
    if q == n:
        return True
    kq, kn = korean_only(query), korean_only(name)
    if kq and kq == kn:
        return True
    return q in n or n in q


def is_excluded_cafe(rt: Runtime, name: str) -> bool:
    """`cafes.yaml`에서 `excluded: true`로 표시한 카페인가.

    사용자 지시로 발행에서 빼 둔 카페다(합류 지시 전까지). 이름 비교는
    `cafe_matches`와 같은 규칙(별칭·줄임말 허용)을 쓴다.
    """
    if not name:
        return False
    for entry in cafe_entries(rt.cafes_cfg):
        if not entry.get("excluded"):
            continue
        names = [str(entry.get("name") or "")] + [
            str(a) for a in (entry.get("aliases") or [])
        ]
        if any(cafe_matches(name, n) for n in names if n):
            return True
    return False


def excluded_cafe_names(rt: Runtime) -> list[str]:
    """발행 제외 카페 이름 목록."""
    return [
        str(e.get("name") or "")
        for e in cafe_entries(rt.cafes_cfg)
        if e.get("excluded") and e.get("name")
    ]


def cafe_entries(cafes_cfg: dict) -> list[dict]:
    """설정에 있는 모든 카페 항목(제휴 + 자사 + 테스트)을 같은 모양으로."""
    out: list[dict] = []
    for group in ("affiliate", "self_owned"):
        for entry in (cafes_cfg or {}).get(group) or []:
            if isinstance(entry, dict) and entry.get("name"):
                out.append(dict(entry))
    for name, value in ((cafes_cfg or {}).get("test") or {}).items():
        entry = dict(value) if isinstance(value, dict) else {"cafe_id": value}
        entry.setdefault("name", str(name))
        out.append(entry)
    return out


def find_cafe_entry(cafe: str, cafes_cfg: dict) -> dict | None:
    """카페 이름/별칭으로 설정 항목 1건을 찾는다(정규화·줄임말 허용)."""
    if not cafe:
        return None
    for entry in cafe_entries(cafes_cfg):
        names = [str(entry.get("name") or "")] + [
            str(a) for a in (entry.get("aliases") or [])
        ]
        if any(cafe_matches(cafe, n) for n in names):
            return entry
    return None


def canonical_board(cafes_cfg: dict, cafe: str, board: str) -> str:
    """게시판 별칭(`board_aliases`)이면 그 카페의 정식 게시판 이름으로 바꾼다."""
    entry = find_cafe_entry(cafe, cafes_cfg)
    if not entry or not entry.get("board"):
        return board
    aliases = [str(a) for a in (entry.get("board_aliases") or [])]
    from v2r.api.catalog import normalize_name

    key = normalize_name(board)
    if key and any(normalize_name(a) == key for a in aliases):
        return str(entry["board"])
    return board


def cafe_default_board(cafes_cfg: dict, cafe: str) -> str:
    """카페별 기본 게시판(`cafes.yaml`). 없으면 빈 문자열."""
    entry = find_cafe_entry(cafe, cafes_cfg)
    return str((entry or {}).get("board") or "")


def resolve_cafe(rt: Runtime, m: Manuscript, spec: TaskSpec) -> str:
    """명령에 카페가 있으면 명령 우선, 없으면 원고 → 기본값."""
    return spec.cafe or m.cafe or DEFAULT_CAFE


def resolve_board(rt: Runtime, m: Manuscript, spec: TaskSpec, cafe: str) -> str:
    """명령 게시판 → (카페가 맞는) 원고 게시판 → 제휴 지정 → 카페별 기본 → 전역 기본.

    명령이 카페를 지정하면 원고 게시판은 그 카페 원고일 때만 쓴다. 다른 카페의
    게시판 이름은 이 카페에 없기 때문이다(전역 기본 `자유게시판`도 대부분 없다).
    """
    if spec.board:
        return spec.board
    if m.board and (not spec.cafe or (m.cafe and cafe_matches(cafe, m.cafe))):
        return canonical_board(rt.cafes_cfg, cafe, m.board)
    entry = find_affiliate(cafe, rt.cafes_cfg)
    if entry and entry.get("board"):
        return str(entry["board"])
    own = cafe_default_board(rt.cafes_cfg, cafe)
    if own:
        return own
    return str(rt.cafes_cfg.get("default_board") or "자유게시판")


def is_test_cafe(rt: Runtime, cafe: str) -> bool:
    """테스트 카페(즉시 발행)인가."""
    return _norm(cafe) in _test_cafes(rt)


# --------------------------------------------------------------------
# 이미지
# --------------------------------------------------------------------
def used_variants(rt: Runtime) -> set[str]:
    """이미 쓴 세탁본 변형 집합."""
    rows = rt.conn.execute("SELECT variant FROM photo_usage").fetchall()
    return {str(r["variant"]) for r in rows}


def record_variant_use(rt: Runtime, sha: str, variant: Path, source_id: str) -> None:
    """세탁본 사용 기록."""
    from v2r.store.db import now_iso

    ts = now_iso()
    rt.conn.execute(
        "INSERT INTO photo_usage (sha256, variant, used_in_source_id, used_at, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(sha256, variant) DO UPDATE SET used_in_source_id = excluded.used_in_source_id,"
        " used_at = excluded.used_at, updated_at = excluded.updated_at",
        (sha, str(variant), source_id, ts, ts, ts),
    )


def placeholder_tokens(body: str) -> list[str]:
    """본문의 `{...}` 토큰 목록(등장 순서, 중괄호 제외)."""
    return [
        raw[1:-1].strip()
        for raw in seone.PLACEHOLDER.findall((body or "").replace("\r\n", "\n"))
    ]


def _match_by_filename(originals: list[Path], keyword: str) -> list[Path]:
    """파일 stem(공백제거·casefold)이 키워드와 같은 원본만 (팥순이 규칙)."""
    key = re.sub(r"\s+", "", keyword or "").casefold()
    if not key:
        return []
    return [p for p in originals if re.sub(r"\s+", "", p.stem).casefold() == key]


def notify_photo_shortage(
    rt: Runtime, spec: TaskSpec, brand: str, folder: str, need: int, have: int
) -> bool:
    """사진이 모자랄 때 "생성할까요?" 안내를 보낸다 (자동 생성하지 않는다).

    사용자 규칙(2026-09-19): 사진은 폴더에 있는 걸 먼저 쓰고, 모자랄 때만 생성하되
    **생성 전에 먼저 물어본다.** 같은 브랜드/폴더는 한 실행에서 한 번만 알린다.
    모의 실행에서는 알리지 않는다.
    """
    if getattr(spec, "dry_run", True):
        return False
    seen = rt.scratch.setdefault("photo_shortage_notified", set())
    key = (str(brand), str(folder))
    if key in seen:
        return False
    seen.add(key)
    from v2r.channels import notify_all

    command = f"사진 생성 승인 {brand} {folder} {max(int(need or 1), 1)}장"
    notify_all(
        rt.channels,
        f"사진 부족: 브랜드 {brand} / {folder} 폴더 — 필요 {need}장, 사용 가능 {have}장."
        f" 생성하려면 '{command}' 이라고 보내세요.",
    )
    return True


def pick_images(rt: Runtime, m: Manuscript, spec: TaskSpec, need: int) -> list[Path]:
    """플레이스홀더 토큰별로 키워드/토큰 폴더에서 미사용 세탁본을 고른다.

    결정 1(2026-09-19): 브랜드 폴더 바로 아래 사진은 직접 쓰지 않는다.
    폴더가 비어 있으면 `ensure_keyword_pool`이 브랜드 루트/인박스 원본으로 채운다.
    원본이 하나도 없으면 `NoPhotoError`가 그대로 올라간다(텔레그램 알림용).
    """
    if need <= 0:
        return []
    from v2r.warehouse import store as wh_store

    brand = spec.brand or m.source or ""
    if not brand:
        raise PublishError("사진을 고를 브랜드를 알 수 없습니다")
    wh = rt.warehouse
    cfg = wh_store.load_brands_config()
    tokens = placeholder_tokens(m.body)[:need]
    used = used_variants(rt)
    out: list[Path] = []

    for token in tokens:
        originals = wh.ensure_keyword_pool(
            brand, m.keyword or token, min_variants=1, token=token, cfg=cfg
        )
        if wh_store.token_select_mode(brand, token, m.keyword, cfg) == "filename_match":
            matched = _match_by_filename(originals, m.keyword or token)
            originals = matched or originals
        picked: Path | None = None
        for original in originals:
            sha = wh.sha256(original)
            if not wh.washed_variants(sha):
                wh.ensure_keyword_pool(
                    brand, m.keyword or token, min_variants=1, token=token, cfg=cfg
                )
            variant = wh.pick_variant(sha, used)
            if variant is None:
                continue
            used.add(str(variant))
            rt.scratch.setdefault("variant_sha", {})[str(variant)] = sha
            picked = variant
            break
        if picked is None:
            folder = wh.keyword_folder(brand, m.keyword or token, cfg=cfg, token=token)
            notify_photo_shortage(rt, spec, brand, folder.name, need, len(out))
            raise PublishError(
                f"사진이 부족합니다 (브랜드 {brand} '{token}' 폴더 {folder.name}의"
                " 미사용 세탁본 없음)"
            )
        out.append(picked)

    if len(out) < need:
        folder = wh.keyword_folder(brand, m.keyword or "", cfg=cfg)
        notify_photo_shortage(rt, spec, brand, folder.name, need, len(out))
        raise PublishError(f"사진이 부족합니다 (필요 {need}장, 사용 가능 {len(out)}장)")
    return out


# --------------------------------------------------------------------
# 계획
# --------------------------------------------------------------------
def _cafe_members(rt: Runtime, cafe_name: str) -> tuple[Any, list[str]]:
    """카페 객체와 가입 계정 login_id 목록(탈퇴·활동중지 제외). 실행당 1회만 조회."""
    from v2r.api.catalog import match_name

    cache: dict = rt.scratch.setdefault("cafe_members", {})
    key = _norm(cafe_name)
    if key not in cache:
        cafe = match_name(cafe_name, rt.catalog.cafes(), key=lambda c: c.name)
        accounts = list(rt.catalog.cafe_accounts(cafe.cafe_id))
        if is_self_cafe(rt, cafe_name):
            # 자사 카페 계정은 카페 등급이 '스탭'인 것만 쓴다 (V2R 회원 조회의 등급 이름으로 확인)
            accounts = [ca for ca in accounts if is_staff_level(ca.level_name)]
        members = [ca.login_id for ca in accounts]
        cache[key] = (cafe, members)
    return cache[key]


def is_self_cafe(rt: Runtime, cafe_name: str) -> bool:
    """설정 `self_owned`에 있는 카페인지(제외 표시 포함)."""
    return any(cafe_matches(cafe_name, n) for n in self_cafe_names(rt, include_excluded=True))


def writable_logins(
    rt: Runtime, cafe_name: str, board: str, candidates: list[str]
) -> set[str]:
    """`cafe_name`의 `board`에 글을 쓸 수 있는 계정(casefold) 집합.

    카페 가입 계정 ∩ `candidates` 로 조회 범위를 좁힌 뒤 게시판 권한을 본다.
    결과는 실행 중 재사용한다(`rt.scratch`).
    """
    from v2r.api.catalog import match_name

    cafe, members = _cafe_members(rt, cafe_name)
    wanted = {c.casefold() for c in candidates}
    logins = [m for m in members if m.casefold() in wanted] if candidates else list(members)
    cache: dict = rt.scratch.setdefault("writable_logins", {})
    key = (_norm(cafe_name), _norm(board), tuple(sorted(logins)))
    if key not in cache:
        menus = rt.catalog.menus(cafe.cafe_id, logins) if logins else []
        if not menus:
            cache[key] = set()
        else:
            menu = match_name(board, menus, key=lambda x: x.name)
            cache[key] = {a.casefold() for a in menu.writable_accounts}
    return cache[key]


def _pool_for_cafe(
    rt: Runtime, spec: TaskSpec, cafe_name: str, board: str, pool: list[Account]
) -> tuple[list[Account], bool]:
    """계정 풀을 '그 카페에 가입 + 그 게시판 쓰기 가능'으로 좁힌다.

    반환: `(좁힌 풀, 보류 여부)`. 모의 실행에서 카탈로그를 아직 안 받았으면
    네트워크를 쓰지 않고 `(원래 풀, True)`를 돌려준다(계정은 실행 시 결정).
    """
    ids = [a.login_id for a in pool]
    members_cache = rt.scratch.get("cafe_members") or {}
    if spec.dry_run and _norm(cafe_name) not in members_cache:
        return pool, True  # 모의 실행은 네트워크를 쓰지 않는다 (계정은 실행 시 결정)
    try:
        allowed = writable_logins(rt, cafe_name, board, ids)
    except Exception as exc:
        if spec.dry_run:
            return pool, True
        raise AssignError(
            f"'{cafe_name}' 카페의 '{board}' 게시판 권한을 확인하지 못했습니다: {exc}"
        ) from exc
    narrowed = [a for a in pool if a.login_id.casefold() in allowed]
    if not narrowed:
        raise AssignError(
            f"'{cafe_name}' 카페의 '{board}' 게시판에 글을 쓸 수 있는 계정이 없습니다"
            " (카페 가입 여부와 게시판 쓰기 권한을 확인하세요)"
        )
    return narrowed, False


#: 자사 카페 글 허용 시간대(KST): 08:00 ~ 다음날 02:00. 그 밖에는 발행·예약 모두 금지(사용자 규칙 2026-09-19).
SELF_WINDOW_OPEN_HOUR = 8
SELF_WINDOW_CLOSE_HOUR = 2


def in_self_window(at: datetime) -> bool:
    """`at`(KST)이 자사 카페 허용 시간대(08:00~02:00) 안인가."""
    hour = at.astimezone(KST).hour
    return hour >= SELF_WINDOW_OPEN_HOUR or hour < SELF_WINDOW_CLOSE_HOUR


def next_self_window(at: datetime) -> datetime:
    """허용 시간대 밖이면 다음 08:00(KST), 안이면 그대로."""
    local = at.astimezone(KST)
    if in_self_window(local):
        return at
    return local.replace(hour=SELF_WINDOW_OPEN_HOUR, minute=0, second=0, microsecond=0)


#: 자사 카페 일상 글에서 카페마다 고정해 두는 계정 수 (규칙 §4).
#: 사용자 규칙: **정확히 10개를 무작위로** 골라 고정한다 (풀이 10개 미만이면 있는 만큼).
SELF_DAILY_ACCOUNTS_MIN = 10
SELF_DAILY_ACCOUNTS_MAX = 10


def self_daily_rng(rt: Runtime) -> random.Random:
    """실행 1회분 난수기 (같은 실행 안에서는 같은 흐름, 실행마다 새로 뽑는다)."""
    rng = rt.scratch.get("self_daily_rng")
    if not isinstance(rng, random.Random):
        rng = random.Random()
        rt.scratch["self_daily_rng"] = rng
    return rng


def pick_self_daily_accounts(
    pool: list[Any], rng: random.Random | None = None, count: int = SELF_DAILY_ACCOUNTS_MAX
) -> list[str]:
    """자사 카페 일상 글에 고정해 둘 계정 — 풀에서 **무작위 10개** (규칙 §4).

    풀은 이미 '자사 카페' 작업 구분 + 카페 스탭 등급 + 그 게시판 쓰기 가능으로
    좁혀진 목록이다. 마지막 사용 시각(LRU) 순서를 쓰지 않고 그때그때 무작위로
    고른다 — 같은 계정 묶음이 매번 반복되지 않게 하려는 것이다.
    """
    ids = [a if isinstance(a, str) else a.login_id for a in pool]
    if not ids:
        raise AssignError("사용 가능한 계정이 없습니다 (계정 시트를 확인하세요)")
    rng = rng or random.Random()
    want = min(int(count or SELF_DAILY_ACCOUNTS_MAX), len(ids))
    return rng.sample(ids, want)


def _avoid_consecutive(
    cafes: list[str], assigned: dict[int, str], chosen_by_cafe: dict[str, list[str]]
) -> None:
    """같은 카페에서 같은 계정이 연속으로 쓰이지 않게 자리를 민다 (규칙 §4).

    계정 풀이 1개뿐이면 바꿀 수 없으므로 그대로 둔다.
    """
    previous: dict[str, str] = {}
    for i, cafe in enumerate(cafes):
        key = _norm(cafe)
        account = assigned.get(i)
        if not account:
            continue
        if previous.get(key, "").casefold() == account.casefold():
            pool = chosen_by_cafe.get(key) or []
            alternatives = [a for a in pool if a.casefold() != account.casefold()]
            if alternatives:
                account = alternatives[0]
                assigned[i] = account
        previous[key] = account


def plan(rt: Runtime, spec: TaskSpec, manuscripts: list[Manuscript]) -> list[Slot]:
    """원고 목록 → 슬롯 목록(계정·카페·시각·이미지 확정)."""
    if not manuscripts:
        return []

    cafes = [resolve_cafe(rt, m, spec) for m in manuscripts]
    boards = [resolve_board(rt, m, spec, c) for m, c in zip(manuscripts, cafes)]
    workflows = [
        "affiliate" if find_affiliate(c, rt.cafes_cfg) is not None else "self"
        for c in cafes
    ]

    # --- 계정 배정 (카페별 work_type을 지켜 나눠 배정) ---
    pool_all = load_accounts(rt, prefer_cache=bool(spec.dry_run))
    restricted = set(restricted_accounts(rt)) | set(rt.scratch.get("restricted_now") or set())
    comment_only = all_comment_accounts(rt)
    assigned: dict[int, str] = {}
    chosen_by_cafe: dict[str, list[str]] = {}
    need_assign = [i for i, m in enumerate(manuscripts) if not m.account]
    if need_assign:
        # 카페·게시판마다 쓸 수 있는 계정이 다르다 → (work_type, 카페, 게시판)으로 묶는다
        groups: dict[tuple[str, str, str], list[int]] = {}
        for i in need_assign:
            wt = work_type_for(spec.task, cafes[i], rt.cafes_cfg)
            groups.setdefault((wt, cafes[i], boards[i]), []).append(i)
        for (work_type, cafe_name, board), idxs in groups.items():
            pool = eligible(pool_all, work_type, comment_only, restricted)
            deferred = False
            if pool:
                pool, deferred = _pool_for_cafe(rt, spec, cafe_name, board, pool)
            if deferred:
                # 모의 실행: 카탈로그 없이 계정을 못 정한다 → 표시만 남긴다
                for i in idxs:
                    assigned[i] = DEFERRED_ACCOUNT
                continue
            need = spec.account_count or len(idxs)
            self_daily_pick = (
                getattr(spec, "per_cafe", False)
                and not spec.account_count
                and spec.account_mode != "manual"
            )
            if spec.account_mode == "manual":
                if not pool:
                    raise AssignError(
                        "계정 시트를 읽지 못해 지정 계정을 검증할 수 없습니다"
                    )
                pool_ids = [
                    a.login_id
                    for a in pool
                    if a.login_id.casefold() not in {r.casefold() for r in restricted}
                ]
                chosen = assign(pool_ids, mode="manual", explicit=spec.accounts)
            elif self_daily_pick:
                # 규칙 §4: 카페마다 **무작위 10개**를 골라 고정하고 돌려 쓴다.
                # 게시판이 달라도 같은 카페면 같은 10개를 쓴다 (게시판마다 새로 뽑으면
                # 카페 전체로는 20개가 넘어가므로, 실행 1회분은 카페 단위로 기억한다).
                fixed: dict[str, list[str]] = rt.scratch.setdefault("self_daily_fixed", {})
                key = _norm(cafe_name)
                if key not in fixed:
                    fixed[key] = pick_self_daily_accounts(pool, self_daily_rng(rt))
                pool_ids = {(a if isinstance(a, str) else a.login_id).casefold() for a in pool}
                chosen = [a for a in fixed[key] if a.casefold() in pool_ids]
                if not chosen:
                    # 고정 10개가 이 게시판에 못 쓰면 이 게시판만 따로 뽑는다
                    chosen = pick_self_daily_accounts(pool, self_daily_rng(rt))
            else:
                if not pool:
                    raise AssignError("사용 가능한 계정이 없습니다 (계정 시트를 확인하세요)")
                chosen = assign(
                    pool,
                    mode="auto",
                    count=min(need, len(pool)),
                    last_used=rt.account_state.last_used_map(),
                )
            # 순번은 카페 단위로 이어 간다 — 게시판마다 0부터 다시 세면 앞쪽 계정만 쓰게 된다
            offsets: dict[str, int] = rt.scratch.setdefault("account_rotation_offset", {})
            start = offsets.get(_norm(cafe_name), 0) if self_daily_pick else 0
            for position, i in enumerate(idxs):
                assigned[i] = rotate(chosen, start + position)
            if self_daily_pick:
                offsets[_norm(cafe_name)] = start + len(idxs)
            chosen_by_cafe.setdefault(_norm(cafe_name), []).extend(
                a for a in chosen if a not in chosen_by_cafe.get(_norm(cafe_name), [])
            )
        # 같은 카페에서 같은 계정을 연속으로 쓰지 않는다 (규칙 §4)
        _avoid_consecutive(cafes, assigned, chosen_by_cafe)

    # --- 시각 계획 ---
    immediate_flags = [spec.immediate or is_test_cafe(rt, c) for c in cafes]
    timed_idx = [i for i, imm in enumerate(immediate_flags) if not imm]
    # 자사 카페 일상 글(`카페별`)은 예약하지 않는다: 전부 즉시 발행이고, 글과 글
    # 사이 간격은 실행기가 쉬면서 만든다 (규칙 §4, worker._pace_slots)
    times: dict[int, datetime] = {}
    if timed_idx:
        start_date = datetime.strptime(spec.start_date, "%Y-%m-%d").date()
        planned = plan_slots(
            len(timed_idx),
            date=start_date,
            window_start=spec.window_start,
            window_end=spec.window_end,
            interval_min=spec.interval_min,
            interval_max=spec.interval_max,
            per_cafe=[cafes[i] for i in timed_idx],
        )
        times = {i: t for i, t in zip(timed_idx, planned)}

    slots: list[Slot] = []
    for i, m in enumerate(manuscripts):
        account = m.account or assigned.get(i, "")
        if not account:
            raise AssignError("계정을 배정하지 못했습니다")
        at = times.get(i)
        rev = None
        if workflows[i] == "affiliate" and at is not None:
            rev = revision_at(at, cafes[i], rt.cafes_cfg)
        elif workflows[i] == "affiliate":
            rev = revision_at(datetime.now(KST), cafes[i], rt.cafes_cfg)
        need = seone.count_placeholders(m.body) if m.images_enabled else 0
        images = pick_images(rt, m, spec, need) if need else []
        slots.append(
            Slot(
                manuscript=m,
                account=account,
                cafe=cafes[i],
                board=boards[i],
                head=m.head,
                scheduled_at=at,
                revision_at=rev,
                workflow=workflows[i],
                images=images,
            )
        )
    return slots


# --------------------------------------------------------------------
# 실행
# --------------------------------------------------------------------
def _comment_tree(m: Manuscript) -> list[dict]:
    """원고의 댓글을 12노드 기본 트리에 맞춘다."""
    if not m.comments:
        return []
    texts = {re.sub(r"\s+", "", c.label): (c.text or "") for c in m.comments}
    included: set[str] = set()
    tree: list[dict] = []
    for node in comment_mod.DEFAULT_TREE:
        label = node["label"]
        text = texts.get(label)
        if not text:
            continue
        parent = node.get("parent")
        if parent and parent not in included:
            continue
        included.add(label)
        tree.append({**node, "text": text})
    return tree


def build_comments(rt: Runtime, slot: Slot, root_start: datetime, cafe_id: Any) -> list[dict]:
    """댓글 API 페이로드. 댓글 원고가 없으면 빈 목록."""
    tree = _comment_tree(slot.manuscript)
    if not tree:
        return []
    pool = comment_pool(rt, slot.workflow)
    if not pool:
        return []
    items = comment_mod.assign_comment_accounts(
        tree,
        pool,
        slot.account,
        manuscript_type=getattr(slot.manuscript, "manuscript_type", ""),
    )
    items = comment_mod.schedule(items, root_start)
    items = comment_mod.resolve_conflicts(items)
    members: dict[str, dict] = {}
    try:
        for ca in rt.catalog.cafe_accounts(cafe_id):
            members[ca.login_id] = {"member_key": ca.member_key or "", "nick": ca.nick or ""}
    except Exception:
        members = {}
    return comment_mod.to_api_payload(items, members)


def build_daily_comments(
    rt: Runtime,
    slot: Slot,
    root_start: datetime,
    cafe_id: Any,
    *,
    job_id: int | None = None,
    rng: random.Random | None = None,
) -> list[dict]:
    """자사 카페 일상 글의 랜덤 댓글 페이로드 (루트 댓글만, 규칙 §5).

    실패하면 빈 목록을 돌려주고 경고만 남긴다 — 글은 그대로 발행된다.
    """
    from v2r.content import daily_comments as dc

    rng = rng or random.Random()
    count = dc.draw_count(rng)
    if count <= 0:
        return []

    def warn(message: str) -> None:
        try:
            rt.events.log(job_id, "warn", message)
        except Exception:  # 이벤트 기록 실패가 발행을 막지 않는다
            log.warning("%s", message)

    try:
        members_list = list(rt.catalog.cafe_accounts(cafe_id))
    except Exception as exc:
        warn(f"카페 회원 목록을 읽지 못해 댓글 0개로 발행합니다: {exc}")
        return []
    author = str(slot.account or "").casefold()
    # 일상 글 댓글은 시트의 '자사 댓글' 계정(브랜드 원고 댓글용)을 쓰지 않는다(사용자 규칙 2026-09-19).
    # 그 카페의 스탭 등급 회원 중 글쓴이를 뺀 계정에서 랜덤으로 고른다.
    pool = [
        ca
        for ca in members_list
        if str(ca.login_id).casefold() != author and is_staff_level(ca.level_name)
    ]
    if not pool:
        warn("글쓴이 말고 스탭 등급 댓글 계정이 이 카페에 없어 댓글 0개로 발행합니다")
        return []

    count = min(count, len(pool))
    chosen = rng.sample(pool, count)
    m = slot.manuscript
    texts = dc.generate_texts(rt.llm, m.title, m.body, count, on_warn=warn)
    if not texts:
        return []
    count = min(count, len(texts))
    times = dc.plan_times(root_start, count, rng)
    # 허용 시간대 밖(02:00~08:00)으로 떨어진 댓글은 다음 08:00 이후로 민다
    shifted: list[datetime] = []
    for at in times:
        if not in_self_window(at):
            at = next_self_window(at) + timedelta(minutes=rng.randint(5, 40))
        if shifted and at <= shifted[-1]:
            at = shifted[-1] + timedelta(minutes=1)
        shifted.append(at)
    times = shifted
    items = [
        {
            "label": f"일상댓글{i + 1}",
            "depth": 0,
            "parent": None,
            "role": "comment",
            "account": chosen[i].login_id,
            "text": texts[i],
            "start_at": times[i],
        }
        for i in range(count)
    ]
    members = {
        ca.login_id: {"member_key": ca.member_key or "", "nick": ca.nick or ""}
        for ca in members_list
    }
    return comment_mod.to_api_payload(items, members)


def mask_login(login: str) -> str:
    """계정을 앞 3글자만 남긴다 (기록·출력용)."""
    text = str(login or "")
    return (text[:3] + "…") if len(text) > 3 else text


def comment_role_rows(rt: Runtime, slot: Slot) -> list[dict]:
    """모의 실행용 댓글 역할 표: 라벨 → 작성 계정(가림) / reply_member(가림).

    실제 계정은 발행할 때 다시 뽑으므로 여기 값은 **역할 확인용 예시**다.
    보는 곳은 역할이다: 후기형이면 대대댓글2가 여분 계정, 대대대댓글2가 작성자다.
    """
    tree = _comment_tree(slot.manuscript)
    if not tree:
        return []
    pool = comment_pool(rt, slot.workflow)
    if not pool:
        return []
    items = comment_mod.assign_comment_accounts(
        tree,
        pool,
        slot.account,
        manuscript_type=getattr(slot.manuscript, "manuscript_type", ""),
    )
    by_label = {it["label"]: it for it in items}
    rows: list[dict] = []
    for it in items:
        parent = by_label.get(it.get("parent") or "")
        reply_member = (
            parent.get("account", "") if parent and int(it.get("depth", 0)) >= 2 else ""
        )
        account = str(it.get("account", ""))
        rows.append(
            {
                "label": it["label"],
                "account": mask_login(account),
                "reply_member": mask_login(reply_member),
                "is_author": account.casefold() == str(slot.account).casefold(),
            }
        )
    return rows


def _daily_pool(rt: Runtime) -> list[Manuscript]:
    """제휴 카페 일상 글 풀 — **`affiliate_daily_pool.jsonl`만** 읽는다.

    제휴 카페 일상 글은 자사 카페 xlsx 일상 글과 완전히 별개다(사용자 결정).
    ChatGPT 웹 세션으로 만든 `affiliate_daily_pool.jsonl`이 유일한 정규 출처다.
    풀이 비었을 때만 옛 `랜덤일상` 시트로 되돌아가고, 그때 경고를 남긴다.
    """
    pool = rt.scratch.get("daily_pool")
    if pool is None:
        from v2r.warehouse.daily_generator import AFFILIATE_POOL_FILENAME, load_pool

        try:
            pool = load_pool(rt.warehouse.root, AFFILIATE_POOL_FILENAME)
        except Exception as exc:
            log.warning("제휴 일상 글 풀 읽기 실패: %s", exc)
            pool = []
        if not pool:
            log.warning(
                "제휴 일상 글 풀(%s)이 비어 있어 옛 %s 시트로 되돌아갑니다."
                " ('제휴 일상 글 만들어줘'로 풀을 채우세요)",
                AFFILIATE_POOL_FILENAME,
                DAILY_POOL_SOURCE,
            )
            entry = _entry_by_name(rt, DAILY_POOL_SOURCE)
            pool = load_manuscripts(rt, entry) if entry else []
        rt.scratch["daily_pool"] = pool
    return pool


def _take_daily(rt: Runtime, cafe_name: str = "", dry_run: bool = True) -> Manuscript:
    """제휴 카페 일상 글 1건 — **미리 만들어 둔 풀(`affiliate_daily_pool.jsonl`)에서 꺼내 쓴다.**

    사용자 결정(2026-09-19 최종): 발행 때마다 GPT로 즉석 생성하지 않는다. 풀은
    `제휴 일상 글 N개 생성` 명령으로 미리 채워 두고(약 100개 단위), 발행 시 그 카페 글을 우선 고른다.
    같은 실행 안·이미 발행한 글은 다시 쓰지 않는다. 풀이 비면 실패로 알려 채우게 한다.
    """
    used: set = rt.scratch.setdefault("daily_used", set())
    pool = _daily_pool(rt)
    left = [
        m
        for m in pool
        if (m.source, m.source_row) not in used
        and not rt.publications.exists(m.source, m.source_row, m.content_hash)
    ]
    target = _norm(cafe_name) if cafe_name else ""
    same_cafe = [m for m in left if target and _norm(m.cafe or "") == target]
    candidates = same_cafe or left
    if not candidates:
        raise PublishError(
            "제휴 일상 글 풀이 비었습니다 ('제휴 일상 글 100개 생성'으로 채우세요)"
        )
    picked = random.choice(candidates)
    used.add((picked.source, picked.source_row))
    # 제휴 일상 글도 이모지 거름망을 지난다 (content_hash는 그대로 둔다 → 중복 판정 유지)
    spots = sanitize_mod.emoji_spots(picked)
    if spots:
        log.warning("이모지 제거: 제목/본문/댓글 %d곳 (제휴 일상 글)", len(spots))
        picked = sanitize_mod.sanitize_manuscript(picked)
    return picked


def _attach_images(rt: Runtime, slot: Slot, browser_page: Any = None) -> list[dict]:
    """이미지가 있으면 **업로드 API**로 올려 SE-ONE 이미지 컴포넌트를 얻는다.

    (2026-09-19) 브라우저 SE-ONE 붙여넣기는 V2R의 자동화 감지에 막혀 쓰지 않는다.
    `POST /naver_cafe_articles/upload_image` → S3 → 컴포넌트 (`v2r/api/images.py`).
    """
    if not slot.images:
        return []
    from v2r.api.images import upload_images

    return upload_images(rt.client, list(slot.images))


def _keyword_tags(m: Manuscript) -> list[str]:
    """제휴·브랜드 글의 태그: 키워드 1개(공백 제거)뿐이다 (legacy 태그 규칙).

    키워드는 A열 `키워드` → 비면 G열 `말머리`(브랜드 시트가 키워드를 여기 적는다)
    순으로 시트 파서가 채운다. 둘 다 없으면 태그 없이 발행한다(경고만).
    """
    from v2r.content.manuscript import tags_from_keyword

    return list(m.tags or []) or tags_from_keyword(m.keyword)


def _strip_placeholders(body: str, components: list[dict]) -> str:
    """사진 없이 발행하는 원고(이미지 없음=Y 등)는 `{…}` 자리표시를 지운다."""
    if not components:
        return seone.PLACEHOLDER.sub("", body)
    return body


def _prepare_content(body: str, components: list[dict]) -> tuple[str, str]:
    """'이미지 없음' 규칙을 적용한 본문과 SE-ONE content_json.

    사진 수와 자리표시 수가 안 맞으면 여기서 `ValueError`가 난다 → V2R에 글을
    만들기 전에 미리 돌려 보면(사전 점검) 되감기 없이 실패할 수 있다.
    """
    body = _strip_placeholders(body, components)
    return body, seone.content_json(body, components)


def _create_and_verify(
    rt: Runtime,
    *,
    title: str,
    tags: list[str],
    body: str,
    components: list[dict],
    content: str | None = None,
    cafe: Any,
    menu: Any,
    head: Any,
    login_id: str,
    start_at: datetime | None,
    comments: list[dict],
    parent_source_id: str | None = None,
    target_view_count: int = 0,
    parent_id: Any = None,
    on_created: Any = None,
    job_id: int | None = None,
) -> tuple[str, str | None]:
    """글 1건 등록 후 GET 재확인.

    반환: `(source_id, pending_reason)`. `pending_reason`이 있으면 글은 서버에 있고
    등록 확정만 못 본 상태(실패 아님).
    """
    # 사전 점검에서 이미 만들어 둔 content가 있으면 그대로 쓴다(두 번 만들지 않는다)
    if content is None:
        body, content = _prepare_content(body, components)
    else:
        body = _strip_placeholders(body, components)
    destination = api_articles.build_destination(
        cafe, menu, head, login_id, start_at, target_view_count, parent_id
    )
    source_id = api_articles.create_article(
        rt.client,
        title=title,
        tags=list(tags or []),
        content_json=content,
        destination=destination,
        comments=comments,
        parent_source_id=parent_source_id,
    )
    if on_created is not None:  # 등록 직후 source_id를 먼저 보존한다 (C-2)
        on_created(str(source_id))
    detail = api_articles.get_article(rt.client, source_id)
    problems = api_articles.verify_article(
        detail,
        title=title,
        tags=list(tags or []),
        menu_id=getattr(menu, "menu_id", menu),
        head_id=getattr(head, "head_id", None) if head is not None else None,
        body_lines=seone.body_lines_for_verify(body),
        image_count=len(components),
        start_at=start_at,
        # 페이로드는 답글이 루트에 중첩된다 → 전체 노드 수로 비교한다
        comments_count=api_articles.count_comment_nodes(comments or []),
        # 개수뿐 아니라 읽는 순서까지 본다 (docs/reference/live-comment-order.md §1)
        expected_comment_sequence=comment_mod.payload_sequence(comments or []),
    )
    if problems:
        raise PublishError("등록 검증 실패: " + "; ".join(problems))
    if start_at is None:

        def _waiting(waited: float) -> None:
            """30초마다 "아직 기다리는 중" 한 줄 (조용한 정체를 막는다, #3)."""
            try:
                rt.events.log(job_id, "info", f"등록 확인 대기 {int(waited)}초: {title}")
            except Exception:  # pragma: no cover - 이벤트 실패가 발행을 막지 않는다
                log.info("등록 확인 대기 %d초: %s", int(waited), title)

        try:
            api_articles.wait_written(
                rt.client,
                source_id,
                scheduled_at=start_at,
                max_wait_s=WAIT_WRITTEN_MAX_S,
                on_wait=_waiting,
            )
        except api_articles.PendingError as exc:
            return str(source_id), f"예약 확인 대기: {exc}"
        except V2RApiError as exc:
            if "시간 초과" in str(exc):
                return str(source_id), f"등록 확인 대기: {exc}"
            raise
    return str(source_id), None


def sanitize_slot(rt: Runtime, slot: Slot, job_id: int | None = None) -> Manuscript:
    """발행 직전 이모지 거름망 (사용자 절대 규칙: 이모지는 모든 원고에서 제외).

    어느 경로로 온 원고든(엑셀·시트·모델 생성) 여기서 한 번 더 지운다. 2026-09-19
    자사 카페 일상 글 18건이 제목 이모지를 달고 나간 사고의 재발 방지막이다.
    `slot.manuscript`도 바꿔 둬야 `build_comments`가 깨끗한 댓글을 만든다.
    """
    m = slot.manuscript
    spots = sanitize_mod.emoji_spots(m)
    if not spots:
        return m
    cleaned = sanitize_mod.sanitize_manuscript(m)
    slot.manuscript = cleaned
    message = f"이모지 제거: 제목/본문/댓글 {len(spots)}곳 ({', '.join(spots)})"
    try:
        rt.events.log(job_id, "warn", message)
    except Exception:  # 이벤트 기록 실패가 발행을 막지 않는다
        log.warning("%s", message)
    return cleaned


def assert_no_emoji(title: str, body: str, comments: list[dict] | None = None) -> None:
    """등록 직전 마지막 확인. 이모지가 남아 있으면 발행을 멈춘다(이중 안전장치)."""
    spots: list[str] = []
    if sanitize_mod.has_emoji(title):
        spots.append("제목")
    if sanitize_mod.has_emoji(body):
        spots.append("본문")
    for node in api_articles.flatten_comment_nodes(comments or []):
        text = node.get("contents") or node.get("text") or ""
        if sanitize_mod.has_emoji(text):
            spots.append("댓글")
            break
    if spots:
        raise PublishError("이모지가 남아 있어 발행을 멈췄습니다: " + ", ".join(spots))


def _warn_event(rt: Runtime, job_id: int | None, message: str) -> None:
    """경고 이벤트 1줄(기록 실패도 삼킨다)."""
    try:
        rt.events.log(job_id, "warn", message)
    except Exception:  # pragma: no cover - 방어용
        log.warning("%s", message)


def run_slot(
    rt: Runtime,
    spec: TaskSpec,
    slot: Slot,
    *,
    browser_page: Any = None,
    job_id: int | None = None,
    heartbeat: Any = None,
) -> dict:
    """슬롯 1건 실행. dry_run이면 계획만 돌려준다."""
    # 이모지 거름망 — 내용이 만들어지기 전에 원고부터 깨끗하게 한다(사용자 절대 규칙)
    m = sanitize_slot(rt, slot, job_id)
    planned = {
        "source": m.source,
        "row": m.source_row,
        "title": m.title,
        "account": slot.account,
        "cafe": slot.cafe,
        "board": slot.board,
        "workflow": slot.workflow,
        "scheduled_at": slot.scheduled_at.isoformat() if slot.scheduled_at else "즉시",
        "revision_at": slot.revision_at.isoformat() if slot.revision_at else None,
        "images": [str(p) for p in slot.images],
    }
    if spec.dry_run:
        planned["manuscript_type"] = m.manuscript_type
        if getattr(spec, "random_comments", False) and slot.workflow != "affiliate":
            # 모의 실행은 모델을 부르지 않는다 → 개수만 뽑아 보여준다 (규칙 §5)
            from v2r.content import daily_comments as dc

            planned["comments"] = dc.draw_count()
            return {**planned, "status": "planned", "dry_run": True}
        try:
            planned["comment_roles"] = comment_role_rows(rt, slot)
        except Exception as exc:  # 역할 표는 참고용이라 모의 실행을 막지 않는다
            planned["comment_roles"] = []
            planned["comment_note"] = f"댓글 역할 표를 만들지 못했습니다: {exc}"
        return {**planned, "status": "planned", "dry_run": True}

    # 마지막 관문 — V2R 글 목록 색인에 같은 글이 있으면 등록하지 않는다(사용자 절대 규칙).
    # 선택 단계에서 걸렀더라도, 그 사이에 다른 슬롯이 같은 글을 올렸을 수 있다.
    try:
        _dup, _why = duplicate.is_duplicate_against_index(rt, m, slot.cafe)
    except Exception as exc:  # noqa: BLE001 - 색인 조회 실패가 발행을 세우지 않는다
        log.warning("중복 관문 조회 실패(그냥 진행): %s", exc)
        _dup, _why = False, ""
    if _dup:
        rt.events.log(job_id, "warn", f"중복으로 발행 취소: {m.title} ({_why})")
        raise PublishError(f"V2R 기존 글과 중복: {_why}")

    key = (m.source, m.source_row, m.content_hash)
    root_start = slot.scheduled_at or datetime.now(KST)
    created_any = False  # 이번 슬롯에서 V2R에 글이 실제로 만들어졌는가
    # 이 발행 행의 "본 글"(제휴면 수정글, 일반이면 그 글)이 실제로 등록됐는가.
    # False면 create_article 이전 단계에서 끊긴 것이므로 재시도 가능한 failed로 내린다.
    target_created = False
    pending: str | None = None
    # 제휴 체인에서 방금 만든 일상 글. 수정글 등록 전에 끊기면 이 글을 지운다(되감기).
    daily_source_id: str | None = None
    rolled_back_daily: str | None = None
    daily_used_key: tuple | None = None  # 일상 글 원고의 사용 기록 키(되감기 때 되돌린다)

    def _beat() -> None:
        if heartbeat is not None:
            heartbeat()

    def _on_created(stage: str, *, target: bool = True):
        def hook(source_id: str) -> None:
            nonlocal created_any, target_created
            created_any = True
            if target:
                target_created = True
            rt.publications.mark(*key, "uncertain", stage, source_id=source_id)
            _beat()

        return hook

    def _rollback_daily() -> str | None:
        """수정글이 안 만들어졌는데 일상 글만 남았다 → 그 일상 글을 지운다(최선 노력).

        모호한 오류(ambiguous/network)에서는 부르지 않는다: 글이 생겼는지 알 수 없어
        reconcile이 처리해야 한다.
        """
        nonlocal rolled_back_daily
        if target_created or not daily_source_id or rolled_back_daily:
            return rolled_back_daily
        try:
            api_articles.delete_article(rt.client, daily_source_id)
        except Exception as exc:  # 되감기 실패는 실패 처리를 막지 않는다
            rt.events.log(
                job_id, "warn", f"일상 글 되감기 실패: rolled_back_daily={daily_source_id} ({exc})"
            )
        else:
            rt.events.log(job_id, "warn", f"일상 글 되감기: rolled_back_daily={daily_source_id}")
        rolled_back_daily = daily_source_id
        # 되감은 일상 글은 다시 뽑을 수 있게 사용 기록도 지운다
        if daily_used_key is not None:
            try:
                rt.publications.mark(*daily_used_key, "failed", "일상 글 되감기")
            except Exception:
                pass
        return rolled_back_daily

    def _rollback_note() -> str:
        """되감기를 했으면 결과/이벤트에 남길 꼬리표."""
        return f" (rolled_back_daily={rolled_back_daily})" if rolled_back_daily else ""

    def _mark_precreate_failed(reason: str) -> None:
        """본 글 등록 전에 끊긴 실패 → 재시도할 수 있게 failed로 남긴다."""
        if target_created:
            return  # 글은 이미 서버에 있다 → 미확정 유지
        text = " ".join(str(reason).split())
        rolled = _rollback_daily()
        label = "등록 전 실패(일상 글 되감기)" if rolled else "등록 전 실패"
        rt.publications.mark(*key, "failed", f"{label}: {text}"[:200])

    try:
        cafe, menu, head = rt.catalog.resolve(slot.cafe, slot.board, slot.account)
        components = _attach_images(rt, slot, browser_page)

        if slot.workflow == "affiliate":
            daily = _take_daily(rt, getattr(cafe, "name", None) or slot.cafe, bool(spec.dry_run))
            # --- 사전 점검: V2R에 글을 만들기 전에 수정글 재료를 모두 준비한다 ---
            # (사진 수 vs 자리표시, content_json, 댓글 payload) 여기서 터지면 되감기가 필요 없다.
            rev_at = slot.revision_at or (root_start + timedelta(hours=4))
            rev_tags = _keyword_tags(m)
            if not rev_tags:
                # 키워드가 A열·G열 둘 다 비었다 → 태그 없이 발행하고 경고만 남긴다
                planned["tag_note"] = "태그 없음"
                rt.events.log(
                    job_id, "warn", f"키워드가 없어 태그 없이 발행합니다 (행 {m.source_row})"
                )
            rev_body, rev_content = _prepare_content(m.body, components)
            payload = build_comments(rt, slot, rev_at, getattr(cafe, "cafe_id", None))
            # 마지막 확인: 여기까지 이모지가 남아 있으면 발행하지 않는다
            assert_no_emoji(m.title, rev_body, payload)
            assert_no_emoji(daily.title, daily.body, [])
            rt.publications.mark(
                *key,
                "uncertain",
                "daily_submitting",
                account=slot.account,
                cafe=slot.cafe,
                menu_id=str(getattr(menu, "menu_id", "")),
                scheduled_at=slot.scheduled_at.isoformat() if slot.scheduled_at else None,
            )
            daily_id, daily_pending = _create_and_verify(
                rt,
                title=daily.title,
                tags=daily.tags,
                body=daily.body,
                components=[],
                cafe=cafe,
                menu=menu,
                head=head,
                login_id=slot.account,
                start_at=slot.scheduled_at,
                comments=[],
                on_created=_on_created("daily_created", target=False),
                job_id=job_id,
            )
            del daily_pending  # 일상 글 확정 보류는 수정글 등록을 막지 않는다
            daily_source_id = daily_id
            rt.publications.mark(*key, "uncertain", "daily_done", source_id=daily_id)
            if daily.source and daily.content_hash:
                # 일상 글 자체도 사용 기록을 남겨 다음 실행에서 다시 뽑히지 않게 한다
                daily_used_key = (daily.source, daily.source_row, daily.content_hash)
                rt.publications.mark(
                    *daily_used_key,
                    "done",
                    "daily_used",
                    source_id=daily_id,
                )
            _beat()

            # source_id는 부모(일상 글)로 남겨둔다 → reconcile이 자식(수정글)을 찾는다 (C-1)
            rt.publications.mark(*key, "uncertain", "revision_submitting", source_id=daily_id)
            source_id, pending = _create_and_verify(
                rt,
                title=m.title,
                tags=rev_tags,
                body=rev_body,
                components=components,
                content=rev_content,
                cafe=cafe,
                menu=menu,
                head=head,
                login_id=slot.account,
                start_at=rev_at,
                comments=payload,
                parent_source_id=daily_id,
                target_view_count=random.randint(80, 100),
                on_created=_on_created("revision_created"),
                job_id=job_id,
            )
            planned["daily_source_id"] = daily_id
        else:
            if getattr(spec, "random_comments", False):
                payload = build_daily_comments(
                    rt, slot, root_start, getattr(cafe, "cafe_id", None), job_id=job_id
                )
            else:
                payload = build_comments(rt, slot, root_start, getattr(cafe, "cafe_id", None))
            # 마지막 확인: 여기까지 이모지가 남아 있으면 발행하지 않는다
            assert_no_emoji(m.title, m.body, payload)
            planned["comments"] = len(payload)
            rt.publications.mark(
                *key,
                "uncertain",
                "submitting",
                account=slot.account,
                cafe=slot.cafe,
                menu_id=str(getattr(menu, "menu_id", "")),
                scheduled_at=slot.scheduled_at.isoformat() if slot.scheduled_at else None,
            )
            source_id, pending = _create_and_verify(
                rt,
                title=m.title,
                tags=m.tags,
                body=m.body,
                components=components,
                cafe=cafe,
                menu=menu,
                head=head,
                login_id=slot.account,
                start_at=slot.scheduled_at,
                comments=payload,
                on_created=_on_created("created"),
                job_id=job_id,
            )

    except V2RApiError as exc:
        # `rate_limited_long`/`login_budget`은 classify()가 되살릴 수 없으므로
        # 예외가 들고 온 kind를 우선한다.
        kind = exc.kind or classify(exc)
        if kind in RATE_KINDS and not created_any:
            # 서버가 요청을 아예 받지 않았다 → 원고를 소모하지 않도록 재시도 가능한
            # failed로 내려 둔다(uncertain으로 남기면 다음 시도에서 건너뛴다).
            _mark_precreate_failed(f"{kind}: {exc}")
            rt.events.log(job_id, "warn", f"레이트 제한({kind}): {m.title}")
            raise PublishError(
                f"발행 보류({kind}): {exc}",
                kind=kind,
                retry_after=exc.retry_after,
            ) from exc
        if kind in DEFINITIVE_REJECTIONS and not created_any:
            # 서버가 요청을 거부해 글이 생기지 않았다 → 다른 계정으로 재시도 가능하게 failed
            rt.publications.mark(*key, "failed", f"거부({kind})")
        elif kind not in api_articles.AMBIGUOUS_KINDS:
            # 본 글이 아직 안 만들어졌고 모호한 오류도 아니다 → failed
            _mark_precreate_failed(f"{kind}: {exc}")
        if kind == "account_restricted":
            until = datetime.now(KST) + timedelta(days=RESTRICT_DAYS)
            rt.account_state.restrict(slot.account, until, RESTRICT_CODE, "계정 제한(27000)")
            rt.events.log(job_id, "warn", f"계정 제한: {slot.account}")
            raise RetryWithOtherAccount(f"계정 제한: {slot.account}") from exc
        rt.events.log(job_id, "error", f"발행 실패({kind}): {m.title}{_rollback_note()}")
        raise PublishError(f"발행 실패({kind}): {exc}{_rollback_note()}") from exc
    except PublishError as exc:
        # 등록 검증 실패처럼 create 이후에 난 것은 미확정 유지, 그 전이면 failed
        _mark_precreate_failed(str(exc))
        if rolled_back_daily:
            rt.events.log(job_id, "error", f"발행 실패: {m.title}{_rollback_note()}")
            raise PublishError(f"{exc}{_rollback_note()}") from exc
        raise
    except Exception as exc:  # 그 외(NoPhotoError, seone ValueError 등)는 그대로 실패로
        _mark_precreate_failed(str(exc))
        rt.events.log(job_id, "error", f"발행 실패: {m.title}: {exc}{_rollback_note()}")
        raise PublishError(f"발행 실패: {exc}{_rollback_note()}") from exc

    url = api_articles.article_url(source_id)
    if pending:
        # 글은 서버에 있고 확정만 못 봤다 → 실패가 아니라 미확정으로 남긴다 (M-7)
        rt.publications.mark(
            *key, "uncertain", "written_pending", source_id=source_id, url=url
        )
        rt.events.log(job_id, "info", f"예약 확인 대기: {m.title} {url}")
        return {
            **planned,
            "status": "pending",
            "source_id": source_id,
            "url": url,
            "message": f"예약 확인 대기: {m.title}",
            "reason": pending,
        }
    rt.publications.mark(*key, "done", "done", source_id=source_id, url=url)
    # --- 여기서부터는 뒷정리다. 글은 이미 올라갔으므로 무엇이 터져도 발행을 실패로
    #     만들거나 다음 슬롯을 막아서는 안 된다 (장애 2026-09-20 #3). ---
    try:
        # 방금 올린 글을 V2R 글 목록 색인에도 바로 남긴다 → 다시 동기화하지 않아도
        # 최신이고, 중복 관문이 같은 글을 또 올리지 않는다 (규칙 §3)
        article_sync.record_published(
            rt,
            cafe=slot.cafe,
            cafe_id=getattr(cafe, "cafe_id", None),
            source_id=source_id,
            login_id=slot.account,
            title=m.title,
            body_hash=m.content_hash,
        )
        duplicate.forget_cafe_titles(rt, slot.cafe)  # 캐시에 방금 글을 반영한다
    except Exception as exc:  # noqa: BLE001
        log.warning("발행 뒤 색인 기록 실패(발행은 성공): %s", exc)
        _warn_event(rt, job_id, f"색인 기록 실패(발행은 성공): {exc}")
    try:
        rt.account_state.touch_used(slot.account)
    except Exception as exc:  # noqa: BLE001
        log.warning("계정 사용 기록 실패(발행은 성공): %s", exc)
    for path in slot.images:
        try:
            sha = rt.scratch.get("variant_sha", {}).get(str(path), Path(path).parent.name)
            record_variant_use(rt, sha, Path(path), source_id)
        except Exception:
            pass
    try:
        rt.events.log(job_id, "info", f"발행 완료: {m.title} {url}")
    except Exception:  # noqa: BLE001 pragma: no cover
        log.info("발행 완료: %s %s", m.title, url)
    return {**planned, "status": "done", "source_id": source_id, "url": url}


__all__ = [
    "DEFERRED_ACCOUNT",
    "PublishError",
    "RetryWithOtherAccount",
    "Slot",
    "assert_no_emoji",
    "sanitize_slot",
    "build_daily_comments",
    "cafe_default_board",
    "cafe_matches",
    "canonical_board",
    "find_cafe_entry",
    "load_accounts",
    "load_manuscripts",
    "notify_photo_shortage",
    "pick_self_daily_accounts",
    "plan",
    "prepare_manuscripts",
    "prepare_per_cafe",
    "brand_source_keys",
    "count_today_for_cafe",
    "self_cafe_names",
    "refresh_source",
    "resolve_board",
    "resolve_cafe",
    "run_slot",
    "select_source_entries",
    "writable_logins",
]
