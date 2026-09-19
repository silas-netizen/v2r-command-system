"""ChatGPT 웹앱(구독 요금제)으로 사진을 생성한다. **OpenAI API를 쓰지 않는다.**

흐름:
1. `open_gpt()` — `data/browser-profile-gpt` 영속 프로필로 chatgpt.com을 연다.
2. `wait_for_login(page)` — 사용자가 **직접** 창에서 로그인할 때까지 기다린다.
   비밀번호는 이 프로그램이 절대 입력하지도, 저장하지도 않는다.
   한 번 로그인하면 프로필에 세션 쿠키가 남아 다음부터 자동으로 통과한다.
3. `generate_image(page, prompt, out_dir)` — 프롬프트를 입력창에 넣고 보낸 뒤
   생성된 이미지를 원본 해상도로 받아 PNG로 저장하고, 긴 변 1024px JPEG로 만든다.
4. `generate_batch(brand, keyword, n)` — 프롬프트 n개를 차례로 돌려
   `warehouse/inbox/new/<브랜드>/<키워드>/`에 떨군 뒤 `collect_new`로 적재 + **세탁**한다.

규칙 0: 세탁 안 된 이미지는 절대 발행에 쓰지 않는다. 그래서 `generate_batch`는
언제나 `photo_request.collect_new`까지 마친다.

"AI 느낌" 제거는 두 겹으로 한다.
- 프롬프트: `photo_request.ANTI_AI_RULES` (자연광·손떨림·생활감·비대칭·글자 없음 …)
- 후처리: `postprocess()` 가 미세 노이즈 + 재압축 + 소량 크롭 + ≤1도 회전을 넣고,
  이어지는 세탁(`photo_washer`)이 랜덤 EXIF를 심는다.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops

log = logging.getLogger(__name__)

#: 사용자에게 보여 줄 로그인 안내 문구 (요구사항 고정 문구)
LOGIN_PROMPT = "브라우저 창에서 ChatGPT에 직접 로그인해 주세요"
#: 대기 중 30초마다 찍는 안내
WAITING_NOTICE = "아직 로그인 전입니다. 창에서 로그인해 주세요"
#: ChatGPT 웹앱 주소
GPT_URL = "https://chatgpt.com"
PHOTO_THREAD = "photos"
DAILY_THREAD = "affiliate_daily"
#: 로그인 대기 기본 한도(초)
LOGIN_TIMEOUT = 900
#: 이미지 1장 생성 대기 기본 한도(초)
IMAGE_TIMEOUT = 240
#: 결과 이미지 긴 변 목표 (약 1K)
TARGET_LONG_SIDE = 1024
#: 프롬프트 사이 쉬는 시간(초) 범위 — 레이트리밋 회피
SLEEP_BETWEEN = (5.0, 10.0)

# --- 셀렉터 (fragile: ChatGPT UI가 바뀌면 여기만 고치면 된다) -------------
#: 프롬프트 입력창 후보 (위에서부터 시도)
COMPOSER_SELECTORS: tuple[str, ...] = (
    "#prompt-textarea",
    "div[contenteditable='true']#prompt-textarea",
    "textarea[data-id='root']",
    "form div[contenteditable='true']",
    "main textarea",
)
#: 전송 버튼 후보
SEND_SELECTORS: tuple[str, ...] = (
    "button[data-testid='send-button']",
    "button[aria-label='Send prompt']",
    "button[aria-label='프롬프트 보내기']",
    "form button[type='submit']",
)
#: 생성이 끝났을 때 나타나는 이미지 후보
IMAGE_SELECTORS: tuple[str, ...] = (
    "img[alt*='Generated' i]",
    "img[alt*='생성' ]",
    "div[data-testid^='conversation-turn'] img[src^='http']",
    "main img[src*='oaiusercontent']",
    "main img[src^='blob:']",
)
#: 원본 화질 내려받기 버튼 후보
DOWNLOAD_SELECTORS: tuple[str, ...] = (
    "[data-testid='image-gen-download-button']",
    "button[aria-label*='Download' i]",
    "button[aria-label*='다운로드']",
    "a[download]",
)
#: 로그인 계정이 있을 때만 나타나는 것들 (아바타/프로필/사이드바 계정 영역).
#: **입력창은 판정에 쓰지 않는다** — 로그아웃 방문자에게도 입력창이 보이기 때문.
ACCOUNT_SELECTORS: tuple[str, ...] = (
    "[data-testid='profile-button']",
    "button[aria-label*='profile' i]",
    "button[aria-label*='계정']",
    "img[alt*='User' i]",
    "[data-testid='accounts-profile-button']",
    "nav [data-testid='account-menu']",
    "button[data-testid='workspace-switcher']",
)
#: 로그아웃 상태에서만 보이는 버튼들 (하나라도 보이면 로그인 안 된 것)
LOGGED_OUT_SELECTORS: tuple[str, ...] = (
    "[data-testid='login-button']",
    "[data-testid='signup-button']",
    "button:has-text('Log in')",
    "button:has-text('로그인')",
    "button:has-text('Sign up')",
    "button:has-text('회원가입')",
    "a:has-text('Log in')",
    "a:has-text('로그인')",
)
#: 로그인 페이지로 튕겼는지 판단할 URL 조각
AUTH_URL_MARKERS: tuple[str, ...] = ("/auth/login", "/auth/signup", "auth0.openai.com")

#: 사용 한도 안내 문구 (영/한 혼용)
LIMIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"usage limit", re.I),
    re.compile(r"rate limit", re.I),
    re.compile(r"you've (?:hit|reached) (?:the|your)", re.I),
    re.compile(r"limit reached", re.I),
    re.compile(r"image generation .{0,40}limit", re.I),
    re.compile(r"please try again (?:later|after|in)", re.I),
    re.compile(r"이미지\s*생성.{0,10}한도"),
    re.compile(r"사용\s*한도"),
    re.compile(r"제한.{0,10}도달"),
    re.compile(r"(?:잠시\s*후|나중에)\s*다시\s*(?:시도|이용)"),
)


class GptImageError(RuntimeError):
    """ChatGPT 이미지 생성 실패."""


class GptLimitError(GptImageError):
    """사용 한도에 걸렸다. `wait_text`에 화면에 뜬 안내 문구를 담는다."""

    def __init__(self, wait_text: str = "") -> None:
        self.wait_text = (wait_text or "").strip()
        super().__init__(f"ChatGPT 이미지 사용 한도: {self.wait_text or '안내 문구 없음'}")


# --- 브라우저 -------------------------------------------------------------
def default_profile_dir() -> Path:
    """GPT 전용 영속 프로필 경로 (`data/browser-profile-gpt`)."""
    try:
        from v2r.config import get_settings  # 지연 임포트

        base = Path(get_settings().data_dir)
    except Exception:
        base = Path("data")
    return base / "browser-profile-gpt"


THREADS_FILENAME = "gpt_threads.json"


def threads_path(profile_dir: str | Path | None = None) -> Path:
    """용도별로 이어 쓰는 ChatGPT 대화 URL 저장 파일(`data/gpt_threads.json`)."""
    base = Path(profile_dir) if profile_dir is not None else default_profile_dir()
    return base.parent / THREADS_FILENAME


def load_threads(profile_dir: str | Path | None = None) -> dict[str, str]:
    path = threads_path(profile_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def thread_url(name: str, profile_dir: str | Path | None = None) -> str:
    return load_threads(profile_dir).get(name, "")


def remember_thread(name: str, page, profile_dir: str | Path | None = None) -> str:
    """지금 페이지가 대화 URL(`/c/...`)이면 `name` 용도로 기억한다. 새 창을 계속 여는 대신
    같은 대화에서 이어가기 위한 것(사용자 규칙). 저장된 URL을 돌려준다."""
    try:
        url = str(page.url or "")
    except Exception:
        return ""
    if "/c/" not in url:
        return ""
    data = load_threads(profile_dir)
    if data.get(name) == url:
        return url
    data[name] = url
    path = threads_path(profile_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:  # pragma: no cover
        log.warning("대화 URL 저장 실패: %s", exc)
    return url


def goto_thread(name: str, page, profile_dir: str | Path | None = None) -> bool:
    """기억해 둔 대화로 이동한다. 없거나 열리지 않으면 False(새 대화 사용)."""
    url = thread_url(name, profile_dir)
    if not url:
        return False
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        if "/c/" in str(page.url or "") and composer(page, 8000) is not None:
            return True
    except Exception as exc:  # pragma: no cover - 네트워크
        log.warning("저장된 대화 열기 실패(%s): %s", name, exc)
    return False


def open_gpt(headless: bool = False, profile_dir: str | Path | None = None):
    """영속 프로필로 chatgpt.com을 연다. `(playwright, context, page)` 반환.

    비밀번호는 넣지 않는다. 로그인은 사용자가 창에서 직접 한다.
    """
    from playwright.sync_api import sync_playwright

    path = Path(profile_dir) if profile_dir is not None else default_profile_dir()
    path.mkdir(parents=True, exist_ok=True)

    playwright = sync_playwright().start()
    # PC에 설치된 실제 브라우저(크롬 → 엣지)를 우선 사용한다. Playwright 내장 크로미움은
    # 설치 상태에 따라 "Executable doesn't exist"로 실패한 전력이 있어 마지막 수단으로만 쓴다.
    last_exc: Exception | None = None
    context = None
    for channel in ("chrome", "msedge", None):
        try:
            kwargs = dict(
                headless=headless,
                viewport={"width": 1440, "height": 950},
                accept_downloads=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            if channel:
                kwargs["channel"] = channel
            context = playwright.chromium.launch_persistent_context(str(path), **kwargs)
            log.info("브라우저 실행: %s", channel or "bundled-chromium")
            break
        except Exception as exc:  # pragma: no cover - 환경 의존
            last_exc = exc
            log.warning("브라우저 실행 실패(%s): %s", channel or "bundled", str(exc).splitlines()[0][:160])
    if context is None:
        playwright.stop()
        raise RuntimeError(f"브라우저를 열 수 없습니다: {last_exc}")
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto(GPT_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:  # pragma: no cover - 네트워크
        log.warning("chatgpt.com 열기 실패: %s", exc)
    return playwright, context, page


def composer(page, timeout_ms: int = 3000):
    """프롬프트 입력창 로케이터. 못 찾으면 None."""
    for sel in COMPOSER_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout_ms):
                return loc
        except Exception:
            continue
    return None


def _any_visible(page, selectors, timeout_ms: int) -> bool:
    """후보 셀렉터 중 화면에 보이는 게 하나라도 있으면 True."""
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=timeout_ms):
                return True
        except Exception:
            continue
    return False


def is_logged_in(page, timeout_ms: int = 2500) -> bool:
    """정말 로그인된 상태인지 판정한다.

    **입력창(composer)만으로는 판정하지 않는다.** chatgpt.com은 로그아웃 방문자에게도
    입력창을 보여 주기 때문에 예전 판정은 항상 "로그인 완료"로 오인했다.

    세 조건을 모두 만족해야 로그인으로 본다.
    (a) `로그인`/`Log in`/`회원가입`/`Sign up` 버튼이 **안 보이고**
    (b) 계정 메뉴/아바타 요소가 **있고**
    (c) URL이 `/auth/login` 같은 인증 페이지가 **아니다**
    """
    # (c) URL
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if any(marker in url for marker in AUTH_URL_MARKERS):
        return False

    # (a) 로그아웃 버튼이 보이면 즉시 탈락
    if _any_visible(page, LOGGED_OUT_SELECTORS, timeout_ms=1200):
        return False

    # (b) 계정 요소가 있어야 한다
    return _any_visible(page, ACCOUNT_SELECTORS, timeout_ms=timeout_ms)


def wait_for_login(page, timeout: int = LOGIN_TIMEOUT, poll: float = 3.0) -> bool:
    """**정말** 로그인될 때까지 기다린다 (`is_logged_in` 기준).

    한도 안에 로그인하지 않으면 `False`를 돌려준다(예외를 던지지 않는다).
    호출자는 "로그인 대기 — 내일 재시도"처럼 조용히 물러나면 된다.
    30초마다 아직 로그인 전이라는 안내를 한 줄 찍는다.
    """
    if is_logged_in(page):
        return True

    print(LOGIN_PROMPT, flush=True)
    log.info(LOGIN_PROMPT)
    deadline = time.monotonic() + max(int(timeout), 0)
    next_notice = time.monotonic() + 30
    while time.monotonic() < deadline:
        time.sleep(poll)
        if is_logged_in(page):
            print("ChatGPT 로그인 확인됨.", flush=True)
            return True
        if time.monotonic() >= next_notice:
            print(WAITING_NOTICE, flush=True)
            next_notice = time.monotonic() + 30
    return False


# --- 세션 점검 ------------------------------------------------------------
#: 로그인 풀렸을 때 보낼 안내 (요구사항 고정 문구)
RELOGIN_NOTICE = (
    "ChatGPT 로그인이 풀렸습니다. PC에서 scripts\\gpt-login.cmd 를 실행해 "
    "다시 로그인해 주세요"
)


def _cookie_expiry(profile_dir: Path) -> float | None:
    """프로필 쿠키 DB에서 chatgpt.com 쿠키의 가장 늦은 만료 시각(epoch 초).

    **쿠키 값은 절대 읽지 않는다.** 만료 시각(메타)만 본다.
    """
    db = profile_dir / "Default" / "Network" / "Cookies"
    if not db.is_file():
        db = profile_dir / "Network" / "Cookies"
    if not db.is_file():
        return None

    import shutil as _shutil
    import sqlite3
    import tempfile

    # 브라우저가 잠가 둘 수 있으므로 복사본을 읽는다
    tmp = Path(tempfile.gettempdir()) / f"v2r-cookies-{int(time.time())}.db"
    try:
        _shutil.copy2(db, tmp)
        con = sqlite3.connect(str(tmp))
        try:
            row = con.execute(
                "SELECT MAX(expires_utc) FROM cookies WHERE host_key LIKE ?",
                ("%chatgpt.com",),
            ).fetchone()
        finally:
            con.close()
    except Exception as exc:
        log.info("쿠키 만료 확인 실패: %s", exc)
        return None
    finally:
        tmp.unlink(missing_ok=True)

    if not row or not row[0]:
        return None
    # 크롬은 1601-01-01 기준 마이크로초로 저장한다
    return int(row[0]) / 1_000_000 - 11_644_473_600


def check_gpt_session(profile_dir: str | Path | None = None, timeout_ms: int = 12000) -> dict:
    """로그인 상태를 **비밀번호 입력 없이** 확인한다.

    1차: 헤드리스로 프로필을 열어 입력창이 보이는지 본다.
    Cloudflare 챌린지 등으로 판정이 안 되면 2차로 프로필 쿠키의 **만료 시각만**
    읽어 추정한다 (쿠키 값은 읽지도, 찍지도 않는다).
    """
    path = Path(profile_dir) if profile_dir is not None else default_profile_dir()
    out: dict[str, Any] = {
        "ok": True,
        "logged_in": False,
        "method": "",
        "profile": str(path),
        "note": "",
    }
    if not path.is_dir():
        out["note"] = "프로필 폴더가 없습니다 (아직 한 번도 로그인하지 않음)."
        return out

    playwright = context = page = None
    try:
        playwright, context, page = open_gpt(headless=True, profile_dir=path)
        title = ""
        try:
            title = page.title() or ""
        except Exception:
            pass
        if is_logged_in(page, timeout_ms=timeout_ms):
            out.update(logged_in=True, method="headless", note="계정 메뉴 확인됨")
            return out
        if "just a moment" in title.lower() or "__cf_chl" in (page.url or ""):
            out["note"] = "헤드리스가 Cloudflare 챌린지에 막힘 → 쿠키 만료로 판정"
        else:
            out["note"] = "헤드리스에서 계정 메뉴를 찾지 못함 → 쿠키 만료로 판정"
    except Exception as exc:
        out["note"] = f"헤드리스 확인 실패({exc.__class__.__name__}) → 쿠키 만료로 판정"
    finally:
        _close(playwright, context, page)

    expiry = _cookie_expiry(path)
    out["method"] = "cookie-expiry"
    if expiry is None:
        out["note"] += " / chatgpt.com 쿠키 없음"
        return out
    remain = expiry - time.time()
    out["expires_in_days"] = round(remain / 86400, 1)
    out["logged_in"] = remain > 0
    out["note"] += f" / 쿠키 만료까지 {out['expires_in_days']}일"
    return out


# --- 이미지 생성 ----------------------------------------------------------
def _submit(page, prompt: str) -> None:
    """입력창에 프롬프트를 넣고 보낸다."""
    loc = composer(page, timeout_ms=15000)
    if loc is None:
        raise GptImageError("프롬프트 입력창을 찾지 못했습니다 (로그인 상태를 확인하세요).")
    loc.click()
    try:
        loc.fill("")
    except Exception:
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    # 줄바꿈이 전송으로 잡히지 않게 한 덩어리로 삽입한다
    page.keyboard.insert_text(prompt.replace("\n", " "))
    page.wait_for_timeout(400)

    for sel in SEND_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500) and btn.is_enabled(timeout=1500):
                btn.click()
                return
        except Exception:
            continue
    page.keyboard.press("Enter")


def _page_text(page) -> str:
    try:
        return page.locator("main").first.inner_text(timeout=3000)
    except Exception:
        try:
            return page.content()
        except Exception:
            return ""


def _limit_text(text: str) -> str | None:
    """한도 안내 문구가 있으면 그 문장을 돌려준다."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in LIMIT_PATTERNS:
            if pattern.search(stripped):
                return stripped[:300]
    return None


