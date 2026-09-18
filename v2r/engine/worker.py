"""작업 실행기: 명령 접수 → 큐 → 작업 실행 → 채널 보고 (DESIGN §5)."""

from __future__ import annotations

import hashlib
import itertools
import logging
import re
import socket
import time
import uuid
from typing import Any

from v2r.channels import format_report, notify_all
from v2r.command.parser import describe_spec, parse_korean_command
from v2r.command.spec import ALLOWED_TASKS, TaskSpec, today_kst
from v2r.engine import publish as publish_mod
from v2r.engine import reconcile as reconcile_mod
from v2r.engine import status as status_mod
from v2r.engine.context import Runtime

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
    """실행기 소유자 이름."""
    try:
        return socket.gethostname()
    except Exception:
        return "local"


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


def _generate_photos(rt: Runtime, spec: TaskSpec) -> dict:
    """ChatGPT 웹앱(구독)으로 사진을 직접 만들고 적재 + 세탁까지 한다."""
    from v2r.warehouse.gpt_images import generate_batch

    brand = spec.brand or ""
    if not brand:
        return {"ok": False, "error": "사진을 만들 브랜드를 알 수 없습니다 (`브랜드 X`를 넣어 주세요)"}
    keyword = getattr(spec, "keyword", "") or spec.source or ""
    count = spec.count or 1
    out = generate_batch(brand, keyword, count, warehouse=rt.warehouse)
    if out.get("login_pending"):
        notify_all(rt.channels, f"ChatGPT {out['message']}")
    elif out.get("limited"):
        notify_all(rt.channels, f"ChatGPT 이미지 사용 한도: {out.get('wait_text', '')}")
    elif out.get("generated"):
        notify_all(
            rt.channels,
            f"GPT 사진 {out['generated']}장 생성 (브랜드 {out['brand']} / {out['keyword']})",
        )
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
        return {
            "ok": True,
            "slots": 0,
            "skipped": skipped,
            "dry_run": spec.dry_run,
            "message": "발행할 원고가 없습니다",
        }

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
    need_browser = (not spec.dry_run) and any(s.images for s in slots)
    results: list[dict] = []
    failures: list[str] = []
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
            if stop_requested(rt):  # 슬롯 사이에서 중지 확인 (M-9)
                stopped = True
                failures.append(f"{slot.manuscript.title}: 중지 요청으로 건너뜀")
                continue
            m = slot.manuscript
            touched.append((m.source, m.source_row, m.content_hash))
            try:
                beat()
            except publish_mod.PublishError as exc:
                failures.append(f"{m.title}: {exc}")
                break
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
                else:
                    failures.append(f"{m.title}: {exc}")
            except publish_mod.PublishError as exc:
                failures.append(f"{m.title}: {exc}")
            if not spec.dry_run:
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
    return out


def dispatch(rt: Runtime, job: Any, owner: str | None = None) -> dict:
    """작업 1건 실행. 결과 딕셔너리 반환."""
    job_id = int(job["id"]) if job is not None else None
    spec = TaskSpec.from_json(job["spec_json"])
    task = spec.task

    if task in PUBLISH_TASKS:
        return _run_publish(rt, job_id, spec, owner)
    if task == "status":
        return {"ok": True, "report": status_mod.status_report(rt, exclude_job_id=job_id)}
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
    if task == "gpt_keepalive":
        return _gpt_keepalive(rt, spec)
    if task == "request_photos":
        return _request_photos(rt, spec)
    if task == "wash_photos":
        return _wash_photos(rt, spec)
    if task == "cleanup_orphans":
        from v2r.engine.cleanup import cleanup_orphans

        return cleanup_orphans(rt, spec)
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


def serve(rt: Runtime, poll_seconds: int = 5, announce: bool = True) -> None:  # pragma: no cover - 장시간 루프
    """채널을 폴링하며 명령을 받아 실행한다. 어떤 예외로도 멈추지 않는다."""
    owner = default_owner()
    print(f"serve 시작: 채널 {len(rt.channels)}개, {poll_seconds}초 간격", flush=True)
    log.info("serve 시작: 채널 %d개", len(rt.channels))
    if announce:
        notify_all(rt.channels, SERVE_HELLO)
    while True:
        try:
            serve_poll(rt, owner)
        except KeyboardInterrupt:
            print("serve 중지", flush=True)
            return
        except Exception as exc:
            log.exception("serve 폴링 실패(계속 진행): %s", exc)
        time.sleep(poll_seconds)


__all__ = [
    "ALLOWED_TASKS",
    "dispatch",
    "drain",
    "handle_text",
    "idem_key",
    "run_once",
    "serve",
]
