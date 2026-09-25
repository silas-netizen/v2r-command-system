"""DB(keyword_exposure)의 최신 판정을 시트(노출 현황 탭)에 다시 맞춘다 — Apps Script API만 사용.

대상: 시트 H에 있는 키워드 중, DB 최신 검사 시각(날짜)이 시트 J와 다른 행.
쓰는 열: G(상태)·J(검사 시각)·K(검색량, DB에 있을 때)·L(노출된 검색량)·A(노출완이면 카페, article_index로 유도)·I(비어 있을 때만).
B~F·H는 건드리지 않는다. 사용: .venv\\Scripts\\python.exe scripts\\sheet_resync_from_db.py [브랜드 ...] [--dry]
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from v2r.knowledge.keyword_exposure import KOREAN_STATUS, cafe_for_article_url, integrated_search_url  # noqa: E402
from v2r.sources import sheets_api as api  # noqa: E402
from v2r.sources.sheets_writer import build_exposure_column_updates  # noqa: E402


def _norm(s: str) -> str:
    return "".join(str(s or "").split()).casefold()


def main(argv: list[str]) -> int:
    dry = "--dry" in argv
    only = {a for a in argv if not a.startswith("--")}
    src = yaml.safe_load(open(ROOT / "config/sources.yaml", encoding="utf-8"))["brand_sheets"]
    db = sqlite3.connect(ROOT / "data/v2r.sqlite")
    cfg = api.load_sheets_api_config(ROOT)
    for brand, v in src.items():
        if not isinstance(v, dict) or "exposure_gid" not in v or (only and brand not in only):
            continue
        sid = v.get("spreadsheet_id") or v.get("id")
        gid = v["exposure_gid"]
        r = httpx.get(f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}", follow_redirects=True, timeout=90)
        rows = list(csv.reader(io.StringIO(r.text)))[1:]
        sheet = {}
        for row in rows:
            if len(row) > 9 and row[7].strip():
                sheet[_norm(row[7])] = {"keyword": row[7].strip(), "A": row[0].strip(), "G": row[6].strip(), "I": row[8].strip(), "J": row[9].strip()}
        kdb = sqlite3.connect(ROOT / "data/keywords" / f"{brand}.sqlite")
        vol = {}
        for kw, total in kdb.execute("select keyword, total from keywords where total is not null and total > 0"):
            vol[_norm(kw)] = int(total)
        latest = db.execute(
            "select keyword, status, article_url, cafe, checked_at from keyword_exposure k where brand=? and checked_at = "
            "(select max(checked_at) from keyword_exposure where brand=k.brand and keyword=k.keyword)",
            (brand,),
        ).fetchall()
        updates = []
        for kw, status, url, cafe, checked_at in latest:
            row = sheet.get(_norm(kw))
            if not row or status in ("unknown", "unpublished"):
                continue
            j_date = row["J"][:10]
            if status == "exposed":
                # 카페는 항상 글 URL(별칭/카페 번호)로 다시 계산한다. 못 찾으면 DB 값, 그것도 없으면 시트 값 유지.
                cafe = cafe_for_article_url(ROOT / "data/v2r.sqlite", url) or cafe or row["A"]
            if (
                j_date == checked_at[:10]
                and row["G"] == KOREAN_STATUS.get(status, status)
                and (status != "exposed" or (row["A"] and row["A"] == cafe))
            ):
                continue
            u = build_exposure_column_updates(
                status=status,
                checked_at_kst=checked_at,
                cafe=cafe or None,
                volume=vol.get(_norm(kw)),
                existing_i=row["I"],
                integrated_search_url_fn=lambda k: integrated_search_url(k),
                keyword=row["keyword"],
            )
            updates.append({"keyword": row["keyword"], **u})
        out = {"brand": brand, "sheet_rows": len(rows), "to_update": len(updates), "exposed_with_cafe": sum(1 for u in updates if u.get("A"))}
        if not dry:
            done, missing = 0, 0
            for i in range(0, len(updates), 50):
                # 러너 작업자 6개와 같은 웹앱을 나눠 쓰므로 잠금 대기 초과(HTML 오류)가 날 수 있다 → 묶음마다 최대 6회, 10초 간격 재시도
                for attempt in range(6):
                    try:
                        res = api.api_update_by_key(sid, updates[i : i + 50], repo_root=ROOT, config=cfg)
                        break
                    except api.SheetsApiError:
                        if attempt == 5:
                            raise
                        time.sleep(10)
                done += res.get("updated", 0)
                missing += len(res.get("missing") or [])
            out.update({"updated": done, "missing": missing})
        print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
