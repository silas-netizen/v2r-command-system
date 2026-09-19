"""이미지 업로드 API — 브라우저(SE-ONE 붙여넣기) 없이 사진을 첨부한다.

실측(2026-09-19):
1. `POST /naver_cafe_articles/upload_image` `{file_extension, file_name}` → `{result: {url, fields}}`
   (S3 presigned POST).
2. `fields` + `file`을 그 `url`에 multipart POST → 204. 최종 주소 = `url + fields.key`.
3. SE-ONE 이미지 컴포넌트는 실물 글(네이버 동기화본)의 모양을 따르되 `src/domain/path`만 S3 주소로 채운다.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from v2r.api.client import V2RClient
from v2r.api.errors import V2RApiError

PATH_UPLOAD = "/naver_cafe_articles/upload_image"


def _image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return 0, 0


def _se_id() -> str:
    return f"SE-{uuid.uuid4()}"


def build_image_component(url: str, file_name: str, file_size: int, width: int, height: int) -> dict:
    """업로드된 S3 주소로 SE-ONE `image` 컴포넌트를 만든다."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    domain = f"{parts.scheme}://{parts.netloc}"
    return {
        "id": _se_id(),
        "layout": "default",
        "@ctype": "image",
        "src": url,
        "internalResource": True,
        "represent": False,
        "path": parts.path,
        "domain": domain,
        "fileSize": int(file_size),
        "width": int(width),
        "widthPercentage": 0,
        "height": int(height),
        "originalWidth": int(width),
        "originalHeight": int(height),
        "fileName": file_name,
        "caption": None,
        "format": "normal",
        "displayFormat": "normal",
        "imageLoaded": True,
        "contentMode": "normal",
        "origin": {"srcFrom": "local", "@ctype": "imageOrigin"},
        "ai": False,
    }


def upload_image(client: V2RClient, path: str | Path, *, http: Any = None) -> dict:
    """사진 1장을 업로드하고 SE-ONE 이미지 컴포넌트를 돌려준다."""
    p = Path(path)
    if not p.is_file():
        raise V2RApiError(f"이미지 파일이 없습니다: {p}")
    ext = (p.suffix.lstrip(".") or "jpg").lower()
    if ext == "jpeg":
        ext = "jpg"
    stem = f"v2r_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    res = client.post(PATH_UPLOAD, json={"file_extension": ext, "file_name": stem})
    res = res.get("result", res) if isinstance(res, dict) else {}
    url, fields = res.get("url"), res.get("fields") or {}
    if not url or not fields.get("key"):
        raise V2RApiError("upload_image 응답에 url/fields.key가 없습니다")
    data = p.read_bytes()
    poster = http or httpx
    resp = poster.post(
        url,
        data=dict(fields),
        files={"file": (f"{stem}.{ext}", data, "image/jpeg" if ext == "jpg" else f"image/{ext}")},
        timeout=60,
    )
    if int(getattr(resp, "status_code", 0)) >= 300:
        raise V2RApiError(f"S3 업로드 실패: HTTP {resp.status_code}")
    final = f"{url.rstrip('/')}/{str(fields['key']).lstrip('/')}"
    w, h = _image_size(p)
    return build_image_component(final, f"{stem}.{ext}", len(data), w, h)


def upload_images(client: V2RClient, paths: list[str | Path], *, http: Any = None) -> list[dict]:
    return [upload_image(client, p, http=http) for p in paths]


__all__ = ["build_image_component", "upload_image", "upload_images", "PATH_UPLOAD"]
