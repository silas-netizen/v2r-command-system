"""여러 사이트 로그인 유지 (Claude 플랫폼·Make) — 네이버(`naver_session.py`)와 같은 방식.

- `--login <사이트>`: 전용 프로필(`data/browser-profile-<사이트>`)로 보이는 창을 띄우고 사용자가
  직접(구글 계정 등으로) 로그인할 때까지 기다린다. 비밀번호는 입력·저장하지 않는다.
- `--check [<사이트>]`: 헤드리스로 로그인 필수 페이지를 방문해 유지 여부를 확인·연장하고,
  OK면 프로필을 백업, 풀렸으면 백업으로 복구해 재시도. 결과에 `warnings`.
사이트 표(`SITES`)만 늘리면 다른 사이트도 같은 방식으로 유지된다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from v2r.warehouse import naver_session as ns

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    login_url: str
    touch_url: str  # 로그인 없이는 로그인 페이지로 튕기는 곳
    logged_out_markers: tuple[str, ...]  # URL에 이게 있으면 로그아웃 상태
    logged_in_markers: tuple[str, ...]  # 화면 글자 중 하나라도 있으면 로그인 상태


SITES: dict[str, Site] = {
    "claude": Site(
        key="claude",
        name="Claude 플랫폼(API 사용량)",
        login_url="https://platform.claude.com/login",
        touch_url="https://platform.claude.com/settings/usage",
        logged_out_markers=("/login", "accounts.google.com", "auth0"),
        logged_in_markers=("Usage", "사용량", "Cost", "비용", "Workspace", "Log out", "로그아웃", "Settings"),
    ),
    "make": Site(
        key="make",
        name="Make(시나리오 작업량)",
        login_url="https://us2.make.com/login",
        touch_url="https://us2.make.com/495291/organization/dashboard",
        logged_out_markers=("/login", "accounts.google.com"),
        logged_in_markers=("Operations", "Dashboard", "Scenarios", "Organization", "작업"),
    ),
}

RELOGIN_NOTICE = "{name} 로그인이 풀렸습니다. PC에서 scripts\\web-login.cmd {key} 를 실행해 다시 로그인해 주세요."


def site_of(key: str) -> Site:
    k = (key or "").strip().lower()
    if k not in SITES:
        raise KeyError(f"모르는 사이트: {key} (가능: {', '.join(SITES)})")
    return SITES[k]


def profile_dir(site: Site) -> Path:
    return ns._data_dir() / f"browser-profile-{site.key}"


def _logged_out(page, site: Site) -> bool:
    url = (page.url or "").lower()
    if any(m in url for m in site.logged_out_markers):
        return True
    try:
        body = page.inner_text("body")[:6000]
    except Exception:
        return False
    return not any(m in body for m in site.logged_in_markers)


CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def real_chrome() -> str | None:
    import os
    import shutil

    for p in (*CHROME_CANDIDATES, os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
        if Path(p).exists():
            return p
    return shutil.which("chrome") or shutil.which("chrome.exe")


def login_with_real_chrome(site: Site, timeout: int = ns.LOGIN_TIMEOUT) -> bool:
    """구글은 자동화 브라우저의 로그인을 막는다("브라우저 또는 앱이 안전하지 않을 수 있습니다").

    그래서 로그인만은 **자동화 없이 진짜 크롬**을 우리 전용 프로필 폴더로 띄운다.
    사용자가 로그인하고 창을 닫으면, 같은 프로필을 헤드리스로 열어 로그인 여부를 확인한다.
    (실측 2026-09-21: Playwright로 띄운 크롬은 구글 로그인 거부, 네이버는 허용)
    """
    import subprocess

    chrome = real_chrome()
    if not chrome:
        print("크롬 실행 파일을 찾지 못했습니다.", flush=True)
        return False
    path = profile_dir(site)
    path.mkdir(parents=True, exist_ok=True)
    args = [chrome, f"--user-data-dir={path}", "--no-first-run", "--no-default-browser-check",
            "--new-window", site.login_url]
    print(f"{site.name}: 진짜 크롬 창을 엽니다. 로그인한 뒤 **그 창을 닫아 주세요** (최대 15분).", flush=True)
    proc = subprocess.Popen(args)
    deadline = time.monotonic() + max(int(timeout), 0)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(3)
    else:
        print("시간이 다 됐습니다. 창을 닫고 다시 실행해 주세요.", flush=True)
        return False
    time.sleep(2)
    out = _check_once(site, path)
    if out.get("logged_in") and not out.get("unverified"):
        ns.backup_profile(path)
        print(f"{site.name} 로그인 확인됨(프로필 저장·백업).", flush=True)
        return True
    print(f"{site.name} 로그인을 확인하지 못했습니다: {out.get('note')}", flush=True)
    return False


def login_interactive(site: Site, timeout: int = ns.LOGIN_TIMEOUT) -> bool:
    path = profile_dir(site)
    playwright, context, page = ns._launch(path, headless=False)
    try:
        page.goto(site.touch_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        if not _logged_out(page, site):
            ns.remember_user_agent(page, path)
            print(f"{site.name}: 이미 로그인돼 있습니다. 세션을 연장했습니다.", flush=True)
            return True
        page.goto(site.login_url, wait_until="domcontentloaded", timeout=60000)
        print(f"{site.name} 창에서 로그인해 주세요(구글 계정 등). 최대 15분 기다립니다. 비밀번호는 프로그램이 입력하지 않습니다.", flush=True)
        deadline = time.monotonic() + max(int(timeout), 0)
        next_notice = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(3)
            url = (page.url or "").lower()
            if any(m in url for m in site.logged_out_markers):
                continue
            try:
                page.goto(site.touch_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)
            except Exception:
                continue
            if not _logged_out(page, site):
                ns.remember_user_agent(page, path)
                print(f"{site.name} 로그인 확인됨. 이제 창을 닫아도 됩니다.", flush=True)
                return True
            if time.monotonic() >= next_notice:
                print(ns.WAITING_NOTICE, flush=True)
                next_notice = time.monotonic() + 30
        return False
    finally:
        ns._close(playwright, context, page)


#: 이 프로필로 이미 떠 있는 진짜 크롬의 원격 디버깅 포트(슬랙·앱스 스크립트 조작용, 2026-09-22부터 상주).
CDP_PORTS: dict[str, int] = {"claude": 9333}


def _check_via_running_chrome(site: Site, path: Path) -> dict | None:
    """같은 프로필을 진짜 크롬이 붙잡고 있으면 그 크롬에 CDP로 붙어 확인한다.

    사고 2026-09-23 09:20: 상주 크롬이 프로필을 쓰는 동안 헤드리스 크로미움을 같은 폴더로
    또 띄우자 쿠키를 못 읽어 "풀림" 오경보가 났고, 그 위에 백업 복구까지 돌았다.
    """
    port = CDP_PORTS.get(site.key)
    if not port:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        return None
    out: dict[str, Any] = {"ok": True, "site": site.key, "logged_in": False, "profile": str(path), "note": "", "via": f"cdp:{port}"}
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=3000)
            except Exception:  # noqa: BLE001
                return None  # 상주 크롬 없음 → 평소 방식
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(site.touch_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                if _logged_out(page, site):
                    out["note"] = "상주 크롬에서도 로그인 표식 없음 → 풀림"
                else:
                    out.update(logged_in=True, note="상주 크롬(CDP)으로 세션 연장 방문 완료")
            except Exception as exc:  # noqa: BLE001
                out.update(logged_in=True, unverified=True, note=f"상주 크롬 접속 실패({exc.__class__.__name__}) → 판정 보류")
            finally:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
            browser.close()  # CDP 연결만 끊는다(크롬은 그대로)
    except Exception as exc:  # noqa: BLE001
        out.update(logged_in=True, unverified=True, note=f"CDP 점검 실패({exc.__class__.__name__}) → 판정 보류")
    return out


def _check_once(site: Site, path: Path) -> dict:
    via_chrome = _check_via_running_chrome(site, path)
    if via_chrome is not None:
        return via_chrome
    out: dict[str, Any] = {"ok": True, "site": site.key, "logged_in": False, "profile": str(path), "note": ""}
    playwright = context = page = None
    try:
        playwright, context, page = ns._launch(path, headless=True, user_agent=ns.saved_user_agent(path))
        try:
            page.goto(site.touch_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
        except Exception as exc:  # noqa: BLE001
            out.update(logged_in=True, unverified=True, note=f"접속 실패({exc.__class__.__name__}) → 판정 보류")
            return out
        if _logged_out(page, site):
            out["note"] = "로그인 페이지로 이동/로그인 표식 없음 → 풀림"
            return out
        out.update(logged_in=True, note="세션 연장 방문 완료")
        return out
    except Exception as exc:  # noqa: BLE001
        out.update(ok=False, note=f"점검 실패: {exc.__class__.__name__}: {str(exc)[:120]}")
        return out
    finally:
        ns._close(playwright, context, page)


def check_site(site: Site) -> dict:
    path = profile_dir(site)
    if not path.is_dir() and not ns.backup_dir(path).is_dir():
        return {"ok": True, "site": site.key, "logged_in": False, "note": "프로필 없음(아직 로그인 안 함)", "warnings": []}
    out = _check_once(site, path) if path.is_dir() else {"ok": False, "site": site.key, "logged_in": False, "note": "프로필 없음"}
    out.setdefault("warnings", [])
    if not out.get("logged_in") and ns.backup_dir(path).is_dir() and ns.restore_profile(path):
        again = _check_once(site, path)
        again.setdefault("warnings", [])
        again["restored_from_backup"] = True
        again["note"] = f"풀림({out.get('note')}) → 백업 복구 후: {again.get('note')}"
        out = again
    if out.get("logged_in") and not out.get("unverified") and not out.get("via"):
        # 상주 크롬이 쓰는 중인 프로필은 복사하지 않는다(쓰는 중 복사 → 깨진 백업)
        out["backup"] = ns.backup_profile(path)
    return out


def check_all() -> dict:
    results = {k: check_site(s) for k, s in SITES.items()}
    return {"ok": True, "sites": results, "logged_in": all(r.get("logged_in") for r in results.values())}


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(argv if argv is not None else sys.argv[1:])
    if "--check" in args:
        keys = [a for a in args if a in SITES] or list(SITES)
        rc = 0
        for k in keys:
            out = check_site(SITES[k])
            print(f"{SITES[k].name}: {'OK' if out.get('logged_in') else '풀림'} {out.get('note')}")
            rc |= 0 if out.get("logged_in") else 1
        return rc
    keys = [a for a in args if a in SITES]
    if not keys:
        print(f"사이트를 적어 주세요: {', '.join(SITES)}")
        return 2
    rc = 0
    for k in keys:
        # 구글 로그인은 자동화 브라우저에서 거부되므로 진짜 크롬으로 (2026-09-21)
        ok = login_with_real_chrome(SITES[k])
        rc |= 0 if ok else 1
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
