"""内容寻址的资产存储。

**这一层是 L0（原始证据）的守门人。** 它刻意不提供"覆盖"或"删除"原始件的方法 ——
把 docs/ok/01 §2 的"原始件不可变"从纪律变成**编译期约束**：
想改原始件，你得绕开整个模块去直接操作文件系统。

布局（两级散列目录，避免单目录堆积几十万文件）::

    <assets_dir>/<sha[0:2]>/<sha[2:4]>/<sha256>.<ext>

同一个文件重复上传不会占额外空间，且天然去重。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

_CHUNK: Final = 1024 * 1024  # 1 MiB

#: 这些扩展名的"原始件"标记为不可变。它们是从设备直接来的证据。
_IMMUTABLE_EXTS: Final = frozenset(
    {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf", ".bmp", ".webp"}
)


class StorageError(RuntimeError):
    pass


class UnsupportedFileType(StorageError):
    """文件类型在允许列表之外。

    名字不带 ``Error`` 后缀，与 ruff 的 N818 建议相左 —— **这是有意保留的**：
    它从第一版起就是公开 API 名称（``storage/__init__.py`` 导出、``main.py``
    注册成全局异常处理器、路由与测试都按这个名字引用）。为一个 lint 建议
    改动公开名字，换来的收益只是少一行配置，而代价是一次横跨多处的重命名。
    """


@dataclass(frozen=True, slots=True)
class StoredAsset:
    """一次写入的结果。不可变 —— 资产元数据一旦落定就不再变。"""

    sha256: str
    storage_path: str  # 相对 assets_dir
    bytes: int
    mime: str
    width: int | None
    height: int | None
    dpi: int | None
    is_immutable: bool


def _ext_for(filename: str | None, mime: str) -> str:
    if filename:
        ext = Path(filename).suffix.lower()
        if ext:
            return ext
    guessed = mimetypes.guess_extension(mime) or ""
    return guessed.lower()


def _probe_image(path: Path) -> tuple[int | None, int | None, int | None]:
    """读图像尺寸与 DPI。

    DPI 很重要：管线要按 DPI 归一化所有"毫米/像素"阈值（N7 要求兼容 300/600/1200），
    所以这里能读到就一定要存下来。
    """
    try:
        with Image.open(path) as im:
            width, height = im.size
            dpi: int | None = None
            raw_dpi = im.info.get("dpi")
            if isinstance(raw_dpi, tuple) and raw_dpi:
                try:
                    # Pillow 返回 (x, y)，可能是 float 或 IFraction。
                    # 用 round 而不是 int()：int() 是**截断**，对 299.97 会给出
                    # 299 这种"看起来像真的、但差一"的 DPI，而管线里所有
                    # 毫米/像素阈值都按 DPI 折算（N7），差一会被放大到判据上。
                    dpi = round(float(raw_dpi[0]))
                except (TypeError, ValueError):
                    dpi = None
            if dpi is not None and not (30 <= dpi <= 4800):
                # 明显不合理的值（有些扫描仪写入的是每米分辨率）→ 丢弃
                dpi = None
            return width, height, dpi
    except (UnidentifiedImageError, OSError, ValueError):
        return None, None, None


class AssetStore:
    """内容寻址存储。

    **不提供的方法**（有意为之）：``update`` / ``overwrite`` / ``delete_raw``。
    原始件只进不出。要"修改"，只能派生一个新资产。
    """

    def __init__(self, root: Path, *, allowed_exts: tuple[str, ...] | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.allowed_exts = allowed_exts

    # ------------------------------------------------------------------ 写
    def put_stream(
        self,
        stream: BinaryIO,
        *,
        original_filename: str | None = None,
        mime: str | None = None,
        kind: str = "derived",
        max_bytes: int | None = None,
    ) -> StoredAsset:
        """流式写入，全程不把整个文件读进内存。

        先落到临时文件（同时增量算 sha256），再原子移动到内容寻址路径。
        这样即使上传中断也不会留下半个"已存在"的资产。
        """
        ext = _ext_for(original_filename, mime or "")
        if self.allowed_exts is not None and ext and ext not in self.allowed_exts:
            raise UnsupportedFileType(
                f"不支持的扩展名 {ext!r}；允许：{', '.join(self.allowed_exts)}"
            )

        digest = hashlib.sha256()
        total = 0
        tmp_fd, tmp_name = tempfile.mkstemp(dir=self.root, prefix=".upload-", suffix=".part")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "wb") as out:
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise StorageError(f"文件超过上限（{total} > {max_bytes} 字节）")
                    digest.update(chunk)
                    out.write(chunk)

            sha = digest.hexdigest()
            final_ext = ext or ".bin"
            rel = Path(sha[:2]) / sha[2:4] / f"{sha}{final_ext}"
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)

            if dest.exists():
                # 内容寻址的天然去重：同内容同路径。校验大小后丢弃临时文件。
                tmp_path.unlink(missing_ok=True)
            else:
                shutil.move(str(tmp_path), str(dest))
                # 尽量收紧权限（Windows 上无意义，POSIX 上防止误改）。
                # 用 contextlib.suppress 而不是 try/except/pass：这里的意图是
                # "失败无所谓"，而 except/pass 读起来更像"吞掉了一个错误"。
                with contextlib.suppress(OSError):
                    dest.chmod(0o444)

            width, height, dpi = _probe_image(dest)
            is_immutable = final_ext in _IMMUTABLE_EXTS
            return StoredAsset(
                sha256=sha,
                storage_path=rel.as_posix(),
                bytes=total,
                mime=mime or mimetypes.guess_type(f"x{final_ext}")[0] or "application/octet-stream",
                width=width,
                height=height,
                dpi=dpi,
                is_immutable=is_immutable,
            )
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def put_bytes(self, data: bytes, **kwargs: object) -> StoredAsset:
        return self.put_stream(io.BytesIO(data), **kwargs)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ 读
    def abs_path(self, storage_path: str) -> Path:
        """解析为绝对路径，并**拒绝越界**（防御目录穿越）。

        这里做两道检查，缺一不可：

        1. **显式拒绝反斜杠与 ``..`` 组件** —— 这一道是平台无关的。
           只靠下面的 ``is_relative_to`` 是不够的：在 POSIX 上 ``"..\\..\\x"``
           里的反斜杠**不是路径分隔符**，于是整串被当作一个普通文件名，
           解析结果仍在 root 内、不报错。而同一个字符串在 Windows 上
           会被当成真正的两级上跳。

           也就是说，同一个值在不同平台上语义不同：**POSIX 上放行、
           Windows 上穿越**。这种"只在某个平台上成立"的防护是最坏的一种 ——
           开发（Linux 容器）时看不见，出事在别处。
           ``storage_path`` 永远由本模块以 POSIX 形式生成
           （``ab/cd/<sha>.jpg``），所以反斜杠或 ``..`` 必然是外部注入的。
        2. ``is_relative_to`` 兜底 —— 防住 ``POSIX 上真实存在``的路径穿越，
           并覆盖任何我们没想到的形式。
        """
        raw = str(storage_path)
        if "\\" in raw:
            raise StorageError(f"非法资产路径（含反斜杠）：{storage_path!r}")
        if any(part == ".." for part in PurePosixPath(raw).parts):
            raise StorageError(f"非法资产路径（含上跳）：{storage_path!r}")

        p = (self.root / raw).resolve()
        root = self.root.resolve()
        if not p.is_relative_to(root):
            raise StorageError(f"非法资产路径：{storage_path!r}")
        return p

    def exists(self, storage_path: str) -> bool:
        try:
            return self.abs_path(storage_path).is_file()
        except StorageError:
            return False

    def open(self, storage_path: str) -> BinaryIO:
        """只读打开。返回的流不可写 —— 这是 L0 的边界。"""
        return self.abs_path(storage_path).open("rb")

    # -------------------------------------------------------------- 派生
    def render_thumbnail(
        self, source_path: str, *, max_px: int = 480, keep_alpha: bool = False
    ) -> bytes:
        """**生成**缩略图的字节 —— 不落库、不写内容寻址存储。

        缩略图是可随时重建的派生物，它的归宿是调用方自己的缓存目录。

        这个方法以前叫 ``put_thumbnail``，而且内部走的是 ``self.put_bytes(...)``，
        于是派生物被写进了那个被文档描述为"L0 原始证据、只进不出"的目录：
        **每个 max_px 都会永久占一份，而且没有任何办法清理**（存储层刻意不提供
        删除与覆盖）。命名从 ``put_`` 改成 ``render_``，就是让"它不持久化"
        这件事写在名字上 —— 存储层的 ``put_*`` 意味着"写一次、然后永久存在"。

        ``keep_alpha=True`` 走 PNG：**戳贴片必须保留圆外透明**（M3b 戳墙）。
        默认的 JPEG 会把 alpha 拍平成黑/白底，墙上就看不出"圆外透明"了。
        """
        src = self.abs_path(source_path)
        try:
            with Image.open(src) as im:
                im = im.convert("RGBA" if keep_alpha else "RGB")
                im.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                if keep_alpha:
                    im.save(buf, format="PNG", optimize=True)
                else:
                    im.save(buf, format="JPEG", quality=85, optimize=True)
        except (UnidentifiedImageError, OSError) as exc:
            raise UnsupportedFileType(f"无法生成缩略图：{exc}") from exc

        return buf.getvalue()
