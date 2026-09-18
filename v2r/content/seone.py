"""SE-ONE 문서(content_json) 생성 (api-spec §4).

규칙: 본문 한 줄 = paragraph 1개(빈 줄 보존), 연속 텍스트 줄은 하나의 text 컴포넌트,
`{사진}` 같은 플레이스홀더 자리에 이미지 컴포넌트 삽입, 컴포넌트 0개면 빈 텍스트 1개.
"""

from __future__ import annotations

import json
import re
import uuid

PLACEHOLDER = re.compile(r"\{[^{}]*\}")

VERSION = "2.9.0"


def _se_id() -> str:
    """SE-<uuid4> 형태의 컴포넌트 id."""
    return f"SE-{uuid.uuid4()}"


def _document_id() -> str:
    """문서 id (uuid4 hex 대문자 26자)."""
    return uuid.uuid4().hex.upper()[:26]


def _paragraph(text: str) -> dict:
    """한 줄 → paragraph 노드."""
    return {
        "id": _se_id(),
        "@ctype": "paragraph",
        "nodes": [{"id": _se_id(), "value": text, "@ctype": "textNode"}],
    }


def _text_component(lines: list[str]) -> dict:
    """연속 텍스트 줄 → text 컴포넌트 1개."""
    return {
        "id": _se_id(),
        "layout": "default",
        "@ctype": "text",
        "value": [_paragraph(line) for line in lines],
    }


def _normalize(body: str) -> str:
    """줄바꿈 표기 통일(CRLF/CR → LF). `\\r`이 문단에 남지 않게 한다."""
    return (body or "").replace("\r\n", "\n").replace("\r", "\n")


def count_placeholders(body: str) -> int:
    """본문 안의 플레이스홀더 개수."""
    return len(PLACEHOLDER.findall(_normalize(body)))


def body_lines_for_verify(body: str) -> list[str]:
    """검증용 본문 줄 목록(플레이스홀더 제거 후)."""
    return [PLACEHOLDER.sub("", raw) for raw in _normalize(body).split("\n")]


def build_document(body: str, image_components: list[dict]) -> dict:
    """본문과 이미지 컴포넌트로 SE-ONE 문서 dict를 만든다.

    플레이스홀더 수와 이미지 컴포넌트 수가 다르면 `ValueError`를 던진다.
    사진이 모자랄 때 조용히 건너뛰면 "사진 없는 글"이 등록되므로
    (api-spec §4: 사진 실패 시 발행 중단) 반드시 실패시킨다.
    """
    body = _normalize(body)
    images = list(image_components or [])
    slots = count_placeholders(body)
    if len(images) != slots:
        if len(images) < slots:
            raise ValueError(f"사진 수가 부족합니다: 자리 {slots}, 사진 {len(images)}")
        raise ValueError(f"사진 수가 많습니다: 자리 {slots}, 사진 {len(images)}")
    components: list[dict] = []
    pending: list[str] = []
    image_index = 0

    def flush() -> None:
        if pending:
            components.append(_text_component(list(pending)))
            pending.clear()

    for raw_line in body.split("\n"):
        matches = PLACEHOLDER.findall(raw_line)
        if not matches:
            pending.append(raw_line)
            continue

        text_only = PLACEHOLDER.sub("", raw_line)
        # 플레이스홀더만 있던 줄도 빈 문단으로 보존
        pending.append(text_only)
        flush()
        for _ in matches:
            if image_index < len(images):
                components.append(images[image_index])
            image_index += 1

    flush()

    if not components:
        components.append(_text_component([""]))

    return {
        "document": {
            "version": VERSION,
            "theme": "default",
            "language": "ko-KR",
            "id": _document_id(),
            "di": {
                "dif": False,
                "dio": [{"dis": "N", "dia": {"t": 0, "p": 0, "st": 2827, "sk": 0}}],
            },
            "components": components,
            "documentId": "",
        }
    }


def content_json(body: str, images: list[dict] | None = None) -> str:
    """SE-ONE 문서를 API가 요구하는 JSON 문자열로 직렬화."""
    doc = build_document(body, list(images or []))
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "PLACEHOLDER",
    "body_lines_for_verify",
    "build_document",
    "content_json",
    "count_placeholders",
]
