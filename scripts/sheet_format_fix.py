"""5개 브랜드 노출 현황 탭 형식 일괄 정리(Apps Script API 최종판 필요).

순서(시트마다): info → set_header(A1 '카페', 다르면) → dedupe_by_key → delete_blank_rows → reapply_format
→ 노출완인데 A(카페)가 빈 행을 DB(keyword_exposure)의 노출 카페로 채움 → info 재확인.
결과는 표준 출력(JSON 줄)으로 낸다. 사용: .venv\\Scripts\\python.exe scripts\\sheet_format_fix.py [--dry]
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from v2r.sources import sheets_api as api  # noqa: E402

HEADER_A = "카페"


def _norm(s: str) -> str:
    return "".join(str(s or "").split()).casefold()


def main(dry: bool = False) -> int:
    src = yaml.safe_load(open(ROOT / "config/sources.yaml", encoding="utf-8"))["brand_sheets"]
    db = sqlite3.connect(ROOT / "data/v2r.sqlite")
    cfg = api.load_sheets_api_config(ROOT)
    for brand, v in src.items():
        if not isinstance(v, dict) or "exposure_gid" not in v:
            continue
        sid = v.get("spreadsheet_id") or v.get("id")
        gid = v["exposure_gid"]
        out: dict = {"brand": brand}
        try:
            before = api.api_info(sid, repo_root=ROOT, config=cfg)
        except api.SheetsApiError as exc:
            print(json.dumps({"brand": brand, "error": str(exc)}, ensure_ascii=False))
            continue
        out["before"] = before
        if dry:
            print(json.dumps(out, ensure_ascii=False))
            continue
        if (before.get("header") or [""])[0] != HEADER_A:
            out["set_header"] = api.api_set_header(sid, "A", HEADER_A, repo_root=ROOT, config=cfg)
        out["dedupe"] = api.api_dedupe_by_key(sid, repo_root=ROOT, config=cfg)
        out["blank"] = api.api_delete_blank_rows(sid, repo_root=ROOT, config=cfg)
        out["format"] = api.api_reapply_format(sid, repo_root=ROOT, config=cfg)

        # 노출완인데 카페(A)가 빈 행 → DB의 최근 노출 카페로 채움
        r = httpx.get(
            f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}",
            follow_redirects=True,
            timeout=90,
        )
        rows = list(csv.reader(io.StringIO(r.text)))[1:]
        need = [row[7].strip() for row in rows if len(row) > 7 and row[6].strip() == "노출완" and not row[0].strip() and row[7].strip()]
        cafe_of: dict[str, str] = {}
        for kw, cafe in db.execute(
            "select keyword, cafe from keyword_exposure where brand=? and status='exposed' and cafe<>'' order by checked_at",
            (brand,),
        ):
            cafe_of[_norm(kw)] = cafe
        updates = [{"keyword": kw, "A": cafe_of[_norm(kw)]} for kw in need if _norm(kw) in cafe_of]
        filled = 0
        for i in range(0, len(updates), 50):
            filled += api.api_update_by_key(sid, updates[i : i + 50], repo_root=ROOT, config=cfg).get("updated", 0)
        out["cafe_fill"] = {"need": len(need), "filled": filled, "no_db_cafe": len(need) - len(updates)}
        out["after"] = api.api_info(sid, repo_root=ROOT, config=cfg)
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(dry="--dry" in sys.argv))
