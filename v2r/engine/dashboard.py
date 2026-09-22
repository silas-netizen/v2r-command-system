"""운영 현황판·일일 보고·중간 보고 생성기 (새 모양, 2026-09-22).

기준 모양은 손으로 만든 1안 `docs/reports/dashboard-2026-09-21.html/.md` 이다.
같은 자료 한 벌(`ReportData`)을 모아 HTML 과 MD 두 가지로 그린다.

섹션 차례
---------
1. 한눈에 — 6칸 카드(발행 성공 / 사용 계정 / 게시판 연속 / V2R 로그인 /
   원고 비용 / 실행기). 중간 보고에는 **목표 대비 진행률** 카드가 하나 더 붙는다.
2. 발행 · 카페별 — 성공·실패·제한 글·미확정·계정 수·게시판 수·게시판 연속·상태
   (중간 보고는 남은 건수·예상 종료 시각이 더 붙는다)
3. 시간대별 발행 (0~23시 막대)
4. 브랜드 원고 · LLM — 요금제 호출·캐시율·유료 건수 (`data/llm_usage-YYYY-MM.jsonl`)
5. 로그인 · 세션 유지 — V2R·네이버·Claude 플랫폼·CLI·ChatGPT·Make
6. 오늘 진행 · 결정 대기 — `docs/reports/pending.md`

모든 수치는 DB(publications·events·jobs)·심장박동·세션 점검 작업 기록에서 실측한다.
HTML 은 바깥 스크립트·폰트를 전혀 쓰지 않는 자급자족 한 장이다.

내보내는 것
-----------
- `build_dashboard(rt)` — `현황판` 명령 → `data/dashboard.html` (오늘 기준)
- `build_daily_report(rt)` — 어제 기준 `docs/reports/dashboard-YYYY-MM-DD.html/.md`
- `build_progress_report(rt)` — 오늘 기준 `docs/reports/progress-YYYY-MM-DD-HHMM.html/.md`
"""

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from v2r.engine.context import Runtime
from v2r.store.db import KST

log = logging.getLogger(__name__)

#: 실행기가 '멈춤'으로 보이기 시작하는 무활동 시간(분)
STALL_MINUTES = 30
#: 심장박동이 이보다 오래되면 실행기를 멈춘 것으로 본다(초)
HEARTBEAT_STALE_SECONDS = 180

#: 보고서가 쌓이는 곳
REPORT_DIR = ("docs", "reports")
#: 미처리(대기) 목록 파일 — 6번 섹션의 재료
PENDING_FILE = ("docs", "reports", "pending.md")

SECTION_TITLES = {
    "header": "V2R 현황판",
    "overview": "한눈에",
    "cafes": "발행 · 카페별",
    "hourly": "시간대별 발행 (건)",
    "llm": "브랜드 원고 · LLM",
    "sessions": "로그인 · 세션 유지",
    "progress": "오늘 진행 · 결정 대기",
}

#: 보고서 종류별 제목 꼬리표
KIND_LABELS = {
    "live": "현황판",
    "daily": "일일 보고",
    "progress": "중간 보고",
}

#: 상태 뱃지 등급
OK, WARN, BAD, OFF = "ok", "warn", "bad", "off"


# --------------------------------------------------------------------
# 작은 도구
# --------------------------------------------------------------------
def _esc(value: object) -> str:
    """HTML 이스케이프. None·빈 값은 `-`."""
    text = "" if value is None else str(value)
    text = text.strip()
    return html.escape(text) if text else "-"


def mask_account(login: object) -> str:
    """계정 아이디를 앞 3글자만 남기고 가린다."""
    text = str(login or "").strip()
    if not text:
        return "-"
    if len(text) <= 3:
        return text + "…"
    return text[:3] + "…"


def _parse(ts: object) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(KST)).strftime("%Y-%m-%d")


def _yesterday(now: datetime | None = None) -> str:
    return ((now or datetime.now(KST)) - timedelta(days=1)).strftime("%Y-%m-%d")


def _report_dir(rt: Runtime, out_dir: str | Path | None = None) -> Path:
    if out_dir is not None:
        return Path(out_dir)
    return Path(rt.settings.repo_root).joinpath(*REPORT_DIR)


#: 게시판 한 칸의 정체 — `board` 가 비면 `menu_id` 를 쓴다
BOARD_EXPR = "COALESCE(NULLIF(TRIM(COALESCE(board, '')), ''), NULLIF(COALESCE(menu_id, ''), ''), '(미지정)')"
CAFE_EXPR = "COALESCE(NULLIF(TRIM(COALESCE(cafe, '')), ''), '(미지정)')"


def _has_column(rt: Runtime, table: str, column: str) -> bool:
    """`publications.error` 처럼 **있을 수도 없을 수도 있는** 칸인지 확인."""
    try:
        rows = rt.conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:  # pragma: no cover - 표가 없으면 없는 칸이다
        return False
    return any(str(r[1]) == column for r in rows)


def limited_sql(rt: Runtime) -> str:
    """'제한 글'(ID/IP 등록 제한) 판정식.

    다른 일꾼이 `publications` 상태를 정리하는 중이라 **느슨하게** 본다:
    `status='failed'` 이면서 `stage` 또는 (있으면) `error` 에 '제한' 이라는 글자가
    보이면 제한으로 센다. 표시가 없으면 자연히 0이 된다.
    """
    parts = ["COALESCE(stage, '') LIKE '%제한%'"]
    if _has_column(rt, "publications", "error"):
        parts.append("COALESCE(error, '') LIKE '%제한%'")
    return "(status = 'failed' AND (" + " OR ".join(parts) + "))"


