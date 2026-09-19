"""채널 공통 규약과 보고 문장 포맷."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class IncomingCommand:
    """채널로 들어온 명령 한 건."""

    channel: str
    sender_id: str
    chat_id: str
    text: str
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Channel(Protocol):
    """텔레그램·슬랙 등 채널이 지켜야 할 최소 규약."""

    name: str
    enabled: bool

    def poll(self) -> list[IncomingCommand]:
        """새 메시지를 가져온다. 허용되지 않은 발신자는 무시한다."""
        ...

    def send(self, chat_id: str, text: str) -> bool:
        """특정 대화방에 보고 문장을 보낸다."""
        ...

    def broadcast(self, text: str) -> int:
        """허용된 모든 대화방에 보고 문장을 보낸다. 성공 건수 반환."""
        ...

    def send_photo(self, chat_id: str, path: Any, caption: str = "") -> bool:
        """사진 파일 한 장을 보낸다. 사진을 못 보내는 채널은 그냥 False."""
        return False

    def broadcast_photo(self, path: Any, caption: str = "") -> int:
        """허용된 모든 대화방에 사진을 보낸다. 사진 미지원 채널은 0."""
        return 0


def format_report(
    job_id: int | str,
    status: str,
    description: str = "",
    url: str | None = None,
    error: str | None = None,
) -> str:
    """작업 상태를 한국어 한 줄 보고 문장으로 만든다."""
    desc = (description or "").strip()
    if status in {"failed", "error", "실패"}:
        return f"작업 {job_id} 실패: {(error or desc or '알 수 없는 오류').strip()}"
    if status in {"uncertain", "불확실"}:
        return f"작업 {job_id} 불확실: {desc} (수동 확인 필요)"
    if status in {"done", "published", "완료"}:
        line = f"작업 {job_id} 완료: {desc}"
        return f"{line} {url}".rstrip() if url else line
    return f"작업 {job_id} 진행: {desc}"
