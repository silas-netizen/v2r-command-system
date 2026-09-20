"""작업 실행기: 명령 접수 → 큐 → 작업 실행 → 채널 보고 (DESIGN §5)."""

from __future__ import annotations

import hashlib
import itertools
import logging
import os
import random
import re
import socket
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from v2r.channels import format_report, notify_all
from v2r.command.parser import describe_spec, parse_korean_command
from v2r.command.spec import ALLOWED_TASKS, TaskSpec, today_kst
from v2r.engine import publish as publish_mod
from v2r.engine import reconcile as reconcile_mod
from v2r.engine import status as status_mod
from v2r.engine.context import Runtime
from v2r.api.auth import MAINTAIN_TICK_S
from v2r.engine.publish import KST

log = logging.getLogger(__name__)

PUBLISH_TASKS = {"publish_daily", "publish_brand", "publish_info", "publish_batch"}
DEFAULT_WASH_COUNT = 10
#: `사진 세탁`에서 원본 1장당 만들 세탁본 수 (계획 2026-09-19)
DEFAULT_PER_ORIGINAL = 3
#: 한 번의 세탁 작업에서 새로 만들 수 있는 변형 수 상한(안전장치)
DEFAULT_MAX_NEW = 3000
_URL_RE = re.compile(r"https?://\S+")
_COUNTER = itertools.count(1)


def _nonce() -> str:
    """같은 초 안에서도 겹치지 않는 값."""
    return f"{time.monotonic_ns()}-{next(_COUNTER)}-{uuid.uuid4().hex}"


def idem_key(text: str, task: str | None = None) -> str:
    """멱등 키.

    발행 작업은 "같은 날 같은 문장"을 한 번만 등록한다(중복 발행 방지).
    조회·점검·중지 같은 작업은 언제든 다시 실행할 수 있어야 하므로 시각을 섞는다.
    """
    if task is not None and task not in PUBLISH_TASKS:
        from v2r.store.db import now_iso

        seed = f"{text}|{now_iso()}|{_nonce()}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return hashlib.sha256((text + today_kst()).encode("utf-8")).hexdigest()


def _enqueue_key(rt: Runtime, text: str, task: str) -> str:
    """발행 작업이 실패·취소로 끝났으면 다시 등록할 수 있게 키를 새로 만든다."""
    key = idem_key(text, task)
    if task not in PUBLISH_TASKS:
        return key
    if rt.jobs.has_open_job(key):
        return key  # 대기·실행 중이면 그대로 재사용(중복 등록 방지)
    previous = rt.jobs.find_by_idem(key)
    if previous is not None and previous.get("status") in ("failed", "cancelled"):
        from v2r.store.db import now_iso

        return hashlib.sha256(f"{key}|{now_iso()}|{_nonce()}".encode("utf-8")).hexdigest()
    return key


# --------------------------------------------------------------------
# 중지 플래그 (큐를 거치지 않는 즉시 중지, M-9)
# --------------------------------------------------------------------
STOP_FLAG_NAME = "STOP"


def stop_flag_path(rt: Runtime):
    """중지 플래그 파일 경로(data/STOP)."""
    return rt.settings.data_dir / STOP_FLAG_NAME


def request_stop(rt: Runtime) -> None:
    """중지 요청 기록."""
    rt.scratch["stop_requested"] = True
    try:
        path = stop_flag_path(rt)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stop", encoding="utf-8")
    except Exception as exc:  # 파일을 못 써도 메모리 플래그로 동작
        log.warning("중지 플래그 기록 실패: %s", exc)


def stop_requested(rt: Runtime) -> bool:
    """중지 요청이 있는가."""
    if rt.scratch.get("stop_requested"):
        return True
    try:
        return stop_flag_path(rt).exists()
    except Exception:
        return False


def clear_stop(rt: Runtime) -> None:
    """중지 플래그 해제(새 발행 작업 시작 시)."""
    rt.scratch.pop("stop_requested", None)
    try:
        path = stop_flag_path(rt)
        if path.exists():
            path.unlink()
    except Exception:
        pass


def default_owner() -> str:
    """실행기 소유자 이름 — `호스트이름:프로세스번호`.

    호스트 이름만 쓰면 같은 PC 의 **두 번째 실행기가 같은 리스를 자기 것으로 본다**
    (장애 2026-09-20 B: publish_daily 65·67 동시 실행). 프로세스 번호를 붙여
    서로 다른 실행기임이 드러나게 한다.
    """
    try:
        host = socket.gethostname()
    except Exception:
        host = "local"
    return f"{host}:{os.getpid()}"


#: 다른 실행기의 발행이 끝나기를 기다리는 최대 시간(초). 넘으면 경고만 남기고 간다.
PARALLEL_WAIT_MAX_S = 2 * 60 * 60
#: 기다리는 동안 다시 확인하는 간격(초) — 그 사이 내 리스도 연장한다
PARALLEL_POLL_S = 30


def wait_for_other_publish(
    rt: Runtime,
    job_id: int | None,
    owner: str | None,
    *,
    max_wait_s: float = PARALLEL_WAIT_MAX_S,
    sleep: Any = None,
    now_fn: Any = None,
) -> dict:
    """다른 실행기가 발행 중이면 끝날 때까지 기다린다(리스는 계속 연장).

    리스 이름(`호스트:pid`)과 `data/serve.lock` 으로 이미 막지만,
    그래도 **두 줄로 동시에 올리는 일**만은 없어야 하므로 한 겹 더 둔다.
    """
    sleep = sleep or time.sleep
    now_fn = now_fn or time.monotonic
    started = now_fn()
    waited = 0.0
    notified = False
    while True:
        try:
            holder = None
            for other in rt.jobs.running_jobs(exclude_id=job_id, task_prefix="publish_"):
                holder = rt.jobs.live_lease_holder(other, owner or "")
                if holder:
                    break
        except Exception as exc:  # noqa: BLE001 - 조회 실패로 발행을 막지 않는다
            log.warning("동시 발행 점검 실패(그냥 진행): %s", exc)
            return {"waited_s": waited, "holder": None, "timed_out": False}
        if holder is None:
            if notified:
                rt.events.log(job_id, "info", f"다른 실행기 발행이 끝나 이어서 시작합니다({int(waited)}초 대기)")
            return {"waited_s": waited, "holder": None, "timed_out": False}
        if not notified:
            notified = True
            rt.events.log(
                job_id, "warn", f"다른 실행기({holder})가 발행 중이라 기다립니다 — 동시 발행 금지"
            )
        if waited >= max_wait_s:
            rt.events.log(
                job_id, "error", f"다른 실행기({holder}) 발행이 {int(waited)}초째 끝나지 않습니다"
            )
            return {"waited_s": waited, "holder": holder, "timed_out": True}
        if owner and job_id is not None:
            try:
                rt.jobs.heartbeat(job_id, owner)  # 기다리는 동안 내 리스를 놓치지 않는다
            except Exception as exc:  # noqa: BLE001
                log.warning("대기 중 리스 연장 실패: %s", exc)
        sleep(PARALLEL_POLL_S)
        waited = now_fn() - started


# --------------------------------------------------------------------
# 명령 접수
# --------------------------------------------------------------------
def handle_text(rt: Runtime, text: str, *, via_channel: bool = False) -> dict:
    """한국어 명령 → 작업 큐 등록."""
    spec = parse_korean_command(text)
    if spec is None and not via_channel and rt.llm is not None:
        from v2r.command.llm_fallback import interpret_with_llm

        try:
            spec = interpret_with_llm(text, rt.llm)
        except Exception as exc:
            log.info("모델 폴백 실패: %s", exc)
            spec = None
    if spec is None:
        return {"ok": False, "error": "명령을 해석하지 못했습니다", "description": ""}

    description = describe_spec(spec)
    if spec.task == "stop":
        # 중지는 큐에 넣지 않고 즉시 처리한다 (M-9)
        request_stop(rt)
        cancelled = rt.jobs.cancel_open(default_owner())
        rt.events.log(None, "warn", f"중지 요청: 진행 중 작업 {cancelled}건 취소")
        notify_all(rt.channels, f"중지 요청을 받았습니다 (작업 {cancelled}건 취소)")
        return {
            "ok": True,
            "job_id": None,
            "stopped": True,
            "cancelled": cancelled,
            "description": description,
            "message": f"중지 요청 처리: 작업 {cancelled}건 취소",
        }

    # 새 명령이 들어오면 이전 '중지' 플래그는 해제한다 (남아 있으면 실행기가 영영 작업을 안 잡는다 — 2026-09-19 실측)
    try:
        stop_flag_path(rt).unlink(missing_ok=True)
        rt.scratch.pop("stop_requested", None)
    except Exception:
        pass
    job_id = rt.jobs.enqueue(spec, _enqueue_key(rt, text, spec.task))
    rt.events.log(job_id, "info", f"명령 접수: {description}")
    return {"ok": True, "job_id": job_id, "description": description}


