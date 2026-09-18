"""G1: Make 시나리오의 Claude 모듈 지침을 읽어 창고에 정리한다.

사용자가 브라우저에서 직접 로그인한다. 비밀번호·쿠키는 영구 프로필 폴더 밖에 저장하지 않는다.
Make 화면 구조는 바뀔 수 있으므로 선택자는 작은 함수로 나눠 두었다.
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAKE_URL = "https://us2.make.com/"
PROFILE_DIRNAME = "browser-profile-make"
TARGET_FOLDERS = ("★NEW 카페 바이럴★", "☆일상 글 작성 모음☆")
LOGIN_WAIT_SEC = 15 * 60
POLL_SEC = 2.0
CLAUDE_WORDS = ("claude", "anthropic")

# 지침 필드 분류용 라벨 힌트
SYSTEM_HINTS = ("system", "시스템")
USER_HINTS = ("user", "prompt", "message", "사용자", "프롬프트")


def _safe_name(name: str) -> str:
    """파일 이름으로 쓸 수 있게 다듬는다."""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip())
    cleaned = cleaned.strip(". ")
    return (cleaned or "무제")[:120]


# --- 브라우저 단계 -------------------------------------------------
def wait_for_login(page: Any, timeout_sec: float = LOGIN_WAIT_SEC) -> bool:
    """사용자가 직접 로그인할 때까지 기다린다."""
    print("Make에 직접 로그인해 주세요", flush=True)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            url = page.url or ""
            if "/scenarios" in url:
                return True
            nav = page.query_selector("text=Scenarios")
            if nav is not None:
                return True
        except Exception as exc:  # 페이지 전환 중 예외는 무시하고 계속
            log.debug("로그인 대기 중 예외: %s", exc)
        time.sleep(POLL_SEC)
    log.warning("로그인 대기 시간이 초과되었습니다")
    return False


def open_folder(page: Any, folder_name: str) -> bool:
    """시나리오 목록에서 폴더를 연다."""
    try:
        if "/scenarios" not in (page.url or ""):
            page.goto(MAKE_URL + "scenarios", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        target = page.query_selector(f"text={folder_name}")
        if target is None:
            log.warning("폴더를 찾지 못했습니다: %s", folder_name)
            return False
        target.click()
        page.wait_for_timeout(2500)
        return True
    except Exception as exc:
        log.warning("폴더 열기 실패(%s): %s", folder_name, exc)
        return False


def list_scenarios(page: Any) -> list[dict[str, str]]:
    """현재 폴더의 시나리오 이름과 링크를 모은다."""
    try:
        items = page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll('a[href*="/scenario"]').forEach((a) => {
                    const name = (a.innerText || a.textContent || '').trim();
                    const href = a.href || '';
                    if (!name || !href || seen.has(href)) return;
                    seen.add(href);
                    out.push({ name, href });
                });
                return out;
            }"""
        )
    except Exception as exc:
        log.warning("시나리오 목록 수집 실패: %s", exc)
        return []
    return [i for i in items if isinstance(i, dict) and i.get("name")]


def extract_claude_modules(page: Any) -> list[dict[str, str]]:
    """열려 있는 시나리오에서 Claude/Anthropic 모듈의 지침 텍스트를 뽑는다."""
    collected: list[dict[str, str]] = []
    try:
        handles = page.query_selector_all(
            "[class*='module'], [data-module-id], [role='button']"
        )
    except Exception as exc:
        log.warning("모듈 탐색 실패: %s", exc)
        return collected

    for handle in handles:
        try:
            text = (handle.inner_text() or "").lower()
        except Exception:
            continue
        if not any(word in text for word in CLAUDE_WORDS):
            continue
        try:
            handle.click()
            page.wait_for_timeout(1500)
            fields = _read_visible_fields(page)
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception as exc:
            log.debug("모듈 설정 열기 실패: %s", exc)
            continue
        if fields:
            collected.extend(fields)
    return collected


def _read_visible_fields(page: Any) -> list[dict[str, str]]:
    """설정 패널에서 보이는 입력·텍스트영역의 라벨과 값을 읽는다."""
    try:
        return page.evaluate(
            """() => {
                const out = [];
                const nodes = document.querySelectorAll(
                    'textarea, [contenteditable], input[type=text]'
                );
                nodes.forEach((el) => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 && rect.height === 0) return;
                    const value = (el.value !== undefined && el.value !== null && el.value !== '')
                        ? el.value
                        : (el.innerText || el.textContent || '');
                    if (!value || !value.trim()) return;
                    let label = el.getAttribute('aria-label')
                        || el.getAttribute('placeholder')
                        || el.getAttribute('name') || '';
                    if (!label) {
                        const group = el.closest('label, .form-group, [class*=field], [class*=panel]');
                        if (group) label = (group.innerText || '').split('\\n')[0].trim();
                    }
                    out.push({ label: (label || '').trim(), value: value.trim() });
                });
                return out;
            }"""
        ) or []
    except Exception as exc:
        log.debug("필드 읽기 실패: %s", exc)
        return []


