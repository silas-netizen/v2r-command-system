"""발행 실행기: 원고 준비 → 슬롯 계획 → 슬롯 실행. DESIGN §5, §6 기준."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from v2r.accounts.assign import AssignError, assign, rotate
from v2r.accounts.loader import Account, load_from_rows
from v2r.accounts.rules import eligible, find_affiliate, work_type_for
from v2r.api import articles as api_articles
from v2r.api.errors import V2RApiError, classify
from v2r.command.spec import TaskSpec
from v2r.content import comments as comment_mod
from v2r.content import duplicate, seone
from v2r.content.manuscript import Manuscript
from v2r.engine.context import Runtime
from v2r.engine.scheduler import KST, plan_slots, revision_at
from v2r.sources import sheets

DAILY_POOL_SOURCE = "랜덤일상"
DEFAULT_CAFE = "고요한 아침"
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


class PublishError(RuntimeError):
    """발행 실패(복구 불가)."""


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


def _sheet_entries(rt: Runtime) -> list[dict]:
    """sources.yaml의 원고 시트 목록."""
    return [
        e
        for e in (rt.sources_cfg.get("sources") or [])
        if isinstance(e, dict) and (e.get("kind") or "sheet") == "sheet"
    ]


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
        for wanted in ("일상글목록", DAILY_POOL_SOURCE):
            entry = _entry_by_name(rt, wanted)
            if entry is not None:
                return [entry]
        return entries[:1]
    if spec.task == "publish_brand":
        picked = [
            e
            for e in entries
            if _norm(e.get("name", "")) != _norm(DAILY_POOL_SOURCE)
            and ("각색" in (e.get("name") or "") or "제휴" in (e.get("name") or ""))
        ]
        return picked or [e for e in entries if _norm(e.get("name", "")) != _norm(DAILY_POOL_SOURCE)]
    if spec.task == "publish_info":
        entry = _entry_by_name(rt, "정보성")
        if entry is None:
            raise PublishError("정보성 글 시트가 sources.yaml에 없습니다")
        return [entry]
    return entries  # publish_batch


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


def load_manuscripts(rt: Runtime, entry: dict, prefer_cache: bool = False) -> list[Manuscript]:
    """시트 1건을 원고 목록으로. `prefer_cache`면 네트워크를 쓰지 않는다."""
    name = str(entry.get("name") or "")
    cache_get, cache_put = _cache_hooks(rt)
    rows = sheets.load_source(
        entry, cache_get=cache_get, cache_put=cache_put, prefer_cache=prefer_cache
    )
    return _parse_rows(rows, name, rt.cafes_cfg)


def refresh_source(rt: Runtime, entry: dict) -> int:
    """시트 1건을 새로 읽어 캐시에 저장. 행 수 반환."""
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


def prepare_manuscripts(
    rt: Runtime, spec: TaskSpec, skipped: list[dict] | None = None
) -> list[Manuscript]:
    """발행할 원고를 고른다. 이미 발행됐거나 중복인 행은 건너뛴다."""
    skipped = skipped if skipped is not None else []
    picked: list[Manuscript] = []
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
            history = _done_history(rt, name)
            for m in items:
                if rt.publications.exists(name, m.source_row, m.content_hash):
                    skipped.append(
                        {"source": name, "row": m.source_row, "reason": "이미 발행됨"}
                    )
                    continue
                verdict = duplicate.check_against_history(m, history)
                if verdict:
                    reason = "중복(완전일치)" if verdict == "exact" else "중복(유사)"
                    if not spec.dry_run:  # 모의 실행은 DB를 바꾸지 않는다
                        rt.publications.mark(
                            name, m.source_row, m.content_hash, "skipped", stage=verdict
                        )
                    skipped.append({"source": name, "row": m.source_row, "reason": reason})
                    continue
                picked.append(m)
                history.append(m)  # 같은 실행 안에서의 중복도 잡는다

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


def resolve_cafe(rt: Runtime, m: Manuscript, spec: TaskSpec) -> str:
    """원고 → 명령 → 기본값 순."""
    return m.cafe or spec.cafe or DEFAULT_CAFE


def resolve_board(rt: Runtime, m: Manuscript, spec: TaskSpec, cafe: str) -> str:
    """원고 → 명령 → 제휴 카페 지정 게시판 → 기본 게시판."""
    if m.board:
        return m.board
    if spec.board:
        return spec.board
    entry = find_affiliate(cafe, rt.cafes_cfg)
    if entry and entry.get("board"):
        return str(entry["board"])
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


def pick_images(rt: Runtime, m: Manuscript, spec: TaskSpec, need: int) -> list[Path]:
    """창고에서 미사용 세탁본을 need장 고른다. 모자라면 PublishError."""
    if need <= 0:
        return []
    brand = spec.brand or m.source or ""
    wh = rt.warehouse
    originals = wh.list_originals(brand) if brand else wh.list_originals()
    if not originals:
        raise PublishError(f"사진이 부족합니다 (브랜드 {brand or '전체'} 원본 없음)")
    used = used_variants(rt)
    out: list[Path] = []
    for original in originals:
        if len(out) >= need:
            break
        sha = wh.sha256(original)
        while len(out) < need:
            variant = wh.pick_variant(sha, used)
            if variant is None:
                break
            used.add(str(variant))
            rt.scratch.setdefault("variant_sha", {})[str(variant)] = sha
            out.append(variant)
    if len(out) < need:
        raise PublishError(
            f"사진이 부족합니다 (필요 {need}장, 사용 가능 {len(out)}장)"
        )
    return out


# --------------------------------------------------------------------
# 계획
# --------------------------------------------------------------------
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
    need_assign = [i for i, m in enumerate(manuscripts) if not m.account]
    if need_assign:
        groups: dict[str, list[int]] = {}
        for i in need_assign:
            wt = work_type_for(spec.task, cafes[i], rt.cafes_cfg)
            groups.setdefault(wt, []).append(i)
        for work_type, idxs in groups.items():
            pool = eligible(pool_all, work_type, comment_only, restricted)
            need = spec.account_count or len(idxs)
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
            else:
                if not pool:
                    raise AssignError("사용 가능한 계정이 없습니다 (계정 시트를 확인하세요)")
                chosen = assign(
                    pool,
                    mode="auto",
                    count=min(need, len(pool)),
                    last_used=rt.account_state.last_used_map(),
                )
            for position, i in enumerate(idxs):
                assigned[i] = rotate(chosen, position)

    # --- 시각 계획 ---
    immediate_flags = [spec.immediate or is_test_cafe(rt, c) for c in cafes]
    timed_idx = [i for i, imm in enumerate(immediate_flags) if not imm]
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
    items = comment_mod.assign_comment_accounts(tree, pool, slot.account)
    items = comment_mod.schedule(items, root_start)
    items = comment_mod.resolve_conflicts(items)
    members: dict[str, dict] = {}
    try:
        for ca in rt.catalog.cafe_accounts(cafe_id):
            members[ca.login_id] = {"member_key": ca.member_key or "", "nick": ca.nick or ""}
    except Exception:
        members = {}
    return comment_mod.to_api_payload(items, members)


def _daily_pool(rt: Runtime) -> list[Manuscript]:
    """제휴 일상 글 풀(랜덤일상)."""
    pool = rt.scratch.get("daily_pool")
    if pool is None:
        entry = _entry_by_name(rt, DAILY_POOL_SOURCE)
        pool = load_manuscripts(rt, entry) if entry else []
        rt.scratch["daily_pool"] = pool
    return pool


def _take_daily(rt: Runtime) -> Manuscript:
    """실행 중 겹치지 않게 일상 글 1건을 뽑는다."""
    pool = _daily_pool(rt)
    used: set[int] = rt.scratch.setdefault("daily_used", set())
    left = [m for m in pool if m.source_row not in used]
    if not left:
        raise PublishError("제휴 일상 글 원고가 부족합니다 (랜덤일상 시트 확인)")
    picked = random.choice(left)
    used.add(picked.source_row)
    return picked


def _attach_images(rt: Runtime, slot: Slot, browser_page: Any) -> list[dict]:
    """이미지가 있으면 SE-ONE 붙여넣기로 컴포넌트를 얻는다."""
    if not slot.images:
        return []
    if browser_page is None:
        raise PublishError("이미지 첨부에는 브라우저 세션이 필요합니다")
    from v2r.browser.seone_paste import attach_images_via_paste

    return attach_images_via_paste(
        browser_page,
        rt.settings.v2r_site,
        slot.cafe,
        slot.account,
        slot.board,
        list(slot.images),
    )


def _create_and_verify(
    rt: Runtime,
    *,
    title: str,
    tags: list[str],
    body: str,
    components: list[dict],
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
) -> tuple[str, str | None]:
    """글 1건 등록 후 GET 재확인.

    반환: `(source_id, pending_reason)`. `pending_reason`이 있으면 글은 서버에 있고
    등록 확정만 못 본 상태(실패 아님).
    """
    content = seone.content_json(body, components)
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
        comments_count=len(comments or []),
    )
    if problems:
        raise PublishError("등록 검증 실패: " + "; ".join(problems))
    if start_at is None:
        try:
            api_articles.wait_written(
                rt.client, source_id, scheduled_at=start_at, max_wait_s=WAIT_WRITTEN_MAX_S
            )
        except api_articles.PendingError as exc:
            return str(source_id), f"예약 확인 대기: {exc}"
        except V2RApiError as exc:
            if "시간 초과" in str(exc):
                return str(source_id), f"등록 확인 대기: {exc}"
            raise
    return str(source_id), None


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
    m = slot.manuscript
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
        return {**planned, "status": "planned", "dry_run": True}

    key = (m.source, m.source_row, m.content_hash)
    root_start = slot.scheduled_at or datetime.now(KST)
    created_any = False  # 이번 슬롯에서 V2R에 글이 실제로 만들어졌는가
    pending: str | None = None

    def _beat() -> None:
        if heartbeat is not None:
            heartbeat()

    def _on_created(stage: str):
        def hook(source_id: str) -> None:
            nonlocal created_any
            created_any = True
            rt.publications.mark(*key, "uncertain", stage, source_id=source_id)
            _beat()

        return hook

    try:
        cafe, menu, head = rt.catalog.resolve(slot.cafe, slot.board, slot.account)
        components = _attach_images(rt, slot, browser_page)

        if slot.workflow == "affiliate":
            daily = _take_daily(rt)
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
                on_created=_on_created("daily_created"),
            )
            del daily_pending  # 일상 글 확정 보류는 수정글 등록을 막지 않는다
            rt.publications.mark(*key, "uncertain", "daily_done", source_id=daily_id)
            _beat()

            rev_at = slot.revision_at or (root_start + timedelta(hours=4))
            payload = build_comments(rt, slot, rev_at, getattr(cafe, "cafe_id", None))
            # source_id는 부모(일상 글)로 남겨둔다 → reconcile이 자식(수정글)을 찾는다 (C-1)
            rt.publications.mark(*key, "uncertain", "revision_submitting", source_id=daily_id)
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
                start_at=rev_at,
                comments=payload,
                parent_source_id=daily_id,
                target_view_count=random.randint(80, 100),
                on_created=_on_created("revision_created"),
            )
            planned["daily_source_id"] = daily_id
        else:
            payload = build_comments(rt, slot, root_start, getattr(cafe, "cafe_id", None))
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
            )

    except V2RApiError as exc:
        kind = classify(exc)
        if kind in DEFINITIVE_REJECTIONS and not created_any:
            # 서버가 요청을 거부해 글이 생기지 않았다 → 다른 계정으로 재시도 가능하게 failed
            rt.publications.mark(*key, "failed", f"거부({kind})")
        if kind == "account_restricted":
            until = datetime.now(KST) + timedelta(days=RESTRICT_DAYS)
            rt.account_state.restrict(slot.account, until, RESTRICT_CODE, "계정 제한(27000)")
            rt.events.log(job_id, "warn", f"계정 제한: {slot.account}")
            raise RetryWithOtherAccount(f"계정 제한: {slot.account}") from exc
        rt.events.log(job_id, "error", f"발행 실패({kind}): {m.title}")
        raise PublishError(f"발행 실패({kind}): {exc}") from exc
    except PublishError:
        raise
    except Exception as exc:  # 그 외는 그대로 실패로
        rt.events.log(job_id, "error", f"발행 실패: {m.title}: {exc}")
        raise PublishError(f"발행 실패: {exc}") from exc

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
    rt.account_state.touch_used(slot.account)
    for path in slot.images:
        try:
            sha = rt.scratch.get("variant_sha", {}).get(str(path), Path(path).parent.name)
            record_variant_use(rt, sha, Path(path), source_id)
        except Exception:
            pass
    rt.events.log(job_id, "info", f"발행 완료: {m.title} {url}")
    return {**planned, "status": "done", "source_id": source_id, "url": url}


__all__ = [
    "PublishError",
    "RetryWithOtherAccount",
    "Slot",
    "load_accounts",
    "load_manuscripts",
    "plan",
    "prepare_manuscripts",
    "refresh_source",
    "run_slot",
    "select_source_entries",
]
