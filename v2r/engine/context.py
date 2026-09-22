"""실행 컨텍스트(Runtime): 설정·DB·저장소·외부 자원 한 묶음.

무거운 자원(API 클라이언트·창고·모델·채널)은 처음 쓸 때 만든다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from v2r.config import Settings, get_settings, load_yaml
from v2r.store.accounts_state import AccountStateStore
from v2r.store.article_index import ArticleIndexStore
from v2r.store.db import connect, init_schema
from v2r.store.events import EventLog
from v2r.store.jobs import JobStore
from v2r.store.publications import PublicationStore
from v2r.store.republish import RepublishQueue
from v2r.store.sources_cache import SourceCache


@dataclass
class Runtime:
    """작업 실행에 필요한 자원 묶음."""

    settings: Settings
    conn: sqlite3.Connection
    jobs: JobStore
    publications: PublicationStore
    article_index: ArticleIndexStore
    account_state: AccountStateStore
    sources_cache: SourceCache
    events: EventLog
    republish: RepublishQueue = None  # type: ignore[assignment]
    cafes_cfg: dict = field(default_factory=dict)
    sources_cfg: dict = field(default_factory=dict)
    accounts_cfg: dict = field(default_factory=dict)

    # 지연 생성 자원
    _client: Any = None
    _catalog: Any = None
    _warehouse: Any = None
    _llm: Any = None
    _llm_ready: bool = False
    _channels: Any = None
    # 실행 중 보조 상태(제휴 일상 글 풀 등)
    scratch: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Runtime을 직접 만든 곳(테스트 등)에서도 재발행 대기 줄은 항상 쓸 수 있어야 한다
        if self.republish is None and self.conn is not None:
            self.republish = RepublishQueue(self.conn)

    # ---- 생성/정리 ----
    @classmethod
    def open(cls, settings: Settings | None = None, conn: sqlite3.Connection | None = None) -> "Runtime":
        """설정을 읽고 DB를 연 실행 컨텍스트를 만든다."""
        st = settings or get_settings()
        connection = conn if conn is not None else connect(st.db_path)
        init_schema(connection)
        return cls(
            settings=st,
            conn=connection,
            jobs=JobStore(connection),
            publications=PublicationStore(connection),
            article_index=ArticleIndexStore(connection),
            account_state=AccountStateStore(connection),
            sources_cache=SourceCache(connection),
            events=EventLog(connection),
            republish=RepublishQueue(connection),
            cafes_cfg=load_yaml("cafes"),
            sources_cfg=load_yaml("sources"),
            accounts_cfg=load_yaml("accounts_sources"),
        )

    def close(self) -> None:
        """열린 자원을 조용히 닫는다."""
        for closer in (self._client, self.conn):
            if closer is None:
                continue
            try:
                closer.close()
            except Exception:
                pass
        self._client = None
        self._catalog = None
        self._warehouse = None
        self._channels = None
        self._llm = None
        self._llm_ready = False

    # ---- 지연 자원 ----
    @property
    def client(self) -> Any:
        """V2R API 클라이언트(토큰은 AuthSession이 관리)."""
        if self._client is None:
            from v2r.api.auth import AuthSession
            from v2r.api.client import V2RClient

            # 토큰이 없으면 첫 요청 때 credentials()로 로그인한다.
            self._client = V2RClient(base_url=self.settings.v2r_api, auth=AuthSession())
        return self._client

    @property
    def catalog(self) -> Any:
        """카페·게시판·말머리 카탈로그."""
        if self._catalog is None:
            from v2r.api.catalog import Catalog

            self._catalog = Catalog(self.client)
        return self._catalog

    @property
    def warehouse(self) -> Any:
        """이미지·원고 창고."""
        if self._warehouse is None:
            from v2r.warehouse.store import Warehouse

            self._warehouse = Warehouse(self.settings.warehouse_dir)
        return self._warehouse

    @property
    def llm(self) -> Any:
        """모델 라우터. 키가 없으면 None."""
        if not self._llm_ready:
            self._llm_ready = True
            try:
                from v2r.llm.router import LLMRouter

                router = LLMRouter.from_settings(self.settings)
                self._llm = router if getattr(router, "enabled", False) else None
            except Exception:
                self._llm = None
        return self._llm

    @property
    def channels(self) -> list:
        """활성 보고 채널 목록."""
        if self._channels is None:
            from v2r.channels import build_channels

            try:
                self._channels = build_channels(self.settings)
            except Exception:
                self._channels = []
        return self._channels


__all__ = ["Runtime"]
