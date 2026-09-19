"""SE-ONE 편집기에 이미지를 붙여넣기(Ctrl+V)로 첨부하고 컴포넌트를 뽑아온다.

흐름: `/nc/seone` 열기 → 카페 → 계정 → 게시판 순서로 선택(계정 전에는 게시판 비활성)
→ 본문 클릭 → 이미지마다 paste 이벤트 → 문서에서 새 이미지 컴포넌트 확인.
사진이 하나라도 붙지 않으면 `PasteError`를 올린다(사진 없이 진행 금지).
"""

from __future__ import annotations

import base64
import mimetypes
import subprocess
import time
from pathlib import Path

IMAGE_CTYPES = {"image", "imageGroup", "imageStrip"}
EDITOR_BODY_SELECTORS = (
    ".se-content [contenteditable=true]",
    ".se-viewer [contenteditable=true]",
    "div.se-text-paragraph",
    "[contenteditable=true]",
    "iframe#se2_iframe",
)

# 문서 읽기: 표준 SmartEditor API → Vue 루트의 seoneGetDocument 순으로 시도.
_GET_DOCUMENT_JS = """() => {
  try {
    if (window.SmartEditor && window.SmartEditor.getEditor) {
      const ed = window.SmartEditor.getEditor('cafepc001');
      if (ed && ed.getDocumentData) return ed.getDocumentData();
    }
  } catch (e) {}
  try {
    const roots = [window.__vue_app__, document.querySelector('#app'), document.body];
    for (const r of roots) {
      if (!r) continue;
      const inst = r.__vue_app__ || r.__vue__ || (r._vnode && r._vnode.component);
      const ctx = inst && (inst.proxy || inst.ctx || inst);
      if (ctx && typeof ctx.seoneGetDocument === 'function') return ctx.seoneGetDocument();
    }
    if (typeof window.seoneGetDocument === 'function') return window.seoneGetDocument();
  } catch (e) {}
  return null;
}"""

# 합성 paste 이벤트: DataTransfer에 File을 실어 편집기에 dispatch.
_PASTE_JS = """(payload) => {
  const { b64, mime, name, selector } = payload;
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const file = new File([bytes], name, { type: mime });
  const dt = new DataTransfer();
  dt.items.add(file);
  let target = selector ? document.querySelector(selector) : null;
  if (!target) target = document.activeElement || document.body;
  const evt = new ClipboardEvent('paste', {
    bubbles: true, cancelable: true, clipboardData: dt,
  });
  try { Object.defineProperty(evt, 'clipboardData', { value: dt }); } catch (e) {}
  target.dispatchEvent(evt);
  return true;
}"""


class PasteError(RuntimeError):
    """이미지 붙여넣기 실패."""


# ----------------------------------------------------------------------
# 순수 함수 (테스트 대상)
# ----------------------------------------------------------------------
def extract_image_components(doc) -> list[dict]:
    """SE-ONE 문서에서 실제 파일이 붙은 이미지 컴포넌트만 순서대로 뽑는다."""
    found: list[dict] = []
    seen: set[int] = set()
    ordered: list[dict] = []

    # 문서 순서를 지키기 위해 너비 우선이 아닌 깊이 우선 선순회를 쓴다.
    def walk(node) -> None:
        if isinstance(node, dict):
            if id(node) in seen:
                return
            seen.add(id(node))
            ordered.append(node)
            for key in ("document", "components", "comps", "body", "value"):
                if key in node:
                    walk(node[key])
            for key, value in node.items():
                if key in ("document", "components", "comps", "body", "value"):
                    continue
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)

    for node in ordered:
        if str(node.get("@ctype", "")) not in IMAGE_CTYPES:
            continue
        if _has_image_payload(node):
            found.append(node)
    return found


def _has_image_payload(component: dict) -> bool:
    """`src`/`path`/`fileName`이 비어있지 않고 `fileSize > 0`인지."""
    has_ref = any(
        str(component.get(key) or "").strip() for key in ("src", "path", "fileName")
    )
    try:
        size = int(component.get("fileSize") or 0)
    except (TypeError, ValueError):
        size = 0
    return has_ref and size > 0


def component_key(component: dict) -> str:
    """새 컴포넌트 판별용 키."""
    for key in ("id", "src", "path", "fileName"):
        value = component.get(key)
        if value:
            return f"{key}:{value}"
    return repr(sorted(component.items(), key=lambda kv: kv[0]))


# ----------------------------------------------------------------------
# 브라우저 조작
# ----------------------------------------------------------------------
def _select_by_text(page, label: str, value: str, retries: int = 3) -> None:
    """드롭다운(select) 또는 클릭형 목록에서 보이는 텍스트로 값을 고른다."""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            for handle in page.locator("select").all():
                options = handle.locator("option").all_inner_texts()
                if any(value.strip() == opt.strip() for opt in options):
                    handle.select_option(label=value)
                    page.wait_for_timeout(400)
                    return
                if any(value.strip() in opt for opt in options):
                    match = next(o for o in options if value.strip() in o)
                    handle.select_option(label=match)
                    page.wait_for_timeout(400)
                    return
            # select가 아니면 커스텀 드롭다운: 라벨을 눌러 열고 항목을 클릭
            opener = page.get_by_text(label, exact=False).first
            if opener.count() > 0:
                opener.click(timeout=3000)
                page.wait_for_timeout(300)
            item = page.get_by_text(value, exact=True).first
            if item.count() == 0:
                # 정확 일치가 없으면 부분 일치(이모지·부제가 붙은 이름)로
                item = page.get_by_text(value, exact=False).first
            item.click(timeout=3000)
            page.wait_for_timeout(400)
            return
        except Exception as exc:  # 다음 시도
            last_error = exc
            page.wait_for_timeout(600 * (attempt + 1))
    raise PasteError(f"{label} 선택 실패: {value} ({last_error})")


