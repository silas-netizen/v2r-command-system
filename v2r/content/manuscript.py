"""원고 모델과 원고 본문 파서. legacy §5 기준."""

from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, Field

# 마크다운 장식 제거용
_DECOR_LINE = re.compile(r"^\s*(?:[-=*_]{3,})\s*$")
_HEAD_MARK = re.compile(r"^\s*#{1,6}\s*")
_FENCE = re.compile(r"^\s*```.*$")

#: 섹션 라벨 정규식 (`제목 :`, `본문 :`, `댓글3 :`, `대대댓글2 :` …)
_SECTION = re.compile(
    r"^\s*(?P<label>제목|본문|대{0,3}댓글\s*\d*)\s*[:：]\s*(?P<rest>.*)$"
)
#: "댓글 세트", "댓글 세트 1" 같은 장식용 소제목
_SET_HEADING = re.compile(r"^\s*(?:댓글|대댓글)?\s*세트\s*\d*\s*$")

_ROLE_BY_DEPTH = {0: "comment", 1: "reply", 2: "reply2", 3: "reply3"}


class CommentNode(BaseModel):
    """댓글 트리 노드."""

    label: str
    text: str = ""
    depth: int = 0
    role: str = "comment"


class Manuscript(BaseModel):
    """원고 한 건."""

    title: str
    body: str
    cafe: str = ""
    board: str = ""
    head: str = ""
    source: str = ""
    #: 사진 폴더를 고를 때 쓰는 브랜드 이름(비어 있으면 `pick_images`가 `source`로 대신 찾는다).
    #: `local_brand` 원천처럼 여러 브랜드가 한 폴더에 섞여 있을 때 채운다.
    brand: str = ""
    source_row: int = 0
    keyword: str = ""
    tags: list[str] = Field(default_factory=list)
    account: str = ""
    account_type: str = ""
    images_enabled: bool = True
    manuscript_type: str = ""
    comments: list[CommentNode] = Field(default_factory=list)
    content_hash: str = ""


def content_hash(title: str, body: str) -> str:
    """제목+본문의 공백 정규화 sha256."""
    norm_title = re.sub(r"\s+", " ", title or "").strip()
    norm_body = re.sub(r"[ \t]+", " ", body or "")
    norm_body = "\n".join(line.strip() for line in norm_body.splitlines()).strip()
    return hashlib.sha256(f"{norm_title}\n{norm_body}".encode("utf-8")).hexdigest()


def tags_from_keyword(kw: str) -> list[str]:
    """키워드 → 태그 목록(공백 제거)."""
    cleaned = re.sub(r"\s+", "", kw or "")
    return [cleaned] if cleaned else []


def _strip_decor(line: str) -> str:
    """마크다운 장식(#, *, 백틱)을 걷어낸다. 들여쓰기는 유지."""
    out = _HEAD_MARK.sub("", line)
    out = out.replace("`", "")
    # 굵게/기울임 표시 제거 (본문 중괄호 자리표시자는 건드리지 않음)
    out = re.sub(r"\*{1,3}", "", out)
    return out.rstrip()


def _depth_of(label: str) -> int:
    """`대대댓글2` → 2."""
    return len(label) - len(label.lstrip("대"))


def parse_article(text: str) -> Manuscript:
    """원고 텍스트를 Manuscript로 파싱한다."""
    title = ""
    body_lines: list[str] = []
    comments: list[CommentNode] = []
    current: str | None = None  # "title" | "body" | 댓글 라벨

    for raw in (text or "").splitlines():
        if _FENCE.match(raw) or _DECOR_LINE.match(raw):
            continue
        line = _strip_decor(raw)
        if _SET_HEADING.match(line):
            continue
        m = _SECTION.match(line)
        if m:
            label = re.sub(r"\s+", "", m.group("label"))
            rest = m.group("rest").strip()
            if label == "제목":
                current = "title"
                title = rest
            elif label == "본문":
                current = "body"
                if rest:
                    body_lines.append(rest)
            else:
                depth = _depth_of(label)
                comments.append(
                    CommentNode(
                        label=label,
                        text=rest,
                        depth=depth,
                        role=_ROLE_BY_DEPTH.get(depth, "comment"),
                    )
                )
                current = label
            continue

        if current == "title":
            if line.strip():
                title = (title + " " + line.strip()).strip()
        elif current == "body":
            body_lines.append(line)
        elif current is None:
            if line.strip():
                # 라벨 없이 시작하면 첫 줄이 제목, 나머지는 본문
                title = line.strip()
                current = "body"
        else:
            if line.strip():
                node = comments[-1]
                node.text = (node.text + " " + line.strip()).strip()

    body = "\n".join(body_lines).strip("\n")
    return Manuscript(
        title=title.strip(),
        body=body,
        comments=comments,
        content_hash=content_hash(title, body),
    )
