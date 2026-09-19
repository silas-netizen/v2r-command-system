"""Playwright 세션 헬퍼.

비밀번호는 절대 자동 입력하지 않는다. 로그인 화면이 보이면 사용자가 직접
브라우저 창에서 로그인할 때까지 기다린다(프로필을 남겨 다음부터 재사용).
"""

from __future__ import annotations

import time
from pathlib import Path

LOGIN_PROMPT = "브라우저 창에서 직접 로그인해 주세요"
POLL_SECONDS = 2
MAX_WAIT_SECONDS = 600

PASSWORD_SELECTOR = "input[type=password]"


def default_profile_dir() -> Path:
    """기본 브라우저 프로필 경로 (`data/browser-profile`)."""
    try:
        from v2r.config import get_settings  # 지연 임포트

        base = Path(get_settings().data_dir)
    except Exception:
        base = Path("data")
    return base / "browser-profile"


def default_site() -> str:
    """설정의 V2R 사이트 주소."""
    try:
        from v2r.config import get_settings

        return str(get_settings().v2r_site).rstrip("/")
    except Exception:
        return "https://v2r.daboja.im"


def open_site(headless: bool = False, profile_dir: str | Path | None = None):
    """영속 프로필로 브라우저를 연다. `(playwright, context, page)` 반환."""
    from playwright.sync_api import sync_playwright

    path = Path(profile_dir) if profile_dir is not None else default_profile_dir()
    path.mkdir(parents=True, exist_ok=True)

    playwright = sync_playwright().start()
    # 설치된 실제 브라우저(크롬 → 엣지) 우선, 내장 크로미움은 마지막 수단
    context = None
    last_exc: Exception | None = None
    for channel in ("chrome", "msedge", None):
        try:
            kwargs = dict(headless=headless, viewport={"width": 1440, "height": 950}, accept_downloads=True)
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


def ensure_logged_in(
    page,
    site: str | None = None,
    max_wait_seconds: int = MAX_WAIT_SECONDS,
    poll_seconds: int = POLL_SECONDS,
) -> bool:
    """로그인 상태를 확인한다. 로그인 폼이 보이면 사용자가 직접 로그인할 때까지 대기."""
    base = (site or default_site()).rstrip("/")
    page.goto(f"{base}/nc/board?view=list", wait_until="domcontentloaded")
    # SPA가 /login 으로 갈아타는 데 잠깐 걸린다 → 바로 판정하면 '로그인됨'으로 오판한다
    page.wait_for_timeout(1500)

    if not _login_needed(page):
        return True

    # 규칙(2026-09-19): 로그인은 프로그램이 스스로 처리한다 — 비밀번호를 화면에 치지 않고,
    # API 로그인으로 받은 갱신 쿠키(refresh_token)를 브라우저에 넣어 세션을 만든다.
    if inject_api_session(page, base):
        return True

    print(LOGIN_PROMPT)
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        time.sleep(poll_seconds)
        if not _login_needed(page):
            return True
    raise TimeoutError(f"로그인 대기 시간 초과({max_wait_seconds}초). {LOGIN_PROMPT}.")


API_HOST = "api-v2r.daboja.im"


def inject_api_session(page, base: str) -> bool:
    """API 로그인 → `refresh_token` 쿠키를 브라우저 컨텍스트에 넣고 사이트를 다시 연다.

    값은 기록하지 않는다. 성공하면 True(로그인 화면이 사라짐), 실패하면 False.
    """
    try:
        from v2r.config import get_settings
        from v2r.engine.context import Runtime

        st = get_settings()
        rt = Runtime.open(st)
        http = rt.client._client
        rt.client.auth.login(http, st.v2r_email, st.v2r_password)
        cookies = [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path or "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }
            for c in http.cookies.jar
            if c.name == "refresh_token"
        ]
        if not cookies:
            return False
        page.context.add_cookies(cookies)
        page.goto(f"{base}/nc/board?view=list", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        return not _login_needed(page)
    except Exception as exc:  # 실패하면 사용자 로그인 대기로 넘어간다
        print(f"자동 로그인 실패: {type(exc).__name__}")
        return False


def _login_needed(page) -> bool:
    """로그인 페이지에 있거나 비밀번호 입력란이 보이면 True."""
    try:
        if "/login" in str(page.url or ""):
            return True
    except Exception:
        pass
    return _password_visible(page)


def _password_visible(page) -> bool:
    """비밀번호 입력란이 화면에 있는지 확인(내용은 건드리지 않는다)."""
    try:
        return page.locator(PASSWORD_SELECTOR).first.is_visible(timeout=2000)
    except Exception:
        return False


def close(playwright=None, context=None, page=None) -> None:
    """열린 자원을 조용히 닫는다."""
    for closer in (page, context, playwright):
        if closer is None:
            continue
        try:
            closer.close() if closer is not playwright else closer.stop()
        except Exception:
            pass


__all__ = ["close", "default_profile_dir", "ensure_logged_in", "open_site"]
