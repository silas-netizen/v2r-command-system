"""V2R 로그인/인증 (docs/reference/v2r-auth-protocol.md 구현).

비밀번호·토큰은 절대 로그에 남기지 않는다.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .errors import V2RApiError, classify

log = logging.getLogger(__name__)

LOGIN_PATH = "/auths/login"
CHALLENGE_PATH = "/auths/challenge"
LOGOUT_PATH = "/auths/logout"
#: 액세스 토큰 갱신 경로 (docs/reference/v2r-auth-protocol.md §토큰 갱신).
#: 본문 형태는 번들 추정값이라 **미검증** — 4xx면 로그인으로 폴백한다.
REFRESH_PATH = "/auths/refresh_token"

#: 리프레시 쿠키 이름(도메인 api-v2r.daboja.im, 경로 /auths).
REFRESH_COOKIE_NAME = "refresh_token"

#: 만료 이 시간 전부터는 미리 갱신한다(초).
TOKEN_SKEW_S = 300.0

#: serve가 토큰을 미리 손봐 두는 주기(30분).
MAINTAIN_TICK_S = 30 * 60.0

#: 리프레시 쿠키 수명(로그인 후 약 24시간, 서버가 안 알려주면 이 값으로 가정).
REFRESH_COOKIE_TTL_S = 24 * 3600.0
#: 리프레시 쿠키 만료 이 시간 전에 **하루 한 번** 로그인해 새 쿠키를 받는다.
REFRESH_COOKIE_RENEW_S = 3600.0

#: 하루 로그인 자체 한도. 서버 한도(20/일/지문)에 닿기 전에 우리가 먼저 멈춘다.
LOGIN_BUDGET_PER_DAY = 15
LOGIN_WINDOW_S = 24 * 3600.0

#: 서버가 비-브라우저 User-Agent를 403 `{"detail":"bot user-agent blocked"}`로
#: 차단한다(2026-09-19 라이브 확인). 화면 코드와 같은 UA/Origin을 보낸다.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SITE_ORIGIN = "https://v2r.daboja.im"

#: 모든 요청에 붙는 브라우저 위장 기본 헤더.
BROWSER_HEADERS = {
    "Accept": "application/json",
    "User-Agent": BROWSER_USER_AGENT,
    "Origin": SITE_ORIGIN,
    "Referer": f"{SITE_ORIGIN}/",
}

FINGERPRINT_KEYS = (
    "canvas_hash",
    "color_depth",
    "hardware_concurrency",
    "has_touch",
    "language",
    "platform",
    "screen_height",
    "screen_width",
    "timezone",
    "version",
    "webdriver",
    "webgl_renderer",
    "webgl_vendor",
)


def _data_dir() -> Path:
    """설정의 data_dir. config 임포트 실패 시 저장소 기준 폴더."""
    try:
        from ..config import get_settings  # 지연 임포트 (병렬 작성 모듈)

        return Path(get_settings().data_dir)
    except Exception:
        return Path(__file__).resolve().parent.parent.parent / "data"


def _env_setting(name: str, env_key: str, default: str = "") -> str:
    """설정값 조회. config 임포트 실패 시 환경변수 폴백."""
    try:
        from ..config import get_settings  # 지연 임포트

        value = getattr(get_settings(), name, "") or ""
        if value:
            return str(value)
    except Exception:
        pass
    return os.environ.get(env_key, default)


def api_base() -> str:
    """V2R API 베이스 URL."""
    return _env_setting("v2r_api", "V2R_API", "https://api-v2r.daboja.im")


def credentials() -> tuple[str, str]:
    """(email, password). 값은 로그로 내보내지 않는다."""
    return (
        _env_setting("v2r_email", "V2R_EMAIL"),
        _env_setting("v2r_password", "V2R_PASSWORD"),
    )


def _now() -> float:
    """현재 epoch 초. 테스트에서 가짜 시계로 바꿔 끼울 수 있게 함수로 둔다."""
    return time.time()


def decode_exp(token: str) -> float | None:
    """JWT의 `exp`(만료 epoch 초)를 **서명 검증 없이** 읽는다.

    형식이 JWT가 아니거나 `exp`가 없으면 None. 토큰 값 자체는 로그에 남기지 않는다.
    """
    if not isinstance(token, str) or token.count(".") < 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    try:
        return float(exp)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _write_atomic(path: Path, text: str) -> None:
    """같은 폴더의 임시 파일에 쓰고 원자적으로 바꿔 끼운다(동시 프로세스 안전)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _default_fingerprint() -> dict:
    """고정 지문 1회 생성값."""
    return {
        "canvas_hash": secrets.token_hex(16),
        "color_depth": 24,
        "hardware_concurrency": os.cpu_count() or 8,
        "has_touch": False,
        "language": "ko-KR",
        "platform": "Win32",
        "screen_height": 1080,
        "screen_width": 1920,
        "timezone": "Asia/Seoul",
        "version": "v1",
        "webdriver": False,
        "webgl_renderer": (
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
        "webgl_vendor": "Google Inc. (NVIDIA)",
    }


def fingerprint_json(fingerprint: dict) -> str:
    """키 정렬 + 공백 없는 JSON 직렬화 (fp_hash 및 헤더 공통)."""
    ordered = {k: fingerprint[k] for k in sorted(fingerprint)}
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


@dataclass
class DeviceProfile:
    """`data/device.json`에 고정 저장되는 기기 지문."""

    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    fingerprint: dict = field(default_factory=_default_fingerprint)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "DeviceProfile":
        """저장본을 읽고, 없으면 새로 만들어 저장한다."""
        target = Path(path) if path is not None else _data_dir() / "device.json"
        if target.exists():
            try:
                raw = json.loads(target.read_text(encoding="utf-8"))
                fp = raw.get("fingerprint") or {}
                if isinstance(fp, dict) and fp:
                    # 키가 일부 빠졌어도 기기 일관성(auth-protocol §4)을 위해
                    # device_id와 기존 값은 유지하고 빠진 키만 기본값으로 보충한다.
                    defaults = _default_fingerprint()
                    merged = {k: fp.get(k, defaults[k]) for k in FINGERPRINT_KEYS}
                    profile = cls(
                        device_id=str(raw.get("device_id") or uuid.uuid4()),
                        fingerprint=merged,
                        path=target,
                    )
                    if merged != {k: fp.get(k) for k in FINGERPRINT_KEYS}:
                        profile.save()
                    return profile
            except (ValueError, OSError):
                pass
        profile = cls(path=target)
        profile.save()
        return profile

    def save(self) -> None:
        """디스크에 저장."""
        target = self.path or (_data_dir() / "device.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {"device_id": self.device_id, "fingerprint": self.fingerprint},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.path = target

    def fp_hash(self) -> str:
        """정렬 지문 JSON의 SHA-256 hex."""
        return hashlib.sha256(
            fingerprint_json(self.fingerprint).encode("utf-8")
        ).hexdigest()

    def signal_headers(self) -> dict[str, str]:
        """POST/PUT 등에 붙는 브라우저 신호 헤더."""
        return {
            "X-Device-Id": self.device_id,
            "X-Browser-Signal": fingerprint_json(self.fingerprint),
            "X-Browser-Signal-Status": "ok",
            "User-Agent": BROWSER_USER_AGENT,
        }


MAX_POW_DIFFICULTY = 8
MAX_POW_ATTEMPTS = 50_000_000


def solve_pow(challenge: dict, path: str, fp_hash: str) -> int:
    """`0`*difficulty로 시작하는 해시를 만드는 최소 proof_nonce를 찾는다.

    난이도·시도 횟수에 상한을 둬 무한 루프를 막는다.
    """
    version = challenge.get("version", "")
    nonce = challenge.get("nonce", "")
    timestamp = challenge.get("timestamp", "")
    difficulty = int(challenge.get("difficulty", 0) or 0)
    if difficulty > MAX_POW_DIFFICULTY:
        raise V2RApiError(f"PoW 난이도가 너무 높습니다: {difficulty}")
    prefix = "0" * difficulty
    head = f"{version}:{path}:{fp_hash}:{nonce}:{timestamp}:"
    for n in range(MAX_POW_ATTEMPTS):
        digest = hashlib.sha256(f"{head}{n}".encode("utf-8")).hexdigest()
        if digest.startswith(prefix):
            return n
    raise V2RApiError(f"PoW 해답을 찾지 못했습니다 (난이도 {difficulty})")


def build_proof(challenge: dict, path: str, fp_hash: str, proof_nonce: int) -> str:
    """`X-Challenge-Proof` 헤더 값(JSON 문자열)."""
    return json.dumps(
        {
            "version": challenge.get("version"),
            "nonce": challenge.get("nonce"),
            "timestamp": challenge.get("timestamp"),
            "difficulty": challenge.get("difficulty"),
            "signature": challenge.get("signature"),
            "path": path,
            "fp_hash": fp_hash,
            "proof_nonce": proof_nonce,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _extract_challenge(payload: Any) -> dict | None:
    """응답에서 challenge dict 추출."""
    if isinstance(payload, dict):
        ch = payload.get("challenge")
        if isinstance(ch, dict):
            return ch
        if "difficulty" in payload and "nonce" in payload:
            return payload
    return None


#: 챌린지 요구로 해석하는 오류 코드/사유 토큰
CHALLENGE_TOKENS = ("CHALLENGE", "POW", "PROOF")


def _needs_challenge(err: V2RApiError, status: int) -> bool:
    """PoW 챌린지를 풀고 재시도해야 하는 응답인지.

    느슨하게 판정하면 비밀번호 오류에도 같은 비밀번호로 2회 로그인해
    일일 로그인 한도를 두 배로 쓴다. 챌린지 신호가 분명할 때만 참.
    """
    if not 400 <= status < 500:
        return False
    text = f"{err.code or ''} {err.reason or ''}".upper()
    if any(token in text for token in CHALLENGE_TOKENS):
        return True
    extra = err.extra if isinstance(err.extra, dict) else {}
    return any(k in extra for k in ("challenge", "nonce", "difficulty", "signature"))


class AuthSession:
    """액세스 토큰 보관(`data/session.json`)과 로그인·갱신 흐름.

    서버가 기기 지문당 하루 20회 로그인만 허용하므로(2026-09-19 장애),
    토큰 파일 **하나**를 모든 프로세스가 공유하고 만료 5분 전에는
    `refresh_token`으로 갱신한다. 로그인은 최후 수단이며 하루 15회에서
    스스로 멈춘다. 토큰·쿠키 값은 어떤 경로로도 로그에 남기지 않는다.
    """

    def __init__(
        self,
        device: DeviceProfile | None = None,
        session_path: Path | None = None,
        login_log_path: Path | None = None,
    ) -> None:
        self.device = device or DeviceProfile.load()
        self.session_path = (
            Path(session_path)
            if session_path is not None
            else _data_dir() / "session.json"
        )
        self.login_log_path = (
            Path(login_log_path)
            if login_log_path is not None
            else self.session_path.parent / "login_log.json"
        )
        self.token: str | None = None
        self.refresh_token: str | None = None
        self.refresh_expires_at: float | None = None
        self.expires_at: float | None = None
        self.obtained_at: float | None = None
        self._refresh_shape_logged = False
        self._load_file()

    # ---- 파일 ----
    def _load_file(self) -> str | None:
        """저장 파일을 다시 읽어 메모리 상태를 맞춘다(다른 프로세스의 갱신 반영)."""
        try:
            raw = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        token = raw.get("access_token")
        if isinstance(token, str) and token:
            self.token = token
            refresh = raw.get("refresh_token")
            self.refresh_token = refresh if isinstance(refresh, str) and refresh else None
            self.refresh_expires_at = _as_float(raw.get("refresh_expires_at"))
            self.expires_at = _as_float(raw.get("expires_at")) or decode_exp(token)
            self.obtained_at = _as_float(raw.get("obtained_at"))
        return self.token

    def _save_file(
        self,
        token: str,
        refresh_token: str | None = None,
        expires_at: float | None = None,
        refresh_expires_at: float | None = None,
    ) -> None:
        """토큰·리프레시 쿠키(와 그 만료)·액세스 만료를 원자적으로 저장한다."""
        if refresh_token is None:
            refresh_token = self.refresh_token
            if refresh_expires_at is None:
                refresh_expires_at = self.refresh_expires_at
        if expires_at is None:
            expires_at = decode_exp(token)
        payload = {
            "access_token": token,
            "refresh_token": refresh_token,
            "refresh_expires_at": refresh_expires_at,
            "obtained_at": _now(),
            "expires_at": expires_at,
        }
        _write_atomic(
            self.session_path, json.dumps(payload, ensure_ascii=False)
        )
        self.token = token
        self.refresh_token = refresh_token
        self.refresh_expires_at = refresh_expires_at
        self.expires_at = expires_at
        self.obtained_at = payload["obtained_at"]

    def invalidate(self) -> None:
        """토큰 폐기 + 저장 파일 삭제."""
        self.token = None
        self.expires_at = None
        self.obtained_at = None
        try:
            self.session_path.unlink()
        except OSError:
            pass

    # ---- 만료 ----
    def expiring_soon(self, skew: float = TOKEN_SKEW_S) -> bool:
        """만료까지 `skew`초 미만인가. 만료 시각을 모르면 False(그대로 재사용)."""
        if self.expires_at is None:
            return False
        return (float(self.expires_at) - _now()) < float(skew)

    def refresh_cookie_expiring(self, skew: float = REFRESH_COOKIE_RENEW_S) -> bool:
        """리프레시 쿠키가 `skew`초 안에 죽는가(= 이제 로그인 한 번이 필요하다).

        쿠키가 아예 없으면 갱신 자체가 불가능하므로 참으로 본다.
        """
        if not self.refresh_token:
            return True
        if self.refresh_expires_at is None:
            return False  # 만료를 모르면 일단 갱신으로 버틴다
        return (float(self.refresh_expires_at) - _now()) < float(skew)

    # ---- 로그인 횟수 예산 ----
    def _recent_logins(self, now: float | None = None) -> list[float]:
        """최근 24시간 안의 로그인 시각 목록."""
        now = _now() if now is None else now
        try:
            raw = json.loads(self.login_log_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = raw.get("logins") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            value = _as_float(item)
            if value is not None and (now - value) < LOGIN_WINDOW_S:
                out.append(value)
        return sorted(out)

    def login_count_today(self) -> int:
        """최근 24시간 로그인 횟수."""
        return len(self._recent_logins())

    def check_login_budget(self) -> None:
        """예산을 넘었으면 로그인하지 않고 `kind="login_budget"`으로 막는다."""
        used = self.login_count_today()
        if used >= LOGIN_BUDGET_PER_DAY:
            raise V2RApiError(
                f"로그인 횟수 보호: 최근 24시간 로그인 {used}회 "
                f"(자체 한도 {LOGIN_BUDGET_PER_DAY}회, 서버 한도 20회)로 "
                "더 로그인하지 않습니다. 저장된 토큰이 살아날 때까지 기다려 주세요.",
                kind="login_budget",
            )

    def _record_login(self) -> None:
        """로그인 시도 1회를 기록한다(성공 여부와 무관하게 서버는 센다)."""
        items = self._recent_logins()
        items.append(_now())
        try:
            _write_atomic(
                self.login_log_path,
                json.dumps({"logins": items}, ensure_ascii=False),
            )
        except OSError as exc:  # 기록 실패가 로그인을 막지는 않는다
            log.warning("로그인 횟수 기록 실패: %s", exc)

    # ---- 로그인 ----
    def _post_login(
        self,
        client: httpx.Client,
        email: str,
        password: str,
        proof: str | None = None,
    ) -> httpx.Response:
        self.check_login_budget()
        self._record_login()
        headers = self.device.signal_headers()
        if proof is not None:
            headers["X-Challenge-Proof"] = proof
        # multipart/form-data 강제: (None, value) 형태 필드
        return client.post(
            LOGIN_PATH,
            files={"username": (None, email), "password": (None, password)},
            headers=headers,
        )

    def login(self, client: httpx.Client, email: str, password: str) -> str:
        """폼 로그인(+필요 시 PoW 챌린지) 후 access_token 반환."""
        response = self._post_login(client, email, password)
        if response.status_code < 400:
            return self._store_token(response)

        err = V2RApiError.from_response(response, "로그인 실패")
        if err.kind == "rate_limited":
            raise err
        if not _needs_challenge(err, response.status_code):
            raise err

        # 챌린지 필요 → PoW 후 재시도
        # 챌린지는 GET 전용(POST는 405 Method Not Allowed, 2026-09-19 라이브 확인)
        ch_res = client.get(CHALLENGE_PATH, headers=self.device.signal_headers())
        if ch_res.status_code >= 400:
            raise V2RApiError.from_response(ch_res, "챌린지 발급 실패")
        challenge = _extract_challenge(ch_res.json())
        if challenge is None:
            raise V2RApiError("챌린지 응답 형식 오류", status=ch_res.status_code)

        fp_hash = self.device.fp_hash()
        proof_nonce = solve_pow(challenge, LOGIN_PATH, fp_hash)
        proof = build_proof(challenge, LOGIN_PATH, fp_hash, proof_nonce)

        retry = self._post_login(client, email, password, proof=proof)
        if retry.status_code >= 400:
            raise V2RApiError.from_response(retry, "챌린지 재시도 로그인 실패")
        return self._store_token(retry)

    def _store_token(self, response: httpx.Response) -> str:
        payload = response.json()
        token = None
        if isinstance(payload, dict):
            block = payload.get("token")
            if isinstance(block, dict):
                token = block.get("access_token")
            elif isinstance(block, str):
                token = block
            if not token:
                token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise V2RApiError("로그인 응답에 access_token 없음", status=response.status_code)
        cookie, cookie_exp = _refresh_cookie(response)
        if cookie:
            # 서버가 만료를 안 알려주면 로그인 후 약 24시간으로 가정한다(실측).
            self._save_file(
                token,
                refresh_token=cookie,
                refresh_expires_at=cookie_exp or (_now() + REFRESH_COOKIE_TTL_S),
            )
        else:
            self._save_file(token)
        return token

    # ---- 갱신 ----
    def refresh(self, client: httpx.Client) -> str | None:
        """`/auths/refresh_token`으로 액세스 토큰을 갱신한다.

        본문 형태(`{"access_token": <옛 토큰>, "refresh_token": null}`)는 화면
        번들에서 읽은 **미검증** 값이다. 4xx/형식 오류면 None을 돌려 호출자가
        로그인으로 폴백하게 한다. 토큰·쿠키 값은 로그에 넣지 않는다.
        """
        if not self.token:
            return None
        headers = self.device.signal_headers()
        if self.refresh_token:
            # 쿠키는 요청 헤더로 직접 붙인다(클라이언트 쿠키 항아리를 건드리지 않게).
            headers["Cookie"] = f"{REFRESH_COOKIE_NAME}={self.refresh_token}"
        try:
            response = client.post(
                REFRESH_PATH,
                json={"access_token": self.token, "refresh_token": None},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            log.info("토큰 갱신 요청 실패(%s) → 로그인으로 폴백", type(exc).__name__)
            return None
        if not self._refresh_shape_logged:
            self._refresh_shape_logged = True
            log.info(
                "토큰 갱신 응답 확인: status=%s keys=%s set_cookie=%s",
                response.status_code,
                _payload_keys(response),
                REFRESH_COOKIE_NAME in (response.headers.get("set-cookie") or ""),
            )
        if response.status_code >= 400:
            log.info("토큰 갱신 거부(HTTP %s) → 로그인으로 폴백", response.status_code)
            return None
        try:
            return self._store_token(response)
        except (V2RApiError, ValueError):
            log.info("토큰 갱신 응답에 access_token 없음 → 로그인으로 폴백")
            return None

    def ensure_token(self, client: httpx.Client) -> str:
        """저장 토큰 재사용 → 만료 임박이면 갱신 → 그래도 안 되면 로그인.

        결정 전에 파일을 다시 읽어, 다른 프로세스가 방금 받아 둔 토큰을 쓴다.
        """
        if self.token and not self.expiring_soon():
            return self.token
        self._load_file()  # 다른 프로세스가 갱신했을 수 있다
        if self.token and not self.expiring_soon():
            return self.token
        if self.token:
            refreshed = self.refresh(client)
            if refreshed:
                return refreshed
        email, password = credentials()
        if not email or not password:
            raise V2RApiError("V2R_EMAIL / V2R_PASSWORD 미설정")
        return self.login(client, email, password)

    # ---- 주기 점검 ----
    def maintain(self, client: httpx.Client, tick: float = MAINTAIN_TICK_S) -> str:
        """serve가 주기적으로 부르는 토큰 관리 1회분.

        목표는 **하루 로그인 1회**다:
        - 리프레시 쿠키가 1시간 안에 죽는다 → 지금 로그인해 새 24시간 쿠키를 받는다.
        - 액세스 토큰이 다음 주기 전에 죽는다 → 갱신한다.
        - 그 외에는 아무것도 하지 않는다.

        돌려주는 값은 `login` / `refresh` / `keep` / `skip`(자격증명 없음).
        """
        self._load_file()  # 다른 프로세스가 갱신했을 수 있다
        email, password = credentials()

        if self.refresh_cookie_expiring():
            # 하루 한 번 예정된 로그인. 쿠키가 없을 때(첫 기동)도 여기로 온다.
            if not email or not password:
                return "skip"
            try:
                self.check_login_budget()
            except V2RApiError as exc:
                log.warning("%s", exc)
                return "skip"
            self.login(client, email, password)
            return "login"

        if self.token and not self.expiring_soon(max(TOKEN_SKEW_S, tick + TOKEN_SKEW_S)):
            return "keep"
        if self.token and self.refresh(client):
            return "refresh"
        if not email or not password:
            return "skip"
        try:
            self.check_login_budget()
        except V2RApiError as exc:
            log.warning("%s", exc)
            return "skip"
        self.login(client, email, password)
        return "login"

    def report(self) -> dict:
        """사람이 볼 세션 상태. 토큰·쿠키 값은 절대 넣지 않는다."""
        self._load_file()
        now = _now()
        return {
            "session_file": str(self.session_path),
            "has_token": bool(self.token),
            "token_expires_at": self.expires_at,
            "token_expires_in_s": (
                None if self.expires_at is None else float(self.expires_at) - now
            ),
            "has_refresh_cookie": bool(self.refresh_token),
            "refresh_expires_at": self.refresh_expires_at,
            "refresh_expires_in_s": (
                None
                if self.refresh_expires_at is None
                else float(self.refresh_expires_at) - now
            ),
            "logins_24h": self.login_count_today(),
            "login_budget": LOGIN_BUDGET_PER_DAY,
        }

    def logout(self, client: httpx.Client) -> None:
        """best-effort 로그아웃."""
        email, _ = credentials()
        try:
            client.post(
                LOGOUT_PATH,
                json={"loginId": email},
                headers=self.device.signal_headers(),
            )
        except Exception:
            pass
        finally:
            self.invalidate()


def _as_float(value: Any) -> float | None:
    """숫자로 읽히면 float, 아니면 None."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _refresh_cookie(response: httpx.Response) -> tuple[str | None, float | None]:
    """응답의 `refresh_token` 쿠키 (값, 만료 epoch). 값은 로그에 남기지 않는다."""
    value: str | None = None
    try:
        value = response.cookies.get(REFRESH_COOKIE_NAME) or None
    except Exception:  # pragma: no cover - 방어
        value = None
    if not value:
        return None, None
    # Max-Age를 먼저 본다(우리 시계 기준). 없으면 쿠키 항아리의 Expires.
    expires = _max_age_expiry(response)
    if expires is None:
        try:
            for cookie in response.cookies.jar:
                if cookie.name == REFRESH_COOKIE_NAME and cookie.expires:
                    expires = float(cookie.expires)
                    break
        except Exception:  # pragma: no cover - 방어
            expires = None
    return value, expires


def _max_age_expiry(response: httpx.Response) -> float | None:
    """`Set-Cookie`의 `Max-Age`로 만료를 계산(쿠키 항아리가 못 읽었을 때)."""
    raw = response.headers.get("set-cookie") or ""
    if REFRESH_COOKIE_NAME not in raw:
        return None
    for part in raw.split(";"):
        key, _, val = part.strip().partition("=")
        if key.lower() == "max-age":
            age = _as_float(val)
            if age is not None:
                return _now() + age
    return None


def _payload_keys(response: httpx.Response) -> list[str]:
    """응답 본문의 최상위 키 목록(값은 절대 포함하지 않는다)."""
    try:
        data = response.json()
    except ValueError:
        return []
    if isinstance(data, dict):
        keys = sorted(data)
        token = data.get("token")
        if isinstance(token, dict):
            keys += [f"token.{k}" for k in sorted(token)]
        return keys
    return []


__all__ = [
    "AuthSession",
    "DeviceProfile",
    "FINGERPRINT_KEYS",
    "LOGIN_BUDGET_PER_DAY",
    "LOGIN_PATH",
    "MAINTAIN_TICK_S",
    "REFRESH_COOKIE_RENEW_S",
    "REFRESH_COOKIE_TTL_S",
    "REFRESH_PATH",
    "TOKEN_SKEW_S",
    "api_base",
    "decode_exp",
    "build_proof",
    "classify",
    "credentials",
    "fingerprint_json",
    "solve_pow",
]
