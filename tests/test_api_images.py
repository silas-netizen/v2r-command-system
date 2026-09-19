"""이미지 업로드 API 테스트 (네트워크 없이 대역)."""

from pathlib import Path

from PIL import Image

from v2r.api import images


class _Client:
    def __init__(self):
        self.calls = []

    def post(self, path, json=None, **k):
        self.calls.append((path, json))
        return {"result": {"url": "https://bucket.s3.amazonaws.com/", "fields": {"key": "123_a.jpg", "policy": "p"}}}


class _Http:
    def __init__(self):
        self.sent = None

    def post(self, url, data=None, files=None, timeout=0):
        self.sent = (url, data, files)

        class R:
            status_code = 204

        return R()


def test_upload_image_builds_component(tmp_path: Path):
    p = tmp_path / "a.jpg"
    Image.new("RGB", (400, 300), (10, 20, 30)).save(p, "JPEG")
    c, h = _Client(), _Http()
    comp = images.upload_image(c, p, http=h)
    assert c.calls[0][0] == images.PATH_UPLOAD and c.calls[0][1]["file_extension"] == "jpg"
    assert h.sent[0] == "https://bucket.s3.amazonaws.com/" and "file" in h.sent[2]
    assert comp["@ctype"] == "image"
    assert comp["src"] == "https://bucket.s3.amazonaws.com/123_a.jpg"
    assert comp["path"] == "/123_a.jpg" and comp["domain"] == "https://bucket.s3.amazonaws.com"
    assert comp["width"] == 400 and comp["height"] == 300 and comp["fileSize"] > 0
    assert comp["fileName"].endswith(".jpg")
