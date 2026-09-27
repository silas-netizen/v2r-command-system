"""통합 현황판(한 장, 덮어쓰기) — 일상 글 · 노출 확인 · 키워드 1만.

docs/reports/ops-board-<오늘>.md 를 매시 정각에 다시 쓴다. 핵심 숫자만.
사용: .venv\\Scripts\\python.exe scripts\\ops_board.py [--note "문제 → 조치"]
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRANDS = ["팥순이", "우아덤", "장으뜸", "코숨핏", "뉴더미스", "갱년기"]
CAFES = ["고요한 아침", "글로시 마이", "송도포털", "러브 인썸", "마이 웨딩 드림"]
TARGET_PER_CAFE = 100
KW_TARGET = 10_000
EXPOSURE_EXPECT_PER_HOUR = 300

CONF_SQL = (
    "select count(*) from keywords where relevance_llm between 0 and 3 and relevance_codex between 0 and 3 "
    "and (relevance_llm<3 or coalesce(bridge_rationale,'')<>'') and (relevance_codex<3 or coalesce(bridge_rationale,'')<>'')"
)


def bar(pct: float, width: int = 20) -> str:
    n = max(0, min(width, round(pct / 100 * width)))
    return "█" * n + "░" * (width - n)


def main(argv: list[str]) -> int:
    now = dt.datetime.now()
    today = now.strftime("%Y-%m-%d")
    notes_path = ROOT / "data" / f"ops_notes-{today}.json"
    notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else []
    if "--note" in argv:
        notes.append({"at": now.strftime("%H:%M"), "text": argv[argv.index("--note") + 1]})
        notes_path.write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")
    hour_ago = (now - dt.timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M")

    c = sqlite3.connect(ROOT / "data" / "v2r.sqlite")
    # 일상 글
    like = f"{today}%"
    pub_total = c.execute("select count(*) from publications where created_at like ?", (like,)).fetchone()[0]
    pub_per = dict(c.execute("select cafe,count(*) from publications where created_at like ? group by 1", (like,)).fetchall())
    pub_hour = c.execute("select count(*) from publications where created_at>=?", (hour_ago,)).fetchone()[0]
    job = c.execute("select status from jobs where task='publish_daily' order by id desc limit 1").fetchone()
    pub_target = TARGET_PER_CAFE * len(CAFES)
    pub_state = {"running": "진행 중", "done": "완료", "failed": "실패", "queued": "대기"}.get(job[0], job[0]) if job else "없음"
    pub_flag = "🟢" if (pub_total >= pub_target or pub_hour >= 30) else ("🟡" if pub_hour >= 10 else "🔴")
    if 2 <= now.hour < 8:
        pub_flag = "⏸"  # 발행 허용 시간 밖

    # 노출 확인
    exp_hour = c.execute("select count(*) from keyword_exposure where checked_at>=?", (hour_ago,)).fetchone()[0]
    exp_today = c.execute("select count(*) from keyword_exposure where checked_at like ?", (like,)).fetchone()[0]
    q1, q3 = 0, 0
    for tier, n in c.execute("select tier, sum(done_at is null) from exposure_queue group by tier"):
        if tier == 1:
            q1 = n or 0
        elif tier == 3:
            q3 = n or 0
    checked = dict(c.execute("select brand, count(distinct keyword) from keyword_exposure group by 1").fetchall())
    exp_flag = "🟢" if exp_hour >= EXPOSURE_EXPECT_PER_HOUR else ("🟡" if exp_hour >= 100 else "🔴")
    exp_eta = f"약 {q3 / max(exp_hour, 1) / 24:.1f}일" if exp_hour else "속도 0"

    # 키워드
    kw_rows = []
    for b in BRANDS:
        p = ROOT / "data" / "keywords" / f"{b}.sqlite"
        if not p.exists():
            continue
        k = sqlite3.connect(p)
        conf = k.execute(CONF_SQL).fetchone()[0]
        pend = k.execute("select count(*) from keywords where relevance_llm is not null and relevance_codex is null").fetchone()[0]
        kw_rows.append((b, conf, pend))
    fp_path = ROOT / "data" / "keywords" / "fill_progress.json"
    fp = json.loads(fp_path.read_text(encoding="utf-8")) if fp_path.exists() else {}

    L = []
    L.append(f"# V2R 운영 현황판 — {today} {now:%H:%M}")
    L.append("")
    L.append(f"## {pub_flag} 일상 글  {pub_total} / {pub_target}건  ({pub_total*100//pub_target}%)  `{bar(pub_total*100/pub_target)}`")
    L.append(f"- 최근 1시간 {pub_hour}건 · 작업 {pub_state} · " + ", ".join(f"{cf[:4]} {pub_per.get(cf,0)}" for cf in CAFES))
    L.append("")
    L.append(f"## {exp_flag} 노출 확인  최근 1시간 {exp_hour}건 · 오늘 {exp_today}건")
    L.append(f"- 대기: 노출완 재검사 {q1:,} · 밀려남/미확인 {q3:,} → 1사이클 남은 기간 {exp_eta}")
    L.append("- 검사한 키워드: " + " · ".join(f"{b} {checked.get(b,0):,}" for b in BRANDS if b != "갱년기"))
    L.append("")
    L.append("## 키워드 1만 개")
    L.append("| 브랜드 | 확정 | 진행 | 2차 대기 | 채우기 |")
    L.append("|---|---:|---|---:|---|")
    for b, conf, pend in kw_rows:
        pct = min(conf * 100 / KW_TARGET, 100)
        st = (fp.get(b) or {}).get("status") or "-"
        st_k = {"running": "수집 중", "backlog_waiting": "적체 대기", "seed_waiting": "시드 대기", "achieved": "달성", "done": "종료"}.get(st, st)
        mark = "✅" if conf >= KW_TARGET else ""
        L.append(f"| {b} | {conf:,} {mark} | `{bar(pct, 12)}` {pct:.0f}% | {pend:,} | {st_k} |")
    L.append("")
    L.append("## 오늘 문제 · 조치")
    L += [f"- {n['at']} {n['text']}" for n in notes] or ["- 없음"]
    L.append("")
    L.append("## 사용자가 할 일")
    L.append("- 없음")
    L.append("")
    out = ROOT / "docs" / "reports" / f"ops-board-{today}.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(out)
    if "--slack" in argv:
        # 슬랙용 압축판(정각 1회). 표 대신 줄글.
        kw_line = " · ".join(f"{b} {conf:,}" + ("✅" if conf >= KW_TARGET else "") for b, conf, _ in kw_rows)
        lines = [
            f"📊 운영 현황판 {today} {now:%H:%M}",
            f"{pub_flag} 일상 글 {pub_total}/{pub_target} · 최근 1시간 {pub_hour}건 · {pub_state}",
            f"{exp_flag} 노출 확인 최근 1시간 {exp_hour}건 · 대기 {q3:,} · 남은 {exp_eta}",
            f"🔑 키워드 {kw_line}",
        ]
        lines += [f"⚠ {n['at']} {n['text'][:80]}" for n in notes[-2:]]
        try:
            sys.path.insert(0, str(ROOT))
            from v2r.channels import build_channels, push_channels
            from v2r.config import get_settings

            sent = 0
            for ch in push_channels(build_channels(get_settings())):
                sent += ch.broadcast("\n".join(lines))
            print("slack sent", sent)
        except Exception as exc:  # noqa: BLE001
            print("slack failed", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
