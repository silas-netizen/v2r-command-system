"""밀려남 키워드 → 브랜드 원고 대량 생성기 (2026-09-23).

`brand_queue`에서 우선순위 순으로 꺼내 기존 `brand_writer.generate_manuscript` 경로로
원고를 만들고(요금제 길 강제, `config/bulk.yaml`의 `allow_paid_fallback: false`이면
유료 길로 절대 넘어가지 않는다), 검증 + GPT 교차 검증을 통과한 것만
`warehouse/manuscripts/brand-queue/<브랜드>/<키워드>.json`에 저장한다.

요금제 한도(`PlanLimit`)에 걸리면 그 자리에서 **멈추고 대기**한다 — 유료 폴백 금지
(사용자 방침: 0원 우선). 남은 대기열 항목은 `pending`으로 그대로 둔다.

구분자 `target`(사용자 지시 2026-09-23 01:35): `v2r`(우리 실행기 발행분, 하루
50건) / `vpc`(가상 PC로 넘겨 따로 처리할 나머지). 생성량은 발행 50건에 묶지
않는다 — 요청한 만큼 만들고, 유일한 상한은 재고(`ready`) 전체가
`config/bulk.yaml`의 `max_ready`를 넘지 않는 것뿐이다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from v2r.config import load_yaml
from v2r.content import brand_queue

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def _bulk_cfg() -> dict:
    return (load_yaml("bulk") or {}).get("bulk") or {}


def _plan_locked(rt: Any) -> bool:
    llm = getattr(rt, "llm", None)
    if llm is None or not hasattr(llm, "plan_locked_until"):
        return False
    try:
        return llm.plan_locked_until() is not None
    except Exception:
        return False


def _out_dir(rt: Any, brand: str) -> Path:
    return Path(rt.settings.warehouse_dir) / "manuscripts" / "brand-queue" / brand


def generate_for_brand(rt: Any, brand: str, n: int, target: str = "") -> dict:
    """브랜드 하나에 대해 대기열에서 `n`건을 생성한다.

    `target`(`v2r`/`vpc`)을 주면 그 구분자로 채우고 그 구분자 대기열만 쓴다.
    비우면 `brand_queue.refill`의 자동 배정(하루 `v2r_daily_cap`까지는
    v2r, 그다음은 vpc)을 따르고, 어느 target이든 꺼내서 만든다.
    """
    from v2r.content import brand_writer as bw
    from v2r.engine import worker as _worker

    if not brand:
        return {"ok": False, "error": "브랜드를 알 수 없습니다"}
    if rt.llm is None:
        return {"ok": False, "error": "ANTHROPIC_API_KEY가 없어 원고를 생성할 수 없습니다"}

    cfg = _bulk_cfg()
    allow_paid_fallback = bool(cfg.get("allow_paid_fallback", False))
    max_ready = int(cfg.get("max_ready", 0) or 0)
    target = (target or "").strip().lower()

    n = max(0, int(n))
    room = max_ready - brand_queue.ready_count(rt) if max_ready else n
    stock_full = max_ready and room <= 0
    n_to_fill = max(0, min(n, room)) if max_ready else n
    if n_to_fill:
        brand_queue.refill(rt, brand, n_to_fill, target=target)
    todo = brand_queue.pending(rt, brand, limit=n, target=target)
    if not todo:
        msg = f"{brand}: 대기열에 새로 만들 키워드가 없습니다"
        if stock_full:
            msg = f"{brand}: 재고 상한({max_ready}건)에 닿아 더 채우지 않았습니다"
        return {"ok": True, "brand": brand, "generated": 0, "waiting_plan_limit": False,
                 "stock_full": bool(stock_full), "message": msg}

    forced = "" if allow_paid_fallback else "plan"
    previous_force = getattr(rt.llm, "force_backend", "")
    if forced and hasattr(rt.llm, "force_backend"):
        rt.llm.force_backend = forced

    made: list[tuple] = []
    failed: list[dict] = []
    waiting = False
    out_dir = _out_dir(rt, brand)
    closings: list[str] = []
    guide_cache: dict[str, str] = {}
    examples_cache: dict[str, Any] = {}
    recent_openings = bw.load_recent_openings(
        Path(rt.settings.warehouse_dir) / "manuscripts" / "generated"
    )
    xlsx = Path(rt.settings.repo_root) / "data" / f"brand_sheet_{brand}.xlsx"

    try:
        for row in todo:
            if not allow_paid_fallback and _plan_locked(rt):
                # 요금제 한도 — 유료로 넘어가지 않고 여기서 멈춘다 (남은 항목은 pending 유지)
                waiting = True
                break

            mtype = row["mtype"] or ""
            guide = guide_cache.get(mtype)
            if guide is None:
                guide = _worker._brand_guide_text(rt, brand, mtype)
                guide_cache[mtype] = guide
            examples = examples_cache.get(mtype)
            if examples is None:
                examples = bw.load_examples(
                    brand, mtype, str(xlsx) if xlsx.exists() else None
                )
                examples_cache[mtype] = examples

            brand_queue.mark(rt, row["id"], "generating")
            reference_brief = ""
            reference_meta: dict = {}
            try:
                from v2r.content import top_reference

                if str((top_reference._cfg() or {}).get("mode", "")) == "alert":
                    # 2026-09-24 지시: 자동 주입 대신 차이 점수 사전 알림.
                    # 임계 이상 다르면 이 키워드는 hold로 돌리고 원고를
                    # 만들지 않는다(사용자 확인 대기).
                    alert = top_reference.evaluate_alert(rt, brand, row["keyword"])
                    if alert.get("hold"):
                        brand_queue.mark(rt, row["id"], "hold", attempts=int(row["attempts"] or 0))
                        log.info(
                            "형식 알림: hold(%s/%s) 불일치=%s url=%s",
                            brand, row["keyword"], alert.get("gaps"), alert.get("url"),
                        )
                        continue
                else:
                    reference_brief = top_reference.reference_for(
                        rt, brand, row["keyword"], meta=reference_meta
                    )
            except Exception as exc:  # 참고 형식은 덤 — 실패해도 원고 생성은 막지 않는다
                log.warning("참고 형식 조회 실패(%s/%s): %s", brand, row["keyword"], exc)
            relevance_llm, bridge_rationale = brand_queue.bridge_info(
                brand, row["keyword"], rt.settings.data_dir
            )
            try:
                m, stats = _worker.generate_and_crosscheck_one(
                    rt,
                    brand,
                    row["keyword"],
                    "",
                    mtype,
                    guide,
                    examples,
                    recent_openings,
                    closings,
                    reference_brief=reference_brief,
                    relevance=relevance_llm,
                    bridge_rationale=bridge_rationale,
                )
            except Exception as exc:
                from v2r.llm.plan_backend import PlanLimit

                attempts = int(row["attempts"] or 0) + 1
                if isinstance(exc, PlanLimit) and not allow_paid_fallback:
                    brand_queue.mark(rt, row["id"], "pending", attempts=int(row["attempts"] or 0))
                    waiting = True
                    break
                status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                brand_queue.mark(rt, row["id"], status, attempts=attempts)
                failed.append({"keyword": row["keyword"], "error": str(exc)})
                continue

            stats["reference_url"] = reference_meta.get("reference_url", "")
            hard_unresolved = stats.get("unresolved_hard") or []
            out_path = out_dir / f"{row['keyword']}.json"
            bw.save_json(m, out_path, stats)
            if hard_unresolved:
                attempts = int(row["attempts"] or 0) + 1
                status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                brand_queue.mark(rt, row["id"], status, attempts=attempts)
                failed.append(
                    {"keyword": row["keyword"], "error": f"검증 미통과: {hard_unresolved}"}
                )
                continue
            brand_queue.mark(rt, row["id"], "ready", manuscript_path=str(out_path))
            made.append((row, m, stats))
    finally:
        if forced and hasattr(rt.llm, "force_backend"):
            rt.llm.force_backend = previous_force

    tokens = dict(getattr(rt.llm, "usage", {}) or {})
    backends: dict[str, int] = {}
    for _, _m, s in made:
        name = s.get("backend") or ""
        if name:
            backends[name] = backends.get(name, 0) + 1

    return {
        "ok": True,
        "brand": brand,
        "generated": len(made),
        "failed": failed,
        "waiting_plan_limit": waiting,
        "keywords": [r["keyword"] for r, _m, _s in made],
        "backends": backends,
        "tokens": tokens,
        "message": (
            f"{brand} 대량 원고 {len(made)}건 생성"
            + (f", 실패 {len(failed)}건" if failed else "")
            + (" — 요금제 한도라 대기 중입니다 (유료 폴백 없음)" if waiting else "")
        ),
    }


def generate_all(
    rt: Any, n_per_brand: int = 0, brands: list[str] | None = None, target: str = ""
) -> dict:
    """설정된 브랜드 전부를 생성한다.

    `n_per_brand`를 주면(명령에 `N건`을 적은 경우) 브랜드마다 그 건수만큼,
    비우면(0) `config/bulk.yaml`의 `daily_quota`(밀려남 수 비례 하루 기본
    할당, 합 = `v2r_daily_cap`)를 브랜드별로 쓴다.
    """
    cfg = _bulk_cfg()
    brand_list = brands or cfg.get("brands") or []
    quota = cfg.get("daily_quota") or {}
    results = {}
    stopped_on_limit = False
    for brand in brand_list:
        n = n_per_brand or int(quota.get(brand, 0) or 0) or 5
        if stopped_on_limit:
            results[brand] = {"ok": True, "brand": brand, "generated": 0,
                               "skipped": "요금제 한도 대기 중이라 건너뜀"}
            continue
        res = generate_for_brand(rt, brand, n, target=target)
        results[brand] = res
        if res.get("waiting_plan_limit"):
            stopped_on_limit = True
    total = sum(r.get("generated", 0) for r in results.values())
    return {
        "ok": True,
        "brands": results,
        "generated_total": total,
        "waiting_plan_limit": stopped_on_limit,
        "message": f"전체 대량 원고 {total}건 생성"
        + (" — 요금제 한도로 일부 브랜드는 대기 중입니다" if stopped_on_limit else ""),
    }


def status(rt: Any) -> dict:
    """대기열 현황: target별(v2r/vpc) 대기·준비·내보냄 수, 오늘 생성 수, 재고 상한 대비."""
    from datetime import datetime

    from v2r.store.db import KST

    cfg = _bulk_cfg()
    brands = cfg.get("brands") or []
    per_brand = {b: brand_queue.counts(rt, b) for b in brands}
    overall = brand_queue.counts(rt)
    by_target = brand_queue.counts_by_target(rt)

    today = datetime.now(KST).strftime("%Y-%m-%d")
    today_ready = rt.conn.execute(
        "SELECT COUNT(*) AS n FROM brand_queue WHERE status = 'ready' AND updated_at >= ?",
        (today,),
    ).fetchone()["n"]

    max_ready = int(cfg.get("max_ready", 0) or 0)
    ready_total = overall.get("ready", 0)
    room = max(0, max_ready - ready_total) if max_ready else 0

    return {
        "ok": True,
        "pending": overall.get("pending", 0),
        "generating": overall.get("generating", 0),
        "ready": ready_total,
        "failed": overall.get("failed", 0),
        "published": overall.get("published", 0),
        "exported": overall.get("exported", 0),
        "today_generated": today_ready,
        "per_brand": per_brand,
        "by_target": by_target,
        "max_ready": max_ready,
        "room_to_max_ready": room,
        "message": (
            f"대량 원고 대기열 — 대기 {overall.get('pending', 0)}건, "
            f"준비완료 {ready_total}건, 실패 {overall.get('failed', 0)}건, "
            f"내보냄 {overall.get('exported', 0)}건, 오늘 생성 {today_ready}건"
            + (f" (재고 상한 {max_ready}건까지 {room}건 여유)" if max_ready else "")
            + " — "
            + " / ".join(
                f"{t}: 대기 {c.get('pending', 0)}·준비 {c.get('ready', 0)}·"
                f"내보냄 {c.get('exported', 0)}"
                for t, c in sorted(by_target.items())
            )
        ),
    }


def export_vpc(rt: Any, out_dir: str | Path | None = None) -> dict:
    """`target='vpc'`인 `ready` 원고를 가상 PC용 두 산출물로 내보낸다.

    (인수인계 형식 확정, 2026-09-23) 사용자 인수인계 문서(§1·§2)의 형식대로:

    1. 브랜드별 `data/export/<브랜드>_YYMMDD.xlsx`(시트 이름 = 키워드,
       `게시글 쓰기 원본` 탭과 같은 열 구성 — A 키워드/B 본문(12개 댓글
       블록)/D 작성계정/E 원고유형/F 완료 링크).
    2. 통합 댓글 프로그램용 `data/export/댓글_YYMMDD.xlsx`(게시글마다 시트,
       시트명 = 게시글번호, 완료 링크가 없으면 키워드) +
       `data/export/댓글_YYMMDD_csv/<이름>.csv`(UTF-8 BOM, CRLF). 열 3개
       고정(작성자 구분/탐지할 댓글/댓글 내용), 행 순서와 계정 배정표는
       인수인계 §2 그대로.

    두 산출물이 모두 만들어진 행만 대기열 상태를 `exported`로 바꾼다.
    """
    from v2r.content import vpc_export

    rows = brand_queue.ready_rows_for_export(rt, "vpc")
    if not rows:
        return {"ok": True, "exported": 0, "message": "내보낼 가상 PC 원고가 없습니다"}

    out_root = Path(out_dir) if out_dir else Path(rt.settings.repo_root) / "data" / "export"
    brand_files = vpc_export.export_brand_sheets(rt, rows, out_root)
    comment_out = vpc_export.export_comment_program(rt, rows, out_root)

    excluded_keywords = set(comment_out["excluded"])
    exported_ids = [
        row["id"]
        for row in rows
        if row["brand"] in brand_files and row["keyword"] not in excluded_keywords
    ]
    for row_id in exported_ids:
        brand_queue.mark(rt, row_id, "exported")

    return {
        "ok": True,
        "exported": len(exported_ids),
        "excluded": comment_out["excluded"],
        "no_link": comment_out["no_link"],
        "brand_files": brand_files,
        "comment_program_xlsx": comment_out["path"],
        "comment_program_csv_dir": comment_out["csv_dir"],
        "message": (
            f"가상 PC로 {len(exported_ids)}건 내보냈습니다"
            + (f", 자리표시 댓글로 제외 {len(comment_out['excluded'])}건" if comment_out["excluded"] else "")
            + (f" — 완료 링크 없음(시트 이름=키워드) {len(comment_out['no_link'])}건" if comment_out["no_link"] else "")
        ),
    }


__all__ = ["generate_for_brand", "generate_all", "status", "export_vpc"]