def _find_image(page) -> tuple[Any, str] | None:
    """충분히 큰 결과 이미지 (로케이터, src)를 찾는다."""
    for sel in IMAGE_SELECTORS:
        try:
            images = page.locator(sel)
            count = images.count()
        except Exception:
            continue
        for i in range(count - 1, -1, -1):  # 마지막(최신)부터
            img = images.nth(i)
            try:
                width = img.evaluate("el => el.naturalWidth || 0")
                src = img.evaluate("el => el.currentSrc || el.src || ''")
            except Exception:
                continue
            if int(width or 0) >= 256 and src:
                return img, str(src)
    return None


def _fetch_bytes(page, src: str) -> bytes:
    """`src`의 실제 이미지 바이트를 가져온다 (data:/blob:/http 모두 처리)."""
    if src.startswith("data:"):
        _, _, payload = src.partition(",")
        return base64.b64decode(payload)
    if src.startswith("blob:"):
        encoded = page.evaluate(
            """async (url) => {
                const res = await fetch(url);
                const buf = await res.arrayBuffer();
                let s = '';
                const bytes = new Uint8Array(buf);
                for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
                return btoa(s);
            }""",
            src,
        )
        return base64.b64decode(encoded)
    resp = page.request.get(src, timeout=120000)
    if not resp.ok:
        raise GptImageError(f"이미지 내려받기 실패 (HTTP {resp.status}): {src[:120]}")
    return resp.body()


