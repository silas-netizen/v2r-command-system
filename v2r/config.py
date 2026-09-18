"""설정 단일 진입점. `.env` + `config/*.yaml` 로드."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

# 저장소 루트 = 이 파일의 두 단계 위 (v2r/config.py -> repo/)
REPO_ROOT = Path(__file__).resolve().parent.parent


def _split_ids(raw: str | None) -> list[str]:
    """쉼표 구분 ID 목록 파싱."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class Settings:
    """환경 설정값. 비밀번호·토큰은 절대 로그에 남기지 않는다."""

    v2r_email: str = ""
    v2r_password: str = ""
    v2r_site: str = "https://v2r.daboja.im"
    v2r_api: str = "https://api-v2r.daboja.im"
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: list[str] = field(default_factory=list)
    slack_bot_token: str = ""
    slack_allowed_channel_ids: list[str] = field(default_factory=list)
    slack_webhook_url: str = ""
    anthropic_api_key: str = ""
    repo_root: Path = REPO_ROOT
    data_dir: Path = REPO_ROOT / "data"
    warehouse_dir: Path = REPO_ROOT / "warehouse"
    config_dir: Path = REPO_ROOT / "config"
    db_path: Path = REPO_ROOT / "data" / "v2r.sqlite"
    tz: str = "Asia/Seoul"

    def __repr__(self) -> str:  # 비밀값 가림
        return (
            f"Settings(v2r_email={self.v2r_email!r}, v2r_site={self.v2r_site!r}, "
            f"v2r_api={self.v2r_api!r}, repo_root={str(self.repo_root)!r}, "
            f"tz={self.tz!r}, secrets=***)"
        )

    __str__ = __repr__


def _build_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env")
    root = REPO_ROOT
    data_dir = Path(os.getenv("V2R_DATA_DIR") or (root / "data"))
    warehouse_dir = Path(os.getenv("V2R_WAREHOUSE_DIR") or (root / "warehouse"))
    config_dir = root / "config"

    settings = Settings(
        v2r_email=os.getenv("V2R_EMAIL", ""),
        v2r_password=os.getenv("V2R_PASSWORD", ""),
        v2r_site=os.getenv("V2R_SITE", "https://v2r.daboja.im"),
        v2r_api=os.getenv("V2R_API", "https://api-v2r.daboja.im"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_chat_ids=_split_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS")),
        slack_bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
        slack_allowed_channel_ids=_split_ids(os.getenv("SLACK_ALLOWED_CHANNEL_IDS")),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        repo_root=root,
        data_dir=data_dir,
        warehouse_dir=warehouse_dir,
        config_dir=config_dir,
        db_path=Path(os.getenv("V2R_DB_PATH") or (data_dir / "v2r.sqlite")),
        tz=os.getenv("V2R_TZ", "Asia/Seoul"),
    )
    # 저장 폴더 준비
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.warehouse_dir.mkdir(parents=True, exist_ok=True)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """설정 캐시 반환."""
    return _build_settings()


def load_yaml(name: str) -> dict:
    """`config/<name>.yaml` 읽기. 없으면 빈 dict."""
    stem = name[:-5] if name.endswith(".yaml") else name
    path = get_settings().config_dir / f"{stem}.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp)
    return data if isinstance(data, dict) else {}