# --------------------------------------------------------------------
# 개별 작업
# --------------------------------------------------------------------
def _sync_entries(rt: Runtime, spec: TaskSpec, all_kinds: bool) -> dict:
    entries = [e for e in (rt.sources_cfg.get("sources") or []) if isinstance(e, dict)]
    # 브랜드 원고 시트 + 인박스 각색 xlsx도 함께 동기화한다
    names = {publish_mod._norm(e.get("name", "")) for e in entries}
    for extra in publish_mod.brand_sheet_entries(rt) + publish_mod.discovered_xlsx_entries(rt):
        if publish_mod._norm(extra.get("name", "")) not in names:
            names.add(publish_mod._norm(extra.get("name", "")))
            entries.append(extra)
    if not all_kinds:
        entries = [
            e
            for e in entries
            if (e.get("kind") or "sheet")
            in ("sheet", "brand", publish_mod.XLSX_DAILY_KIND, "daily_pool")
        ]
    if spec.source:
        entries = [
            e
            for e in entries
            if publish_mod._norm(spec.source) in publish_mod._norm(e.get("name", ""))
        ]
    result: dict[str, Any] = {"synced": [], "errors": []}
    for entry in entries:
        name = str(entry.get("name") or "")
        try:
            n = publish_mod.refresh_source(rt, entry)
            result["synced"].append({"name": name, "rows": n})
        except Exception as exc:
            result["errors"].append(f"{name}: {exc}")
    return result


def _adapter_for(rt: Runtime):
    """일상 글 각색 어댑터(모델이 없으면 None)."""
    router = rt.llm
    if router is None:
        return None
    from v2r.llm.prompts import DAILY_ADAPT_SYSTEM

    def adapt_one(item: dict) -> dict:
        text = (item.get("text") or "")[:6000]
        data = router.complete_json(
            "daily_adapt", DAILY_ADAPT_SYSTEM, f"제목: {item.get('title', '')}\n\n{text}"
        )
        if isinstance(data, list):
            data = data[0] if data else {}
        out = dict(item)
        if isinstance(data, dict):
            out["title"] = data.get("title") or out.get("title", "")
            out["text"] = data.get("body") or out.get("text", "")
        return out

    return adapt_one


def _collect_daily(rt: Runtime, spec: TaskSpec) -> dict:
    from v2r.warehouse import daily_collector

    urls = _URL_RE.findall(spec.notes or "")
    if not urls:
        urls = [str(u) for u in (rt.sources_cfg.get("daily_urls") or [])]
    if not urls:
        return {"ok": False, "error": "수집할 공개 주소가 없습니다 (명령에 URL을 넣거나 sources.yaml daily_urls 설정)"}
    if spec.count:
        urls = urls[: spec.count]
    items = daily_collector.collect(urls)
    items = daily_collector.adapt(items, _adapter_for(rt))
    saved = daily_collector.save_to_warehouse(rt.warehouse, items)
    return {"ok": True, "collected": len(items), "saved": len(saved)}


def _collect_photos(rt: Runtime, spec: TaskSpec) -> dict:
    from v2r.warehouse.photo_collector import collect_from_folder, import_inbox

    inbox = rt.settings.warehouse_dir / "inbox"
    if not spec.brand and not spec.source:
        # 브랜드 지정이 없으면 인박스 전체를 브랜드 폴더 규칙대로 가져온다
        stats = import_inbox(rt.warehouse)
        return {"ok": not stats["errors"], "inbox": str(inbox), **stats}
    brand = spec.brand or "공용"
    folder = spec.source or "기본"
    stats = collect_from_folder(inbox, brand, folder, rt.warehouse)
    return {"ok": not stats["errors"], "inbox": str(inbox), **stats}


def _affiliate_cafes(rt: Runtime) -> list[str]:
    """설정의 제휴 카페 이름 목록 (씨씨앙·양평맘·쌍둥이맘)."""
    out: list[str] = []
    for entry in (rt.cafes_cfg or {}).get("affiliate") or []:
        name = (entry or {}).get("name") if isinstance(entry, dict) else None
        if name:
            out.append(str(name))
    return out