def _open_editor_page(page, base: str, attempts: int = 3) -> None:
    """`/nc/seone`을 연다. SPA가 즉시 다른 경로로 갈아타면 `net::ERR_ABORTED`가 나는데,
    그때는 실패가 아니라 페이지가 이미 바뀐 것이므로 잠시 기다렸다 주소만 확인한다."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            page.goto(f"{base}/nc/seone", wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            return
        except Exception as exc:
            last = exc
            if "ERR_ABORTED" in str(exc):
                page.wait_for_timeout(1500 * (i + 1))
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                if "/nc/" in (page.url or ""):
                    if "seone" in page.url:
                        return
                    # 로그인/목록으로 튕겼다면 한 번 더 시도
                    continue
            else:
                page.wait_for_timeout(1000 * (i + 1))
    raise PasteError(f"SE-ONE 페이지 열기 실패: {last}")


def _focus_editor(page) -> str | None:
    """편집기 본문을 클릭해 포커스를 준다. 사용한 선택자를 돌려준다."""
    for selector in EDITOR_BODY_SELECTORS:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible(timeout=1500):
                locator.click(timeout=3000)
                return selector
        except Exception:
            continue
    raise PasteError("SE-ONE 편집기 본문을 찾지 못했습니다.")


def _paste_via_clipboard_event(page, image_bytes: bytes, mime: str, name: str, selector: str | None) -> None:
    """합성 paste 이벤트로 편집기에 파일을 전달한다(기본 경로)."""
    payload = {
        "b64": base64.b64encode(image_bytes).decode("ascii"),
        "mime": mime,
        "name": name,
        "selector": selector,
    }
    page.evaluate(_PASTE_JS, payload)


def _paste_via_os_clipboard(page, image_path: Path) -> None:
    """예비 경로: 윈도우 클립보드에 이미지를 올리고 Ctrl+V."""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        f"$img=[System.Drawing.Image]::FromFile('{image_path}'); "
        "[System.Windows.Forms.Clipboard]::SetImage($img); $img.Dispose()"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise PasteError(f"OS 클립보드 복사 실패: {completed.stderr.strip()}")
    page.keyboard.press("Control+V")


def _read_document(page):
    try:
        return page.evaluate(_GET_DOCUMENT_JS)
    except Exception:
        return None


def _wait_for_new_component(page, known: set[str], timeout_s: float) -> dict:
    """새 이미지 컴포넌트가 나타날 때까지 문서를 폴링한다."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        doc = _read_document(page)
        if doc is not None:
            for component in extract_image_components(doc):
                key = component_key(component)
                if key not in known:
                    known.add(key)
                    return component
        page.wait_for_timeout(700)
    return {}


def attach_images_via_paste(
    page,
    site: str,
    cafe_name: str,
    account_id: str,
    board_name: str,
    image_paths: list[Path],
    timeout_s: float = 45,
) -> list[dict]:
    """SE-ONE에 이미지를 붙여넣고 새로 생긴 이미지 컴포넌트 목록을 반환한다."""
    if not image_paths:
        return []

    base = str(site).rstrip("/")
    _open_editor_page(page, base)

    # 순서 고정: 카페 → 계정 → 게시판 (계정 선택 전 게시판 비활성)
    _select_by_text(page, "카페", cafe_name)
    _select_by_text(page, "계정", account_id)
    _select_by_text(page, "게시판", board_name)

    selector = _focus_editor(page)

    known = {component_key(c) for c in extract_image_components(_read_document(page) or {})}
    attached: list[dict] = []

    for path in image_paths:
        path = Path(path)
        if not path.is_file():
            raise PasteError(f"이미지 파일이 없습니다: {path}")
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        data = path.read_bytes()

        component: dict = {}
        try:
            _paste_via_clipboard_event(page, data, mime, path.name, selector)
            component = _wait_for_new_component(page, known, timeout_s)
        except PasteError:
            raise
        except Exception:
            component = {}

        if not component:
            # 예비 경로: OS 클립보드 + Ctrl+V
            try:
                page.locator(selector).first.click(timeout=3000)
            except Exception:
                pass
            _paste_via_os_clipboard(page, path.resolve())
            component = _wait_for_new_component(page, known, timeout_s)

        if not component:
            raise PasteError(f"이미지 첨부 확인 실패: {path.name}")
        attached.append(component)

    if len(attached) != len(image_paths):
        raise PasteError(
            f"이미지 {len(image_paths)}장 중 {len(attached)}장만 첨부되었습니다."
        )
    return attached


__all__ = [
    "IMAGE_CTYPES",
    "PasteError",
    "attach_images_via_paste",
    "component_key",
    "extract_image_components",
]
