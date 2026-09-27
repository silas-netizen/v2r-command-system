"""오늘 일상 글 현황판(한 장, 덮어쓰기) — docs/reports/daily-posts-board-<오늘>.md

사용: .venv\\Scripts\\python.exe scripts\\daily_posts_board.py [--note "문제/조치 한 줄"]
`--note`는 data/daily_posts_notes-<오늘>.json 에 시각과 함께 누적돼 현황판 '문제와 조치' 절에 나온다.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET_PER_CAFE = 100
CAFES = ["고요한 아침", "글로시 마이", "송도포털", "러브 인썸", "마이 웨딩 드림"]
WINDOW_END_HOUR = 2  # 02:00 까지 발행


def main(argv: list[str]) -> int:
    now = dt.datetime.now()
    today = now.strftime("%Y-%m-%d")
    notes_path = ROOT / "data" / f"daily_posts_notes-{today}.json"
    notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else []
    if "--note" in argv:
        notes.append({"at": now.strftime("%H:%M"), "text": argv[argv.index("--note") + 1]})
        notes_path.write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")

    c = sqlite3.connect(ROOT / "data" / "v2r.sqlite")
    like = f"{today}%"
    total = c.execute("select count(*) from publications where created_at like ?", (like,)).fetchone()[0]
    per = dict(c.execute("select cafe,count(*) from publications where created_at like ? group by 1", (like,)).fetchall())
    since = (now - dt.timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M")
    last_hour = c.execute("select count(*) from publications where created_at>=?", (since,)).fetchone()[0]
    job = c.execute(
        "select id,status,created_at,updated_at,substr(coalesce(error,''),1,120) from jobs where task='publish_daily' order by id desc limit 1"
    ).fetchone()
    target = TARGET_PER_CAFE * len(CAFES)
    remain = max(target - total, 0)
    eta = "완료" if remain == 0 else (f"약 {now + dt.timedelta(hours=remain / last_hour):%H:%M}" if last_hour else "속도 0 — 진행 없음")
    status_kor = {"running": "진행 중", "done": "완료", "failed": "실패", "queued": "대기"}.get(job[1], job[1]) if job else "작업 없음"

    lines = [
        f"# 오늘 일상 글 현황판 ({today}, 갱신 {now:%H:%M})",
        "",
        f"- **오늘 합계 {total}건 / 목표 {target}건 ({total * 100 // target}%)**, 남은 {remain}건",
        f"- 최근 60분 {last_hour}건 (시간당 속도), 완료 예상 {eta} (발행 허용 02:00까지)",
        f"- 발행 작업: {status_kor}" + (f" (작업 {job[0]}, 마지막 갱신 {job[3][11:16]})" if job else ""),
        "",
        "| 카페 | 오늘 | 목표 | 남은 |",
        "|---|---:|---:|---:|",
    ]
    for cafe in CAFES:
        n = per.get(cafe, 0)
        lines.append(f"| {cafe} | {n} | {TARGET_PER_CAFE} | {max(TARGET_PER_CAFE - n, 0)} |")
    lines += ["", "## 오늘 문제와 조치"]
    if job and job[1] == "failed":
        lines.append(f"- {job[3][11:16]} 작업 실패: {job[4]}")
    lines += [f"- {n['at']} {n['text']}" for n in notes] or ["- 없음"]
    lines += ["", "## 사용자가 할 일", "- 없음", ""]
    out = ROOT / "docs" / "reports" / f"daily-posts-board-{today}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
