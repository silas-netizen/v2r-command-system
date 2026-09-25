"""5개 브랜드 노출 현황 탭의 A(카페)·G(노출 상태) 배경색을 config/sheet_colors.yaml 대로 통일한다(Apps Script apply_colors).

사용: .venv\\Scripts\\python.exe scripts\\sheet_apply_colors.py [브랜드 ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from v2r.sources import sheets_api as api  # noqa: E402


def main(argv: list[str]) -> int:
    only = {a for a in argv if not a.startswith("--")}
    colors = yaml.safe_load(open(ROOT / "config/sheet_colors.yaml", encoding="utf-8"))
    a_map = {str(k): str(v) for k, v in (colors.get("cafes") or {}).items()}
    g_map = {str(k): str(v) for k, v in (colors.get("statuses") or {}).items()}
    src = yaml.safe_load(open(ROOT / "config/sources.yaml", encoding="utf-8"))["brand_sheets"]
    cfg = api.load_sheets_api_config(ROOT)
    rc = 0
    for brand, v in src.items():
        if not isinstance(v, dict) or "exposure_gid" not in v or (only and brand not in only):
            continue
        sid = v.get("spreadsheet_id") or v.get("id")
        try:
            res = api.api_apply_colors(sid, a_map, g_map, repo_root=ROOT, config=cfg)
            print(json.dumps({"brand": brand, **res}, ensure_ascii=False), flush=True)
        except api.SheetsApiError as exc:
            rc = 1
            print(json.dumps({"brand": brand, "error": str(exc)}, ensure_ascii=False), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
