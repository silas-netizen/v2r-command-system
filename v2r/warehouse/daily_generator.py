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

#: 풀 파일 이름 (자사 카페 일상 글 — 모델 API로 생성)
POOL_FILENAME = "daily_pool.jsonl"
#: 제휴 카페 일상 글 풀 (ChatGPT 웹 세션으로 생성, API 토큰 0)
AFFILIATE_POOL_FILENAME = "affiliate_daily_pool.jsonl"
#: 모델 호출 1회당 생성 건수
BATCH_SIZE = 20
#: 길이 상한 (지침 §4 "일상 글 20~30자 또는 20~40자")
TITLE_MAX = 20
BODY_MAX = 40

#: 금지 문장부호 (지침 §2: 쉼표·마침표·말줄임표 전면 금지)
_BANNED_PUNCT = re.compile(r"[,.]|…")
_WS = re.compile(r"\s+")


def pool_path(warehouse_dir: str | Path, filename: str = POOL_FILENAME) -> Path:
    """풀 파일 경로."""
    return Path(warehouse_dir) / "manuscripts" / filename


def affiliate_pool_path(warehouse_dir: str | Path) -> Path:
    """제휴 카페 일상 글 풀 경로."""
    return pool_path(warehouse_dir, AFFILIATE_POOL_FILENAME)


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
# 제휴 카페 일상 글 — ChatGPT 웹 세션 생성 (API 토큰 0)
# --------------------------------------------------------------------
#: 제휴 일상 글 1회 요청당 건수 (웹 채팅은 응답이 길면 잘리므로 작게)
AFFILIATE_BATCH_SIZE = 10

#: 제휴 일상 글 작성 규칙 (지침 `제휴 게시판(소재 포함) 일상 글 작성.md` 요약)
AFFILIATE_RULES = (
    "너는 맘카페 회원이야. 카페에 올릴 짧은 일상 글을 쓴다.\n"
    "규칙:\n"
    "1. 제목 1줄, 본문 1줄. 딱 2줄이다.\n"
    f"2. 제목은 {TITLE_MAX}자 이내, 본문은 {BODY_MAX}자 이내.\n"
    "3. 쉼표(,) 마침표(.) 말줄임표(…)를 절대 쓰지 마라.\n"
    "4. ㅋㅋ ㅠㅠ 같은 한글 이모티콘은 써도 된다. 이모지(그림문자)는 금지.\n"
    "5. 브랜드명 제품명 광고 느낌 금지. 그냥 회원이 수다 떠는 말투.\n"
    "6. 나열식 정리식 말고 상황 중심으로 툭 던지듯이.\n"
    "7. 페르소나와 소재는 매번 다르게. 같은 패턴 반복 금지.\n"
    "출력 형식은 정확히 아래를 반복한다 (번호 없이):\n"
    "제목: ...\n본문: ...\n"
)

_TITLE_LINE = re.compile(r"^\s*제목\s*[:：]\s*(.+)$")
_BODY_LINE = re.compile(r"^\s*본문\s*[:：]\s*(.+)$")


def build_affiliate_prompt(cafe: str, count: int, avoid: list[str] | None = None) -> str:
    """제휴 일상 글 요청 프롬프트."""
    target = (cafe or "").strip() or "맘카페"
    parts = [
        AFFILIATE_RULES,
        f"카페 이름: {target}\n이 카페 회원이 쓸 법한 짧은 일상 글 {count}개를 만들어 줘.",
    ]
    if avoid:
        sample = "\n".join(f"- {t}" for t in list(avoid)[-30:])
        parts.append(f"아래 제목은 이미 썼다 겹치지 않게 해 줘\n{sample}")
    return "\n\n".join(parts)


def parse_affiliate_reply(text: str) -> list[tuple[str, str]]:
    """`제목: / 본문:` 형식 응답 → 규칙을 지킨 (제목, 본문) 목록."""
    out: list[tuple[str, str]] = []
    pending: str | None = None
    for raw in str(text or "").splitlines():
        m = _TITLE_LINE.match(raw)
        if m:
            pending = clean_line(m.group(1))
            continue
        m = _BODY_LINE.match(raw)
        if m and pending is not None:
            body = clean_line(m.group(1))
            if is_valid(pending, body):
                out.append((pending, body))
            pending = None
    return out


