"""ChatGPT 웹앱 이미지 생성 테스트 (브라우저 없이 동작하는 부분만)."""

from __future__ import annotations

import random
import time
from pathlib import Path

import pytest
from PIL import Image

from v2r.command.parser import parse_korean_command
from v2r.command.spec import ALLOWED_TASKS
from v2r.warehouse import gpt_images, photo_request


# --- 프롬프트: AI 느낌 제거 문구가 들어간다 -------------------------------
def test_prompts_include_anti_ai_style(tmp_path: Path):
    prompts = photo_request.build_gpt_prompts("우아덤", "키워드", tmp_path, 3)
    assert len(prompts) == 3
    for text in prompts:
        assert photo_request.ANTI_AI_RULES in text
        # 핵심 지시가 실제로 담겨 있는지 (문구가 바뀌어도 의미가 남게 확인)
        for must in ("스냅샷", "모션 블러", "워터마크", "대칭"):
            assert must in text
        assert "3D 렌더" in text and "금지" in text


def test_prompt_scenes_differ(tmp_path: Path):
    prompts = photo_request.build_gpt_prompts("우아덤", "키워드", tmp_path, 4)
    assert len({p.split("장면: ")[1][:20] for p in prompts}) == 4


# --- 후처리: 1024px 이하 JPEG + 노이즈 ------------------------------------
def _png(path: Path, size=(2048, 1536), color=(130, 120, 110)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def test_postprocess_makes_small_jpeg_with_noise(tmp_path: Path):
    src = _png(tmp_path / "gen.png")
    dest = gpt_images.postprocess(src, tmp_path / "out.jpg", rng=random.Random(7))

    assert dest.is_file()
    with Image.open(dest) as img:
        assert (img.format or "").upper() == "JPEG"
        assert max(img.size) <= gpt_images.TARGET_LONG_SIDE
        # 회전·크롭으로 원본 비율 그대로는 아니다
        assert img.size[0] < 1024 or img.size[1] < 1024 or max(img.size) == 1024
        colors = img.convert("RGB").getcolors(maxcolors=200000)
    # 단색 PNG였는데 노이즈가 들어가 색이 여러 개가 된다
    assert colors is None or len(colors) > 50


def test_postprocess_keeps_small_image_small(tmp_path: Path):
    src = _png(tmp_path / "small.png", size=(320, 240))
    dest = gpt_images.postprocess(src, tmp_path / "small.jpg", rng=random.Random(3))
    with Image.open(dest) as img:
        assert max(img.size) <= 320


def test_add_noise_changes_pixels():
    flat = Image.new("RGB", (64, 64), (100, 100, 100))
    noisy = gpt_images.add_noise(flat, sigma=5.0)
    assert noisy.size == flat.size
    assert noisy.tobytes() != flat.tobytes()


def test_postprocess_output_can_be_washed(tmp_path: Path):
    """규칙 0: 생성물은 반드시 세탁 단계를 통과할 수 있어야 한다."""
    from v2r.warehouse.photo_washer import make_variants, read_camera_meta

    src = _png(tmp_path / "gen.png", size=(1400, 1050))
    jpg = gpt_images.postprocess(src, tmp_path / "gen.jpg", rng=random.Random(11))
    made = make_variants(jpg, 2, tmp_path / "washed")
    assert len(made) == 2
    assert all(read_camera_meta(p)["Make"] for p in made)


# --- 한도 감지 -------------------------------------------------------------
@pytest.mark.parametrize(
    "line",
    [
        "You've hit the usage limit for image generation.",
        "이미지 생성 한도에 도달했습니다.",
        "Please try again later.",
        "잠시 후 다시 시도해 주세요.",
    ],
)
def test_limit_text_detected(line):
    assert gpt_images._limit_text(f"앞줄\n{line}\n뒷줄") == line


def test_limit_text_ignores_normal_output():
    assert gpt_images._limit_text("사진을 만들었어요. 마음에 드시나요?") is None


def test_limit_error_carries_wait_text():
    err = gpt_images.GptLimitError("3시간 후에 다시 시도하세요")
    assert err.wait_text == "3시간 후에 다시 시도하세요"
    assert isinstance(err, gpt_images.GptImageError)


# --- 설정/경로 -------------------------------------------------------------
def test_profile_dir_is_gpt_specific():
    assert gpt_images.default_profile_dir().name == "browser-profile-gpt"


def test_login_prompt_text():
    assert gpt_images.LOGIN_PROMPT == "브라우저 창에서 ChatGPT에 직접 로그인해 주세요"


def test_wait_for_login_gives_up_quietly(monkeypatch):
    monkeypatch.setattr(gpt_images, "is_logged_in", lambda page, timeout_ms=0: False)
    assert gpt_images.wait_for_login(object(), timeout=1, poll=0.1) is False


def test_wait_for_login_true_when_account_present(monkeypatch):
    monkeypatch.setattr(gpt_images, "is_logged_in", lambda page, timeout_ms=0: True)
    assert gpt_images.wait_for_login(object(), timeout=1, poll=0.1) is True


# --- 로그인 판정 (로그아웃 방문자도 입력창을 본다) --------------------------
class _FakePage:
    """`is_logged_in` 판정용 가짜 페이지."""

    def __init__(self, url="https://chatgpt.com/", visible=()):
        self.url = url
        self._visible = set(visible)

    def locator(self, selector):
        page = self

        class _Loc:
            @property
            def first(self):
                return self

            def is_visible(self, timeout=0):
                return selector in page._visible

        return _Loc()


def test_logged_out_visitor_with_composer_is_not_logged_in():
    """회귀: 입력창만 보고 '로그인 완료'로 오인하던 버그."""
    page = _FakePage(visible={"#prompt-textarea"})
    assert gpt_images.is_logged_in(page) is False


def test_login_button_visible_means_logged_out():
    page = _FakePage(visible={"#prompt-textarea", "button:has-text('로그인')",
                              "[data-testid='profile-button']"})
    assert gpt_images.is_logged_in(page) is False


def test_auth_url_means_logged_out():
    page = _FakePage(url="https://auth.openai.com/auth/login",
                     visible={"[data-testid='profile-button']"})
    assert gpt_images.is_logged_in(page) is False


@pytest.mark.parametrize(
    "sel",
    [
        "[data-testid='profile-button']",
        "button[aria-label*='profile' i]",
        "img[alt*='User' i]",
    ],
)
def test_account_element_means_logged_in(sel):
    page = _FakePage(visible={"#prompt-textarea", sel})
    assert gpt_images.is_logged_in(page) is True


def test_no_account_element_means_logged_out():
    assert gpt_images.is_logged_in(_FakePage()) is False


def test_waiting_notice_printed_every_30s(monkeypatch, capsys):
    """로그인 전에는 30초마다 안내를 찍는다."""
    monkeypatch.setattr(gpt_images, "is_logged_in", lambda page, timeout_ms=0: False)
    clock = {"t": 0.0}
    monkeypatch.setattr(gpt_images.time, "monotonic", lambda: clock["t"])

    def _sleep(sec):
        clock["t"] += 31  # 한 번 잘 때마다 30초 경계를 넘긴다

    monkeypatch.setattr(gpt_images.time, "sleep", _sleep)

    assert gpt_images.wait_for_login(object(), timeout=90, poll=1) is False
    out = capsys.readouterr().out
    assert gpt_images.LOGIN_PROMPT in out
    assert out.count(gpt_images.WAITING_NOTICE) >= 2


def test_generate_batch_stops_on_login_pending(monkeypatch, tmp_path: Path):
    from v2r.warehouse.store import Warehouse

    monkeypatch.setattr(gpt_images, "open_gpt", lambda **kw: (None, None, object()))
    monkeypatch.setattr(gpt_images, "wait_for_login", lambda page, timeout=0: False)

    wh = Warehouse(tmp_path / "wh")
    out = gpt_images.generate_batch("우아덤", "키워드", 1, warehouse=wh)
    assert out["login_pending"] is True
    assert out["message"] == "로그인 대기 — 내일 재시도"
    assert out["ok"] is False


def test_generate_batch_collects_and_washes(monkeypatch, tmp_path: Path):
    from v2r.warehouse.store import Warehouse, sha256

    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()

    def _fake_generate(page, prompt, out_dir, timeout=0, stem="", poll=0.0):
        src = _png(tmp_path / f"{stem}.png", size=(1200, 900), color=(90, 140, 110))
        return gpt_images.postprocess(src, Path(out_dir) / f"{stem}.jpg")

    monkeypatch.setattr(gpt_images, "open_gpt", lambda **kw: (None, None, object()))
    monkeypatch.setattr(gpt_images, "wait_for_login", lambda page, timeout=0: True)
    monkeypatch.setattr(gpt_images, "generate_image", _fake_generate)
    monkeypatch.setattr(gpt_images, "SLEEP_BETWEEN", (0.0, 0.0))

    out = gpt_images.generate_batch("우아덤", "키워드", 2, warehouse=wh)
    assert out["ok"] is True, out["errors"]
    assert out["generated"] == 2
    # 규칙 0: 즉시 적재 + 세탁까지 끝났다
    assert out["collected"]["added"] == 2
    assert out["collected"]["variants"] >= 2
    originals = wh.list_originals("우아덤", "키워드")
    assert len(originals) == 2
    assert all(wh.washed_variants(sha256(p)) for p in originals)


def test_generate_batch_reports_limit(monkeypatch, tmp_path: Path):
    from v2r.warehouse.store import Warehouse

    def _boom(page, prompt, out_dir, timeout=0, stem="", poll=0.0):
        raise gpt_images.GptLimitError("2시간 뒤에 다시 시도하세요")

    monkeypatch.setattr(gpt_images, "open_gpt", lambda **kw: (None, None, object()))
    monkeypatch.setattr(gpt_images, "wait_for_login", lambda page, timeout=0: True)
    monkeypatch.setattr(gpt_images, "generate_image", _boom)
    monkeypatch.setattr(gpt_images, "SLEEP_BETWEEN", (0.0, 0.0))

    out = gpt_images.generate_batch("우아덤", "키워드", 3, warehouse=Warehouse(tmp_path / "wh"))
    assert out["limited"] is True
    assert out["wait_text"] == "2시간 뒤에 다시 시도하세요"
    assert out["generated"] == 0


# --- 명령 해석 -------------------------------------------------------------
def test_generate_photos_is_allowed_task():
    assert "generate_photos" in ALLOWED_TASKS


@pytest.mark.parametrize(
    "text",
    [
        "사진 생성해줘",
        "이미지 만들어줘",
        "브랜드 우아덤 키워드 키워드 사진 3개 생성",
        "우아덤 이미지 2장 만들어",
    ],
)
def test_generate_photos_patterns(text):
    spec = parse_korean_command(text)
    assert spec is not None
    assert spec.task == "generate_photos", text


def test_generate_photos_slots():
    spec = parse_korean_command("브랜드 우아덤 키워드 단호박 사진 3개 생성해줘")
    assert spec is not None
    assert spec.task == "generate_photos"
    assert spec.brand == "우아덤"
    assert spec.keyword == "단호박"
    assert spec.count == 3


def test_generate_photos_counts_sheets():
    spec = parse_korean_command("브랜드 우아덤 사진 4장 만들어줘")
    assert spec is not None and spec.count == 4


def test_request_photos_still_wins_for_request_wording():
    spec = parse_korean_command("브랜드 팥순이 키워드 단호박 사진 요청")
    assert spec is not None and spec.task == "request_photos"


def test_daily_generation_not_hijacked():
    spec = parse_korean_command("일상 글 3개 생성해줘")
    assert spec is not None and spec.task == "generate_daily"


# --- 세션 점검 / 킵얼라이브 ------------------------------------------------
def test_check_gpt_session_without_profile(tmp_path: Path):
    out = gpt_images.check_gpt_session(tmp_path / "없는프로필")
    assert out["logged_in"] is False
    assert "프로필 폴더가 없습니다" in out["note"]


def test_check_gpt_session_falls_back_to_cookie_expiry(monkeypatch, tmp_path: Path):
    """헤드리스가 막히면 쿠키 만료 시각만 보고 판정한다 (쿠키 값은 안 읽는다)."""
    profile = tmp_path / "prof"
    profile.mkdir()

    def _boom(**kwargs):
        raise RuntimeError("Cloudflare")

    monkeypatch.setattr(gpt_images, "open_gpt", _boom)
    monkeypatch.setattr(gpt_images, "_cookie_expiry", lambda p: time.time() + 86400 * 10)

    out = gpt_images.check_gpt_session(profile)
    assert out["logged_in"] is True
    assert out["method"] == "cookie-expiry"
    assert out["expires_in_days"] == 10.0


def test_check_gpt_session_expired_cookie(monkeypatch, tmp_path: Path):
    profile = tmp_path / "prof"
    profile.mkdir()
    monkeypatch.setattr(gpt_images, "open_gpt", lambda **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(gpt_images, "_cookie_expiry", lambda p: time.time() - 60)

    out = gpt_images.check_gpt_session(profile)
    assert out["logged_in"] is False


def test_relogin_notice_text():
    assert gpt_images.RELOGIN_NOTICE == (
        "ChatGPT 로그인이 풀렸습니다. PC에서 scripts\\gpt-login.cmd 를 실행해 "
        "다시 로그인해 주세요"
    )


def test_login_script_exists():
    script = Path(__file__).resolve().parent.parent / "scripts" / "gpt-login.cmd"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "--login" in text and "gpt_images" in text


@pytest.mark.parametrize(
    "text", ["gpt 세션 점검", "지피티 세션 유지", "GPT 로그인 점검해줘", "지피티 유지해줘"]
)
def test_gpt_keepalive_patterns(text):
    spec = parse_korean_command(text)
    assert spec is not None and spec.task == "gpt_keepalive", text


def test_gpt_keepalive_notifies_when_logged_out(monkeypatch):
    from v2r.engine import worker

    sent: list[str] = []
    monkeypatch.setattr(
        "v2r.warehouse.gpt_images.check_gpt_session",
        lambda **k: {"ok": True, "logged_in": False, "method": "cookie-expiry", "note": ""},
    )
    monkeypatch.setattr(worker, "notify_all", lambda ch, text: sent.append(text) or 1)

    class _RT:
        channels = []

    out = worker._gpt_keepalive(_RT(), None)
    assert out["notified"] is True
    assert sent == [gpt_images.RELOGIN_NOTICE]


def test_gpt_keepalive_quiet_when_logged_in(monkeypatch):
    from v2r.engine import worker

    sent: list[str] = []
    monkeypatch.setattr(
        "v2r.warehouse.gpt_images.check_gpt_session",
        lambda **k: {"ok": True, "logged_in": True, "method": "headless", "note": ""},
    )
    monkeypatch.setattr(worker, "notify_all", lambda ch, text: sent.append(text) or 1)

    class _RT:
        channels = []

    out = worker._gpt_keepalive(_RT(), None)
    assert "notified" not in out
    assert sent == []


def test_describe_mentions_gpt():
    from v2r.command.parser import describe_spec

    spec = parse_korean_command("브랜드 우아덤 키워드 단호박 사진 2개 생성")
    assert spec is not None
    text = describe_spec(spec)
    assert "사진 생성(GPT)" in text
    assert "브랜드 우아덤" in text and "키워드 단호박" in text


# --- gpt_chat.ask (스트리밍 종료 판정) --------------------------------------
class _ChatPage:
    """텍스트가 점점 늘다가 멈추는 가짜 응답 페이지."""

    def __init__(self, frames, streaming_until=1):
        self.url = "https://chatgpt.com/"
        self._frames = list(frames)
        self._i = -1
        self._streaming_until = streaming_until
        self.submitted = []
        self.keyboard = self
        self.wait_for_timeout = lambda ms: None

    # gpt_images._submit 이 쓰는 최소 API
    def locator(self, selector):
        page = self

        class _Loc:
            @property
            def first(self):
                return self

            @property
            def last(self):
                return self

            def is_visible(self, timeout=0):
                if "stop" in selector.lower() or "중지" in selector:
                    return page._i < page._streaming_until
                return selector == "#prompt-textarea"

            def is_enabled(self, timeout=0):
                return True

            def click(self):
                pass

            def fill(self, text):
                pass

            def count(self):
                return 1 if selector == "[data-message-author-role='assistant']" else 0

            def nth(self, i):
                return self

            def inner_text(self, timeout=0):
                if selector == "main":
                    return "정상 응답"
                page._i = min(page._i + 1, len(page._frames) - 1)
                return page._frames[page._i]

        return _Loc()

    def insert_text(self, text):
        self.submitted.append(text)

    def press(self, key):
        pass


def test_ask_returns_finished_reply(monkeypatch):
    from v2r.warehouse import gpt_chat

    monkeypatch.setattr(gpt_chat.time, "sleep", lambda s: None)
    page = _ChatPage(["제목: 하", "제목: 하나\n본문: 글", "제목: 하나\n본문: 글이다",
                      "제목: 하나\n본문: 글이다", "제목: 하나\n본문: 글이다"],
                     streaming_until=2)
    out = gpt_chat.ask(page, "테스트 프롬프트", timeout=30, poll=0)
    assert out == "제목: 하나\n본문: 글이다"
    assert page.submitted and "테스트 프롬프트" in page.submitted[0]


def test_ask_raises_on_limit(monkeypatch):
    from v2r.warehouse import gpt_chat

    monkeypatch.setattr(gpt_chat.time, "sleep", lambda s: None)
    monkeypatch.setattr(gpt_chat, "_page_text", lambda page: "You've hit the usage limit")
    page = _ChatPage(["아무거나"])
    with pytest.raises(gpt_images.GptLimitError):
        gpt_chat.ask(page, "프롬프트", timeout=10, poll=0)


def test_ask_needs_composer(monkeypatch):
    from v2r.warehouse import gpt_chat

    monkeypatch.setattr(gpt_chat, "composer", lambda page, timeout_ms=0: None)
    with pytest.raises(gpt_chat.GptChatError):
        gpt_chat.ask(object(), "프롬프트", timeout=5)