def _generate_affiliate_daily(rt: Runtime, spec: TaskSpec) -> dict:
    """제휴 카페 일상 글을 ChatGPT 웹 세션으로 만든다 (API 토큰 0)."""
    from v2r.warehouse import daily_generator

    cafes = [spec.cafe] if spec.cafe else _affiliate_cafes(rt)
    if not cafes:
        return {"ok": False, "error": "제휴 카페를 찾을 수 없습니다 (cafes 설정을 확인하세요)"}
    count = spec.count or daily_generator.AFFILIATE_BATCH_SIZE
    per_cafe = max(1, -(-count // max(len(cafes), 1)))  # 올림 나눗셈
    out = daily_generator.generate_affiliate_pool_via_gpt(
        cafes, per_cafe, warehouse_dir=rt.warehouse.root
    )
    if out.get("login_pending"):
        notify_all(rt.channels, f"ChatGPT {out['message']}")
    elif out.get("added"):
        notify_all(
            rt.channels,
            f"제휴 일상 글 {out['added']}건 생성 (카페 {', '.join(out['cafes'])})",
        )
    out["pool_total"] = len(
        daily_generator.load_pool(rt.warehouse.root, daily_generator.AFFILIATE_POOL_FILENAME)
    )
    return out


#: 브랜드 지침 파일 이름에 들어가는 브랜드 표시 (지침 폴더에서 원문을 찾을 때 쓴다)
BRAND_GUIDE_DIR = "★NEW 카페 바이럴★"


def _brand_guide_text(rt: Runtime, brand: str, manuscript_type: str = "") -> str:
    """브랜드 지침 원문. 못 찾으면 빈 문자열."""
    root = Path(rt.warehouse.guides_dir) / BRAND_GUIDE_DIR
    if not root.exists():
        return ""
    want_review = (manuscript_type or "").strip() == "후기형"
    best = ""
    for path in sorted(root.glob("*.md")):
        if brand not in path.name:
            continue
        is_review = "후기형" in path.name
        if want_review != is_review and best:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if want_review == is_review:
            return text
        best = best or text
    return best


def _existing_brand_keywords(rt: Runtime, brand: str) -> set[str]:
    """이미 원고가 있는 키워드 (시트 캐시 + 생성 폴더)."""
    import re as _re

    from v2r.sources.sheets import parse_affiliate_rows

    out: set[str] = set()
    cached = rt.sources_cache.get(brand)
    rows = (cached or {}).get("rows") if isinstance(cached, dict) else None
    if rows:
        for m in parse_affiliate_rows(rows, source=brand):
            if m.keyword:
                out.add(_re.sub(r"\s+", "", m.keyword))
    folder = Path(rt.settings.warehouse_dir) / "manuscripts" / "generated" / brand
    if folder.exists():
        for path in folder.glob("*.json"):
            out.add(_re.sub(r"\s+", "", path.stem))
    return out


def _generate_brand(rt: Runtime, spec: TaskSpec) -> dict:
    """브랜드 시트 `노출 현황`의 `밀려남` 키워드로 새 원고를 만든다 (발행·시트 쓰기 없음)."""
    import re as _re

    from v2r.content import brand_writer as bw
    from v2r.llm.router import estimate_cost
    from v2r.sources.keyword_list import load_pushed_keywords

    brand = (spec.brand or "").strip()
    if not brand:
        return {"ok": False, "error": "브랜드를 알 수 없습니다 (예: `우아덤 원고 1개 만들어줘`)"}
    if rt.llm is None:
        return {"ok": False, "error": "ANTHROPIC_API_KEY가 없어 원고를 생성할 수 없습니다"}

    count = spec.count or 1
    xlsx = Path(rt.settings.repo_root) / "data" / f"brand_sheet_{brand}.xlsx"
    try:
        pool = load_pushed_keywords(
            brand, rt.sources_cfg, xlsx_path=str(xlsx) if xlsx.exists() else None
        )
    except Exception as exc:
        return {"ok": False, "error": f"키워드 목록을 읽지 못했습니다: {exc}"}

    done = _existing_brand_keywords(rt, brand)
    todo = [p for p in pool if _re.sub(r"\s+", "", p["keyword"]) not in done][:count]
    if not todo:
        return {"ok": False, "error": f"{brand}: 새로 쓸 `밀려남` 키워드가 없습니다"}

    guide = _brand_guide_text(rt, brand, spec.manuscript_type)
    made: list[Any] = []
    failed: list[dict] = []
    unresolved: list[dict] = []
    out_dir = Path(rt.settings.warehouse_dir) / "manuscripts" / "generated" / brand
    for item in todo:
        stats: dict = {}
        try:
            m = bw.generate_manuscript(
                rt,
                brand,
                item["keyword"],
                item.get("cafe", ""),
                spec.manuscript_type,
                guide_text=guide,
                stats=stats,
                mode=(spec.generate_mode or "").strip(),
            )
        except Exception as exc:
            failed.append({"keyword": item["keyword"], "error": str(exc)})
            continue
        bw.save_json(m, out_dir / f"{item['keyword']}.json", stats)
        if stats.get("unresolved"):
            # 포기하지 않고 최대 횟수까지 다시 시켰는데도 남은 규칙 (사용자 지시 2026-09-19)
            unresolved.append(
                {
                    "keyword": item["keyword"],
                    "attempts": stats.get("attempts", 0),
                    "rules": stats["unresolved"],
                }
            )
        made.append((m, stats))

    tokens = dict(getattr(rt.llm, "usage", {}) or {})
    report = (
        Path(rt.settings.repo_root)
        / "docs"
        / "reports"
        / f"brand-draft-{brand}-{spec.start_date}.md"
    )
    if made:
        bw.write_review_md(
            [m for m, _ in made],
            report,
            title=f"브랜드 원고 초안 — {brand} ({spec.start_date})",
        )
    return {
        "ok": bool(made),
        "brand": brand,
        "generated": len(made),
        "failed": failed,
        "unresolved": unresolved,
        "attempts": {m.keyword: s.get("attempts", 0) for m, s in made},
        "keywords": [m.keyword for m, _ in made],
        "report": str(report) if made else "",
        "message": (
            f"{brand} 원고 {len(made)}건을 만들었습니다. 검토용 문서: {report}"
            if made
            else f"{brand} 원고를 만들지 못했습니다"
        ),
        "tokens": tokens,
        # 2026-09 기준 추정 단가로 어림한 값이다 (실제 요금표 확인 필요)
        "estimated_usd": estimate_cost(tokens),
        "mode": (spec.generate_mode or bw.DEFAULT_MODE),
    }


def _generate_daily(rt: Runtime, spec: TaskSpec) -> dict:
    """짧은 일상 글을 만들어 창고 풀에 쌓는다 (결정 2)."""
    from v2r.warehouse import daily_generator

    if rt.llm is None:
        return {"ok": False, "error": "ANTHROPIC_API_KEY가 없어 일상 글을 생성할 수 없습니다"}
    count = spec.count or daily_generator.BATCH_SIZE
    cafes = [spec.cafe] if spec.cafe else _daily_target_cafes(rt)
    per_cafe = max(1, -(-count // max(len(cafes), 1)))  # 올림 나눗셈
    try:
        items = daily_generator.generate_daily_pool(
            rt.llm, cafes, per_cafe, rt.warehouse.guides_dir
        )
    except Exception as exc:
        return {"ok": False, "error": f"일상 글 생성 실패: {exc}"}
    items = items[:count] if count else items
    added = daily_generator.save_pool(rt.settings.warehouse_dir, items)
    total = len(daily_generator.load_pool(rt.settings.warehouse_dir))
    return {
        "ok": bool(items),
        "generated": len(items),
        "added": added,
        "pool_total": total,
        "cafes": cafes,
        "pool_file": str(daily_generator.pool_path(rt.settings.warehouse_dir)),
        "model": rt.llm.model_for("daily_adapt") if hasattr(rt.llm, "model_for") else "",
        "tokens": dict(getattr(rt.llm, "usage", {}) or {}),
        "samples": [
            {"title": m.title, "body": m.body, "cafe": m.cafe} for m in items[:5]
        ],
    }


def _daily_target_cafes(rt: Runtime) -> list[str]:
    """일상 글을 쓸 카페 목록 (제휴 + 자사, 테스트 카페 제외)."""
    out: list[str] = []
    for group in ("affiliate", "self_owned"):
        for entry in rt.cafes_cfg.get(group) or []:
            name = str((entry or {}).get("name") or "").strip()
            if name and name not in out:
                out.append(name)
    return out or ["고요한 아침"]


def _wash_photos(rt: Runtime, spec: TaskSpec) -> dict:
    """`images/originals/<브랜드>/<폴더>`를 전부 돌며 원본당 세탁본 수를 맞춘다.

    - `사진 세탁 3장씩` → 원본 1장당 세탁본 3장 (`per_original`, 기본 3)
    - `팥순이 사진 세탁` → 그 브랜드만
    - 이미 충분한 원본은 건너뛴다(재실행 가능: 멈춘 지점부터 이어서 채운다)
    - `max_new`장을 만들면 안전하게 멈춘다(기본 3000)
    """
    from v2r.warehouse import stock
    from v2r.warehouse.photo_washer import make_variants
    from v2r.warehouse.store import _squash

    per_original = spec.count or DEFAULT_PER_ORIGINAL
    max_new = int(rt.scratch.get("wash_max_new") or DEFAULT_MAX_NEW)
    wh = rt.warehouse
    wh.ensure_dirs()

    brand_key = _squash(spec.brand) if spec.brand else ""
    folders = [
        (brand, folder, files)
        for brand, folder, files in stock.iter_folders(wh)
        if not brand_key or _squash(brand) == brand_key
    ]

    started = time.monotonic()
    made = seen = skipped = 0
    errors: list[str] = []
    report: list[dict] = []
    stopped = False

    for brand, folder, originals in folders:
        created = total = 0
        for original in originals:
            seen += 1
            if seen % 100 == 0:
                log.info(
                    "사진 세탁 진행: 원본 %d장 확인, 변형 %d장 생성 (%.0f초)",
                    seen,
                    made,
                    time.monotonic() - started,
                )
            try:
                sha = wh.sha256(original)
            except OSError as exc:
                errors.append(f"{brand}/{folder}/{original.name}: {exc}")
                continue
            have = len(wh.washed_variants(sha))
            if have >= per_original:
                skipped += 1
                total += have
                continue
            if made >= max_new:
                stopped = True
                total += have
                continue
            need = min(per_original - have, max_new - made)
            try:
                new = make_variants(original, need, wh.washed_folder(sha))
            except Exception as exc:
                errors.append(f"{brand}/{folder}/{original.name}: {exc}")
                total += have
                continue
            created += len(new)
            made += len(new)
            total += have + len(new)
        report.append(
            {
                "brand": brand,
                "folder": folder,
                "originals": len(originals),
                "created": created,
                "variants": total,
            }
        )
        if stopped:
            break

    out = {
        "ok": not errors,
        "per_original": per_original,
        "originals": seen,
        "variants": made,
        "skipped": skipped,
        "folders": report,
        "elapsed_sec": round(time.monotonic() - started, 1),
        "errors": errors,
    }
    if stopped:
        out["stopped"] = True
        out["message"] = (
            f"안전 한도 {max_new}장에 도달해 멈췄습니다. 같은 명령을 다시 실행하면 이어서 채웁니다."
        )
    return out


def _request_photos(rt: Runtime, spec: TaskSpec) -> dict:
    from v2r.warehouse.photo_request import request_photos

    brand = spec.brand or ""
    if not brand:
        return {"ok": False, "error": "사진을 요청할 브랜드를 알 수 없습니다 (`브랜드 X`를 넣어 주세요)"}
    return request_photos(rt, brand, getattr(spec, "keyword", "") or spec.source or "")


def _photo_stock(rt: Runtime, brand: str, folder: str) -> dict:
    """브랜드/폴더의 사진 재고 (원본·세탁본·미사용). 없으면 0."""
    from v2r.warehouse import stock
    from v2r.warehouse.store import _squash

    try:
        used = stock.used_variants(rt.conn)
    except Exception:  # pragma: no cover - DB가 없을 때
        used = set()
    rows = stock.inventory(rt.warehouse, used)
    want_brand, want_folder = _squash(brand), _squash(folder)
    for row in rows:
        if _squash(row["brand"]) != want_brand:
            continue
        if want_folder and _squash(row["folder"]) != want_folder:
            continue
        return row
    return {"brand": brand, "folder": folder, "originals": 0, "variants": 0, "used": 0, "unused": 0}


def photo_approval_command(brand: str, folder: str, count: int) -> str:
    """사진 생성 승인 명령 문구 (부족 알림·재고 보고에서 같은 문장을 쓴다)."""
    return f"사진 생성 승인 {brand} {folder} {max(int(count or 1), 1)}장"


def _generate_photos(rt: Runtime, spec: TaskSpec) -> dict:
    """사진 생성. **승인(`사진 생성 승인 …`)이 있어야만** 실제로 만든다.

    승인이 없으면 만들지 않고 그 폴더의 재고만 알려 준다 (사용자 규칙 2026-09-19).
    승인이 있으면 `collect=False`로 만들어 인박스에 둔 채 사진 승인을 다시 받는다.
    """
    from v2r.channels import notify_photo_all
    from v2r.warehouse.gpt_images import generate_batch
    from v2r.warehouse.store import KEYWORD_FOLDER

    brand = spec.brand or ""
    if not brand:
        return {"ok": False, "error": "사진을 만들 브랜드를 알 수 없습니다 (`브랜드 X`를 넣어 주세요)"}
    keyword = getattr(spec, "keyword", "") or spec.source or ""
    folder = keyword or KEYWORD_FOLDER
    count = spec.count or 1

    if not getattr(spec, "approved", False):
        # 승인 전 — 만들지 않는다. 재고만 알려 주고 승인 명령을 안내한다.
        row = _photo_stock(rt, brand, folder)
        lines = [
            f"사진 재고: 브랜드 {brand} / {folder} 폴더 — 원본 {row['originals']}장,"
            f" 세탁본 {row['variants']}장, 미사용 세탁본 {row['unused']}장"
        ]
        enough = int(row["unused"]) >= count
        if enough:
            lines.append(f"필요 {count}장은 지금 재고로 충분합니다. 생성하지 않았습니다.")
        else:
            lines.append(
                f"필요 {count}장보다 모자랍니다. 생성하려면"
                f" '{photo_approval_command(brand, folder, count)}' 이라고 보내세요."
            )
        message = "\n".join(lines)
        notify_all(rt.channels, message)
        return {
            "ok": True,
            "approved": False,
            "generated": 0,
            "brand": brand,
            "keyword": folder,
            "stock": row,
            "enough": enough,
            "message": message,
        }

    out = generate_batch(brand, folder, count, warehouse=rt.warehouse, collect=False)
    files = [str(p) for p in (out.get("files") or [])]
    if files:
        # 세탁·적재 전이다. 사용자에게 사진을 보여 주고 승인/반려를 받는다.
        brand_name = out.get("brand") or brand
        folder_name = out.get("keyword") or folder
        photo_sent = 0
        for index, path in enumerate(files, start=1):
            caption = (
                f"{brand_name}/{folder_name} {index}/{len(files)}"
                f" — 승인: '사진 승인 {brand_name} {folder_name}'"
                f" / 반려: '사진 반려 {brand_name} {folder_name}'"
            )
            sent_one = notify_photo_all(rt.channels, path, caption)
            if not sent_one:
                # 사진 전송이 안 되는 채널뿐이면 경로만 글로 알린다
                notify_all(rt.channels, f"{caption}\n{path}")
            photo_sent += sent_one
        out["photo_sent"] = photo_sent
        out["pending_approval"] = True
        out["message"] = (
            f"사진 {len(files)}장을 만들었습니다 (아직 세탁·적재 전)."
            f" 승인: '사진 승인 {brand_name} {folder_name}'"
            f" / 반려: '사진 반려 {brand_name} {folder_name}'"
        )
    if out.get("login_pending"):
        notify_all(rt.channels, f"ChatGPT {out['message']}")
    elif out.get("limited"):
        notify_all(rt.channels, f"ChatGPT 이미지 사용 한도: {out.get('wait_text', '')}")
    elif out.get("generated"):
        notify_all(
            rt.channels,
            out.get("message")
            or f"GPT 사진 {out['generated']}장 생성 (브랜드 {out['brand']} / {out['keyword']})",
        )
    return out


def _approve_photos(rt: Runtime, spec: TaskSpec) -> dict:
    """`사진 승인 <브랜드> <키워드>` — 그 인박스 폴더만 적재 + 세탁한다."""
    from v2r.warehouse.photo_request import collect_new
    from v2r.warehouse.store import KEYWORD_FOLDER

    brand = spec.brand or ""
    if not brand:
        return {"ok": False, "error": "승인할 브랜드를 알 수 없습니다 (`사진 승인 우아덤 키워드`)"}
    folder = getattr(spec, "keyword", "") or KEYWORD_FOLDER
    stats = collect_new(rt.warehouse, brand=brand, keyword=folder)
    stats["brand"] = brand
    stats["folder"] = folder
    stats["message"] = (
        f"사진 승인: 브랜드 {brand} / {folder} 폴더 — 원본 {stats.get('added', 0)}장 적재,"
        f" 세탁본 {stats.get('variants', 0)}장 생성"
    )
    notify_all(rt.channels, stats["message"])
    return stats


def _reject_photos(rt: Runtime, spec: TaskSpec) -> dict:
    """`사진 반려 <브랜드> <키워드>` — 그 인박스 폴더의 사진만 지운다."""
    from v2r.warehouse.photo_request import reject_new
    from v2r.warehouse.store import KEYWORD_FOLDER

    brand = spec.brand or ""
    if not brand:
        return {"ok": False, "error": "반려할 브랜드를 알 수 없습니다 (`사진 반려 우아덤 키워드`)"}
    folder = getattr(spec, "keyword", "") or KEYWORD_FOLDER
    out = reject_new(rt.warehouse, brand=brand, keyword=folder)
    out["message"] = (
        f"사진 반려: 브랜드 {out.get('brand') or brand} / {out.get('folder') or folder} 폴더 —"
        f" {out.get('removed', 0)}장 삭제 (원본·세탁본은 그대로)"
    )
    notify_all(rt.channels, out["message"])
    return out


def _gpt_keepalive(rt: Runtime, spec: TaskSpec) -> dict:
    """ChatGPT 로그인 세션이 살아 있는지 점검하고, 풀렸으면 알린다."""
    from v2r.warehouse.gpt_images import RELOGIN_NOTICE, check_gpt_session

    del spec
    out = check_gpt_session()
    if not out.get("logged_in"):
        notify_all(rt.channels, RELOGIN_NOTICE)
        out["notified"] = True
    return out


#: `NoPhotoError` 메시지에서 브랜드·폴더를 뽑는다 (`store.ensure_keyword_pool` 문구)
_NO_PHOTO_RE = re.compile(r"브랜드\s+(\S+?)의\s+'([^']*)'\s*폴더")


def _request_missing_photos(
    rt: Runtime, job_id: int | None, spec: TaskSpec, exc: Exception
) -> None:
    """사진이 없어 발행이 막혔을 때 새 사진 생성 요청서를 보낸다 (실패해도 삼킨다)."""
    brand = spec.brand or ""
    keyword = getattr(spec, "keyword", "") or ""
    m = _NO_PHOTO_RE.search(str(exc))
    if m:
        brand = brand or m.group(1)
        keyword = keyword or m.group(2)
    if not brand:
        return
    try:
        from v2r.warehouse.photo_request import request_photos

        out = request_photos(rt, brand, keyword)
        rt.events.log(
            job_id, "info", f"사진 요청서 발송: 브랜드 {out['brand']} / {out['keyword']}"
        )
    except Exception as err:  # pragma: no cover - 알림 실패는 발행 결과를 바꾸지 않는다
        log.warning("사진 요청서 발송 실패: %s", err)


def _collect_new_photos(rt: Runtime, spec: TaskSpec) -> dict:
    from v2r.warehouse.photo_request import collect_new

    del spec
    stats = collect_new(rt.warehouse)
    if stats.get("added") or stats.get("variants"):
        notify_all(
            rt.channels,
            f"새 사진 {stats['added']}장 확보, 세탁본 {stats['variants']}장 생성",
        )
    return stats


#: 미처리(대기) 목록 파일 — 사람이 손으로 관리하고, 예약이 하루 5번 파일로 보낸다
PENDING_REPORT_PATH = ("docs", "reports", "pending.md")


def pending_report_path(rt: Runtime):
    """미처리 목록 파일 경로."""
    return rt.settings.repo_root.joinpath(*PENDING_REPORT_PATH)


def _pending_report(rt: Runtime) -> dict:
    """미처리 목록 파일을 텔레그램에 **파일로** 보낸다."""
    from v2r.channels import notify_document_all

    path = pending_report_path(rt)
    stamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    if not path.is_file():
        message = f"미처리 목록 파일이 없습니다: {path.name}"
        notify_all(rt.channels, message)
        return {"ok": False, "message": message}
    caption = f"미처리 목록 {stamp}"
    sent = notify_document_all(rt.channels, path, caption)

    # 현황판은 claude.ai 쪽 사본을 실행기가 갱신할 수 없으므로 파일로 함께 보낸다
    board_sent = 0
    board_path = None
    try:
        from v2r.engine.dashboard import build_dashboard

        board_path = build_dashboard(rt)
        board_sent = notify_document_all(
            rt.channels, board_path, f"운영 현황판 {stamp}"
        )
    except Exception as exc:  # 현황판 실패로 미처리 목록 전송까지 죽이지 않는다
        log.warning("현황판 전송 실패: %s", exc)
        notify_all(rt.channels, f"현황판을 보내지 못했습니다: {exc}")

    return {
        "ok": True,
        "message": f"{caption} 전송 완료 ({sent}곳), 현황판 {board_sent}곳",
        "sent": sent,
        "dashboard_sent": board_sent,
        "path": str(path),
        "dashboard_path": str(board_path) if board_path else "",
    }


def _catalog_report(rt: Runtime, spec: TaskSpec) -> dict:
    cafes = rt.catalog.cafes()
    lines = [f"카페 {len(cafes)}개: " + ", ".join(c.name for c in cafes)]
    if spec.cafe:
        from v2r.api.catalog import match_name

        cafe = match_name(spec.cafe, cafes, key=lambda c: c.name)
        accounts = [a.login_id for a in rt.catalog.cafe_accounts(cafe.cafe_id)]
        menus = rt.catalog.menus(cafe.cafe_id, accounts)
        lines.append(f"{cafe.name} 계정 {len(accounts)}개")
        lines.append(f"{cafe.name} 게시판: " + ", ".join(m.name for m in menus))
    return {"ok": True, "report": "\n".join(lines)}


#: 대기 중 리스를 연장하는 주기(초)
SLEEP_CHUNK_S = 30.0


def order_round_robin(slots: list[Any]) -> list[Any]:
    """카페별 줄을 만들어 번갈아 꺼낸다 — 카페끼리 나란히 진행.

    **자사 카페 일상 글(`카페별`)에는 쓰지 않는다**: 각색 xlsx가 이미 카페를 행마다
    번갈아 배치해 두었고, 그 순서를 그대로 지키는 것이 사용자 절대 규칙이다
    (`publish.prepare_per_cafe`). 다른 흐름을 위해 남겨 둔 도구다.
    """
    queues: dict[str, list] = {}
    order: list[str] = []
    for slot in slots:
        key = publish_mod._norm(getattr(slot, "cafe", ""))
        if key not in queues:
            queues[key] = []
            order.append(key)
        queues[key].append(slot)
    out: list[Any] = []
    while any(queues[k] for k in order):
        for key in order:
            if queues[key]:
                out.append(queues[key].pop(0))
    return out


#: 긴 대기 중 "아직 살아 있다"고 이벤트를 남기는 간격(초). 감시견의 정체 판정(15분)보다 짧다.
PROGRESS_EVENT_S = 600.0


def _progress_event(
    rt: Runtime | None, job_id: int | None, message: str, waited: float
) -> None:
    """긴 대기 중 진행 이벤트 1줄. 실패해도 발행을 막지 않는다."""
    if rt is None or job_id is None:
        return
    try:
        rt.events.log(job_id, "info", f"{message} ({int(waited // 60)}분째)")
    except Exception as exc:  # pragma: no cover - 로그 실패는 삼킨다
        log.warning("진행 이벤트 기록 실패: %s", exc)


def _sleep_with_beat(
    seconds: float,
    beat: Any,
    sleep: Any = time.sleep,
    *,
    rt: Runtime | None = None,
    job_id: int | None = None,
) -> None:
    """리스를 연장하며 나눠 쉰다 (긴 대기 중 작업을 뺏기지 않게).

    10분마다 진행 이벤트를 남겨 감시견이 "정체"로 오해하지 않게 한다.
    """
    remaining = max(0.0, float(seconds))
    waited = 0.0
    next_event = PROGRESS_EVENT_S
    while remaining > 0:
        chunk = min(SLEEP_CHUNK_S, remaining)
        sleep(chunk)
        remaining -= chunk
        waited += chunk
        beat()
        if waited >= next_event:
            next_event += PROGRESS_EVENT_S
            _progress_event(rt, job_id, "대기 중", waited)


#: 레이트 제한 대기 상한(6시간)과 리스 연장 간격(30초).
RATE_WAIT_CAP_S = 6 * 3600.0
RATE_BEAT_S = 30.0
#: 기본 대기(서버가 `retry_after`를 안 줄 때) 1시간.
RATE_WAIT_DEFAULT_S = 3600.0
#: 슬롯 하나당 레이트 대기 재시도 횟수 상한.
RATE_MAX_WAITS = 3


def rate_wait_seconds(exc: Any) -> float:
    """예외의 `retry_after`(초). 없으면 1시간. 상한 6시간."""
    value = getattr(exc, "retry_after", None)
    try:
        seconds = float(value) if value is not None else RATE_WAIT_DEFAULT_S
    except (TypeError, ValueError):
        seconds = RATE_WAIT_DEFAULT_S
    return min(max(seconds, 0.0), RATE_WAIT_CAP_S)


def _wait_for_rate_limit(
    rt: Runtime,
    seconds: float,
    beat: Any,
    sleep: Any = time.sleep,
    *,
    job_id: int | None = None,
) -> bool:
    """제한이 풀릴 때까지 30초씩 쉬며 리스를 연장한다.

    중지 요청이 오면 즉시 False. 끝까지 기다렸으면 True(같은 슬롯 재시도).
    """
    remaining = min(max(float(seconds), 0.0), RATE_WAIT_CAP_S)
    waited = 0.0
    next_event = PROGRESS_EVENT_S
    while remaining > 0:
        if stop_requested(rt):
            return False
        chunk = min(RATE_BEAT_S, remaining)
        sleep(chunk)
        remaining -= chunk
        waited += chunk
        beat()
        if waited >= next_event:
            next_event += PROGRESS_EVENT_S
            _progress_event(rt, job_id, "요청 제한 대기 중", waited)
    return not stop_requested(rt)


def _daily_report_path(rt: Runtime, spec: TaskSpec):
    """자사 카페 일상 글 보고서 경로 (`docs/reports/self-daily-<날짜>.md`)."""
    return rt.settings.repo_root / "docs" / "reports" / f"self-daily-{spec.start_date}.md"


def write_daily_report(
    rt: Runtime,
    spec: TaskSpec,
    results: list[dict],
    failed: list[tuple[Any, str]],
) -> str:
    """카페별 발행 결과 표를 보고서 파일로 남기고 경로를 돌려준다.

    사용자가 **순서**(각색 xlsx의 파일·행 순서)를 눈으로 확인할 수 있게
    `순서 / 파일 / 행`을 앞에 붙인다 (모의 실행 보고서도 같은 표다).
    """
    path = _daily_report_path(rt, spec)
    seq_by_row = {
        (str(s.get("source") or ""), int(s.get("row") or 0)): int(s.get("seq") or 0)
        for s in (rt.scratch.get("per_cafe_sequence") or [])
    }

    def _seq(source: Any, row: Any, fallback: int) -> int:
        try:
            return seq_by_row.get((str(source or ""), int(row or 0)), fallback)
        except (TypeError, ValueError):
            return fallback

    lines: list[tuple[int, str]] = []
    for order, r in enumerate(results, start=1):
        seq = _seq(r.get("source"), r.get("row"), order)
        lines.append((
            seq,
            "| {seq} | {source} | {row} | {cafe} | {board} | {account} | {at} | {comments} |"
            " {status} |".format(
                seq=seq,
                source=r.get("source", ""),
                row=r.get("row", ""),
                cafe=r.get("cafe", ""),
                board=r.get("board", ""),
                account=publish_mod.mask_login(r.get("account", "")),
                at=r.get("scheduled_at", ""),
                comments=r.get("comments", 0),
                status=r.get("url") or r.get("status", ""),
            ),
        ))
    for order, (slot, error) in enumerate(failed, start=len(results) + 1):
        m = slot.manuscript
        seq = _seq(getattr(m, "source", ""), getattr(m, "source_row", 0), order)
        lines.append((
            seq,
            "| {seq} | {source} | {row} | {cafe} | {board} | {account} | {at} | - |"
            " 실패: {err} |".format(
                seq=seq,
                source=getattr(m, "source", ""),
                row=getattr(m, "source_row", ""),
                cafe=slot.cafe,
                board=slot.board,
                account=publish_mod.mask_login(slot.account),
                at=slot.scheduled_at.isoformat() if slot.scheduled_at else "즉시",
                err=" ".join(str(error).split())[:120],
            ),
        ))
    # 사용자가 각색 xlsx 순서 그대로 확인할 수 있게 순서대로 적는다
    rows = [line for _seq_no, line in sorted(lines, key=lambda x: x[0])]
    text = "\n".join(
        [
            f"# 자사 카페 일상 글 발행 보고 ({spec.start_date})",
            "",
            f"- 모드: {'모의 실행' if spec.dry_run else '실제 발행'}",
            f"- 성공 {len(results)}건 / 실패 {len(failed)}건",
            "- 순서: 각색 엑셀 파일 이름 오름차순 → 행 순서 그대로 (카페별로 묶지 않음)",
            "",
            "| 순서 | 파일 | 행 | 카페 | 게시판 | 계정 | 발행 시각 | 댓글 | URL/상태 |",
            "|---|---|---|---|---|---|---|---|---|",
            *rows,
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def per_cafe_counts(
    results: list[dict], failed: list[tuple[Any, str]]
) -> dict[str, dict[str, int]]:
    """카페별 성공/실패 건수."""
    out: dict[str, dict[str, int]] = {}
    for r in results:
        cafe = str(r.get("cafe") or "")
        out.setdefault(cafe, {"ok": 0, "fail": 0})["ok"] += 1
    for slot, _error in failed:
        out.setdefault(str(slot.cafe or ""), {"ok": 0, "fail": 0})["fail"] += 1
    return out


def _other_account(rt: Runtime, spec: TaskSpec, slot: Any) -> str | None:
    """계정 제한 뒤 같은 슬롯에 쓸 다른 계정 1개."""
    try:
        pool = publish_mod.load_accounts(rt, prefer_cache=bool(spec.dry_run))
    except Exception:
        return None
    from v2r.accounts.rules import eligible, work_type_for

    restricted = set(publish_mod.restricted_accounts(rt)) | {slot.account}
    work_type = work_type_for(spec.task, slot.cafe, rt.cafes_cfg)
    usable = eligible(pool, work_type, publish_mod.all_comment_accounts(rt), restricted)
    for account in usable:
        login = getattr(account, "login_id", str(account))
        if login and login != slot.account:
            return login
    return None


def _run_publish(
    rt: Runtime, job_id: int | None, spec: TaskSpec, owner: str | None = None
) -> dict:
    """발행 작업 한 건."""
    clear_stop(rt)  # 새 작업 시작 → 이전 중지 요청은 해제
    if spec.task == "publish_daily" and getattr(spec, "per_cafe", False):
        # 다른 실행기가 같은 종류의 발행을 돌고 있으면 나란히 올리지 않는다
        # (장애 2026-09-20 B: 65·67 동시 실행). 끝날 때까지 기다린다.
        wait_for_other_publish(rt, job_id, owner)
    skipped: list[dict] = []
    manuscripts = publish_mod.prepare_manuscripts(rt, spec, skipped)
    if not manuscripts:
        load = rt.scratch.get("source_load") or {}
        attempted = int(load.get("attempted") or 0)
        failed = int(load.get("failed") or 0)
        if attempted and failed >= attempted:
            # 원본을 하나도 읽지 못했다 → 성공으로 보고하지 않는다 (M-4)
            reasons = [
                str(s.get("reason") or "") for s in skipped if s.get("reason")
            ] or ["원본 적재 실패"]
            return {
                "ok": False,
                "slots": 0,
                "skipped": skipped,
                "failures": [f"원본을 하나도 읽지 못했습니다: {'; '.join(reasons)}"],
                "dry_run": spec.dry_run,
                "message": "원본 적재에 모두 실패했습니다",
            }
        out0 = {
            "ok": True,
            "slots": 0,
            "skipped": skipped,
            "dry_run": spec.dry_run,
            "message": "발행할 원고가 없습니다",
        }
        plan_info = rt.scratch.get("per_cafe_plan") if getattr(spec, "per_cafe", False) else None
        if plan_info:
            out0["per_cafe"] = {
                cafe: {"ok": 0, "fail": 0, **info} for cafe, info in plan_info.items()
            }
            out0["message"] = "오늘 목표를 이미 채웠거나 올릴 원고가 없습니다"
        return out0

    from v2r.warehouse.store import NoPhotoError

    try:
        slots = publish_mod.plan(rt, spec, manuscripts)
    except NoPhotoError as exc:
        # 사진 원본이 아예 없다 → 텔레그램 등 채널로 바로 알린다 (결정 1)
        rt.events.log(job_id, "error", str(exc))
        notify_all(rt.channels, str(exc))
        # 이어서 GPT 이미지 생성 프롬프트 묶음을 보내 새 사진을 요청한다 (계획 §확보 2단계)
        _request_missing_photos(rt, job_id, spec, exc)
        return {
            "ok": False,
            "slots": 0,
            "skipped": skipped,
            "failures": [str(exc)],
            "dry_run": spec.dry_run,
            "message": str(exc),
        }
    paced = bool(getattr(spec, "per_cafe", False))
    # 각색 xlsx의 행 순서(카페가 행마다 번갈아 옴)를 그대로 지킨다 — 라운드로빈으로
    # 다시 섞지 않는다 (사용자 절대 규칙, 규칙 §2)
    need_browser = False  # 이미지는 업로드 API로 첨부한다(브라우저 불필요, 2026-09-19)
    results: list[dict] = []
    failures: list[str] = []
    failed_slots: list[tuple[Any, str]] = []
    #: 다음 글을 올려도 되는 시각(monotonic). 연속한 두 글은 어차피 카페가 다르므로
    #: 카페별이 아니라 **전체 하나의 간격**으로 쉰다 (규칙 §4).
    next_allowed = 0.0
    window_notified = False
    rate_notified = False  # 레이트 제한 안내는 작업당 한 번만
    touched: list[tuple] = []
    retried: set[int] = set()
    playwright = context = page = None

    def beat() -> None:
        """긴 루프 중 리스 연장. 잃었으면 중단한다 (M-5)."""
        if owner and job_id is not None:
            if not rt.jobs.heartbeat(job_id, owner):
                raise publish_mod.PublishError("리스 상실: 다른 실행기가 작업을 가져갔습니다")

    if need_browser:
        from v2r.browser import session as browser_session

        playwright, context, page = browser_session.open_site()
        browser_session.ensure_logged_in(page, rt.settings.v2r_site)

    stopped = False
    try:
        for index, slot in enumerate(slots, start=1):
            if stopped or stop_requested(rt):  # 슬롯 사이에서 중지 확인 (M-9)
                stopped = True
                failures.append(f"{slot.manuscript.title}: 중지 요청으로 건너뜀")
                continue
            m = slot.manuscript
            touched.append((m.source, m.source_row, m.content_hash))
            try:
                beat()
            except publish_mod.PublishError as exc:
                failures.append(f"{m.title}: {exc}")
                failed_slots.append((slot, str(exc)))
                break
            if not spec.dry_run and publish_mod.is_self_cafe(rt, slot.cafe):
                # 자사 카페 글은 08:00~02:00(KST)에만 올린다. 밖이면 다음 08:00까지 기다린다.
                now_kst = datetime.now(KST)
                if not publish_mod.in_self_window(now_kst):
                    open_at = publish_mod.next_self_window(now_kst)
                    if not window_notified:
                        notify_all(
                            rt.channels,
                            f"자사 카페 허용 시간대(08:00~02:00) 밖이라 {open_at:%H:%M}까지 대기합니다",
                        )
                        window_notified = True
                    _sleep_with_beat(
                        (open_at - now_kst).total_seconds(), beat, rt=rt, job_id=job_id
                    )
            if paced and not spec.dry_run:
                # 앞 글에서 정한 다음 발행 허용 시각까지 쉰다 (글 사이 2~3분 랜덤)
                wait = next_allowed - time.monotonic()
                if wait > 0:
                    _sleep_with_beat(wait, beat, rt=rt, job_id=job_id)
            rate_waits = 0
            while True:
                try:
                    results.append(
                        publish_mod.run_slot(
                            rt, spec, slot, browser_page=page, job_id=job_id, heartbeat=beat
                        )
                    )
                except publish_mod.RetryWithOtherAccount as exc:
                    other = None if index in retried else _other_account(rt, spec, slot)
                    if other:
                        retried.add(index)
                        rt.events.log(job_id, "info", f"다른 계정으로 재시도: {other}")
                        slot.account = other
                        try:
                            results.append(
                                publish_mod.run_slot(
                                    rt, spec, slot, browser_page=page, job_id=job_id, heartbeat=beat
                                )
                            )
                        except (publish_mod.RetryWithOtherAccount, publish_mod.PublishError) as exc2:
                            failures.append(f"{m.title}: {exc2}")
                            failed_slots.append((slot, str(exc2)))
                    else:
                        failures.append(f"{m.title}: {exc}")
                        failed_slots.append((slot, str(exc)))
                except publish_mod.PublishError as exc:
                    kind = getattr(exc, "kind", None)
                    if kind in publish_mod.RATE_KINDS and rate_waits < RATE_MAX_WAITS:
                        # 남은 슬롯을 줄줄이 실패시키지 않는다. 제한이 풀릴 때까지
                        # 기다렸다가 **같은 슬롯**부터 이어서 간다 (장애 2026-09-19).
                        rate_waits += 1
                        wait = rate_wait_seconds(exc)
                        until = datetime.now(KST) + timedelta(seconds=wait)
                        if not rate_notified:
                            notify_all(
                                rt.channels,
                                f"V2R 로그인 제한: {until:%H:%M}까지 대기 후 이어갑니다",
                            )
                            rate_notified = True
                        rt.events.log(
                            job_id, "warn", f"레이트 제한 대기 {int(wait)}초 ({kind}) — {m.title}"
                        )
                        if _wait_for_rate_limit(rt, wait, beat, job_id=job_id):
                            continue  # 같은 슬롯 재시도 (원고는 소모되지 않는다)
                        stopped = True
                        failures.append(f"{m.title}: 중지 요청으로 대기를 멈췄습니다")
                        break
                    failures.append(f"{m.title}: {exc}")
                    failed_slots.append((slot, str(exc)))
                break
            if stopped:
                continue
            if paced and not spec.dry_run:
                next_allowed = time.monotonic() + 60.0 * random.uniform(
                    float(spec.interval_min), float(max(spec.interval_max, spec.interval_min))
                )
            if not spec.dry_run and (not paced or index % 10 == 0 or index == len(slots)):
                notify_all(
                    rt.channels,
                    format_report(
                        job_id or 0,
                        "running",
                        f"{index}/{len(slots)} {m.title}",
                    ),
                )
    finally:
        if need_browser:
            from v2r.browser import session as browser_session

            browser_session.close(playwright, context, page)

    # 이번 작업이 건드린 건만 본다 (M-2)
    touched_set = set(touched)
    left_uncertain = [
        f"{p['source_key']}#{p['row_number']}"
        for p in rt.publications.list_uncertain()
        if (p["source_key"], int(p["row_number"]), p["content_hash"]) in touched_set
    ]
    pending = [r for r in results if r.get("status") == "pending"]
    out = {
        "ok": not failures,
        "slots": len(slots),
        "results": results,
        "skipped": skipped,
        "failures": failures,
        "uncertain": left_uncertain,
        "dry_run": spec.dry_run,
    }
    if pending:
        out["pending"] = [r.get("title", "") for r in pending]
        out["message"] = f"예약 확인 대기 {len(pending)}건"
    if stopped:
        out["stopped"] = True
        out["message"] = "중지 요청으로 남은 슬롯을 건너뛰었습니다"
    if paced:
        # 카페별 성공/실패 수 + 보고서 파일 (규칙 §6)
        counts = per_cafe_counts(results, failed_slots)
        # 목표 계산 근거(요청/오늘 이미 올림/이번에 계획)를 같이 보여준다 (규칙 §7)
        for cafe, plan_info in (rt.scratch.get("per_cafe_plan") or {}).items():
            counts.setdefault(cafe, {"ok": 0, "fail": 0}).update(plan_info)
        out["per_cafe"] = counts
        # 모의 실행에서 사용자가 순서를 확인할 수 있게 계획 순서를 그대로 싣는다
        out["sequence"] = list(rt.scratch.get("per_cafe_sequence") or [])
        try:
            out["report_file"] = write_daily_report(rt, spec, results, failed_slots)
        except Exception as exc:  # 보고서 실패는 발행 결과를 바꾸지 않는다
            log.warning("일상 글 보고서 기록 실패: %s", exc)
            out["report_file"] = ""
        summary = ", ".join(
            f"{cafe} 성공 {c['ok']}/실패 {c['fail']}" for cafe, c in counts.items()
        )
        notify_all(
            rt.channels,
            f"자사 카페 일상 글 완료 — {summary}\n보고서: {out.get('report_file') or '(없음)'}",
        )
    return out


def _dashboard(rt: Runtime) -> dict:
    """운영 현황판 HTML을 새로 만들고 파일 경로를 알려준다."""
    from v2r.engine.dashboard import build_dashboard

    path = build_dashboard(rt)
    return {"ok": True, "path": str(path), "message": f"현황판을 갱신했습니다: {path}"}


def refresh_dashboard(rt: Runtime) -> None:
    """현황판 자동 갱신(최선 노력). 실패해도 작업 결과에는 영향을 주지 않는다."""
    try:
        from v2r.engine.dashboard import build_dashboard

        build_dashboard(rt)
    except Exception as exc:  # pragma: no cover - 방어용
        log.warning("현황판 갱신 실패: %s", exc)


def dispatch(rt: Runtime, job: Any, owner: str | None = None) -> dict:
    """작업 1건 실행. 결과 딕셔너리 반환."""
    job_id = int(job["id"]) if job is not None else None
    spec = TaskSpec.from_json(job["spec_json"])
    task = spec.task

    if task in PUBLISH_TASKS:
        return _run_publish(rt, job_id, spec, owner)
    if task == "status":
        return {"ok": True, "report": status_mod.status_report(rt, exclude_job_id=job_id)}
    if task == "dashboard":
        return _dashboard(rt)
    if task == "inspect_failures":
        return {"ok": True, "report": status_mod.inspect_failures(rt)}
    if task == "reconcile":
        out = reconcile_mod.reconcile(rt)
        return {"ok": not out.get("errors"), **out}
    if task == "sync_sources":
        out = _sync_entries(rt, spec, all_kinds=False)
        return {"ok": not out.get("errors"), **out}
    if task == "sync_all_sources":
        out = _sync_entries(rt, spec, all_kinds=True)
        return {"ok": not out.get("errors"), **out}
    if task == "sync_article_index":
        from v2r.engine.article_sync import sync_all_self_cafes

        out = sync_all_self_cafes(rt, with_bodies="본문까지" in (spec.notes or ""))
        return {"ok": not out.get("errors"), **out}
    if task == "duplicate_check":
        from v2r.engine.article_sync import duplicate_check

        out = duplicate_check(rt, spec)
        return {"ok": True, **out}
    if task == "generate_brand":
        return _generate_brand(rt, spec)
    if task == "generate_affiliate_daily":
        return _generate_affiliate_daily(rt, spec)
    if task == "generate_daily":
        return _generate_daily(rt, spec)
    if task == "collect_daily":
        return _collect_daily(rt, spec)
    if task == "collect_photos":
        return _collect_photos(rt, spec)
    if task == "collect_new_photos":
        return _collect_new_photos(rt, spec)
    if task == "generate_photos":
        return _generate_photos(rt, spec)
    if task == "approve_photos":
        return _approve_photos(rt, spec)
    if task == "reject_photos":
        return _reject_photos(rt, spec)
    if task == "gpt_keepalive":
        return _gpt_keepalive(rt, spec)
    if task == "request_photos":
        return _request_photos(rt, spec)
    if task == "wash_photos":
        return _wash_photos(rt, spec)
    if task == "cleanup_orphans":
        from v2r.engine.cleanup import cleanup_orphans

        return cleanup_orphans(rt, spec)
    if task == "cleanup_emoji":
        from v2r.engine.emoji_cleanup import cleanup_emoji

        return cleanup_emoji(rt, spec, job_id=job_id)
    if task == "repair_comments":
        from v2r.engine.repair import repair_comments

        return repair_comments(rt, spec, job_id=job_id)
    if task == "learn_guides":
        from v2r.knowledge.make_import import learn_make_guides

        return {"ok": True, **learn_make_guides(rt.warehouse.guides_dir)}
    if task == "open_login":
        from v2r.browser import session as browser_session

        playwright, context, page = browser_session.open_site()
        try:
            ok = browser_session.ensure_logged_in(page, rt.settings.v2r_site)
        finally:
            browser_session.close(playwright, context, page)
        return {"ok": bool(ok), "message": "로그인 세션을 확인했습니다"}
    if task == "stop":
        request_stop(rt)
        cancelled = rt.jobs.cancel_open(default_owner())
        return {"ok": True, "cancelled": cancelled}
    if task == "catalog":
        return _catalog_report(rt, spec)
    if task == "schedule_list":
        from v2r.engine import schedule as schedule_mod

        return {"ok": True, "report": schedule_mod.schedule_report(rt)}
    if task == "schedule_run":
        from v2r.engine import schedule as schedule_mod

        out = schedule_mod.run_now(rt, spec.schedule_name)
        return {"ok": bool(out.get("ok")), "message": out.get("message", "")}
    if task == "pending_report":
        return _pending_report(rt)
    if task == "monitor_status":
        from v2r.engine import monitor as monitor_mod

        return {"ok": True, "report": monitor_mod.monitor_report(rt)}

    raise ValueError(f"처리기가 없는 작업: {task}")


# --------------------------------------------------------------------
# 큐 실행
# --------------------------------------------------------------------
def run_once(rt: Runtime, owner: str | None = None) -> dict | None:
    """큐에서 1건을 실행한다. 없으면 None."""
    owner = owner or default_owner()
    reaped = rt.jobs.reap_stale_running()  # 죽은 실행기의 고아 작업 정리 (M-5)
    if reaped:
        log.warning("리스가 끊긴 작업 %d건을 불확실로 정리했습니다", reaped)
    job = rt.jobs.acquire(owner)
    if job is None:
        return None

    job_id = int(job["id"])
    spec = TaskSpec.from_json(job["spec_json"])
    description = describe_spec(spec)
    try:
        result = dispatch(rt, job, owner)
    except Exception as exc:
        rt.jobs.finish(job_id, "failed", None, str(exc))
        rt.events.log(job_id, "error", f"작업 실패: {exc}")
        notify_all(rt.channels, f"작업 {job_id} 실패: {exc}")
        return {"job_id": job_id, "status": "failed", "error": str(exc), "description": description}

    failures = list(result.get("failures") or result.get("errors") or [])
    uncertain = list(result.get("uncertain") or [])
    if result.get("ok") is False or failures:
        status = "failed"
    elif uncertain:
        status = "uncertain"
    else:
        status = "done"

    error = None
    if status == "failed":
        parts = []
        if result.get("error"):
            parts.append(str(result["error"]))
        parts.extend(failures)
        if uncertain:
            parts.append("미확정: " + ", ".join(uncertain))
        error = "; ".join(p for p in parts if p) or None

    rt.jobs.finish(job_id, status, result, error)
    rt.events.log(job_id, "info", f"작업 종료({status}): {description}")
    if spec.task in PUBLISH_TASKS or spec.task == "reconcile":
        refresh_dashboard(rt)  # 발행·점검이 끝날 때마다 현황판을 새로 그린다
    notify_all(rt.channels, format_report(job_id, status, description))
    return {"job_id": job_id, "status": status, "result": result, "description": description}


def drain(rt: Runtime, owner: str | None = None, limit: int = 100) -> list[dict]:
    """큐가 빌 때까지 실행."""
    done: list[dict] = []
    for _ in range(limit):
        out = run_once(rt, owner)
        if out is None:
            break
        done.append(out)
    return done


def _reply(channel: Any, chat_id: str, text: str) -> None:
    """명령을 보낸 그 대화방에만 답한다. 실패해도 루프를 세우지 않는다."""
    try:
        from v2r.channels import sanitize

        channel.send(chat_id, sanitize(text))
    except Exception as exc:  # pragma: no cover - 알림 실패는 삼킨다
        log.warning("답장 실패(%s): %s", getattr(channel, "name", "?"), exc)


def serve_poll(rt: Runtime, owner: str | None = None) -> dict:
    """serve 루프 1회분: 채널 수신 → 접수 → 큐 실행 → 보낸 방에 답장.

    루프를 절대 죽이지 않기 위해 채널별·명령별로 예외를 가둔다.
    """
    owner = owner or default_owner()
    #: job_id → (채널, 보낸 방) — 결과를 브로드캐스트만 하지 않고 그 방에도 답한다
    origins: dict[int, tuple[Any, str]] = {}
    received = 0

    for channel in rt.channels:
        try:
            incoming = channel.poll()
        except Exception as exc:
            log.warning("채널 %s 수신 실패: %s", getattr(channel, "name", "?"), exc)
            continue
        # 중지 명령은 언제나 먼저 처리한다 (M-9)
        incoming = sorted(
            incoming,
            key=lambda c: 0 if "중지" in (getattr(c, "text", "") or "") else 1,
        )
        for cmd in incoming:
            received += 1
            try:
                out = handle_text(rt, cmd.text, via_channel=True)
            except Exception as exc:
                log.exception("명령 처리 실패: %s", exc)
                _reply(channel, cmd.chat_id, f"명령 처리 중 오류가 났습니다: {exc}")
                continue
            if out.get("stopped"):
                _reply(channel, cmd.chat_id, out.get("message") or "중지했습니다")
                continue
            if not out.get("ok"):
                _reply(channel, cmd.chat_id, out.get("error") or "명령을 해석하지 못했습니다")
                continue
            # 중지 뒤에 들어온 새 명령은 중지 상태를 푼다(그래야 큐가 다시 돈다)
            clear_stop(rt)
            job_id = out.get("job_id")
            if job_id is not None:
                origins[int(job_id)] = (channel, cmd.chat_id)
            _reply(channel, cmd.chat_id, format_report(job_id, "running", out["description"]))

    # 죽은 실행기가 남긴 작업 정리 (M-5). run_once 안에서도 하지만
    # 큐가 비어 있는 동안에도 주기적으로 돌아야 한다.
    try:
        reaped = rt.jobs.reap_stale_running()
        if reaped:
            log.warning("리스가 끊긴 작업 %d건을 불확실로 정리했습니다", reaped)
    except Exception as exc:
        log.warning("고아 작업 정리 실패: %s", exc)
        reaped = 0

    if stop_requested(rt):
        # 중지 플래그가 살아 있으면 새 작업을 꺼내지 않는다. 새 명령이 오면 풀린다.
        return {"received": received, "reaped": reaped, "done": [], "stopped": True}

    try:
        outs = drain(rt, owner)
    except Exception as exc:
        log.exception("큐 실행 실패: %s", exc)
        return {"received": received, "reaped": reaped, "done": [], "error": str(exc)}

    for out in outs:
        origin = origins.get(int(out.get("job_id") or 0))
        if origin is None:
            continue  # 이 방이 시킨 작업이 아니다 (결과는 notify_all이 이미 보냈다)
        channel, chat_id = origin
        result = out.get("result") or {}
        text = format_report(
            out.get("job_id"), out.get("status", ""), out.get("description", ""),
            error=out.get("error"),
        )
        extra = result.get("report") or result.get("message") or ""
        _reply(channel, chat_id, f"{text}\n{extra}".strip())

    return {"received": received, "reaped": reaped, "done": outs, "stopped": False}


#: serve 시작할 때 보내는 안내 문구
SERVE_HELLO = "실행기 시작됨. '상태' 라고 보내보세요"


def maintain_session(rt: Runtime) -> str:
    """토큰 주기 점검 1회분. 실패해도 serve 루프를 죽이지 않는다.

    하루 20회 로그인 제한(장애 2026-09-19) 때문에 serve가 30분마다 불러
    액세스 토큰은 갱신으로 이어 쓰고, 리프레시 쿠키가 끝나갈 때만
    **하루 한 번** 로그인한다.
    """
    try:
        result = rt.client.maintain_auth()
    except Exception as exc:
        log.warning("세션 점검 실패(계속 진행): %s", exc)
        return "error"
    if result in ("login", "refresh"):
        log.info("세션 점검: %s", result)
    return result


#: 두 번째 실행기에게 보여 주는 안내
SECOND_SERVE_MSG = (
    "이미 다른 실행기가 돌고 있습니다(프로세스 {pid}번)."
    " 실행기는 한 대만 켭니다 — 이 창은 그냥 닫으세요."
)


def ensure_single_serve(rt: Runtime) -> Any:
    """실행기 잠금을 잡는다. 두 번째면 안내를 남기고 None."""
    from v2r.engine import lock as lock_mod

    held = lock_mod.acquire_serve_lock(rt)
    if held is not None:
        return held
    pid = lock_mod.holder_pid(rt)
    message = SECOND_SERVE_MSG.format(pid=pid if pid is not None else "?")
    print(message, flush=True)
    log.error("%s", message)
    try:
        rt.events.log(None, "error", f"serve 중복 실행 차단: {message}")
    except Exception as exc:  # noqa: BLE001 - 기록 실패로 종료를 막지 않는다
        log.warning("중복 실행 이벤트 기록 실패: %s", exc)
    return None


def serve(rt: Runtime, poll_seconds: int = 5, announce: bool = True) -> None:  # pragma: no cover - 장시간 루프
    """채널을 폴링하며 명령을 받아 실행한다. 어떤 예외로도 멈추지 않는다."""
    held = ensure_single_serve(rt)
    if held is None:
        return
    owner = default_owner()
    print(f"serve 시작: 채널 {len(rt.channels)}개, {poll_seconds}초 간격", flush=True)
    log.info("serve 시작: 채널 %d개", len(rt.channels))
    if announce:
        notify_all(rt.channels, SERVE_HELLO)
    next_maintain = 0.0
    try:
        while True:
            try:
                if time.monotonic() >= next_maintain:
                    next_maintain = time.monotonic() + MAINTAIN_TICK_S
                    maintain_session(rt)
                watch_tick(rt)  # 예약 발사 + 모든 작업 감시 + 심장박동
                serve_poll(rt, owner)
            except KeyboardInterrupt:
                print("serve 중지", flush=True)
                return
            except Exception as exc:
                log.exception("serve 폴링 실패(계속 진행): %s", exc)
            time.sleep(poll_seconds)
    finally:
        held.release()


def watch_tick(rt: Runtime) -> dict:
    """serve 루프 1회분의 예약·감시. 어떤 예외로도 루프를 죽이지 않는다."""
    from v2r.engine import monitor as monitor_mod
    from v2r.engine import schedule as schedule_mod

    out: dict = {}
    try:
        out["schedule"] = schedule_mod.tick(rt)
    except Exception as exc:  # noqa: BLE001
        log.exception("예약 틱 실패(계속 진행): %s", exc)
        out["schedule"] = {"error": str(exc)}
    try:
        out["monitor"] = monitor_mod.tick(rt)
    except Exception as exc:  # noqa: BLE001
        log.exception("감시 틱 실패(계속 진행): %s", exc)
        out["monitor"] = {"error": str(exc)}
    try:
        schedule_mod.write_heartbeat(rt)
    except Exception as exc:  # noqa: BLE001
        log.warning("심장박동 기록 실패: %s", exc)
    return out


__all__ = [
    "ALLOWED_TASKS",
    "default_owner",
    "dispatch",
    "ensure_single_serve",
    "drain",
    "handle_text",
    "idem_key",
    "maintain_session",
    "run_once",
    "serve",
    "wait_for_other_publish",
    "watch_tick",
]
