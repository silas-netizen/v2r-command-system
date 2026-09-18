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
    context = playwright.chromium.launch_persistent_context(
        str(path),
        headless=headless,
        viewport={"width": 1440, "height": 950},
        accept_downloads=True,
    )
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

    if not _password_visible(page):
        return True

    print(LOGIN_PROMPT)
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        time.sleep(poll_seconds)
        if not _password_visible(page):
            return True
    raise TimeoutError(f"로그인 대기 시간 초과({max_wait_seconds}초). {LOGIN_PROMPT}.")


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
