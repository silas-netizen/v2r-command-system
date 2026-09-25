"""쌍둥이맘 모여라 카페 — 최근 글 작성 계정 등급 실측 (2026-09-25 1회성 조사).

헤드리스로 articleDetail 페이지를 열어 작성자 등급 표시를 텍스트로 캡처한다.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parents[1] / ".pw-browsers"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from v2r.browser import session as bsession  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "twins_accounts"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TARGETS = json.loads(sys.argv[1]) if len(sys.argv) > 1 else []


def main() -> None:
    pw, ctx, page = bsession.open_site(headless=True)
    try:
        ok = bsession.ensure_logged_in(page)
        print("logged_in:", ok)
        results = []
        for t in TARGETS:
            login_id = t["login_id"]
            source_id = t["source_id"]
            url = f"https://v2r.daboja.im/nc/articleDetail/{source_id}"
            row = {"login_id": login_id, "source_id": source_id, "url": url}
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
                body_text = page.inner_text("body")
                row["body_text_len"] = len(body_text)
                txt_path = OUT_DIR / f"{login_id}.txt"
                txt_path.write_text(body_text, encoding="utf-8")
                shot_path = OUT_DIR / f"{login_id}.png"
                page.screenshot(path=str(shot_path), full_page=False)
                row["txt"] = str(txt_path)
                row["png"] = str(shot_path)
                row["ok"] = True
            except Exception as exc:  # noqa: BLE001
                row["ok"] = False
                row["error"] = str(exc)
            results.append(row)
            print(json.dumps(row, ensure_ascii=False))
        (OUT_DIR / "_capture_index.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    finally:
        bsession.close(pw, ctx, page)


if __name__ == "__main__":
    main()
