"""네이버 로그인 세션 유지 (사용자 규칙 2026-09-21).

목표: 사용자가 **한 번만** 로그인하면 이후 수집(web-crawler 등)이 그 세션을 계속 쓴다.

방법
1. `--login` — `data/browser-profile-naver` 영속 프로필로 **보이는** 브라우저 창을 열어
   네이버 로그인 페이지를 띄운다. 사용자가 직접 로그인한다(**"로그인 상태 유지" 체크 권장**).
   프로그램은 아이디·비밀번호를 입력하지도, 저장하지도 않는다.
   로그인은 쿠키 **이름**(`NID_AUT`, `NID_SES`)이 생겼는지로만 확인한다(값은 출력하지 않음).
2. `--check` — 헤드리스로 같은 프로필을 열어 네이버 페이지를 한 번 방문한다.
   이 방문이 세션을 **연장**한다(네이버는 활동이 있으면 유지 기간을 늘린다).
   쿠키가 없거나 로그인 페이지로 튕기면 "풀림"으로 판정 → 실행기가 텔레그램으로 알린다.
3. 로그인 확인 때마다 네이버 도메인 쿠키만 골라 `data/naver_cookies.json`(Playwright
   storageState 형식)에 내보낸다. web-crawler가 `output/cafe.naver.com/cookies.json`으로
   그대로 쓸 수 있게 그 폴더가 있으면 같이 복사한다.

예약: `config/schedule.yaml`의 `네이버 세션 점검`(매일)이 2를 돌린다.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LOGIN_URL = "https://nid.naver.com/nidlogin.login?mode=form&url=https%3A%2F%2Fwww.naver.com"
HOME_URL = "https://www.naver.com/"
#: 세션 연장 겸 로그인 확인용으로 방문할 페이지(로그인 필요한 곳이면 더 확실하다)
TOUCH_URL = "https://nid.naver.com/user2/help/myInfo?lang=ko_KR"  # 로그인 없이는 로그인 페이지로 튕기는 곳
#: 로그인 상태에서만 화면에 보이는 글자(실측 2026-09-21)
LOGIN_MARKERS = ("로그아웃", "내정보", "회원정보")
#: 로그인됐을 때 반드시 있는 쿠키 이름 (값은 절대 다루지 않는다)
LOGIN_COOKIES = ("NID_AUT", "NID_SES")
COOKIE_DOMAIN_SUFFIX = "naver.com"
LOGIN_TIMEOUT = 15 * 60
COOKIES_FILENAME = "naver_cookies.json"
LOGIN_PROMPT = (
    "브라우저 창에서 네이버에 로그인해 주세요. **'로그인 상태 유지'를 체크**하면 더 오래 갑니다. "
    "(최대 15분 기다립니다. 아이디·비밀번호는 프로그램이 입력하지 않습니다)"
)
WAITING_NOTICE = "아직 로그인 전입니다... (창에서 로그인해 주세요)"
RELOGIN_NOTICE = (
    "네이버 로그인이 풀렸습니다. PC에서 scripts\\naver-login.cmd 를 실행해 "
    "다시 로그인해 주세요 ('로그인 상태 유지' 체크)."
)


# --- 경로 -------------------------------------------------------------------
def _data_dir() -> Path:
    try:
        from v2r.config import get_settings  # 지연 임포트

        return Path(get_settings().data_dir)
    except Exception:
        return Path("data")


def default_profile_dir() -> Path:
    """네이버 전용 영속 프로필 (`data/browser-profile-naver`)."""
    return _data_dir() / "browser-profile-naver"


def cookies_path(profile_dir: str | Path | None = None) -> Path:
    base = Path(profile_dir) if profile_dir is not None else default_profile_dir()
    return base.parent / COOKIES_FILENAME


def web_crawler_cookie_targets(root: Path | None = None) -> list[Path]:
    """web-crawler가 읽는 `output/<도메인>/cookies.json` 자리(폴더가 있을 때만)."""
    base = root or (_data_dir().parent.parent / "web-crawler")
    if not base.is_dir():
        return []
    return [base / "output" / "cafe.naver.com" / "cookies.json"]


# --- 쿠키 판정/내보내기 (브라우저 없이 테스트 가능) ---------------------------
def is_naver_cookie(cookie: dict[str, Any]) -> bool:
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    return domain == COOKIE_DOMAIN_SUFFIX or domain.endswith("." + COOKIE_DOMAIN_SUFFIX)


def has_login_cookies(cookies: list[dict[str, Any]]) -> bool:
    names = {str(c.get("name")) for c in cookies if is_naver_cookie(c)}
    return all(n in names for n in LOGIN_COOKIES)


def login_cookie_expiry(cookies: list[dict[str, Any]]) -> float | None:
    """로그인 쿠키 중 가장 이른 만료 시각(epoch). 세션 쿠키(-1)는 None."""
    out: list[float] = []
    for c in cookies:
        if is_naver_cookie(c) and c.get("name") in LOGIN_COOKIES:
            exp = c.get("expires")
            if isinstance(exp, (int, float)) and exp > 0:
                out.append(float(exp))
    return min(out) if out else None


def export_cookies(cookies: list[dict[str, Any]], path: Path, extra_targets: list[Path] | None = None) -> int:
    """네이버 도메인 쿠키만 storageState 형식으로 저장. 저장한 개수 반환."""
    picked = [c for c in cookies if is_naver_cookie(c)]
    payload = {"cookies": picked, "origins": []}
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    for target in [path, *(extra_targets or [])]:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("쿠키 저장 실패(%s): %s", target, exc)
    return len(picked)


# --- 브라우저 -----------------------------------------------------------------
def _launch(path: Path, headless: bool, user_agent: str | None = None):
    from playwright.sync_api import sync_playwright

    path.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    last_exc: Exception | None = None
    context = None
    for channel in ("chrome", "msedge", None):
        try:
            kwargs: dict[str, Any] = dict(
                headless=headless,
                viewport={"width": 1280, "height": 900},
                locale="ko-KR",
            )
            if user_agent:
                kwargs["user_agent"] = user_agent
            if channel:
                kwargs["channel"] = channel
            context = playwright.chromium.launch_persistent_context(str(path), **kwargs)
            break
        except Exception as exc:  # pragma: no cover - 환경 의존
            last_exc = exc
    if context is None:
        playwright.stop()
        raise RuntimeError(f"브라우저를 열 수 없습니다: {last_exc}")
    page = context.pages[0] if context.pages else context.new_page()
    return playwright, context, page


def _close(playwright, context, page) -> None:
    for fn in (
        lambda: page and page.close(),
        lambda: context and context.close(),
        lambda: playwright and playwright.stop(),
    ):
        try:
            fn()
        except Exception:
            pass


def _cookies(context) -> list[dict[str, Any]]:
    try:
        return list(context.cookies())
    except Exception:
        return []


def _looks_logged_out(page) -> bool:
    url = (page.url or "").lower()
    if "nidlogin" in url or "nid.naver.com/login" in url:
        return True
    try:
        body = page.inner_text("body")[:4000]
    except Exception:
        return False  # 본문을 못 읽으면 URL 판정만 쓴다
    return not any(m in body for m in LOGIN_MARKERS)


# --- 로그인(1회) ---------------------------------------------------------------
def login_interactive(profile_dir: str | Path | None = None, timeout: int = LOGIN_TIMEOUT) -> bool:
    """보이는 창을 띄우고 사용자가 로그인할 때까지 기다린다. 성공 시 쿠키 내보내기."""
    path = Path(profile_dir) if profile_dir is not None else default_profile_dir()
    playwright, context, page = _launch(path, headless=False)
    try:
        if has_login_cookies(_cookies(context)):
            # 이미 로그인돼 있으면 확인만 하고 연장
            page.goto(TOUCH_URL, wait_until="domcontentloaded", timeout=60000)
            if not _looks_logged_out(page) and has_login_cookies(_cookies(context)):
                export_cookies(_cookies(context), cookies_path(path), web_crawler_cookie_targets())
                remember_user_agent(page, path)
                print("이미 로그인돼 있습니다. 세션을 연장하고 쿠키를 갱신했습니다.", flush=True)
                return True
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        print(LOGIN_PROMPT, flush=True)
        deadline = time.monotonic() + max(int(timeout), 0)
        next_notice = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(3)
            if has_login_cookies(_cookies(context)) and "nidlogin" not in (page.url or "").lower():
                # 쿠키가 생겼으면 로그인 필수 페이지로 가서 진짜 로그인인지 확인한다
                try:
                    page.goto(TOUCH_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(1500)
                except Exception:
                    continue
                if _looks_logged_out(page):
                    continue
                n = export_cookies(_cookies(context), cookies_path(path), web_crawler_cookie_targets())
                remember_user_agent(page, path)
                print(f"네이버 로그인 확인됨. 쿠키 {n}개 저장. 이제 창을 닫아도 됩니다.", flush=True)
                return True
            if time.monotonic() >= next_notice:
                print(WAITING_NOTICE, flush=True)
                next_notice = time.monotonic() + 30
        return False
    finally:
        _close(playwright, context, page)


# --- 프로필 백업/복구 (수단 2: 프로필이 깨져도 되살린다) -------------------------
BACKUP_SUFFIX = ".bak"
#: 백업에서 뺄 큰 캐시 폴더
_BACKUP_IGNORE = ("Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
                  "ShaderCache", "GrShaderCache", "Service Worker", "CacheStorage")
#: 남은 유지 기간이 이보다 짧으면(연장이 안 먹는 것) 미리 알린다
EARLY_WARN_DAYS = 3.0
UA_FILENAME = "naver_ua.txt"


def backup_dir(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def backup_profile(path: Path) -> bool:
    """로그인 확인된 프로필을 통째로 백업(캐시 제외). 실패해도 점검은 계속."""
    import shutil

    dst = backup_dir(path)
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(path, tmp, ignore=shutil.ignore_patterns(*_BACKUP_IGNORE), dirs_exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        tmp.rename(dst)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("프로필 백업 실패: %s", exc)
        return False


def restore_profile(path: Path) -> bool:
    """백업으로 되돌린다(깨진 현재 프로필은 `.broken`으로 치워 둔다)."""
    import shutil

    src = backup_dir(path)
    if not src.is_dir():
        return False
    try:
        broken = path.with_name(path.name + ".broken")
        if broken.exists():
            shutil.rmtree(broken, ignore_errors=True)
        if path.exists():
            path.rename(broken)
        shutil.copytree(src, path)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("프로필 복구 실패: %s", exc)
        return False


def ua_path(path: Path) -> Path:
    return path.parent / UA_FILENAME


def remember_user_agent(page, path: Path) -> None:
    """로그인 창(보이는 크롬)의 UA를 기록 → 헤드리스 점검도 같은 UA로 (수단 3: 기기 바뀐 척 안 함)."""
    try:
        ua = page.evaluate("navigator.userAgent")
        if ua and "Headless" not in ua:
            ua_path(path).write_text(str(ua), encoding="utf-8")
    except Exception:
        pass


def saved_user_agent(path: Path) -> str | None:
    try:
        text = ua_path(path).read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


# --- 점검·연장 (하루 4회, 수단 1: 방문할 때마다 NID_SES가 30일로 다시 발급된다 — 실측 2026-09-21) ---
def _check_once(path: Path) -> dict:
    out: dict[str, Any] = {"ok": True, "logged_in": False, "profile": str(path), "note": ""}
    playwright = context = page = None
    try:
        playwright, context, page = _launch(path, headless=True, user_agent=saved_user_agent(path))
        before = _cookies(context)
        if not has_login_cookies(before):
            out["note"] = "로그인 쿠키(NID_AUT/NID_SES) 없음 → 풀림"
            return out
        try:
            page.goto(TOUCH_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
        except Exception as exc:  # noqa: BLE001
            out["note"] = f"네이버 접속 실패({exc.__class__.__name__}) → 판정 보류(쿠키는 있음)"
            out["logged_in"] = True
            out["unverified"] = True
            return out
        after = _cookies(context)
        if _looks_logged_out(page) or not has_login_cookies(after):
            out["note"] = "방문 후 로그인 페이지로 이동/쿠키 소실 → 풀림"
            return out
        n = export_cookies(after, cookies_path(path), web_crawler_cookie_targets())
        expiry = login_cookie_expiry(after)
        out.update(logged_in=True, note=f"세션 연장 방문 완료, 쿠키 {n}개 저장")
        if expiry:
            out["expires_in_days"] = round((expiry - time.time()) / 86400, 2)
        return out
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["note"] = f"점검 실패: {exc.__class__.__name__}: {str(exc)[:120]}"
        return out
    finally:
        _close(playwright, context, page)


def check_naver_session(profile_dir: str | Path | None = None) -> dict:
    """로그인 유지 점검·연장. 순서: 점검 → (풀림/실패면) 백업으로 복구 후 재점검 → (OK면) 백업 갱신.

    반환 `warnings`: 연장이 안 먹어 남은 기간이 짧을 때 등, 사람이 봐야 할 것.
    """
    path = Path(profile_dir) if profile_dir is not None else default_profile_dir()
    if not path.is_dir() and not backup_dir(path).is_dir():
        return {"ok": True, "logged_in": False, "profile": str(path),
                "note": "프로필 폴더가 없습니다 (아직 한 번도 로그인하지 않음).", "warnings": []}
    out = _check_once(path) if path.is_dir() else {"ok": False, "logged_in": False, "note": "프로필 없음"}
    out.setdefault("warnings", [])
    if not out.get("logged_in") and backup_dir(path).is_dir():
        # 수단 2: 백업으로 되돌려 한 번 더
        if restore_profile(path):
            again = _check_once(path)
            again.setdefault("warnings", [])
            again["restored_from_backup"] = True
            again["note"] = f"현재 프로필 풀림({out.get('note')}) → 백업으로 복구 후: {again.get('note')}"
            out = again
    if out.get("logged_in") and not out.get("unverified"):
        out["backup"] = backup_profile(path)
        days = out.get("expires_in_days")
        if isinstance(days, (int, float)) and days < EARLY_WARN_DAYS:
            out["warnings"].append(
                f"세션 연장이 안 먹고 있습니다(남은 {days}일). 미리 scripts\\naver-login.cmd 로 다시 로그인해 두세요."
            )
    return out


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(argv if argv is not None else sys.argv[1:])
    if "--check" in args:
        out = check_naver_session()
        print(f"네이버 로그인 상태: {'OK' if out['logged_in'] else '풀림'} {out['note']} "
              f"남은 {out.get('expires_in_days', '?')}일 백업 {out.get('backup')} 경고 {out.get('warnings')}")
        return 0 if out["logged_in"] else 1
    print(f"네이버 로그인 창을 엽니다. 프로필: {default_profile_dir()}")
    ok = login_interactive()
    if not ok:
        print("로그인 대기 시간이 끝났습니다. 다시 실행해 주세요.")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
