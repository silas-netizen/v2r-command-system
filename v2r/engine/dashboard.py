"""운영 현황판(HTML) 생성기.

SQLite(jobs·events·publications)와 창고를 읽어 `data/dashboard.html` 한 장을 만든다.
바깥 스크립트·폰트를 전혀 쓰지 않는 자급자족 파일이며, 휴대폰 폭에서도 읽힌다.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from v2r.engine.context import Runtime
from v2r.store.db import KST

log = logging.getLogger(__name__)

#: 실행기가 '멈춤'으로 보이기 시작하는 무활동 시간(분)
STALL_MINUTES = 30
#: 최근 발행 표에 넣을 건수
RECENT_LIMIT = 20

STATUS_LABELS = {
    "queued": "대기",
    "running": "실행중",
    "done": "완료",
    "failed": "실패",
    "uncertain": "미확정",
    "cancelled": "취소",
    "skipped": "건너뜀",
}

SECTION_TITLES = {
    "header": "V2R 운영 현황",
    "today": "오늘 요약",
    "jobs": "진행 중·대기 작업",
    "cafes": "카페별 발행 현황",
    "recent": "최근 발행 20건",
    "check": "점검",
    "stock": "원고 잔량",
}


# --------------------------------------------------------------------
# 작은 도구
# --------------------------------------------------------------------
def _esc(value: object) -> str:
    """HTML 이스케이프. None·빈 값은 `-`."""
    text = "" if value is None else str(value)
    text = text.strip()
    return html.escape(text) if text else "-"


def _label(status: object) -> str:
    key = str(status or "").strip()
    return STATUS_LABELS.get(key, key or "-")


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


def _short_time(ts: object) -> str:
    """`2026-09-19T14:03:00+09:00` → `09-19 14:03`."""
    dt = _parse(ts)
    if dt is None:
        return _esc(ts)
    return html.escape(dt.strftime("%m-%d %H:%M"))


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _job_description(row: dict) -> str:
    """spec_json의 notes → 없으면 task."""
    try:
        spec = json.loads(row.get("spec_json") or "{}")
    except Exception:
        spec = {}
    notes = str(spec.get("notes") or "").strip()
    return notes or str(row.get("task") or "")


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    """표 HTML. 값은 이미 이스케이프된 것으로 본다."""
    if not rows:
        return f'<p class="empty">{html.escape(empty)}</p>'
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return (
        '<div class="scroll"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + body
        + "</tbody></table></div>"
    )


# --------------------------------------------------------------------
# 자료 모으기
# --------------------------------------------------------------------
def _executor_state(rt: Runtime) -> tuple[str, str]:
    """(상태 한 마디, 설명). 마지막 job 활동 시각으로 추정."""
    row = rt.conn.execute("SELECT MAX(updated_at) AS ts FROM jobs").fetchone()
    last = _parse(row["ts"] if row else None)
    if last is None:
        return "대기", "아직 실행한 작업이 없습니다"
    gap = datetime.now(KST) - last
    when = last.strftime("%m-%d %H:%M")
    if gap <= timedelta(minutes=STALL_MINUTES):
        return "응답 중", f"마지막 작업 활동 {when}"
    minutes = int(gap.total_seconds() // 60)
    return "멈춤", f"마지막 작업 활동 {when} ({minutes}분 전)"


def _today_summary(rt: Runtime) -> dict[str, int]:
    """오늘 발행 성공/실패/미확정/제한 + 진행 중·대기 작업 수."""
    today = _today()
    out = {"done": 0, "failed": 0, "uncertain": 0, "running": 0, "queued": 0, "limited": 0}
    rows = rt.conn.execute(
        "SELECT status, COUNT(*) AS n FROM publications"
        " WHERE substr(created_at, 1, 10) = ? GROUP BY status",  # 오늘 = 발행(생성) 시각 기준(수정으로 갱신된 행 제외)
        (today,),
    ).fetchall()
    for row in rows:
        key = str(row["status"])
        if key in out:
            out[key] = int(row["n"])
    jobs = rt.conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running')"
        " GROUP BY status"
    ).fetchall()
    for row in jobs:
        out[str(row["status"])] = int(row["n"])
    # 등록 제한으로 못 올라간 글(실패로 집계하되 따로도 보여 준다, 2026-09-22)
    out["limited"] = rt.publications.count_limited(today)
    return out


def _open_jobs(rt: Runtime) -> list[dict]:
    """대기·진행 중 작업 + 최근 이벤트 한 줄."""
    rows = rt.conn.execute(
        "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY id"
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        data = dict(row)
        event = rt.conn.execute(
            "SELECT message FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (int(data["id"]),),
        ).fetchone()
        data["progress"] = event["message"] if event else ""
        out.append(data)
    return out


def _cafe_rows(rt: Runtime) -> list[dict]:
    """카페별 오늘 done / 전체 done / uncertain / failed / 제한 / 마지막 발행 시각."""
    today = _today()
    rows = rt.conn.execute(
        "SELECT COALESCE(NULLIF(TRIM(COALESCE(cafe, '')), ''), '(미지정)') AS cafe_name,"
        " SUM(CASE WHEN status = 'done' AND substr(created_at, 1, 10) = ? THEN 1 ELSE 0 END)"
        "   AS today_done,"
        " SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS total_done,"
        " SUM(CASE WHEN status = 'uncertain' THEN 1 ELSE 0 END) AS uncertain,"
        " SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,"
        " SUM(CASE WHEN status = 'failed' AND COALESCE(stage, '') LIKE '%제한%'"
        "   THEN 1 ELSE 0 END) AS limited,"
        " MAX(created_at) AS last_at"
        " FROM publications GROUP BY cafe_name ORDER BY total_done DESC, cafe_name",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def _recent_publications(rt: Runtime, limit: int = RECENT_LIMIT) -> list[dict]:
    rows = rt.conn.execute(
        "SELECT * FROM publications ORDER BY updated_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _last_reconcile(rt: Runtime) -> str:
    """가장 최근 reconcile 작업 결과 요약(한국어)."""
    row = rt.conn.execute(
        "SELECT * FROM jobs WHERE task = 'reconcile' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "최근 점검(reconcile) 기록이 없습니다."
    data = dict(row)
    try:
        result = json.loads(data.get("result_json") or "{}")
    except Exception:
        result = {}
    when = _parse(data.get("updated_at"))
    stamp = when.strftime("%m-%d %H:%M") if when else "-"
    if not isinstance(result, dict) or not result:
        return f"{stamp} 점검 {_label(data.get('status'))} (결과 기록 없음)"
    unresolved = result.get("unresolved") or []
    return (
        f"{stamp} 점검 {_label(data.get('status'))} —"
        f" 확인 {result.get('checked', 0)}건, 완료 {result.get('done', 0)}건,"
        f" 실패 {result.get('failed', 0)}건(그중 제한 {result.get('limited', 0)}건),"
        f" 미해결 {len(unresolved)}건"
    )


def _warnings(rt: Runtime, limit: int = 10) -> list[dict]:
    rows = rt.conn.execute(
        "SELECT * FROM events WHERE level IN ('warn', 'warning', 'error')"
        " ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


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
# HTML 조각
# --------------------------------------------------------------------
CSS = """
:root {
  color-scheme: light dark;
  --bg: #f6f7f9; --card: #ffffff; --fg: #14181f; --muted: #5b6472;
  --line: #dfe3ea; --accent: #1f6feb; --ok: #197f4b; --warn: #a1670a; --bad: #b4242a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12151a; --card: #191d24; --fg: #e8ecf2; --muted: #9aa4b2;
    --line: #2a303a; --accent: #6ea8fe; --ok: #57c78c; --warn: #e2b155; --bad: #f18a8f;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font-family: "Malgun Gothic", "Apple SD Gothic Neo", system-ui, sans-serif;
  font-size: 15px; line-height: 1.55; -webkit-text-size-adjust: 100%;
}
.wrap { max-width: 980px; margin: 0 auto; }
h1 { font-size: 1.45rem; margin: 0 0 4px; }
h2 { font-size: 1.05rem; margin: 0 0 10px; }
section {
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 14px 14px 16px; margin: 0 0 14px;
}
.meta { color: var(--muted); font-size: 0.86rem; margin: 2px 0; }
.pill {
  display: inline-block; padding: 2px 10px; border-radius: 999px;
  border: 1px solid var(--line); font-size: 0.82rem; font-weight: 600;
}
.pill.ok { color: var(--ok); } .pill.bad { color: var(--bad); }
.cards { display: flex; flex-wrap: wrap; gap: 10px; }
.card {
  flex: 1 1 120px; min-width: 110px; border: 1px solid var(--line);
  border-radius: 10px; padding: 10px 12px;
}
.card .n { font-size: 1.5rem; font-weight: 700; }
.card .k { color: var(--muted); font-size: 0.82rem; }
.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; width: 100%; font-size: 0.88rem; }
th, td { border-bottom: 1px solid var(--line); padding: 7px 8px; text-align: left;
  vertical-align: top; white-space: nowrap; }
