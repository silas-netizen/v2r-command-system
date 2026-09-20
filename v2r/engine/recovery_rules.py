"""오류 문구 → 복구 규칙 표 (토큰 0).

감시견이 실패를 만나면 **먼저 이 표**를 본다. 여기서 알아보면 모델을 부르지 않는다.
표에 없는 문구만 Tier 1(모델 1회 분류)로 넘어간다 (`monitor.diagnose`).

각 규칙
    name    : 규칙 이름(알림에 그대로 나온다)
    action  : 감시견이 할 일
              retry       — 같은 명령을 한 번 다시 등록
              wait        — 그냥 기다린다(사람이 할 일 없음)
              refresh     — 세션/토큰을 새로 받는다
              human       — 사람이 해야 한다
    hint    : 사람에게 보여 줄 한 줄 설명
    resume  : 사람이 그대로 복사해 보낼 수 있는 재개 명령(없으면 원래 명령)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RecoveryRule:
    """오류 한 종류에 대한 대처."""

    name: str
    action: str
    hint: str
    resume: str = ""


#: 순서대로 첫 매치. 좁은 규칙을 위에 둔다.
RULES: list[tuple[re.Pattern[str], RecoveryRule]] = [
    (
        re.compile(r"로그인\s*(한도|budget)|login[_ ]budget|하루\s*20회|로그인\s*횟수", re.I),
        RecoveryRule(
            "login_budget",
            "human",
            "하루 로그인 한도(20회)에 걸렸습니다. 내일까지 기다리거나 세션을 손으로 살려야 합니다.",
            "GPT 세션 점검",
        ),
    ),
    (
        re.compile(r"429|레이트|요청\s*제한|너무\s*많|rate.?limit|too many requests", re.I),
        RecoveryRule(
            "rate_limited",
            "wait",
            "서버가 요청을 잠시 막았습니다. 실행기가 스스로 기다렸다가 이어서 합니다.",
        ),
    ),
    (
        re.compile(r"401|403|토큰|token|인증|unauthorized|세션\s*만료", re.I),
        RecoveryRule(
            "token_expired",
            "refresh",
            "로그인 토큰이 만료됐습니다. 실행기가 토큰을 새로 받고 다시 시도합니다.",
        ),
    ),
    (
        re.compile(r"timeout|timed out|시간\s*초과|네트워크|network|connection|연결", re.I),
        RecoveryRule(
            "network",
            "retry",
            "네트워크가 잠깐 끊겼습니다. 같은 명령을 한 번 더 시도합니다.",
        ),
    ),
    (
        re.compile(r"카페|게시판|말머리|menu|board|찾을\s*수\s*없", re.I),
        RecoveryRule(
            "catalog_mismatch",
            "human",
            "카페·게시판 이름이 사이트와 다릅니다. 카탈로그를 다시 읽어야 합니다.",
            "카페 목록",
        ),
    ),
    (
        re.compile(r"사진|이미지.*(부족|없|모자)|재고", re.I),
        RecoveryRule(
            "photo_shortage",
            "human",
            "쓸 사진이 모자랍니다. 사진을 만들거나 세탁해야 합니다.",
            "사진 세탁",
        ),
    ),
    (
        re.compile(r"이모지|emoji", re.I),
        RecoveryRule(
            "emoji",
            "human",
            "이모지 때문에 글이 막혔습니다. 이모지 정리를 돌려 주세요.",
            "이모지 정리",
        ),
    ),
    (
        re.compile(r"중복|duplicate|이미\s*올린|같은\s*글", re.I),
        RecoveryRule(
            "duplicate",
            "wait",
            "이미 올린 글입니다. 다시 올리지 않습니다(정상).",
        ),
    ),
    (
        re.compile(r"리스\s*상실|lease", re.I),
        RecoveryRule(
            "lease_lost",
            "retry",
            "다른 실행기가 작업을 가져갔습니다. 한 번 다시 등록합니다.",
        ),
    ),
]

#: 모델(Tier 1)이 고를 수 있는 규칙 이름
KNOWN_ACTIONS = {"retry", "wait", "refresh", "human"}


def match_rule(error: str) -> RecoveryRule | None:
    """오류 문구에 맞는 규칙. 없으면 None(→ Tier 1)."""
    text = str(error or "")
    if not text.strip():
        return None
    for pattern, rule in RULES:
        if pattern.search(text):
            return rule
    return None


def rule_by_name(name: str) -> RecoveryRule | None:
    """이름으로 규칙 찾기(모델이 고른 이름을 검증할 때 쓴다)."""
    for _, rule in RULES:
        if rule.name == name:
            return rule
    return None


def rule_names() -> list[str]:
    """등록된 규칙 이름 전부."""
    return [rule.name for _, rule in RULES]


__all__ = ["RULES", "KNOWN_ACTIONS", "RecoveryRule", "match_rule", "rule_by_name", "rule_names"]
