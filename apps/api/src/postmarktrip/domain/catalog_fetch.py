"""官方发行页抓取（M3C-3）。

安全边界：
* 只允许 http/https；
* 解析域名后拒绝 loopback/私网/链路本地/保留地址（防 SSRF）；
* 手动处理 3xx，每一跳都重新校验；
* 超时 15s、最多 2MB；
* 原样保存 HTML 快照，解析失败也不丢证据。
"""

from __future__ import annotations

import gzip
import hashlib
import ipaddress
import re
import socket
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 15
REDIRECT_CODES = {301, 302, 303, 307, 308}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "postmarkTrip-catalog/0.3"
)
_CHARSET_RE = re.compile(r"charset=([\w-]+)", re.I)


class FetchError(RuntimeError):
    """抓取失败（URL 不安全、网络、超时、页面过大、非 HTML）。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


@dataclass
class FetchedPage:
    html: str
    body: bytes
    final_url: str
    content_type: str
    sha256: str
    byte_size: int
    fetched_at: datetime


class _NoRedirect(HTTPRedirectHandler):
    """不让 urllib 自动跟随：每一跳都要重新做 SSRF 校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_safe_public_url(url: str) -> bool:
    """只允许公网 http/https；测试和错误提示都复用这个判断。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.username or parts.password:
        return False

    host = parts.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(
        (".local", ".internal", ".localhost")
    ):
        return False

    # IP 字面量不需要 DNS；也避免被测试/恶意 DNS 改写成公网地址。
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return _is_public_ip(host)

    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError:
        return False
    if not infos:
        return False
    return all(_is_public_ip(str(info[4][0])) for info in infos)


def _decode(body: bytes, content_type: str) -> str:
    match = _CHARSET_RE.search(content_type or "")
    candidates = [match.group(1)] if match else []
    candidates.extend(["utf-8", "gb18030"])
    for charset in candidates:
        try:
            return body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _decompress(body: bytes, content_encoding: str) -> bytes:
    """urllib 不会自动解 Content-Encoding；官方页常回 gzip。"""
    encoding = (content_encoding or "").lower().strip()
    if encoding in ("", "identity"):
        return body
    if encoding == "gzip":
        try:
            return gzip.decompress(body)
        except OSError as exc:
            raise FetchError("BAD_ENCODING", "gzip 解压失败") from exc
    if encoding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error as exc:
            raise FetchError("BAD_ENCODING", "deflate 解压失败") from exc
    raise FetchError("UNSUPPORTED_ENCODING", f"不支持的压缩编码：{encoding}")


def fetch_page(url: str) -> FetchedPage:
    current = url.strip()
    if not is_safe_public_url(current):
        raise FetchError("UNSAFE_URL", "只允许公网 http/https 地址")

    opener = build_opener(_NoRedirect)
    for _ in range(6):
        request = Request(
            current,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            },
        )
        try:
            response = opener.open(request, timeout=TIMEOUT_SECONDS)
            final_url = response.geturl()
            if not is_safe_public_url(final_url):
                response.close()
                raise FetchError("UNSAFE_REDIRECT", "重定向到了非公网地址")
            content_type = response.headers.get("Content-Type", "")
            content_encoding = response.headers.get("Content-Encoding", "")
            body = response.read(MAX_BYTES + 1)
            response.close()
            body = _decompress(body, content_encoding)
            break
        except HTTPError as exc:
            if exc.code in REDIRECT_CODES:
                location = exc.headers.get("Location")
                exc.close()
                if not location:
                    raise FetchError("BAD_REDIRECT", "重定向缺少 Location") from exc
                current = urljoin(current, location)
                continue
            raise FetchError("HTTP_ERROR", f"HTTP {exc.code}") from exc
        except URLError as exc:
            raise FetchError("NETWORK", str(exc.reason)) from exc
        except TimeoutError as exc:
            raise FetchError("TIMEOUT", "抓取超时") from exc
    else:
        raise FetchError("TOO_MANY_REDIRECTS", "重定向次数过多")

    if len(body) > MAX_BYTES:
        raise FetchError("TOO_LARGE", f"页面超过 {MAX_BYTES // 1024 // 1024}MB")
    if content_type and "html" not in content_type.lower() and "xml" not in content_type.lower():
        raise FetchError("NOT_HTML", f"不是 HTML：{content_type}")

    return FetchedPage(
        html=_decode(body, content_type),
        body=body,
        final_url=current,
        content_type=content_type,
        sha256=hashlib.sha256(body).hexdigest(),
        byte_size=len(body),
        fetched_at=datetime.now(UTC),
    )


def save_snapshot(data_dir: Path, fetched: FetchedPage) -> str:
    """保存原始 HTML（原始字节）；内容寻址，重复抓取不重复写。返回相对路径。"""
    relative = Path("catalog-snapshots") / f"{fetched.sha256}.html"
    target = Path(data_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(fetched.body)
    return relative.as_posix()
