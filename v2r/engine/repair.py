"""예약 수정글의 댓글 역할 복구 (지우고 같은 내용으로 다시 등록).

왜 필요한가
-----------
`content/comments.py::assign_comment_accounts`가 원고유형을 받기 전에 발행된
제휴 수정글은 댓글2 스레드(대댓글2·대대댓글2·대대대댓글2)가 전부 **본문
작성자 한 계정**으로 잡혀 있다. 작성자가 혼자 묻고 스스로 제품명을 답하고
스스로 "222 저도 효과 봤어요"를 다는 모양이 된다
(`docs/reference/live-comment-order.md` §4).

어떻게 고치나
-------------
예약 댓글만 따로 지우는 엔드포인트는 **검증되지 않았다**(api-spec §9).
반면 "원글을 지우면 딸린 예약 댓글도 함께 취소된다"는 것은 프런트 번들의
오류 문구로 확인됐고, 우리가 이미 쓰는 두 경로(`article/delete`,
`naver_cafe_article_source`)만으로 끝난다. 그래서:

1. `get_article`로 글을 통째로 읽어 제목·태그·본문(content_json)·목적지·
   `parent_source_id`·댓글 본문을 **그대로** 확보한다.
2. 댓글 계정만 기준대로 다시 배정한다(본문·예약 시각은 건드리지 않는다).
3. 원글을 지우고(예약 댓글 동반 취소) 같은 내용으로 다시 등록한다.
4. 다시 `get_article`해서 본문·태그·예약시각·댓글 순서와 **역할**까지 본다.

안전장치: 예약(`RESERVED`) 상태가 아니거나 댓글 예약 시각이 이미 지났으면
아무것도 하지 않고 이유만 돌려준다. 기본은 모의 실행이다.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from v2r.api import articles as api_articles
from v2r.api.client import field, walk_dicts
from v2r.command.spec import TaskSpec
from v2r.content import comments as comment_mod
from v2r.engine.context import Runtime
from v2r.engine.publish import comment_pool
from v2r.sources import sheets

#: 복구를 마친 발행 기록의 단계
REPAIRED_STAGE = "repaired"

#: 원고유형을 못 찾았을 때의 기본값 (관측 12건 중 10건)
DEFAULT_MANUSCRIPT_TYPE = "질문형"

#: 복구 대상 댓글 구조(12노드)
EXPECTED_NODES = len(comment_mod.DEFAULT_TREE)

#: V2R source_id(ULID) 모양
RE_SOURCE_ID = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")


class RepairError(RuntimeError):
    """댓글 복구 실패."""


def source_ids_in(text: str) -> list[str]:
    """명령문에 적힌 source_id 목록(중복 제거, 등장 순서)."""
    out: list[str] = []
    for sid in RE_SOURCE_ID.findall(str(text or "")):
        if sid not in out:
            out.append(sid)
    return out


# --------------------------------------------------------------------
# 읽기
# --------------------------------------------------------------------
def _block(detail: dict, key: str, *, must: str) -> dict:
    value = detail.get(key) if isinstance(detail, dict) else None
    if isinstance(value, dict):
        return value
    found = next((d for d in walk_dicts(detail) if must in d), None)
    if isinstance(found, dict):
        return found
    raise RepairError(f"글 상세에 {key}가 없습니다")


def _status_of(detail: dict) -> str:
    """등록 상태. history가 None이면 destination.status를 본다(라이브 실측)."""
    history = detail.get("naver_cafe_article_history")
    if isinstance(history, dict):
        status = field(history, "status", default="")
        if status:
            return str(status).upper()
    dest = detail.get("naver_cafe_article_destination")
    if isinstance(dest, dict):
        return str(dest.get("status") or "").upper()
    return ""


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def capture(rt: Runtime, source_id: str) -> dict:
    """복구에 필요한 것을 글 상세에서 그대로 뽑아낸다(읽기 전용)."""
    detail = api_articles.get_article(rt.client, source_id)
    src = _block(detail, "naver_cafe_article_source", must="title")
    dest = _block(detail, "naver_cafe_article_destination", must="menu_id")
    body = _block(detail, "naver_cafe_article_source_detail", must="body")
    raw_comments = detail.get("naver_cafe_article_source_comments")
    comments = [c for c in (raw_comments or []) if isinstance(c, dict)]

    start_at = _parse_iso(dest.get("start_at"))
    write_options = dest.get("write_options")
    return {
        "detail": detail,
        "source_id": str(src.get("source_id") or source_id),
        "title": str(src.get("title") or ""),
        "tag_list": list(src.get("tag_list") or []),
        "content_json": str(body.get("body") or ""),
        "parent_source_id": src.get("parent_source_id") or None,
        "status": _status_of(detail),
        "cafe_id": dest.get("cafe_id"),
        "cafe_name": dest.get("cafe_name"),
        "menu_id": dest.get("menu_id"),
        "menu_name": dest.get("menu_name"),
        "head_id": dest.get("head_id"),
        "head_name": dest.get("head_name"),
        "naver_login_id": str(dest.get("naver_login_id") or ""),
        "start_at": start_at,
        "target_view_count": int(dest.get("target_view_count") or 0),
        "write_options": dict(write_options) if isinstance(write_options, dict) else None,
        "comments": comments,
        "body_lines": api_articles.body_lines_of(detail),
        "image_count": api_articles.image_count_of(detail),
    }


def manuscript_type_for(rt: Runtime, source_id: str) -> tuple[str, bool]:
    """발행 기록 → 원본 시트 E열 `원고유형`. 못 찾으면 (기본값, True)."""
    pub = rt.publications.by_source_id(source_id)
    if not pub:
        return DEFAULT_MANUSCRIPT_TYPE, True
    payload = rt.sources_cache.get(str(pub.get("source_key") or ""))
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return DEFAULT_MANUSCRIPT_TYPE, True
    try:
        items = sheets.parse_affiliate_rows(rows, source=str(pub.get("source_key") or ""))
    except Exception:
        return DEFAULT_MANUSCRIPT_TYPE, True
    row_number = int(pub.get("row_number") or 0)
    for m in items:
        if m.source_row == row_number and (m.manuscript_type or "").strip():
            return str(m.manuscript_type).strip(), False
    return DEFAULT_MANUSCRIPT_TYPE, True


# --------------------------------------------------------------------
# 재구성
# --------------------------------------------------------------------
def build_tree(comments: list[dict]) -> list[dict]:
    """라이브 평탄 댓글 배열 → 12노드 기본 트리(본문 텍스트만 옮긴다).

    라이브 배열 순서가 곧 읽는 순서이고 `DEFAULT_TREE`도 같은 순서라서
    자리끼리 그대로 맞춘다(`docs/reference/live-comment-order.md` §1).
    """
    if len(comments) != EXPECTED_NODES:
        raise RepairError(
            f"댓글 {EXPECTED_NODES}개짜리 글만 복구합니다 (현재 {len(comments)}개)"
        )
    tree: list[dict] = []
    for node, live in zip(comment_mod.DEFAULT_TREE, comments):
        tree.append({**node, "text": str(live.get("contents") or "")})
    return tree


def rebuild_comments(
    rt: Runtime,
    state: dict,
    manuscript_type: str,
    rng: Any = None,
    members: dict[str, dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """기준대로 다시 배정한 (항목 목록, API 페이로드)."""
    pool = comment_pool(rt, "affiliate")
    if not pool:
        raise RepairError("제휴 댓글 계정 풀이 비어 있습니다 (config/cafes.yaml)")
    tree = build_tree(state["comments"])
    items = comment_mod.assign_comment_accounts(
        tree, pool, state["naver_login_id"], rng=rng, manuscript_type=manuscript_type
    )
    items = comment_mod.schedule(items, state["start_at"])
    items = comment_mod.resolve_conflicts(items)
    if members is None:
        members = {
            ca.login_id: {"member_key": ca.member_key or "", "nick": ca.nick or ""}
            for ca in rt.catalog.cafe_accounts(state["cafe_id"])
        }
    payload = comment_mod.to_api_payload(items, members)
    return items, payload


def role_problems(items: list[dict], author: str, manuscript_type: str) -> list[str]:
    """배정 결과가 기준(§2)을 지키는지. 불일치 목록을 돌려준다."""
    by_label = {it["label"]: it for it in items}
    problems: list[str] = []
    fold = lambda s: str(s or "").casefold()  # noqa: E731

    roots = [f"댓글{i}" for i in range(1, 6)]
    root_accounts = [fold(by_label.get(lbl, {}).get("account")) for lbl in roots]
    if len(set(root_accounts)) != len(roots):
        problems.append(f"루트 5개가 서로 다른 계정이 아닙니다: {root_accounts}")
    if fold(author) in root_accounts:
        problems.append("루트 댓글에 본문 작성자가 들어 있습니다")

    for i in range(1, 6):
        got = fold(by_label.get(f"대댓글{i}", {}).get("account"))
        if got != fold(author):
            problems.append(f"대댓글{i} 작성 계정이 작성자가 아닙니다: {got}")

    review = comment_mod.is_review_type(manuscript_type)
    lvl2 = fold(by_label.get("대대댓글2", {}).get("account"))
    lvl3 = fold(by_label.get("대대대댓글2", {}).get("account"))
    root2 = fold(by_label.get("댓글2", {}).get("account"))
    if review:
        if lvl2 in root_accounts or lvl2 == fold(author):
            problems.append(f"후기형 대대댓글2는 여분 댓글 계정이어야 합니다: {lvl2}")
        if lvl3 != fold(author):
            problems.append(f"후기형 대대대댓글2는 작성자여야 합니다: {lvl3}")
    else:
        if lvl2 != root2:
            problems.append(f"질문형 대대댓글2는 댓글2 계정이어야 합니다: {lvl2} != {root2}")
        if lvl3 in root_accounts or lvl3 == fold(author):
            problems.append(f"질문형 대대대댓글2는 여분 댓글 계정이어야 합니다: {lvl3}")
    return problems


def live_role_problems(comments: list[dict], items: list[dict]) -> list[str]:
    """등록된 라이브 댓글의 계정·`reply_member`가 계획과 같은지."""
    problems: list[str] = []
    if len(comments) != len(items):
        return [f"댓글 수 불일치: {len(comments)} != {len(items)}"]
    by_label = {it["label"]: it for it in items}
    for i, (live, want) in enumerate(zip(comments, items)):
        label = want["label"]
        got = str(live.get("naver_login_id") or "").casefold()
        expect = str(want.get("account") or "").casefold()
        if got != expect:
            problems.append(f"#{i} {label} 작성 계정 {got} != {expect}")
        rm = live.get("reply_member")
        got_rm = str((rm or {}).get("naver_login_id") or "").casefold() if isinstance(rm, dict) else ""
        parent = by_label.get(want.get("parent") or "")
        want_rm = (
            str(parent.get("account") or "").casefold()
            if (want.get("depth") or 0) >= 2 and parent
            else ""
        )
        if got_rm != want_rm:
            problems.append(f"#{i} {label} reply_member {got_rm or '없음'} != {want_rm or '없음'}")
    return problems


def _mask(login: Any) -> str:
    text = str(login or "")
    return text[:3] if text else "-"


def plan_rows(items: list[dict]) -> list[dict]:
    """보고용 표(idx, 라벨, 계정, reply_member, 예약 시각)."""
    by_label = {it["label"]: it for it in items}
    out: list[dict] = []
    for i, it in enumerate(items):
        parent = by_label.get(it.get("parent") or "")
        rm = parent.get("account") if (it.get("depth") or 0) >= 2 and parent else None
        out.append(
            {
                "idx": i,
                "label": it["label"],
                "account": str(it.get("account") or ""),
                "account_masked": _mask(it.get("account")),
                "reply_member": str(rm or ""),
                "reply_member_masked": _mask(rm) if rm else "-",
                "start_at": api_articles.to_iso_z(it["start_at"]),
            }
        )
    return out


def live_rows(comments: list[dict]) -> list[dict]:
    """라이브 댓글 배열 → 같은 모양의 표(수리 전/후 비교용)."""
    out: list[dict] = []
    for i, c in enumerate(comments):
        label = (
            comment_mod.DEFAULT_TREE[i]["label"]
            if i < len(comment_mod.DEFAULT_TREE)
            else f"#{i}"
        )
        rm = c.get("reply_member")
        rm_id = (rm or {}).get("naver_login_id") if isinstance(rm, dict) else None
        out.append(
            {
                "idx": i,
                "label": label,
                "account": str(c.get("naver_login_id") or ""),
                "account_masked": _mask(c.get("naver_login_id")),
                "reply_member": str(rm_id or ""),
                "reply_member_masked": _mask(rm_id) if rm_id else "-",
                "start_at": str(c.get("start_at") or ""),
            }
        )
    return out


# --------------------------------------------------------------------
# 실행
# --------------------------------------------------------------------
def repair_revision_comments(
    rt: Runtime,
    source_id: str,
    dry_run: bool = True,
    now: datetime | None = None,
    rng: Any = None,
    job_id: int | None = None,
) -> dict:
    """수정글 1건의 댓글 역할을 기준대로 되돌린다.

    모의 실행이면 계획만 돌려준다. 실제 실행이면 원글을 지우고(예약 댓글 동반
    취소) 같은 제목·태그·본문·목적지·`parent_source_id`로 다시 등록한 뒤
    등록 결과를 다시 읽어 검증한다.
    """
    state = capture(rt, source_id)
    out: dict[str, Any] = {
        "ok": False,
        "dry_run": bool(dry_run),
        "source_id": source_id,
        "title": state["title"],
        "cafe_name": state["cafe_name"],
        "account": state["naver_login_id"],
        "status": state["status"],
        "start_at": api_articles.to_iso_z(state["start_at"]),
        "before": live_rows(state["comments"]),
    }

    if state["status"] != "RESERVED":
        out["aborted"] = f"예약(RESERVED) 상태가 아닙니다: {state['status'] or '알 수 없음'}"
        return out
    if state["start_at"] is None:
        out["aborted"] = "예약 시각을 읽지 못했습니다"
        return out
    if not state["parent_source_id"]:
        out["aborted"] = "수정글이 아닙니다 (parent_source_id 없음)"
        return out
    if not state["content_json"]:
        out["aborted"] = "본문(content_json)을 읽지 못했습니다"
        return out

    moment = now or datetime.now(api_articles.KST)
    past = [
        str(c.get("start_at"))
        for c in state["comments"]
        if (_parse_iso(c.get("start_at")) or moment) <= moment
    ]
    if past:
        out["aborted"] = f"이미 지난 댓글 예약이 있습니다: {past[0]}"
        return out

    manuscript_type, assumed = manuscript_type_for(rt, source_id)
    out["manuscript_type"] = manuscript_type
    out["manuscript_type_assumed"] = assumed
    if assumed:
        out["note"] = f"원고유형을 찾지 못해 기본값 {manuscript_type}으로 봅니다"

    try:
        items, payload = rebuild_comments(rt, state, manuscript_type, rng=rng)
    except Exception as exc:
        out["aborted"] = f"댓글 재구성 실패: {exc}"
        return out

    problems = role_problems(items, state["naver_login_id"], manuscript_type)
    if problems:
        out["aborted"] = "재구성 결과가 기준과 다릅니다: " + "; ".join(problems)
        return out

    # 본문 텍스트·예약 시각은 그대로여야 한다 (계정만 바꾼다)
    texts_before = [str(c.get("contents") or "") for c in state["comments"]]
    if [it.get("text", "") for it in items] != texts_before:
        out["aborted"] = "댓글 본문이 보존되지 않았습니다"
        return out
    starts_before = [_parse_iso(c.get("start_at")) for c in state["comments"]]
    if [_parse_iso(r["start_at"]) for r in plan_rows(items)] != starts_before:
        out["aborted"] = "댓글 예약 시각이 보존되지 않았습니다"
        return out

    out["after"] = plan_rows(items)
    if dry_run:
        out["ok"] = True
        out["message"] = "모의 실행: 계획만 만들었습니다"
        return out

    # --- 실제 실행 ---
    api_articles.delete_article(rt.client, source_id)
    out["deleted"] = source_id

    destination = {
        "cafe_id": state["cafe_id"],
        "cafe_name": state["cafe_name"],
        "head_id": state["head_id"],
        "head_name": state["head_name"],
        "menu_id": state["menu_id"],
        "menu_name": state["menu_name"],
        "naver_login_id": state["naver_login_id"],
        "start_at": api_articles.to_iso_z(state["start_at"]),
        "target_view_count": state["target_view_count"],
        "use_comment_ai": True,
        "parent_id": None,
    }
    new_id = api_articles.create_article(
        rt.client,
        title=state["title"],
        tags=list(state["tag_list"]),
        content_json=state["content_json"],
        destination=destination,
        comments=payload,
        parent_source_id=str(state["parent_source_id"]),
        write_options=state["write_options"],
    )
    new_id = str(new_id)
    url = api_articles.article_url(new_id)
    out["new_source_id"] = new_id
    out["url"] = url

    detail = api_articles.get_article(rt.client, new_id)
    verify = api_articles.verify_article(
        detail,
        title=state["title"],
        tags=list(state["tag_list"]),
        menu_id=state["menu_id"],
        head_id=state["head_id"],
        body_lines=state["body_lines"],
        image_count=state["image_count"],
        start_at=state["start_at"],
        comments_count=api_articles.count_comment_nodes(payload),
        expected_comment_sequence=comment_mod.payload_sequence(payload),
    )
    live = [
        c
        for c in (detail.get("naver_cafe_article_source_comments") or [])
        if isinstance(c, dict)
    ]
    verify += live_role_problems(live, items)
    out["after"] = live_rows(live) or out["after"]
    out["problems"] = verify
    out["ok"] = not verify

    pub = rt.publications.by_source_id(source_id)
    if pub:
        rt.publications.mark(
            str(pub["source_key"]),
            int(pub["row_number"]),
            str(pub["content_hash"]),
            "done",
            REPAIRED_STAGE,
            source_id=new_id,
            url=url,
        )
        out["publication"] = f"{pub['source_key']}#{pub['row_number']}"
    rt.events.log(
        job_id,
        "info" if out["ok"] else "error",
        f"댓글 복구: {source_id} → {new_id} {url}"
        + ("" if out["ok"] else " / 검증 실패: " + "; ".join(verify)),
    )
    if not out["ok"]:
        raise RepairError("댓글 복구 검증 실패: " + "; ".join(verify))
    return out


def repair_comments(rt: Runtime, spec: TaskSpec, job_id: int | None = None) -> dict:
    """`repair_comments` 작업 처리기. 명령문에 적힌 source_id들을 차례로 고친다."""
    ids = source_ids_in(spec.notes)
    if not ids:
        return {
            "ok": False,
            "dry_run": spec.dry_run,
            "results": [],
            "errors": [],
            "report": "복구할 글의 source_id를 명령에 적어 주세요 (26자리 ID).",
        }

    results: list[dict] = []
    errors: list[str] = []
    for sid in ids:
        try:
            results.append(
                repair_revision_comments(rt, sid, dry_run=spec.dry_run, job_id=job_id)
            )
        except Exception as exc:
            errors.append(f"{sid}: {exc}")

    ok = not errors and all(r.get("ok") for r in results)
    return {
        "ok": ok,
        "dry_run": spec.dry_run,
        "results": results,
        "errors": errors,
        "report": report(results, errors, spec.dry_run),
    }


def report(results: list[dict], errors: list[str], dry_run: bool) -> str:
    """사람이 읽는 요약."""
    lines: list[str] = [
        f"댓글 복구 {len(results)}건" + (" (모의 실행)" if dry_run else "")
    ]
    for r in results:
        head = f"- {r.get('title', '')} / {r.get('cafe_name', '')} / {r.get('source_id')}"
        if r.get("aborted"):
            lines.append(f"{head} → 건너뜀: {r['aborted']}")
            continue
        lines.append(head + f" ({r.get('manuscript_type', '')})")
        for row in r.get("after") or []:
            lines.append(
                f"    {row['idx']:>2} {row['label']:<8}"
                f" {row['account_masked']:<4} rm={row['reply_member_masked']:<4}"
                f" {row['start_at']}"
            )
        if r.get("new_source_id"):
            lines.append(f"  → 새 글 {r['new_source_id']} {r.get('url', '')}")
    for e in errors:
        lines.append(f"- 실패: {e}")
    if dry_run:
        lines.append("실제로 고치려면 명령 끝에 `실제 발행`을 붙이세요.")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_MANUSCRIPT_TYPE",
    "REPAIRED_STAGE",
    "RepairError",
    "build_tree",
    "capture",
    "live_role_problems",
    "manuscript_type_for",
    "plan_rows",
    "rebuild_comments",
    "repair_comments",
    "repair_revision_comments",
    "role_problems",
    "source_ids_in",
]