# --- 저장 ---------------------------------------------------------
def _classify(fields: list[dict[str, str]]) -> dict[str, list[str]]:
    """라벨 힌트로 시스템/사용자/변수로 나눈다."""
    buckets: dict[str, list[str]] = {"시스템 프롬프트": [], "사용자 프롬프트": [], "변수": []}
    seen: set[str] = set()
    for field in fields:
        value = (field.get("value") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        label = (field.get("label") or "").lower()
        if any(h in label for h in SYSTEM_HINTS):
            key = "시스템 프롬프트"
        elif any(h in label for h in USER_HINTS):
            key = "사용자 프롬프트"
        else:
            key = "변수"
        prefix = f"[{field.get('label') or '이름 없음'}]\n" if key == "변수" else ""
        buckets[key].append(prefix + value)
    return buckets


def render_guide_md(scenario: str, folder: str, fields: list[dict[str, str]]) -> str:
    """지침 마크다운 본문을 만든다."""
    buckets = _classify(fields)
    lines = [f"# {scenario}", "", f"- 폴더: {folder}", ""]
    for section in ("시스템 프롬프트", "사용자 프롬프트", "변수"):
        lines.append(f"## {section}")
        body = buckets[section]
        lines.append("\n\n".join(body) if body else "(없음)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_guide(
    guides_dir: Path, folder: str, scenario: str, fields: list[dict[str, str]]
) -> Path:
    """`guides/<폴더>/<시나리오>.md`로 저장한다."""
    target_dir = Path(guides_dir) / _safe_name(folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{_safe_name(scenario)}.md"
    path.write_text(render_guide_md(scenario, folder, fields), encoding="utf-8")
    return path


def write_index(guides_dir: Path, rows: list[tuple[str, str, int]]) -> Path:
    """수집 결과 목차를 저장한다."""
    guides_dir = Path(guides_dir)
    guides_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# Make 지침 목차", ""]
    for folder, scenario, count in rows:
        lines.append(f"- {folder} / {scenario} — 항목 {count}개")
    if not rows:
        lines.append("- (수집된 지침 없음)")
    path = guides_dir / "INDEX.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _dump_html(page: Any, data_dir: Path, name: str) -> None:
    """디버그용 페이지 HTML 저장."""
    try:
        out_dir = Path(data_dir) / "make-debug"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{_safe_name(name)}.html").write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        log.debug("HTML 덤프 실패: %s", exc)


# --- 진입점 -------------------------------------------------------
def learn_make_guides(
    warehouse_guides_dir: str | Path,
    headless: bool = False,
    data_dir: str | Path | None = None,
    folders: tuple[str, ...] = TARGET_FOLDERS,
    dump_html: bool = False,
) -> dict[str, Any]:
    """브라우저를 열어 사용자 로그인을 기다린 뒤 Claude 모듈 지침을 창고에 저장한다."""
    from playwright.sync_api import sync_playwright

    guides_dir = Path(warehouse_guides_dir)
    if data_dir is None:
        try:
            from ..config import get_settings

            data_dir = get_settings().data_dir
        except Exception:
            data_dir = Path(__file__).resolve().parent.parent.parent / "data"
    data_dir = Path(data_dir)
    profile_dir = data_dir / PROFILE_DIRNAME
    profile_dir.mkdir(parents=True, exist_ok=True)

    rows: list[tuple[str, str, int]] = []
    saved: list[str] = []

    with sync_playwright() as pw:
        # 비밀번호·쿠키는 이 영구 프로필 폴더 안에만 남는다
        context = pw.chromium.launch_persistent_context(
            str(profile_dir), headless=headless
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(MAKE_URL, wait_until="domcontentloaded")
            if not wait_for_login(page):
                return {"ok": False, "error": "로그인 확인 실패", "saved": []}
            if dump_html:
                _dump_html(page, data_dir, "scenarios")

            for folder in folders:
                if not open_folder(page, folder):
                    continue
                scenarios = list_scenarios(page)
                log.info("폴더 %s 시나리오 %d개", folder, len(scenarios))
                for item in scenarios:
                    name = item.get("name", "")
                    href = item.get("href", "")
                    try:
                        page.goto(href, wait_until="domcontentloaded")
                        page.wait_for_timeout(3000)
                    except Exception as exc:
                        log.warning("시나리오 열기 실패(%s): %s", name, exc)
                        continue
                    if dump_html:
                        _dump_html(page, data_dir, f"{folder}-{name}")
                    fields = extract_claude_modules(page)
                    path = save_guide(guides_dir, folder, name, fields)
                    saved.append(str(path))
                    rows.append((folder, name, len(fields)))
                    try:
                        page.go_back(wait_until="domcontentloaded")
                        page.wait_for_timeout(2000)
                    except Exception:
                        open_folder(page, folder)
        finally:
            context.close()

    index_path = write_index(guides_dir, rows)
    return {"ok": True, "saved": saved, "index": str(index_path), "count": len(saved)}


def main(argv: list[str] | None = None) -> int:
    """`python -m v2r.knowledge.make_import` 실행용."""
    parser = argparse.ArgumentParser(description="Make 시나리오 지침 학습")
    parser.add_argument("--guides-dir", default="")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--dump-html", action="store_true", help="디버그용 HTML 저장")
    args = parser.parse_args(argv)

    guides_dir = args.guides_dir
    if not guides_dir:
        from ..config import get_settings

        guides_dir = get_settings().warehouse_dir / "guides"
    result = learn_make_guides(
        guides_dir, headless=args.headless, dump_html=args.dump_html
    )
    print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