def _download_bytes(page, timeout_ms: int = 60000) -> bytes | None:
    """내려받기 버튼이 있으면 원본 화질 파일을 받아 바이트로 돌려준다."""
    for sel in DOWNLOAD_SELECTORS:
        try:
            btn = page.locator(sel).last
            if not btn.is_visible(timeout=1500):
                continue
            with page.expect_download(timeout=timeout_ms) as info:
                btn.click()
            download = info.value
            tmp = Path(download.path()) if download.path() else None
            if tmp and tmp.is_file():
                return tmp.read_bytes()
        except Exception as exc:
            log.debug("내려받기 버튼(%s) 실패: %s", sel, exc)
            continue
    return None


def generate_image(
    page,
    prompt: str,
    out_dir: str | Path,
    timeout: int = IMAGE_TIMEOUT,
    stem: str = "",
    poll: float = 3.0,
) -> Path:
    """프롬프트 1개 → 이미지 1장. 긴 변 1024px JPEG 경로를 돌려준다.

    한도에 걸리면 `GptLimitError`, 그 밖의 실패는 `GptImageError`.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    before = _find_image(page)
    before_src = before[1] if before else ""

    _submit(page, prompt)

    deadline = time.monotonic() + max(int(timeout), 1)
    found: tuple[Any, str] | None = None
    while time.monotonic() < deadline:
        time.sleep(poll)
        text = _page_text(page)
        limit = _limit_text(text)
        if limit:
            raise GptLimitError(limit)
        hit = _find_image(page)
        if hit and hit[1] and hit[1] != before_src:
            # 그리는 중 잘린 이미지를 잡지 않도록 한 번 더 확인한다
            time.sleep(2.0)
            again = _find_image(page)
            if again and again[1] == hit[1]:
                found = again
                break
            found = again or hit
            break
    if not found:
        raise GptImageError(f"{timeout}초 안에 생성 이미지를 찾지 못했습니다.")

    _img_loc, src = found
    raw = _download_bytes(page)
    if not raw:
        raw = _fetch_bytes(page, src)
    if not raw:
        raise GptImageError("이미지 바이트를 받지 못했습니다.")

    name = stem or f"gpt_{int(time.time())}_{random.randint(100, 999)}"
    png = out / f"{name}.png"
    png.write_bytes(raw)
    try:
        jpg = postprocess(png, out / f"{name}.jpg")
    finally:
        png.unlink(missing_ok=True)
    return jpg


# --- 후처리 (AI 느낌 지우기) ----------------------------------------------
def postprocess(
    src: str | Path,
    dest: str | Path,
    long_side: int = TARGET_LONG_SIDE,
    rng: random.Random | None = None,
) -> Path:
    """긴 변 `long_side` JPEG로 만들면서 현실감 흔적을 얹는다.

    - 긴 변 1024px로 축소(원본이 더 작으면 그대로)
    - 가장자리 소량 랜덤 크롭 (완벽한 프레이밍 제거). 회전은 화질을 흐려서 하지 않는다.
    - 아주 약한 가우시안 노이즈 (센서 그레인, 화질 손상 없는 수준)
    - 품질 92~95 JPEG 재압축 (사용자 규칙: 화질 저하 금지)

    EXIF 랜덤화는 이후 `photo_washer.wash`가 맡는다 (규칙 0).
    """
    rng = rng or random.Random()
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(src) as opened:
        img = opened.convert("RGB")

        # 1) 축소 (긴 변 기준)
        width, height = img.size
        longest = max(width, height)
        if longest > long_side:
            scale = long_side / float(longest)
            img = img.resize(
                (max(int(width * scale), 1), max(int(height * scale), 1)),
                Image.LANCZOS,
            )

        # 2) 소량 크롭 (회전은 보간으로 화질을 흐리므로 제거 — 2026-09-19 사용자 피드백)
        width, height = img.size
        margin_x = max(int(width * rng.uniform(0.01, 0.025)), 2)
        margin_y = max(int(height * rng.uniform(0.01, 0.025)), 2)
        if width > 2 * margin_x + 8 and height > 2 * margin_y + 8:
            left = rng.randint(1, margin_x)
            top = rng.randint(1, margin_y)
            right = width - rng.randint(1, margin_x)
            bottom = height - rng.randint(1, margin_y)
            img = img.crop((left, top, right, bottom))

        # 3) 미세 노이즈
        img = add_noise(img, sigma=rng.uniform(0.8, 1.6), seed=rng.randint(0, 10**6))

        # 4) 재압축
        img.save(dest, format="JPEG", quality=rng.randint(92, 95), subsampling=0)
        img.close()
    return dest


def add_noise(img: Image.Image, sigma: float = 4.0, seed: int | None = None) -> Image.Image:
    """가우시안 그레인을 얹는다 (Pillow만 사용, numpy 불필요)."""
    if sigma <= 0:
        return img
    base = img.convert("RGB")
    noise = Image.effect_noise(base.size, float(sigma)).convert("RGB")
    # effect_noise는 128을 중심으로 흔들리므로 -128 만큼 이동해 더한다
    return ImageChops.add(base, noise, scale=1.0, offset=-128)


# --- 묶음 생성 ------------------------------------------------------------
def generate_batch(
    brand: str,
    keyword: str,
    n: int = 3,
    prompts: list[str] | None = None,
    warehouse: Any = None,
    headless: bool = False,
    login_timeout: int = LOGIN_TIMEOUT,
    image_timeout: int = IMAGE_TIMEOUT,
    collect: bool = True,
) -> dict:
    """프롬프트 n개로 이미지를 만들고 인박스에 떨군 뒤 적재 + 세탁까지 한다."""
    from v2r.warehouse import photo_request
    from v2r.warehouse.store import (
        KEYWORD_FOLDER,
        Warehouse,
        brand_folder_name,
        load_brands_config,
    )

    wh = warehouse or Warehouse()
    wh.ensure_dirs()
    cfg = load_brands_config()
    brand_name = brand_folder_name(brand, cfg) or brand
    folder = (keyword or "").strip() or KEYWORD_FOLDER

    count = max(int(n or 0), 0)
    if prompts is None:
        prompts = photo_request.build_gpt_prompts(brand_name, folder, wh.guides_dir, count, cfg)
    else:
        prompts = list(prompts)[:count] if count else list(prompts)
    out_dir = photo_request.drop_folder(wh.root, brand_name, folder)
    out_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "ok": False,
        "brand": brand_name,
        "keyword": folder,
        "folder": str(out_dir),
        "requested": len(prompts),
        "files": [],
        "errors": [],
        "limited": False,
    }
    if not prompts:
        result["ok"] = True
        result["message"] = "생성할 프롬프트가 없습니다."
        return result

    playwright = context = page = None
    try:
        playwright, context, page = open_gpt(headless=headless)
        if not wait_for_login(page, timeout=login_timeout):
            result["login_pending"] = True
            result["message"] = "로그인 대기 — 내일 재시도"
            return result
        # 사용자 규칙: 사진은 늘 같은 대화에서 이어서 만든다(새 대화 남발 금지)
        result["thread_reused"] = goto_thread(PHOTO_THREAD, page)

        for index, prompt in enumerate(prompts):
            try:
                path = generate_image(
                    page,
                    prompt,
                    out_dir,
                    timeout=image_timeout,
                    stem=f"gpt_{int(time.time())}_{index + 1}",
                )
                result["files"].append(str(path))
                remember_thread(PHOTO_THREAD, page)
            except GptLimitError as exc:
                result["limited"] = True
                result["wait_text"] = exc.wait_text
                result["errors"].append(f"사용 한도: {exc.wait_text}")
                break
            except Exception as exc:
                result["errors"].append(f"{index + 1}번 실패: {exc}")
            if index + 1 < len(prompts):
                time.sleep(random.uniform(*SLEEP_BETWEEN))
    except Exception as exc:
        result["errors"].append(f"브라우저 실패: {exc}")
    finally:
        _close(playwright, context, page)

    # 규칙 0: 만든 즉시 적재 + 세탁. 세탁 전 파일은 발행에 쓰지 않는다.
    if collect and result["files"]:
        try:
            result["collected"] = photo_request.collect_new(wh)
        except Exception as exc:
            result["errors"].append(f"수거/세탁 실패: {exc}")

    result["generated"] = len(result["files"])
    result["ok"] = bool(result["files"]) and not result["errors"]
    return result


def _close(playwright=None, context=None, page=None) -> None:
    """열린 자원을 조용히 닫는다."""
    for item in (page, context):
        try:
            if item is not None:
                item.close()
        except Exception:
            pass
    try:
        if playwright is not None:
            playwright.stop()
    except Exception:
        pass


def _login_cli() -> int:
    """`python -m v2r.warehouse.gpt_images --login` — 창을 띄우고 로그인을 기다린다."""
    print(f"ChatGPT 로그인 창을 엽니다. 프로필: {default_profile_dir()}")
    playwright, context, page = open_gpt(headless=False)
    try:
        # 창은 **진짜 로그인이 확인될 때까지** 닫지 않는다 (최대 15분).
        # 예전에는 로그아웃 상태에서도 보이는 입력창을 보고 곧바로 닫아 버렸다.
        if wait_for_login(page, timeout=LOGIN_TIMEOUT):
            print("로그인 완료. 이제 창을 닫아도 됩니다 (세션이 프로필에 저장됩니다).")
            return 0
        print("로그인 대기 시간이 끝났습니다. 다시 실행해 주세요.")
        return 1
    finally:
        _close(playwright, context, page)


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점. `--login`은 로그인 창, `--check`는 세션 점검."""
    args = list(argv if argv is not None else __import__("sys").argv[1:])
    if "--check" in args:
        out = check_gpt_session()
        print(f"로그인 상태: {'OK' if out['logged_in'] else '풀림'} ({out['method']}) {out['note']}")
        return 0 if out["logged_in"] else 1
    return _login_cli()


__all__ = [
    "COMPOSER_SELECTORS",
    "DOWNLOAD_SELECTORS",
    "GPT_URL",
    "IMAGE_SELECTORS",
    "LOGIN_PROMPT",
    "TARGET_LONG_SIDE",
    "ACCOUNT_SELECTORS",
    "AUTH_URL_MARKERS",
    "LOGGED_OUT_SELECTORS",
    "RELOGIN_NOTICE",
    "WAITING_NOTICE",
    "GptImageError",
    "is_logged_in",
    "GptLimitError",
    "add_noise",
    "check_gpt_session",
    "composer",
    "main",
    "default_profile_dir",
    "generate_batch",
    "generate_image",
    "open_gpt",
    "postprocess",
    "wait_for_login",
]


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
