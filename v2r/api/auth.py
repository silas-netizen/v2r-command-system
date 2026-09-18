"""V2R 로그인/인증 (docs/reference/v2r-auth-protocol.md 구현).

비밀번호·토큰은 절대 로그에 남기지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .errors import V2RApiError, classify

LOGIN_PATH = "/auths/login"
CHALLENGE_PATH = "/auths/challenge"
LOGOUT_PATH = "/auths/logout"

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
    """액세스 토큰 보관(`data/session.json`)과 로그인 흐름."""

    def __init__(
        self,
        device: DeviceProfile | None = None,
        session_path: Path | None = None,
    ) -> None:
        self.device = device or DeviceProfile.load()
        self.session_path = (
            Path(session_path)
            if session_path is not None
            else _data_dir() / "session.json"
        )
        self.token: str | None = None
        self._load_file()

    # ---- 파일 ----
    def _load_file(self) -> str | None:
        try:
            raw = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        token = raw.get("access_token")
        if isinstance(token, str) and token:
            self.token = token
        return self.token

    def _save_file(self, token: str) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(
            json.dumps(
                {"access_token": token, "obtained_at": time.time()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def invalidate(self) -> None:
        """토큰 폐기 + 저장 파일 삭제."""
        self.token = None
        try:
            self.session_path.unlink()
        except OSError:
            pass

    # ---- 로그인 ----
    def _post_login(
        self,
        client: httpx.Client,
        email: str,
        password: str,
        proof: str | None = None,
    ) -> httpx.Response:
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
        self.token = token
        self._save_file(token)
        return token

    def ensure_token(self, client: httpx.Client) -> str:
        """저장 토큰 재사용, 없으면 로그인."""
        if self.token:
            return self.token
        if self._load_file():
            return self.token  # type: ignore[return-value]
        email, password = credentials()
        if not email or not password:
            raise V2RApiError("V2R_EMAIL / V2R_PASSWORD 미설정")
        return self.login(client, email, password)

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


__all__ = [
    "AuthSession",
    "DeviceProfile",
    "FINGERPRINT_KEYS",
    "LOGIN_PATH",
    "api_base",
    "build_proof",
    "classify",
    "credentials",
    "fingerprint_json",
    "solve_pow",
]
