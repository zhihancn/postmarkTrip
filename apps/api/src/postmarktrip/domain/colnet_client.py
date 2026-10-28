"""colnetApi 的最小客户端（M3C-5）。

只做一件事：把票图 multipart POST 到 `colnetApi /search`，返回候选 JSON。
不做自动选 top-1、不写数据库、不重试额度。
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from postmarktrip.config import get_settings


class ColnetError(RuntimeError):
    """带可判别 code 的失败，供 API/UI 显示。"""

    def __init__(self, code: str, message: str = "", status: int | None = None) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}" if message else code)


def _build_multipart(
    image_bytes: bytes, filename: str, content_type: str
) -> tuple[bytes, str]:
    boundary = "----postmarkTrip" + uuid.uuid4().hex
    chunks: list[bytes] = [
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode(),
        image_bytes,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def search_image(
    image_bytes: bytes,
    *,
    filename: str = "stamp.png",
    content_type: str = "image/png",
    force: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    base = settings.colnet_api_url.rstrip("/")
    body, content_type_header = _build_multipart(image_bytes, filename, content_type)
    url = f"{base}/search"
    if force:
        url += "?force=true"

    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type_header, "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=settings.colnet_api_timeout_s) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        code = "HTTP_ERROR"
        message = raw[:300]
        try:
            detail = json.loads(raw).get("detail") or {}
            if isinstance(detail, dict):
                code = str(detail.get("code") or code)
                message = str(detail.get("message") or message)
        except (ValueError, AttributeError):
            pass
        if exc.code == 429:
            code = "RATE_LIMIT"
        elif exc.code == 503:
            code = "SESSION"
        elif exc.code == 404:
            code = "IMAGE_NOT_FOUND"
        raise ColnetError(code, message, exc.code) from exc
    except URLError as exc:
        raise ColnetError("NETWORK", str(exc.reason)) from exc
    except TimeoutError as exc:
        raise ColnetError("TIMEOUT", "colnetApi 超时") from exc

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ColnetError("BAD_RESPONSE", "colnetApi 返回的不是 JSON") from exc
    if not isinstance(payload, dict):
        raise ColnetError("BAD_RESPONSE", "colnetApi 返回结构异常")
    if payload.get("success") is False:
        raise ColnetError(
            str(payload.get("code") or "SEARCH_FAILED"),
            str(payload.get("message") or "搜索失败"),
        )
    return payload