def generate_affiliate_pool_via_gpt(
    cafes: list[str],
    per_cafe: int,
    page: Any = None,
    warehouse_dir: str | Path | None = None,
    *,
    ask_fn: Any = None,
    max_calls: int = 10,
    headless: bool = False,
) -> dict:
    """제휴 카페 일상 글을 **ChatGPT 웹 세션**으로 만들어 풀 파일에 쌓는다.

    `page`를 주면 그 페이지를 쓰고, 없으면 직접 브라우저를 열고 로그인을 기다린다.
    `ask_fn(page, prompt)`를 주면 그것으로 물어본다(테스트용 대역).
    반환: `{ok, added, generated, cafes, pool, errors, login_pending?}`
    """
    from v2r.warehouse.store import Warehouse

    root = Path(warehouse_dir) if warehouse_dir is not None else Warehouse().root
    targets = [str(c).strip() for c in (cafes or []) if str(c).strip()]
    per_cafe = max(int(per_cafe or 0), 0)

    result: dict[str, Any] = {
        "ok": False,
        "added": 0,
        "generated": 0,
        "cafes": targets,
        "pool": str(affiliate_pool_path(root)),
        "errors": [],
    }
    if not targets or per_cafe <= 0:
        result["ok"] = True
        result["message"] = "생성할 카페나 건수가 없습니다."
        return result

    owned = None  # 우리가 연 브라우저면 닫는다
    if ask_fn is None:
        from v2r.warehouse import gpt_chat
        from v2r.warehouse.gpt_images import (
            DAILY_THREAD,
            _close,
            goto_thread,
            open_gpt,
            remember_thread,
            wait_for_login,
        )

        raw_ask = gpt_chat.ask

        def ask_fn(pg, prompt):  # 질문 뒤 대화 URL을 기억해 다음에도 같은 대화를 쓴다
            reply = raw_ask(pg, prompt)
            remember_thread(DAILY_THREAD, pg)
            return reply

        if page is None:
            owned = open_gpt(headless=headless)
            page = owned[2]
            if not wait_for_login(page):
                _close(*owned)
                result["login_pending"] = True
                result["message"] = "로그인 대기 — 내일 재시도"
                return result
        # 사용자 규칙: 일상 글은 늘 같은 대화에서 이어서 만든다
        result["thread_reused"] = goto_thread(DAILY_THREAD, page)

    made: list[Manuscript] = []
    seen: set[str] = {m.content_hash for m in load_pool(root, AFFILIATE_POOL_FILENAME)}
    try:
        for cafe in targets:
            remaining = per_cafe
            titles: list[str] = []
            calls = 0
            while remaining > 0 and calls < max_calls:
                calls += 1
                want = min(AFFILIATE_BATCH_SIZE, remaining)
                prompt = build_affiliate_prompt(cafe, want, titles)
                try:
                    reply = ask_fn(page, prompt)
                except Exception as exc:
                    result["errors"].append(f"{cafe}: {exc}")
                    break
                items = parse_affiliate_reply(reply)
                if not items:
                    result["errors"].append(f"{cafe}: 규칙에 맞는 글을 받지 못했습니다")
                    break
                for title, body in items:
                    if remaining <= 0:
                        break
                    digest = content_hash(title, body)
                    if digest in seen:
                        continue
                    seen.add(digest)
                    titles.append(title)
                    made.append(
                        Manuscript(
                            title=title,
                            body=body,
                            cafe=cafe,
                            source="affiliate_daily_pool",
                            source_row=len(made) + 1,
                            images_enabled=False,
                            content_hash=digest,
                        )
                    )
                    remaining -= 1
    finally:
        if owned is not None:
            from v2r.warehouse.gpt_images import _close

            _close(*owned)

    result["generated"] = len(made)
    result["items"] = list(made)  # 호출자가 방금 만든 글을 바로 쓰도록(발행 시 즉석 생성)
    result["added"] = save_pool(root, made, AFFILIATE_POOL_FILENAME)
    result["ok"] = result["added"] > 0 or not result["errors"]
    return result


# --------------------------------------------------------------------
# 풀 파일 입출력
# --------------------------------------------------------------------
def load_pool(warehouse_dir: str | Path, filename: str = POOL_FILENAME) -> list[Manuscript]:
    """풀 파일을 읽어 원고 목록으로. 깨진 줄은 건너뛴다."""
    path = pool_path(warehouse_dir, filename)
    if not path.exists():
        return []
    source_name = "affiliate_daily_pool" if filename == AFFILIATE_POOL_FILENAME else "daily_pool"
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
        data.setdefault("source", source_name)
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


def save_pool(
    warehouse_dir: str | Path, items: list[Manuscript], filename: str = POOL_FILENAME
) -> int:
    """풀 파일에 덧붙인다. 이미 있는 content_hash는 건너뛴다. 추가 건수 반환."""
    path = pool_path(warehouse_dir, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {m.content_hash for m in load_pool(warehouse_dir, filename)}
    source_name = "affiliate_daily_pool" if filename == AFFILIATE_POOL_FILENAME else "daily_pool"
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
                        "source": source_name,
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
    "AFFILIATE_BATCH_SIZE",
    "AFFILIATE_POOL_FILENAME",
    "AFFILIATE_RULES",
    "BATCH_SIZE",
    "BODY_MAX",
    "POOL_FILENAME",
    "TITLE_MAX",
    "affiliate_pool_path",
    "build_affiliate_prompt",
    "clean_line",
    "generate_affiliate_pool_via_gpt",
    "generate_daily_pool",
    "parse_affiliate_reply",
    "is_valid",
    "load_pool",
    "pool_path",
    "save_pool",
]