# --------------------------------------------------------------------
# 자료 모으기
# --------------------------------------------------------------------
def _executor_state(rt: Runtime, now: datetime) -> dict:
    """실행기 상태 — 심장박동 + 그날 발사한 예약 수."""
    out: dict = {"state": "대기", "grade": OFF, "note": "", "fired": 0, "entries": 0}
    age: float | None = None
    beat = Path(rt.settings.data_dir) / "serve_heartbeat.json"
    try:
        data = json.loads(beat.read_text(encoding="utf-8"))
        at = _parse(data.get("at"))
        if at is not None:
            age = (now - at).total_seconds()
    except Exception:
        age = None

    if age is None:
        row = rt.conn.execute("SELECT MAX(updated_at) AS ts FROM jobs").fetchone()
        last = _parse(row["ts"] if row else None)
        if last is None:
            out["note"] = "아직 실행한 작업이 없습니다"
        else:
            gap = now - last
            when = last.strftime("%m-%d %H:%M")
            if gap <= timedelta(minutes=STALL_MINUTES):
                out.update(state="응답 중", grade=OK, note=f"마지막 작업 활동 {when}")
            else:
                minutes = int(gap.total_seconds() // 60)
                out.update(
                    state="멈춤", grade=BAD,
                    note=f"마지막 작업 활동 {when} ({minutes}분 전)",
                )
    elif age <= HEARTBEAT_STALE_SECONDS:
        out.update(state="정상", grade=OK, note=f"심장박동 {int(age)}초 전")
    else:
        out.update(state="멈춤", grade=BAD, note=f"심장박동 {int(age)}초 전 — 기준 {HEARTBEAT_STALE_SECONDS}초")
    return out


def _schedule_fired(rt: Runtime, date: str) -> tuple[int, int]:
    """(그날 발사한 예약 수, 켜져 있는 예약 수)."""
    fired = 0
    try:
        state = json.loads(
            (Path(rt.settings.data_dir) / "schedule_state.json").read_text(encoding="utf-8")
        )
        fired = sum(1 for v in (state.get("last_fired") or {}).values() if str(v) == date)
    except Exception:
        fired = 0
    total = 0
    try:
        from v2r.config import load_yaml

        entries = (load_yaml("schedule") or {}).get("entries") or []
        total = sum(1 for e in entries if e.get("enabled", True))
    except Exception:
        total = 0
    return fired, total


def _cafe_rows(rt: Runtime, date: str) -> list[dict]:
    """카페별 성공·실패·제한·미확정·계정 수·게시판 수·게시판 연속."""
    limited = limited_sql(rt)
    rows = rt.conn.execute(
        f"SELECT {CAFE_EXPR} AS cafe,"
        " SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,"
        f" SUM(CASE WHEN {limited} THEN 1 ELSE 0 END) AS limited,"
        f" SUM(CASE WHEN status = 'failed' AND NOT {limited} THEN 1 ELSE 0 END) AS failed,"
        " SUM(CASE WHEN status = 'uncertain' THEN 1 ELSE 0 END) AS uncertain,"
        " COUNT(DISTINCT NULLIF(TRIM(COALESCE(account, '')), '')) AS accounts,"
        f" COUNT(DISTINCT {BOARD_EXPR}) AS boards,"
        " MAX(created_at) AS last_at"
        " FROM publications WHERE substr(created_at, 1, 10) = ?"
        " GROUP BY cafe ORDER BY done DESC, cafe",
        (date,),
    ).fetchall()
    out = [dict(r) for r in rows]
    runs = _board_runs(rt, date)
    for row in out:
        row["board_run"] = int(runs.get(row["cafe"], 0))
    return out


def _board_runs(rt: Runtime, date: str) -> dict[str, int]:
    """카페마다 **같은 게시판이 연달아** 올라간 횟수(바로 앞 글과 같은 자리 수)."""
    rows = rt.conn.execute(
        f"SELECT {CAFE_EXPR} AS cafe, {BOARD_EXPR} AS board, created_at"
        " FROM publications WHERE substr(created_at, 1, 10) = ? AND status = 'done'"
        " ORDER BY cafe, created_at, rowid",
        (date,),
    ).fetchall()
    out: dict[str, int] = {}
    prev_cafe = prev_board = None
    for row in rows:
        cafe, board = str(row["cafe"]), str(row["board"])
        out.setdefault(cafe, 0)
        if cafe == prev_cafe and board == prev_board:
            out[cafe] += 1
        prev_cafe, prev_board = cafe, board
    return out


def _hourly(rt: Runtime, date: str) -> list[int]:
    """0~23시 시간대별 성공 발행 건수."""
    out = [0] * 24
    rows = rt.conn.execute(
        "SELECT CAST(substr(created_at, 12, 2) AS INTEGER) AS h, COUNT(*) AS n"
        " FROM publications WHERE substr(created_at, 1, 10) = ? AND status = 'done'"
        " GROUP BY h",
        (date,),
    ).fetchall()
    for row in rows:
        hour = row["h"]
        if hour is not None and 0 <= int(hour) <= 23:
            out[int(hour)] = int(row["n"])
    return out


def _accounts_summary(rt: Runtime, date: str) -> dict:
    """그날 쓴 계정 수 — 전체·카페별 최소/최대."""
    rows = rt.conn.execute(
        f"SELECT {CAFE_EXPR} AS cafe,"
        " COUNT(DISTINCT NULLIF(TRIM(COALESCE(account, '')), '')) AS n"
        " FROM publications WHERE substr(created_at, 1, 10) = ? GROUP BY cafe",
        (date,),
    ).fetchall()
    counts = [int(r["n"]) for r in rows if int(r["n"]) > 0]
    total = rt.conn.execute(
        "SELECT COUNT(DISTINCT NULLIF(TRIM(COALESCE(account, '')), '')) AS n"
        " FROM publications WHERE substr(created_at, 1, 10) = ?",
        (date,),
    ).fetchone()["n"]
    return {
        "total": int(total or 0),
        "min": min(counts) if counts else 0,
        "max": max(counts) if counts else 0,
    }


def _llm_usage(rt: Runtime, date: str) -> dict:
    """그날 모델 호출 — 요금제 호출 수·캐시율·유료(API) 건수."""
    out = {
        "plan_calls": 0,
        "paid_calls": 0,
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "cache_ratio": 0.0,
        "brand_calls": 0,
    }
    try:
        from v2r.llm.usage_ledger import read_month

        when = datetime.strptime(date, "%Y-%m-%d")
        rows = read_month(rt.settings.data_dir, when)
    except Exception as exc:  # pragma: no cover - 장부는 덤이다
        log.debug("사용량 장부를 읽지 못했습니다: %s", exc)
        rows = []
    for row in rows:
        if str(row.get("at") or "")[:10] != date:
            continue
        backend = str(row.get("backend") or "").strip().lower()
        if backend == "plan":
            out["plan_calls"] += 1
        else:
            out["paid_calls"] += 1
        out["input"] += int(row.get("input_tokens", 0) or 0)
        out["output"] += int(row.get("output_tokens", 0) or 0)
        out["cache_read"] += int(row.get("cache_read_input_tokens", 0) or 0)
        out["cache_creation"] += int(row.get("cache_creation_input_tokens", 0) or 0)
        if "brand" in str(row.get("purpose") or "").lower():
            out["brand_calls"] += 1
    seen = out["cache_read"] + out["cache_creation"] + out["input"]
    out["cache_ratio"] = (out["cache_read"] / seen) if seen else 0.0
    return out


#: 세션 점검 작업 → 사람이 읽는 이름·유지 방식
SESSION_SOURCES: list[tuple[str, str, str]] = [
    ("naver_keepalive", "네이버", "영속 프로필 · 하루 4회 방문"),
    ("web_keepalive", "Claude 플랫폼", "크롬 프로필 점검"),
    ("plan_keepalive", "Claude Code CLI(요금제)", "자격증명 백업 · 복원"),
    ("gpt_keepalive", "ChatGPT / Codex", "앱 로그인"),
    ("web_keepalive", "Make", "크롬 프로필 점검"),
]


def _last_job(rt: Runtime, task: str) -> dict | None:
    row = rt.conn.execute(
        "SELECT * FROM jobs WHERE task = ? AND status = 'done'"
        " ORDER BY id DESC LIMIT 1",
        (task,),
    ).fetchone()
    return dict(row) if row else None


def _result_of(job: dict | None) -> dict:
    if not job:
        return {}
    try:
        data = json.loads(job.get("result_json") or "{}")
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _sessions(rt: Runtime, now: datetime) -> list[dict]:
    """로그인·세션 유지 표. 세션 점검 작업 기록에서 실측한다."""
    rows: list[dict] = []

    # V2R — API 세션 파일이 살아 있으면 유지로 본다
    session_file = Path(rt.settings.data_dir) / "session.json"
    if session_file.is_file():
        seen = datetime.fromtimestamp(session_file.stat().st_mtime, KST)
        rows.append(
            {
                "name": "V2R",
                "how": "API 세션 자동 갱신",
                "at": seen.strftime("%m-%d %H:%M"),
                "state": "유지",
                "grade": OK,
            }
        )
    else:
        rows.append(
            {"name": "V2R", "how": "API 세션 자동 갱신", "at": "-", "state": "기록 없음", "grade": OFF}
        )

    for task, name, how in SESSION_SOURCES:
        job = _last_job(rt, task)
        result = _result_of(job)
        at = _parse(job.get("updated_at")) if job else None
        stamp = at.strftime("%m-%d %H:%M") if at else "-"
        if not job:
            rows.append({"name": name, "how": how, "at": "-", "state": "점검 전", "grade": OFF})
            continue
        if task == "web_keepalive":
            key = "make" if name == "Make" else "claude"
            site = (result.get("sites") or {}).get(key) or {}
            ok = bool(site.get("logged_in"))
            note = str(site.get("note") or "")
            if not ok and "프로필 없음" in note:
                rows.append({"name": name, "how": how, "at": stamp, "state": "대기", "grade": OFF})
                continue
        else:
            ok = bool(result.get("logged_in", result.get("ok")))
        rows.append(
            {
                "name": name,
                "how": how,
                "at": stamp,
                "state": "유지" if ok else "풀림",
                "grade": OK if ok else BAD,
            }
        )
    return rows


def _targets(rt: Runtime, date: str) -> dict[str, int]:
    """그날 발행 목표(카페별). 그날 등록된 발행 작업의 명세에서 읽는다."""
    out: dict[str, int] = {}
    rows = rt.conn.execute(
        "SELECT spec_json FROM jobs WHERE substr(created_at, 1, 10) = ?"
        " AND task LIKE 'publish%' AND status IN ('queued', 'running', 'done')",
        (date,),
    ).fetchall()
    self_cafes = [
        str(c.get("name") or "").strip()
        for c in (rt.cafes_cfg.get("self_owned") or [])
        if str(c.get("name") or "").strip()
    ]
    for row in rows:
        try:
            spec = json.loads(row["spec_json"] or "{}")
        except Exception:
            continue
        count = int(spec.get("count") or 0)
        if count <= 0:
            continue
        cafe = str(spec.get("cafe") or "").strip()
        if spec.get("per_cafe"):
            targets = [cafe] if cafe else self_cafes
            add = str(spec.get("per_cafe_mode") or "") == "추가로"
            for name in targets:
                out[name] = (out.get(name, 0) + count) if add else max(out.get(name, 0), count)
        elif cafe:
            out[cafe] = out.get(cafe, 0) + count
    return out


def _eta(rt: Runtime, date: str, cafe: str, remaining: int, now: datetime) -> str:
    """남은 건수를 최근 발행 간격으로 나눠 본 예상 종료 시각."""
    if remaining <= 0:
        return "-"
    rows = rt.conn.execute(
        f"SELECT created_at FROM publications WHERE substr(created_at, 1, 10) = ?"
        f" AND status = 'done' AND {CAFE_EXPR} = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 20",
        (date, cafe),
    ).fetchall()
    stamps = [d for d in (_parse(r["created_at"]) for r in rows) if d is not None]
    if len(stamps) < 2:
        return "산출 불가"
    span = (stamps[0] - stamps[-1]).total_seconds()
    gap = span / (len(stamps) - 1)
    if gap <= 0:
        return "산출 불가"
    return (now + timedelta(seconds=gap * remaining)).strftime("%H:%M")


def _v2r_login_note(rt: Runtime, now: datetime) -> tuple[str, str, str]:
    """(값, 설명, 등급) — V2R 로그인 유지 카드."""
    path = Path(rt.settings.data_dir) / "login_log.json"
    last: datetime | None = None
    count = 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        stamps = [float(v) for v in (data.get("logins") or [])]
        count = len(stamps)
        if stamps:
            last = datetime.fromtimestamp(max(stamps), KST)
    except Exception:
        last = None
    if last is None:
        return "기록 없음", "로그인 기록 파일이 없습니다", OFF
    days = (now - last).days
    hours = int((now - last).total_seconds() // 3600) - days * 24
    return (
        "유지",
        f"{last.strftime('%m-%d %H:%M')} 이후 {days}일 {hours}시간 · 로그인 기록 {count}회",
        OK,
    )


def _pending_text(rt: Runtime) -> str:
    path = Path(rt.settings.repo_root).joinpath(*PENDING_FILE)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _pending_lines(text: str, limit: int = 12) -> list[str]:
    """미처리 목록에서 글머리·번호 줄만 뽑는다."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("- ", "* ")):
            out.append(line[2:].strip())
        elif len(line) > 2 and line[0].isdigit() and line[1] in ".)":
            out.append(line[2:].strip())
        if len(out) >= limit:
            break
    return out


def _used_hashes(rt: Runtime) -> set[str]:
    rows = rt.conn.execute(
        "SELECT DISTINCT content_hash FROM publications WHERE content_hash IS NOT NULL"
    ).fetchall()
    return {str(r["content_hash"]) for r in rows}


def manuscript_stock(rt: Runtime) -> dict:
    """제휴 일상 글 풀(카페별 개수·미사용 수)과 각색 xlsx 파일 수."""
    out: dict = {"pool": [], "pool_total": 0, "pool_unused": 0, "xlsx": 0}
    root = Path(rt.settings.warehouse_dir)
    pool_path = root / "manuscripts" / "affiliate_daily_pool.jsonl"
    if pool_path.exists():
        try:
            used = _used_hashes(rt)
        except Exception:  # pragma: no cover - DB가 이상해도 잔량은 보여준다
            used = set()
        per_cafe: dict[str, dict[str, int]] = {}
        for line in pool_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if not isinstance(item, dict):
                continue
            cafe = str(item.get("cafe") or "(미지정)").strip() or "(미지정)"
            bucket = per_cafe.setdefault(cafe, {"total": 0, "unused": 0})
            bucket["total"] += 1
            out["pool_total"] += 1
            if str(item.get("content_hash") or "") not in used:
                bucket["unused"] += 1
                out["pool_unused"] += 1
        out["pool"] = [
            {"cafe": cafe, **counts} for cafe, counts in sorted(per_cafe.items())
        ]
    sheets_dir = root / "inbox" / "sheets"
    if sheets_dir.is_dir():
        out["xlsx"] = sum(1 for p in sheets_dir.glob("*.xlsx") if p.is_file())
    return out


# --------------------------------------------------------------------
# 한 벌로 묶은 자료
# --------------------------------------------------------------------
@dataclass
class ReportData:
    """보고서 한 장을 그리는 데 필요한 자료 전부."""

    kind: str
    date: str
    now: datetime
    cafes: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    hourly: list[int] = field(default_factory=lambda: [0] * 24)
    llm: dict = field(default_factory=dict)
    sessions: list[dict] = field(default_factory=list)
    executor: dict = field(default_factory=dict)
    accounts: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)
    v2r: tuple[str, str, str] = ("-", "", OFF)
    fired: tuple[int, int] = (0, 0)
    pending: str = ""

    @property
    def target_total(self) -> int:
        return sum(self.targets.values())

    @property
    def rate(self) -> float:
        total = self.target_total
        return (self.totals.get("done", 0) / total) if total else 0.0

    @property
    def remaining_total(self) -> int:
        return sum(int(c.get("remaining", 0) or 0) for c in self.cafes)

    @property
    def title(self) -> str:
        tag = KIND_LABELS.get(self.kind, "현황판")
        if self.kind == "daily":
            return f"{SECTION_TITLES['header']} — {self.date} (어제) · {tag}"
        if self.kind == "progress":
            return (
                f"{SECTION_TITLES['header']} — {self.date} {self.now.strftime('%H:%M')} 기준 · {tag}"
            )
        return f"{SECTION_TITLES['header']} — {self.date} · {tag}"


def collect(rt: Runtime, kind: str = "live", date: str = "", now: datetime | None = None) -> ReportData:
    """보고서 자료를 한 번에 모은다(실측)."""
    moment = now or datetime.now(KST)
    day = date or (_yesterday(moment) if kind == "daily" else _today(moment))

    cafes = _cafe_rows(rt, day)
    targets = _targets(rt, day)
    for row in cafes:
        goal = int(targets.get(str(row["cafe"]), 0))
        row["target"] = goal
        row["remaining"] = max(0, goal - int(row["done"] or 0))
        row["eta"] = _eta(rt, day, str(row["cafe"]), row["remaining"], moment) if kind == "progress" else "-"
    # 목표는 있는데 아직 한 건도 안 올라간 카페도 줄로 보여 준다
    known = {str(r["cafe"]) for r in cafes}
    for name, goal in targets.items():
        if name and name not in known:
            cafes.append(
                {
                    "cafe": name, "done": 0, "failed": 0, "limited": 0, "uncertain": 0,
                    "accounts": 0, "boards": 0, "board_run": 0, "last_at": None,
                    "target": goal, "remaining": goal,
                    "eta": "산출 불가" if kind == "progress" else "-",
                }
            )

    totals = {
        key: sum(int(r.get(key, 0) or 0) for r in cafes)
        for key in ("done", "failed", "limited", "uncertain", "board_run")
    }

    data = ReportData(
        kind=kind,
        date=day,
        now=moment,
        cafes=cafes,
        totals=totals,
        hourly=_hourly(rt, day),
        llm=_llm_usage(rt, day),
        sessions=_sessions(rt, moment),
        executor=_executor_state(rt, moment),
        accounts=_accounts_summary(rt, day),
        targets=targets,
        v2r=_v2r_login_note(rt, moment),
        fired=_schedule_fired(rt, day),
        pending=_pending_text(rt),
    )
    return data


# --------------------------------------------------------------------
# 카드 (한눈에)
# --------------------------------------------------------------------
def _cards(data: ReportData) -> list[dict]:
    """6칸(중간 보고는 7칸) 카드."""
    t = data.totals
    goal = data.target_total
    done = int(t.get("done", 0))
    pub_grade = OK if not t.get("failed") and not t.get("limited") else WARN
    pub_note = f"목표 {goal or '-'} · 실패 {t.get('failed', 0)} · 제한 글 {t.get('limited', 0)}"

    acc = data.accounts
    acc_value = (
        f"{acc.get('min', 0)}~{acc.get('max', 0)}" if acc.get("min") != acc.get("max")
        else str(acc.get("max", 0))
    )
    run = int(t.get("board_run", 0))
    fired, entries = data.fired
    ex = data.executor
    llm = data.llm
    paid = int(llm.get("paid_calls", 0))

    cards = [
        {
            "k": "발행 성공", "v": str(done), "d": pub_note, "grade": pub_grade,
        },
        {
            "k": "사용 계정", "v": acc_value, "unit": "/카페",
            "d": f"그날 쓴 계정 전체 {acc.get('total', 0)}개",
            "grade": OK if acc.get("max", 0) else OFF,
        },
        {
            "k": "같은 게시판 연속", "v": str(run),
            "d": "연속 0이 규칙" if run == 0 else "연속 방지 규칙 확인 필요",
            "grade": OK if run == 0 else WARN,
        },
        {
            "k": "V2R 로그인", "v": data.v2r[0], "d": data.v2r[1], "grade": data.v2r[2],
        },
        {
            "k": "원고 비용", "v": "0원" if paid == 0 else f"유료 {paid}건",
            "d": f"요금제 {llm.get('plan_calls', 0)}회 · 캐시 {llm.get('cache_ratio', 0.0) * 100:.0f}%",
            "grade": OK if paid == 0 else WARN,
        },
        {
            "k": "실행기", "v": ex.get("state", "-"),
            "d": f"{ex.get('note', '')} · 예약 {fired}/{entries or '-'} 발사",
            "grade": ex.get("grade", OFF),
        },
    ]
    if data.kind == "progress":
        cards.append(
            {
                "k": "목표 대비 진행률",
                "v": f"{data.rate * 100:.0f}%" if goal else "-",
                "d": f"{done}/{goal or '-'}건 · 남은 {data.remaining_total}건",
                "grade": OK if goal and data.rate >= 0.99 else (WARN if goal else OFF),
            }
        )
    return cards


def _cafe_badge(row: dict, kind: str) -> tuple[str, str]:
    """(뱃지 글자, 등급)."""
    bits = []
    if int(row.get("failed", 0) or 0):
        bits.append(f"실패 {row['failed']}")
    if int(row.get("limited", 0) or 0):
        bits.append(f"제한 {row['limited']}")
    if int(row.get("uncertain", 0) or 0):
        bits.append(f"미확정 {row['uncertain']}")
    if bits:
        grade = BAD if int(row.get("failed", 0) or 0) else WARN
        return " · ".join(bits), grade
    if kind == "progress" and int(row.get("remaining", 0) or 0) > 0:
        return "진행 중", WARN
    return "완료", OK


# --------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------
CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--mute:#6b7683;
--line:#e3e7ec;--head:#f0f3f7;--sum:#fafbfc;--ok:#1a9c5b;--warn:#d9890b;--bad:#d43f3f;
--bar:#2f6fed;--bar2:#c9d6f5;--off:#9aa4b0}
@media (prefers-color-scheme: dark){:root{--bg:#12151a;--card:#191d24;--ink:#e8ecf2;
--mute:#9aa4b2;--line:#2a303a;--head:#20252e;--sum:#1e232b;--ok:#57c78c;--warn:#e2b155;
--bad:#f18a8f;--bar:#6ea8fe;--bar2:#2f3a4d;--off:#6b7683}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Malgun Gothic",
"Apple SD Gothic Neo",system-ui,sans-serif;padding:20px 16px 40px;font-size:14px;
line-height:1.55;-webkit-text-size-adjust:100%}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--mute);margin-bottom:18px}
h2{font-size:15px;margin:26px 0 10px;padding-left:10px;border-left:4px solid var(--bar)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{color:var(--mute);font-size:12px}
.card .v{font-size:26px;font-weight:700;margin:4px 0 2px;font-variant-numeric:tabular-nums}
.card .d{font-size:12px;color:var(--mute)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.off{color:var(--off)}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
font-weight:600;color:#fff;white-space:nowrap}
.pill.ok{background:var(--ok)}.pill.warn{background:var(--warn)}
.pill.bad{background:var(--bad)}.pill.off{background:var(--off)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:10px;overflow:hidden}
th,td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left;
font-variant-numeric:tabular-nums}
th{background:var(--head);color:var(--mute);font-weight:600;font-size:12px}
td.n,th.n{text-align:right}
tr:last-child td{border-bottom:0}
tr.sum td{font-weight:700;background:var(--sum)}
.bar{display:flex;align-items:flex-end;gap:4px;height:120px;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:18px 12px 26px;position:relative}
.bar div{flex:1;background:var(--bar);border-radius:3px 3px 0 0;position:relative;min-width:8px}
.bar div span{position:absolute;bottom:-20px;left:0;right:0;text-align:center;
font-size:10px;color:var(--mute)}
.bar div b{position:absolute;top:-16px;left:0;right:0;text-align:center;font-size:10px;
font-weight:600}
.bar div.low{background:var(--bar2)}
ul{margin:6px 0 0 18px;padding:0}li{margin:4px 0}
.tbl{overflow-x:auto}
.empty{color:var(--mute);margin:4px 0 0}
footer{color:var(--mute);font-size:12px;text-align:center;padding:16px 0 0}
"""


def _pill(text: str, grade: str) -> str:
    return f'<span class="pill {grade}">{html.escape(text)}</span>'


def _cafe_headers(kind: str) -> list[str]:
    base = ["카페", "성공", "실패", "제한 글", "미확정", "계정 수", "게시판 수", "게시판 연속"]
    if kind == "progress":
        base += ["남은 건수", "예상 종료"]
    return base + ["상태"]


def render_html_report(data: ReportData) -> str:
    """보고서 HTML 한 장(바깥 의존 없음)."""
    parts: list[str] = []
    stamp = data.now.strftime("%Y-%m-%d %H:%M KST")
    parts.append(f"<h1>{html.escape(data.title)}</h1>")
    parts.append(
        f'<div class="sub">작성 {html.escape(stamp)} 실측 · 자사 카페 일상 글 · '
        "브랜드 원고 · 로그인 · 비용</div>"
    )

    # 1) 한눈에
    parts.append(f'<h2>{html.escape(SECTION_TITLES["overview"])}</h2><div class="grid">')
    for card in _cards(data):
        unit = card.get("unit")
        unit_html = f'<span style="font-size:13px">{html.escape(unit)}</span>' if unit else ""
        parts.append(
            f'<div class="card"><div class="k">{html.escape(card["k"])}</div>'
            f'<div class="v {card["grade"]}">{html.escape(card["v"])}{unit_html}</div>'
            f'<div class="d">{html.escape(card["d"])}</div></div>'
        )
    parts.append("</div>")

    # 2) 카페별
    parts.append(f'<h2>{html.escape(SECTION_TITLES["cafes"])}</h2>')
    headers = _cafe_headers(data.kind)
    head = "".join(
        f'<th class="n">{html.escape(h)}</th>' if h not in ("카페", "상태", "예상 종료")
        else f"<th>{html.escape(h)}</th>"
        for h in headers
    )
    body: list[str] = []
    for row in data.cafes:
        badge, grade = _cafe_badge(row, data.kind)
        cells = [
            f"<td>{_esc(row['cafe'])}</td>",
            f'<td class="n">{int(row.get("done", 0) or 0)}</td>',
            f'<td class="n">{int(row.get("failed", 0) or 0)}</td>',
            f'<td class="n">{int(row.get("limited", 0) or 0)}</td>',
            f'<td class="n">{int(row.get("uncertain", 0) or 0)}</td>',
            f'<td class="n">{int(row.get("accounts", 0) or 0)}</td>',
            f'<td class="n">{int(row.get("boards", 0) or 0)}</td>',
            f'<td class="n">{int(row.get("board_run", 0) or 0)}</td>',
        ]
        if data.kind == "progress":
            cells.append(f'<td class="n">{int(row.get("remaining", 0) or 0)}</td>')
            cells.append(f"<td>{_esc(row.get('eta'))}</td>")
        cells.append(f"<td>{_pill(badge, grade)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    if body:
        t = data.totals
        sums = [
            "<td>합계</td>",
            f'<td class="n">{t.get("done", 0)}</td>',
            f'<td class="n">{t.get("failed", 0)}</td>',
            f'<td class="n">{t.get("limited", 0)}</td>',
            f'<td class="n">{t.get("uncertain", 0)}</td>',
            "<td></td>", "<td></td>",
            f'<td class="n">{t.get("board_run", 0)}</td>',
        ]
        if data.kind == "progress":
            sums.append(f'<td class="n">{data.remaining_total}</td>')
            sums.append("<td></td>")
        sums.append("<td></td>")
        body.append('<tr class="sum">' + "".join(sums) + "</tr>")
        parts.append(
            f'<div class="tbl"><table><tr>{head}</tr>' + "".join(body) + "</table></div>"
        )
    else:
        parts.append('<p class="empty">발행 기록이 없습니다.</p>')

    # 3) 시간대별
    parts.append(f'<h2>{html.escape(SECTION_TITLES["hourly"])}</h2><div class="bar">')
    top = max(data.hourly) or 1
    for hour, n in enumerate(data.hourly):
        pct = max(2, round(n * 100 / top))
        cls = "" if n else ' class="low"'
        value = f"<b>{n}</b>" if n else ""
        parts.append(f'<div{cls} style="height:{pct}%">{value}<span>{hour:02d}</span></div>')
    parts.append("</div>")
    parts.append(
        f'<ul><li>그날 성공 발행 {sum(data.hourly)}건 · 가장 많은 시간대 '
        f"{data.hourly.index(top) if max(data.hourly) else 0:02d}시 {max(data.hourly)}건</li></ul>"
    )

    # 4) 브랜드 원고 · LLM
    llm = data.llm
    parts.append(f'<h2>{html.escape(SECTION_TITLES["llm"])}</h2><div class="tbl"><table>')
    parts.append("<tr><th>항목</th><th>값</th><th>상태</th></tr>")
    rows_llm = [
        ("요금제 호출", f"{llm.get('plan_calls', 0)}회", OK, "0원"),
        (
            "캐시율",
            f"{llm.get('cache_ratio', 0.0) * 100:.1f}% (캐시 읽기 {llm.get('cache_read', 0):,} 토큰)",
            OK if llm.get("cache_ratio", 0) >= 0.3 else WARN,
            "적용" if llm.get("cache_ratio", 0) >= 0.3 else "낮음",
        ),
        (
            "유료 건수",
            f"{llm.get('paid_calls', 0)}건",
            OK if not llm.get("paid_calls") else WARN,
            "0원" if not llm.get("paid_calls") else "유료 사용",
        ),
        (
            "브랜드 원고 호출",
            f"{llm.get('brand_calls', 0)}회",
            OK if llm.get("brand_calls") else OFF,
            "가동" if llm.get("brand_calls") else "없음",
        ),
        (
            "토큰",
            f"새 입력 {llm.get('input', 0):,} · 출력 {llm.get('output', 0):,}",
            OFF, "실측",
        ),
    ]
    for name, value, grade, badge in rows_llm:
        parts.append(
            f"<tr><td>{html.escape(name)}</td><td>{html.escape(value)}</td>"
            f"<td>{_pill(badge, grade)}</td></tr>"
        )
    parts.append("</table></div>")

    # 5) 로그인 · 세션 유지
    parts.append(f'<h2>{html.escape(SECTION_TITLES["sessions"])}</h2><div class="tbl"><table>')
    parts.append("<tr><th>서비스</th><th>유지 방식</th><th>마지막 확인</th><th>상태</th></tr>")
    for row in data.sessions:
        parts.append(
            f"<tr><td>{_esc(row['name'])}</td><td>{_esc(row['how'])}</td>"
            f"<td>{_esc(row['at'])}</td><td>{_pill(row['state'], row['grade'])}</td></tr>"
        )
    parts.append("</table></div>")

    # 6) 오늘 진행 · 결정 대기
    parts.append(f'<h2>{html.escape(SECTION_TITLES["progress"])}</h2>')
    lines = _pending_lines(data.pending)
    if lines:
        parts.append("<ul>" + "".join(f"<li>{_esc(line)}</li>" for line in lines) + "</ul>")
    else:
        parts.append('<p class="empty">미처리 목록(docs/reports/pending.md)이 비어 있습니다.</p>')

    body_html = "\n".join(parts)
    return (
        "<!doctype html>\n"
        '<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(data.title)}</title>"
        f"<style>{CSS}</style></head><body><div class=\"wrap\">\n"
        f"{body_html}\n"
        "<footer>V2R 운영 현황판 — 자동 생성</footer>"
        "</div></body></html>\n"
    )


# --------------------------------------------------------------------
# MD (같은 내용)
# --------------------------------------------------------------------
_MARK = {OK: "✅", WARN: "⚠️", BAD: "❌", OFF: "⏸"}


def render_md_report(data: ReportData) -> str:
    """HTML 과 같은 내용의 마크다운."""
    out: list[str] = []
    out.append(f"# {data.title}")
    out.append("")
    out.append(f"작성 {data.now.strftime('%Y-%m-%d %H:%M KST')} 실측 · 기준일 {data.date}")
    out.append("")

    out.append(f"## 0. {SECTION_TITLES['overview']}")
    out.append("| 항목 | 값 | 설명 | 상태 |")
    out.append("|---|---|---|---|")
    for card in _cards(data):
        unit = card.get("unit") or ""
        out.append(
            f"| {card['k']} | {card['v']}{unit} | {card['d']} | {_MARK.get(card['grade'], '')} |"
        )
    out.append("")

    out.append(f"## 1. {SECTION_TITLES['cafes']}")
    headers = _cafe_headers(data.kind)
    out.append("| " + " | ".join(headers) + " |")
    out.append("|" + "---|" * len(headers))
    for row in data.cafes:
        badge, grade = _cafe_badge(row, data.kind)
        cells = [
            str(row["cafe"]),
            str(int(row.get("done", 0) or 0)),
            str(int(row.get("failed", 0) or 0)),
            str(int(row.get("limited", 0) or 0)),
            str(int(row.get("uncertain", 0) or 0)),
            str(int(row.get("accounts", 0) or 0)),
            str(int(row.get("boards", 0) or 0)),
            str(int(row.get("board_run", 0) or 0)),
        ]
        if data.kind == "progress":
            cells.append(str(int(row.get("remaining", 0) or 0)))
            cells.append(str(row.get("eta") or "-"))
        cells.append(f"{_MARK.get(grade, '')} {badge}")
        out.append("| " + " | ".join(cells) + " |")
    if data.cafes:
        t = data.totals
        cells = [
            "**합계**", f"**{t.get('done', 0)}**", f"**{t.get('failed', 0)}**",
            f"**{t.get('limited', 0)}**", f"**{t.get('uncertain', 0)}**", "", "",
            f"**{t.get('board_run', 0)}**",
        ]
        if data.kind == "progress":
            cells += [f"**{data.remaining_total}**", ""]
        cells.append("")
        out.append("| " + " | ".join(cells) + " |")
    else:
        out.append("| (발행 기록이 없습니다) |" + " |" * (len(headers) - 1))
    out.append("")

    out.append(f"## 2. {SECTION_TITLES['hourly']}")
    out.append("| 시각 | 건수 |")
    out.append("|---|---|")
    for hour, n in enumerate(data.hourly):
        if n:
            out.append(f"| {hour:02d}시 | {n} |")
    if not sum(data.hourly):
        out.append("| - | 0 |")
    out.append("")

    llm = data.llm
    out.append(f"## 3. {SECTION_TITLES['llm']}")
    out.append("| 항목 | 값 |")
    out.append("|---|---|")
    out.append(f"| 요금제 호출 | {llm.get('plan_calls', 0)}회 |")
    out.append(f"| 캐시율 | {llm.get('cache_ratio', 0.0) * 100:.1f}% |")
    out.append(f"| 유료 건수 | {llm.get('paid_calls', 0)}건 |")
    out.append(f"| 브랜드 원고 호출 | {llm.get('brand_calls', 0)}회 |")
    out.append(
        f"| 토큰 | 새 입력 {llm.get('input', 0):,} · 출력 {llm.get('output', 0):,}"
        f" · 캐시 읽기 {llm.get('cache_read', 0):,} |"
    )
    out.append("")

    out.append(f"## 4. {SECTION_TITLES['sessions']}")
    out.append("| 서비스 | 유지 방식 | 마지막 확인 | 상태 |")
    out.append("|---|---|---|---|")
    for row in data.sessions:
        out.append(
            f"| {row['name']} | {row['how']} | {row['at']} |"
            f" {_MARK.get(row['grade'], '')} {row['state']} |"
        )
    out.append("")

    out.append(f"## 5. {SECTION_TITLES['progress']}")
    lines = _pending_lines(data.pending)
    if lines:
        out.extend(f"- {line}" for line in lines)
    else:
        out.append("- 미처리 목록(docs/reports/pending.md)이 비어 있습니다.")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------
# 파일로 쓰기
# --------------------------------------------------------------------
def render_html(rt: Runtime) -> str:
    """`현황판` 명령이 쓰는 오늘 기준 HTML 문자열."""
    return render_html_report(collect(rt, kind="live"))


def build_dashboard(rt: Runtime) -> Path:
    """현황판 HTML을 만들어 `data/dashboard.html`에 쓰고 경로를 돌려준다."""
    path = Path(rt.settings.data_dir) / "dashboard.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(rt), encoding="utf-8")
    return path


def _write_pair(data: ReportData, out_dir: Path, stem: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{stem}.html"
    md_path = out_dir / f"{stem}.md"
    html_path.write_text(render_html_report(data), encoding="utf-8")
    md_path.write_text(render_md_report(data), encoding="utf-8")
    return html_path, md_path


def build_daily_report(
    rt: Runtime,
    date: str = "",
    now: datetime | None = None,
    out_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    """어제 기준 일일 보고 → `docs/reports/dashboard-YYYY-MM-DD.html/.md`."""
    data = collect(rt, kind="daily", date=date, now=now)
    return _write_pair(data, _report_dir(rt, out_dir), f"dashboard-{data.date}")


def build_progress_report(
    rt: Runtime,
    now: datetime | None = None,
    out_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    """오늘 기준 중간 보고 → `docs/reports/progress-YYYY-MM-DD-HHMM.html/.md`."""
    data = collect(rt, kind="progress", now=now)
    stem = f"progress-{data.date}-{data.now.strftime('%H%M')}"
    return _write_pair(data, _report_dir(rt, out_dir), stem)


__all__ = [
    "KIND_LABELS",
    "ReportData",
    "SECTION_TITLES",
    "build_daily_report",
    "build_dashboard",
    "build_progress_report",
    "collect",
    "limited_sql",
    "manuscript_stock",
    "mask_account",
    "render_html",
    "render_html_report",
    "render_md_report",
]
