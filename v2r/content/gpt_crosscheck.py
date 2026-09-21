"""GPT(Codex) 교차 검증 — 우리가 정한 규칙 목록 안에서만 판정받는다.

왜 만들었나
-----------
우리 검증기(`brand_writer.validate`)는 **우리가 적어 둔 규칙**만 봅니다.
같은 원고를 다른 모델에게 한 번 더 읽혀 보면 우리 눈이 놓친 자리가 보입니다
(2026-09-22 교차 검증 결과: `docs/reports/codex-crosscheck-2026-09-22.md`).

다만 GPT가 **제 마음대로** 평가하면 "의료 근거가 부족하다" "과장 광고 위험"
같은 우리 기준 밖의 말이 쏟아집니다. 우리 원고는 **가상 테스트 사이트용**이라
그런 판단은 쓰지 않습니다 (사용자 지시 2026-09-22).

그래서 이 모듈은
1. 브랜드 정리본 지침 + 대대댓글2 구조 규칙 + 우리 검증기 항목을 **번호 매긴
   체크리스트**로 만들어 넘기고,
2. "각 번호에 대해 통과/위반만 판정하라, 목록 밖의 의견·점수·제안은 쓰지 마라"
   고 못 박고,
3. 점수는 GPT가 아니라 **우리가** (통과 수 / 전체) 로 계산합니다.

안전장치
--------
- 읽기 전용(`-s read-only`)으로 부르고, 파일을 고치게 하지 않습니다.
- `codex.exe` 가 없거나 시간이 넘으면 **"미검증"** 으로 넘어가고 생성은 막지
  않습니다 (교차 검증 때문에 원고가 안 나오면 안 됩니다).
- 점수가 기준(`config/models.yaml` 의 `crosscheck.min_score`) 미만이면
  "GPT 지적" 항목으로 **딱 한 번** 부분 재시도합니다 (무한 반복 금지).
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_EFFORT = "medium"
#: 한 원고당 기다리는 최대 시간(초). 넘으면 "미검증"이다
DEFAULT_TIMEOUT = 300
DEFAULT_MIN_SCORE = 60

#: 정리본의 **말투·형식** 규칙 (부자연스러운 문장도 이 번호로만 잡게 한다)
TONE_ITEMS: tuple[str, ...] = (
    "말투 — 실제 네이버 카페 회원(주부)이 쓴 것처럼 존댓말 구어체다"
    " (설명문·광고문·AI 말투가 아니다)",
    "문장부호 — 쉼표 마침표 말줄임표(... ..)를 쓰지 않는다"
    " (물음표 느낌표 물결은 허용, 문장 구분은 ㅋㅋ ㅠㅠ ㅎㅎ 로 한다)",
    "이모지 — 그림 이모지를 쓰지 않는다 (한글 이모티콘 ㅋㅋ ㅠㅠ ㅎㅎ 만 쓴다)",
    "나열식 금지 — 번호 매김이나 항목 나열로 정리하지 않는다",
    "줄 나눔 — 본문은 한 줄 20자 안팎, 한 문단 4줄 이내로 자주 끊는다",
    "같은 표현·패턴을 연속 3회 이상 되풀이하지 않는다",
)

#: 대대댓글2 **4요소 구조** (우리가 정한 핵심 규칙)
REPLY2_ITEMS: tuple[str, ...] = (
    "대대댓글2 1) 제품을 먼저 꺼낸다 (후기형은 저도 이거 쓰는 중으로 받는다)",
    "대대댓글2 2) 왜 믿을 만한지 근거를 **완전한 문장 하나**로 적는다",
    "대대댓글2 3) 다른 방법으로는 왜 안 되는지 대안의 한계를 한 문장으로 적는다",
    "대대댓글2 4) 검색해보시면 후기 많아요 류로 마무리한다",
    "대대댓글2 — 낱말 나열이 아니라 서술 어미(~요 ~에요 ~더라구요)로 끝나는"
    " 완전한 문장으로 이어진다",
)

#: 판정에 **쓰지 말라고** 못 박는 것 (사용자 지시 2026-09-22)
OUT_OF_SCOPE = (
    "사실 여부 / 의학적 근거 / 과장 광고 / 인과관계 / 효능 주장 / 안전성"
    " — 이 원고는 가상 테스트용이라 **판단 대상이 아니다**. 절대 언급하지 마라"
)

_LABEL_RE = re.compile(r"대*댓글\d")


# ----------------------------------------------------------------- 설정·경로
def load_config(data: dict | None = None) -> dict:
    """`config/models.yaml` 의 `crosscheck` 칸 (없으면 기본값)."""
    if data is None:
        try:
            from v2r.config import load_yaml

            data = load_yaml("models")
        except Exception:
            data = {}
    cfg = (data or {}).get("crosscheck")
    cfg = dict(cfg) if isinstance(cfg, dict) else {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "min_score": int(cfg.get("min_score", DEFAULT_MIN_SCORE)),
        "model": str(cfg.get("model") or DEFAULT_MODEL),
        "effort": str(cfg.get("effort") or DEFAULT_EFFORT),
        "timeout": int(cfg.get("timeout_sec") or DEFAULT_TIMEOUT),
    }


def find_codex() -> str:
    """`codex.exe` 자리를 찾는다. 못 찾으면 빈 문자열 (그러면 미검증이다)."""
    env = (os.getenv("V2R_CODEX_EXE") or "").strip()
    if env and Path(env).exists():
        return env
    found = shutil.which("codex")
    if found:
        return found
    local = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    for pattern in (
        str(Path(local) / "OpenAI" / "Codex" / "bin" / "*" / "codex.exe"),
        str(Path(local) / "OpenAI" / "Codex" / "bin" / "codex.exe"),
    ):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return ""


# ----------------------------------------------------------------- 체크리스트
def checklist(manuscript: Any, rule: Any = None) -> list[str]:
    """이번 원고를 볼 **번호 매긴 규칙 목록**.

    우리 검증기 항목(`validate`) + 대대댓글2 4요소 + 정리본 말투 규칙.
    GPT는 이 목록의 번호에 대해서만 통과/위반을 답한다.
    """
    from v2r.content import brand_writer as bw

    rule = rule or bw.rule_for(bw.brand_of(manuscript), manuscript.manuscript_type)
    items = [
        f"{c['항목']} — {c['기준']}" for c in bw.validate(manuscript, rule)
    ]
    items += list(REPLY2_ITEMS)
    items += list(TONE_ITEMS)
    # 같은 뜻이 두 번 들어가지 않게 (차례는 그대로 둔다)
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def manuscript_text(manuscript: Any) -> str:
    """검증기에 넘길 원고 전문 (제목·본문·댓글 12개)."""
    lines = [f"제목: {manuscript.title}", "", "본문:", manuscript.body or "", "", "댓글:"]
    lines += [f"{c.label}: {c.text}" for c in manuscript.comments]
    return "\n".join(lines)


def build_prompt(
    manuscript: Any,
    items: list[str],
    guide_text: str = "",
    golden: list[str] | None = None,
    guide_limit: int = 14000,
) -> str:
    """체크리스트 프롬프트. **목록 밖 판단 금지**를 못 박는다."""
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(items, start=1))
    gold = "\n".join(f"- {g}" for g in (golden or []) if (g or "").strip())
    return "\n".join(
        [
            "당신은 네이버 카페 마케팅 원고의 **체크리스트 검사기**다."
            " 파일이나 명령은 건드리지 말고 답만 하라.",
            "",
            "아래 <체크리스트>의 **각 번호에 대해서만** 통과/위반을 판정하라.",
            f"- 목록 밖의 의견·감상·제안·점수는 쓰지 마라. 특히 {OUT_OF_SCOPE}",
            "- 말투가 어색하다는 판정도 반드시 말투 관련 번호에 붙여서 말하라",
            "- 판단이 애매하면 통과로 둔다 (확실한 위반만 false)",
            "",
            "출력은 아래 JSON 하나만, 한국어, 다른 말 금지:",
            '{"1": {"pass": true, "where": "", "why": ""},'
            ' "2": {"pass": false, "where": "댓글3", "why": "한 줄 이유"}, ...}',
            "모든 번호를 빠짐없이 넣어라. 점수는 매기지 마라.",
            "",
            "<체크리스트>",
            numbered,
            "</체크리스트>",
            "",
            "<대대댓글2 본보기(골든) — 이 구조를 기준으로 본다>" if gold else "",
            gold,
            "</대대댓글2 본보기>" if gold else "",
            "",
            "<브랜드 지침 — 위 번호의 뜻을 풀어 주는 참고 자료다"
            " (여기서 새 규칙을 만들어 내지 마라)>",
            (guide_text or "")[:guide_limit],
            "</브랜드 지침>",
            "",
            "<원고>",
            manuscript_text(manuscript),
            "</원고>",
        ]
    )


# ----------------------------------------------------------------- 실행·해석
def run_codex(
    prompt: str,
    exe: str = "",
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
) -> tuple[str, str]:
    """codex 를 **읽기 전용**으로 한 번 부른다. `(출력, 오류설명)`."""
    exe = exe or find_codex()
    if not exe:
        return "", "codex 실행 파일을 찾지 못했습니다"
    cmd = [
        exe,
        "exec",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "--color",
        "never",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-",
    ]
    try:
        done = subprocess.run(  # noqa: S603 - 고정된 명령이다
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired:
        return "", f"{timeout}초 안에 답이 오지 않았습니다"
    except Exception as exc:  # 실행 자체가 안 될 때
        return "", f"codex 실행 실패: {exc}"
    out = done.stdout or ""
    if not out.strip():
        return "", (done.stderr or "").strip()[:300] or "빈 응답"
    return out, ""


def parse_result(output: str, items: list[str]) -> dict:
    """codex 출력에서 `{번호: {pass, where, why}}` 를 꺼내 점수를 **우리가** 계산한다."""
    start, end = output.find("{"), output.rfind("}")
    if start < 0 or end <= start:
        return {"checked": False, "error": "JSON을 찾지 못했습니다"}
    try:
        data = json.loads(output[start : end + 1])
    except Exception as exc:
        return {"checked": False, "error": f"JSON을 읽지 못했습니다: {exc}"}
    if not isinstance(data, dict):
        return {"checked": False, "error": "JSON 객체가 아닙니다"}
    judged = 0
    notes: list[str] = []
    for i, text in enumerate(items, start=1):
        row = data.get(str(i)) or data.get(i)
        if not isinstance(row, dict):
            continue
        judged += 1
        if row.get("pass") is True:
            continue
        where = str(row.get("where") or "").strip()
        why = str(row.get("why") or "").strip()
        notes.append(f"{i}. {text} → {where + ': ' if where else ''}{why}")
    if not judged:
        return {"checked": False, "error": "판정된 번호가 없습니다"}
    score = int(round(100 * (judged - len(notes)) / judged))
    return {
        "checked": True,
        "error": "",
        "score": score,
        "judged": judged,
        "total": len(items),
        "notes": notes,
    }


def verdict_of(score: int, fails: int, min_score: int) -> str:
    """점수를 한국어 판정으로 (우리 기준이다)."""
    if fails == 0:
        return "통과"
    return "수정 권고" if score >= min_score else "재작성"


def review(
    manuscript: Any,
    rule: Any = None,
    guide_text: str = "",
    cfg: dict | None = None,
    golden: list[str] | None = None,
) -> dict:
    """원고 한 편을 교차 검증한다. 실패해도 예외를 던지지 않는다 (미검증)."""
    from v2r.content import brand_writer as bw

    cfg = cfg or load_config()
    rule = rule or bw.rule_for(bw.brand_of(manuscript), manuscript.manuscript_type)
    if golden is None:
        try:
            golden = bw.load_golden_reply2(rule.brand, rule.manuscript_type)
        except Exception:
            golden = []
    items = checklist(manuscript, rule)
    prompt = build_prompt(manuscript, items, guide_text, golden)
    output, error = run_codex(
        prompt,
        model=cfg.get("model", DEFAULT_MODEL),
        effort=cfg.get("effort", DEFAULT_EFFORT),
        timeout=int(cfg.get("timeout", DEFAULT_TIMEOUT)),
    )
    if error:
        return {
            "gpt_checked": False,
            "gpt_score": None,
            "gpt_verdict": "미검증",
            "gpt_notes": [],
            "gpt_error": error,
            "gpt_items": len(items),
        }
    parsed = parse_result(output, items)
    if not parsed.get("checked"):
        return {
            "gpt_checked": False,
            "gpt_score": None,
            "gpt_verdict": "미검증",
            "gpt_notes": [],
            "gpt_error": parsed.get("error", ""),
            "gpt_items": len(items),
        }
    score = int(parsed["score"])
    notes = parsed["notes"]
    return {
        "gpt_checked": True,
        "gpt_score": score,
        "gpt_verdict": verdict_of(score, len(notes), int(cfg.get("min_score", 60))),
        "gpt_notes": notes,
        "gpt_error": "",
        "gpt_items": len(items),
        "gpt_judged": parsed.get("judged", 0),
    }


# ----------------------------------------------------------------- 부분 재시도
def labels_in_notes(notes: list[str]) -> list[str]:
    """GPT 지적이 가리키는 댓글 라벨 (없으면 빈 목록 = 재시도하지 않는다)."""
    from v2r.content import brand_writer as bw

    found = {m.group() for note in notes for m in _LABEL_RE.finditer(note)}
    return [label for label in bw.COMMENT_LABELS if label in found]


def retry_once(
    llm: Any,
    manuscript: Any,
    rule: Any,
    notes: list[str],
    guide_text: str = "",
    examples: list[str] | None = None,
) -> bool:
    """GPT 지적으로 **딱 한 번** 부분 재시도. 좋아졌을 때만 갈아 끼운다.

    우리 검증기의 필수 실패가 늘면 되돌린다 (GPT 말 듣다가 원고가 나빠지면 안 된다).
    """
    from v2r.content import brand_writer as bw

    labels = labels_in_notes(notes)
    if not labels or llm is None:
        return False
    before = len(bw.violations(bw.validate(manuscript, rule), include_warnings=False))
    problems = [f"GPT 지적 — {n}" for n in notes]
    system, user = bw.build_partial_retry_prompt(
        rule.brand, manuscript.keyword, labels, problems, rule.manuscript_type,
        guide_text, examples,
    )
    keep = list(manuscript.comments)
    try:
        payload = llm.complete_json("brand_comments", system, user, max_tokens=1500)
    except Exception:
        return False
    manuscript.comments = bw.merge_comments(keep, payload)
    after = len(bw.violations(bw.validate(manuscript, rule), include_warnings=False))
    if after > before:
        manuscript.comments = keep
        return False
    return True


def crosscheck_manuscript(
    llm: Any,
    manuscript: Any,
    rule: Any = None,
    guide_text: str = "",
    examples: list[str] | None = None,
    stats: dict | None = None,
    cfg: dict | None = None,
) -> dict:
    """생성 경로에서 부르는 진입점 — 검증 → (필요하면) 1회 재시도 → 다시 검증.

    결과를 `stats` 에 `gpt_score` / `gpt_verdict` / `gpt_notes` 로 남긴다.
    켜져 있지 않거나 실패하면 **미검증**으로 두고 생성은 그대로 보낸다.
    """
    from v2r.content import brand_writer as bw

    cfg = cfg or load_config()
    rule = rule or bw.rule_for(bw.brand_of(manuscript), manuscript.manuscript_type)
    if not cfg.get("enabled"):
        result = {
            "gpt_checked": False,
            "gpt_score": None,
            "gpt_verdict": "미검증",
            "gpt_notes": [],
            "gpt_error": "crosscheck.enabled=false",
        }
        if stats is not None:
            stats.update(result)
        return result
    first = review(manuscript, rule, guide_text, cfg)
    result = dict(first)
    min_score = int(cfg.get("min_score", DEFAULT_MIN_SCORE))
    score = first.get("gpt_score")
    if (
        first.get("gpt_checked")
        and score is not None
        and score < min_score
        and first.get("gpt_notes")
    ):
        if retry_once(llm, manuscript, rule, first["gpt_notes"], guide_text, examples):
            second = review(manuscript, rule, guide_text, cfg)
            result = dict(second)
            result["gpt_retried"] = True
            result["gpt_score_before"] = score
            result["gpt_notes_before"] = first["gpt_notes"]
            if not second.get("gpt_checked"):
                # 두 번째 호출이 실패하면 첫 판정을 그대로 남긴다
                result.update(
                    {
                        k: first[k]
                        for k in ("gpt_checked", "gpt_score", "gpt_verdict", "gpt_notes")
                    }
                )
    if stats is not None:
        stats.update(result)
    return result
