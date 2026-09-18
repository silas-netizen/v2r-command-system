"""짧은 일상 글 생성기 (결정 2, 2026-09-19).

기존 시트의 일상 글은 딱딱해서 쓰지 않는다. 대신 `제목 1줄 + 본문 1줄`짜리
짧은 일상 글을 모델(Sonnet 5, 용도 `daily_adapt`)로 새로 만들어 풀에 쌓는다.

풀 파일: `<warehouse>/manuscripts/daily_pool.jsonl` (한 줄에 원고 1건, 추가만 한다)
사진 없음, 브랜드 언급 없음.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from v2r.content.manuscript import Manuscript, content_hash

#: 풀 파일 이름
POOL_FILENAME = "daily_pool.jsonl"
#: 모델 호출 1회당 생성 건수
BATCH_SIZE = 20
#: 길이 상한 (지침 §4 "일상 글 20~30자 또는 20~40자")
TITLE_MAX = 20
BODY_MAX = 40

#: 금지 문장부호 (지침 §2: 쉼표·마침표·말줄임표 전면 금지)
_BANNED_PUNCT = re.compile(r"[,.]|…")
_WS = re.compile(r"\s+")


def pool_path(warehouse_dir: str | Path) -> Path:
    """풀 파일 경로."""
    return Path(warehouse_dir) / "manuscripts" / POOL_FILENAME


def clean_line(text: str) -> str:
    """한 줄로 합치고 금지 문장부호를 제거한다."""
    one = _WS.sub(" ", str(text or "").replace("\n", " ")).strip()
    one = _BANNED_PUNCT.sub("", one)
    return _WS.sub(" ", one).strip()


def is_valid(title: str, body: str) -> bool:
    """짧은 일상 글 규칙을 지켰는가."""
    if not title or not body:
        return False
    if len(title) > TITLE_MAX or len(body) > BODY_MAX:
        return False
    if "{" in title or "{" in body or "}" in title or "}" in body:
        return False
    return True


def _build_prompt(cafe: str, count: int, guides: str, avoid: list[str]) -> str:
    """사용자 프롬프트 한 덩어리."""
    parts: list[str] = []
    if guides:
        parts.append(guides)
    target = cafe or "일반 카페"
    parts.append(
        f"카페 이름: {target}\n"
        f"이 카페 회원이 쓸 법한 짧은 일상 글 {count}개를 만들어라.\n"
        f"제목 {TITLE_MAX}자 이내 1줄, 본문 {BODY_MAX}자 이내 1줄.\n"
        "소재는 서로 겹치지 않게 하고 브랜드명은 넣지 않는다."
    )
    if avoid:
        sample = "\n".join(f"- {t}" for t in avoid[-40:])
        parts.append(f"이미 쓴 제목이다 겹치지 않게 하라\n{sample}")
    return "\n\n".join(parts)


def _parse_items(data: Any) -> list[tuple[str, str]]:
    """모델 응답(JSON 배열) → (제목, 본문) 목록."""
    if isinstance(data, dict):
        data = data.get("items") or data.get("posts") or [data]
    out: list[tuple[str, str]] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        title = clean_line(item.get("title") or item.get("제목") or "")
        body = clean_line(item.get("body") or item.get("본문") or item.get("content") or "")
        if is_valid(title, body):
            out.append((title, body))
    return out


def generate_daily_pool(
    rt_llm: Any,
    cafes: list[str],
    per_cafe: int,
    guides_dir: str | Path | None = None,
    *,
    max_calls: int = 20,
) -> list[Manuscript]:
    """카페별 `per_cafe`건씩 짧은 일상 글을 만든다.

    `rt_llm`은 `complete_json(purpose, system, user)`을 가진 라우터다(용도 `daily_adapt`).
    실패한 호출은 건너뛰고 얻은 만큼만 돌려준다. 같은 내용(content_hash)은 한 번만 담는다.
    """
    from v2r.llm.prompts import DAILY_SHORT_SYSTEM, guides_context

    if rt_llm is None:
        raise RuntimeError("ANTHROPIC_API_KEY가 없어 일상 글을 생성할 수 없습니다")
    targets = [c for c in (cafes or []) if str(c).strip()] or [""]
    per_cafe = max(int(per_cafe or 0), 0)
    if per_cafe <= 0:
        return []

    guides = ""
    if guides_dir:
        try:
            guides = guides_context(guides_dir, max_chars=6000)
        except Exception:
            guides = ""

    out: list[Manuscript] = []
    seen: set[str] = set()
    row = 1
    for cafe in targets:
        remaining = per_cafe
        titles: list[str] = []
        calls = 0
        while remaining > 0 and calls < max_calls:
            calls += 1
            want = min(BATCH_SIZE, remaining)
            user = _build_prompt(cafe, want, guides, titles)
            try:
                data = rt_llm.complete_json(
                    "daily_adapt", DAILY_SHORT_SYSTEM, user, max_tokens=4000
                )
            except Exception:
                break
            items = _parse_items(data)
            if not items:
                break
            for title, body in items:
                if remaining <= 0:
                    break
                digest = content_hash(title, body)
                if digest in seen:
                    continue
                seen.add(digest)
                titles.append(title)
                out.append(
                    Manuscript(
                        title=title,
                        body=body,
                        cafe=cafe,
                        source="daily_pool",
                        source_row=row,
                        images_enabled=False,
                        content_hash=digest,
                    )
                )
                row += 1
                remaining -= 1
    return out


# --------------------------------------------------------------------
# 풀 파일 입출력
# --------------------------------------------------------------------
def load_pool(warehouse_dir: str | Path) -> list[Manuscript]:
    """풀 파일을 읽어 원고 목록으로. 깨진 줄은 건너뛴다."""
    path = pool_path(warehouse_dir)
    if not path.exists():
        return []
    out: list[Manuscript] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        data.setdefault("source", "daily_pool")
        data.setdefault("source_row", index)
        data["images_enabled"] = False
        if not data.get("content_hash"):
            data["content_hash"] = content_hash(data.get("title", ""), data.get("body", ""))
        try:
            m = Manuscript.model_validate(data)
        except Exception:
            continue
        m.source_row = index
        out.append(m)
    return out


def save_pool(warehouse_dir: str | Path, items: list[Manuscript]) -> int:
    """풀 파일에 덧붙인다. 이미 있는 content_hash는 건너뛴다. 추가 건수 반환."""
    path = pool_path(warehouse_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {m.content_hash for m in load_pool(warehouse_dir)}
    added = 0
    with path.open("a", encoding="utf-8") as fp:
        for m in items or []:
            digest = m.content_hash or content_hash(m.title, m.body)
            if not digest or digest in existing:
                continue
            existing.add(digest)
            fp.write(
                json.dumps(
                    {
                        "title": m.title,
                        "body": m.body,
                        "cafe": m.cafe,
                        "board": m.board,
                        "source": "daily_pool",
                        "images_enabled": False,
                        "content_hash": digest,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            added += 1
    return added


__all__ = [
    "BATCH_SIZE",
    "BODY_MAX",
    "POOL_FILENAME",
    "TITLE_MAX",
    "clean_line",
    "generate_daily_pool",
    "is_valid",
    "load_pool",
    "pool_path",
    "save_pool",
]