th { color: var(--muted); font-weight: 600; }
td.wrap-cell { white-space: normal; min-width: 180px; }
a { color: var(--accent); }
ul { margin: 6px 0 0; padding-left: 18px; }
li { margin: 2px 0; }
.empty { color: var(--muted); margin: 4px 0 0; }
.s-done { color: var(--ok); } .s-failed { color: var(--bad); }
.s-uncertain { color: var(--warn); }
footer { color: var(--muted); font-size: 0.8rem; text-align: center; padding: 6px 0 20px; }
"""


def _status_cell(status: object) -> str:
    key = str(status or "").strip()
    cls = {"done": "s-done", "failed": "s-failed", "uncertain": "s-uncertain"}.get(key, "")
    return f'<span class="{cls}">{html.escape(_label(key))}</span>' if cls else _esc(_label(key))


def render_html(rt: Runtime) -> str:
    """현황판 HTML 전체 문자열."""
    now = datetime.now(KST)
    state, state_note = _executor_state(rt)
    summary = _today_summary(rt)

    parts: list[str] = []

    # 1) 헤더
    pill = "ok" if state == "응답 중" else ("bad" if state == "멈춤" else "")
    parts.append(
        "<section>"
        f"<h1>{html.escape(SECTION_TITLES['header'])}</h1>"
        f'<p class="meta">생성 시각 {html.escape(now.strftime("%Y-%m-%d %H:%M:%S"))} (KST)</p>'
        f'<p class="meta">실행기 <span class="pill {pill}">{html.escape(state)}</span>'
        f" {html.escape(state_note)}</p>"
        "</section>"
    )

    # 2) 오늘 요약 카드
    cards = [
        ("오늘 발행 성공", summary["done"]),
        ("오늘 실패", summary["failed"]),
        ("제한 걸린 글", summary["limited"]),
        ("오늘 미확정", summary["uncertain"]),
        ("진행 중 작업", summary["running"]),
        ("대기 작업", summary["queued"]),
    ]
    card_html = "".join(
        f'<div class="card"><div class="n">{n}</div><div class="k">{html.escape(k)}</div></div>'
        for k, n in cards
    )
    parts.append(
        "<section>"
        f"<h2>{html.escape(SECTION_TITLES['today'])}</h2>"
        f'<div class="cards">{card_html}</div>'
        "</section>"
    )

    # 3) 진행 중·대기 작업
    job_rows = [
        [
            _esc(job["id"]),
            f'<span class="wrap-cell">{_esc(_job_description(job))}</span>',
            _esc(_label(job["status"])),
            _short_time(job.get("created_at")),
            f'<span class="wrap-cell">{_esc(job.get("progress"))}</span>',
        ]
        for job in _open_jobs(rt)
    ]
    parts.append(
        "<section>"
        f"<h2>{html.escape(SECTION_TITLES['jobs'])}</h2>"
        + _table(
            ["번호", "작업 설명", "상태", "시작 시각", "진행"],
            job_rows,
            "진행 중이거나 대기 중인 작업이 없습니다.",
        )
        + "</section>"
    )

    # 4) 카페별 발행 현황
    cafe_rows = [
        [
            _esc(row["cafe_name"]),
            _esc(row["today_done"]),
            _esc(row["total_done"]),
            _esc(row["uncertain"]),
            _esc(row["failed"]),
            _esc(row["limited"]),
            _short_time(row["last_at"]),
        ]
        for row in _cafe_rows(rt)
    ]
    parts.append(
        "<section>"
        f"<h2>{html.escape(SECTION_TITLES['cafes'])}</h2>"
        + _table(
            ["카페", "오늘 완료", "전체 완료", "미확정", "실패", "제한 걸린 글", "마지막 발행"],
            cafe_rows,
            "발행 기록이 없습니다.",
        )
        + "</section>"
    )

    # 5) 최근 발행 20건
    recent_rows = []
    for pub in _recent_publications(rt):
        url = str(pub.get("url") or "").strip()
        link = (
            f'<a href="{html.escape(url, quote=True)}" rel="noreferrer">열기</a>'
            if url.startswith(("http://", "https://"))
            else "-"
        )
        recent_rows.append(
            [
                _short_time(pub.get("updated_at")),
                _esc(pub.get("cafe")),
                _esc(pub.get("menu_id") or pub.get("cafe")),
                html.escape(mask_account(pub.get("account"))),
                _status_cell(pub.get("status")),
                link,
            ]
        )
    parts.append(
        "<section>"
        f"<h2>{html.escape(SECTION_TITLES['recent'])}</h2>"
        + _table(
            ["시각", "카페", "게시판", "계정", "상태", "링크"],
            recent_rows,
            "최근 발행 기록이 없습니다.",
        )
        + "</section>"
    )

    # 6) 점검
    uncertain_total = rt.conn.execute(
        "SELECT COUNT(*) AS n FROM publications WHERE status = 'uncertain'"
    ).fetchone()["n"]
    warn_rows = _warnings(rt)
    if warn_rows:
        warn_html = "<ul>" + "".join(
            f"<li>{_short_time(w['created_at'])} [{_esc(w['level'])}]"
            f" {_esc(w['message'])}</li>"
            for w in warn_rows
        ) + "</ul>"
    else:
        warn_html = '<p class="empty">최근 경고·오류가 없습니다.</p>'
    parts.append(
        "<section>"
        f"<h2>{html.escape(SECTION_TITLES['check'])}</h2>"
        f'<p class="meta">{html.escape(_last_reconcile(rt))}</p>'
        f'<p class="meta">남은 미확정 발행 {int(uncertain_total)}건</p>'
        "<p class=\"meta\">최근 경고·오류</p>" + warn_html + "</section>"
    )

    # 7) 원고 잔량
    stock = manuscript_stock(rt)
    stock_rows = [
        [_esc(row["cafe"]), _esc(row["total"]), _esc(row["unused"])]
        for row in stock["pool"]
    ]
    parts.append(
        "<section>"
        f"<h2>{html.escape(SECTION_TITLES['stock'])}</h2>"
        f'<p class="meta">제휴 일상 글 풀 전체 {stock["pool_total"]}건'
        f' (미사용 {stock["pool_unused"]}건)</p>'
        + _table(
            ["카페", "풀 개수", "미사용"],
            stock_rows,
            "제휴 일상 글 풀이 비어 있습니다.",
        )
        + f'<p class="meta">자사 각색 엑셀 파일 {stock["xlsx"]}개</p>'
        "</section>"
    )

    body = "\n".join(parts)
    return (
        "<!doctype html>\n"
        '<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(SECTION_TITLES['header'])}</title>"
        f"<style>{CSS}</style></head><body><div class=\"wrap\">\n"
        f"{body}\n"
        "<footer>V2R 운영 현황판 — 자동 생성</footer>"
        "</div></body></html>\n"
    )


def build_dashboard(rt: Runtime) -> Path:
    """현황판 HTML을 만들어 `data/dashboard.html`에 쓰고 경로를 돌려준다."""
    path = Path(rt.settings.data_dir) / "dashboard.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(rt), encoding="utf-8")
    return path


__all__ = ["build_dashboard", "manuscript_stock", "mask_account", "render_html"]
