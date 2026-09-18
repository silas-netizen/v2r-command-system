"""ChatGPT 웹앱(구독)에 글을 물어보는 얇은 래퍼. **API 토큰을 쓰지 않는다.**

`gpt_images`가 만들어 둔 브라우저/로그인 판정을 그대로 재사용한다.
`ask(page, prompt)`는 프롬프트를 입력창에 넣고 보낸 뒤 **응답 스트리밍이 끝날
때까지** 기다렸다가 마지막 어시스턴트 메시지의 텍스트를 돌려준다.
"""

from __future__ import annotations

import logging
import time

from v2r.warehouse.gpt_images import (
    GptLimitError,
    _limit_text,
    _page_text,
    _submit,
    composer,
)

log = logging.getLogger(__name__)

#: 기본 응답 대기 한도(초)
ASK_TIMEOUT = 120

#: 어시스턴트 메시지 후보 (마지막 것을 읽는다)
ASSISTANT_SELECTORS: tuple[str, ...] = (
    "[data-message-author-role='assistant']",
    "div[data-testid^='conversation-turn'] .markdown",
    "main .agent-turn",
)
#: 아직 생성 중일 때만 보이는 것들 (사라지면 스트리밍 끝)
STREAMING_SELECTORS: tuple[str, ...] = (
    "button[data-testid='stop-button']",
    "button[aria-label*='Stop' i]",
    "button[aria-label*='중지']",
    "[data-testid='stop-generating']",
)


class GptChatError(RuntimeError):
    """ChatGPT 대화 실패."""


def _assistant_text(page) -> str:
    """마지막 어시스턴트 메시지의 텍스트. 없으면 빈 문자열."""
    for sel in ASSISTANT_SELECTORS:
        try:
            nodes = page.locator(sel)
            count = nodes.count()
        except Exception:
            continue
        if count:
            try:
                return (nodes.nth(count - 1).inner_text(timeout=3000) or "").strip()
            except Exception:
                continue
    return ""


def _is_streaming(page) -> bool:
    """아직 응답을 쓰는 중인가."""
    for sel in STREAMING_SELECTORS:
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                return True
        except Exception:
            continue
    return False


def ask(page, prompt: str, timeout: int = ASK_TIMEOUT, poll: float = 1.5) -> str:
    """프롬프트 1개를 보내고 응답 텍스트를 받는다.

    스트리밍이 끝났는지는 (a) 중지 버튼이 사라졌고 (b) 텍스트가 2회 연속 그대로면
    끝난 것으로 본다. 사용 한도 문구가 뜨면 `GptLimitError`.
    """
    if composer(page, timeout_ms=15000) is None:
        raise GptChatError("프롬프트 입력창을 찾지 못했습니다 (로그인 상태를 확인하세요).")

    before = _assistant_text(page)
    _submit(page, prompt)

    deadline = time.monotonic() + max(int(timeout), 1)
    last = ""
    stable = 0
    while time.monotonic() < deadline:
        time.sleep(poll)

        limit = _limit_text(_page_text(page))
        if limit:
            raise GptLimitError(limit)

        text = _assistant_text(page)
        if not text or text == before:
            continue
        if text == last:
            stable += 1
            # 중지 버튼이 없고 텍스트가 두 번 연속 같으면 완성으로 본다
            if stable >= 2 and not _is_streaming(page):
                return text
        else:
            stable = 0
            last = text
    if last:
        log.warning("응답 대기 %s초 초과 — 그때까지 받은 텍스트를 사용합니다.", timeout)
        return last
    raise GptChatError(f"{timeout}초 안에 응답을 받지 못했습니다.")


__all__ = ["ASK_TIMEOUT", "ASSISTANT_SELECTORS", "GptChatError", "ask"]
