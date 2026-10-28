"""存储层出口。

注意导出的 API 形状：

* ``put_stream`` / ``put_bytes`` —— **写一次，然后永久存在**。没有覆盖、
  没有删除，这是 L0 不可变约束的实现方式。
* ``open`` / ``abs_path`` —— 只读。
* ``render_thumbnail`` —— **只生成字节，不写入存储**。刻意不叫 ``put_*``：
  缩略图是可随时重建的派生物，它的归宿是调用方自己的缓存目录；
  写进内容寻址存储会让每个尺寸永久占一份且无法清理。
"""

from postmarktrip.storage.content_addressed import (
    AssetStore,
    StorageError,
    StoredAsset,
    UnsupportedFileType,
)

__all__ = ["AssetStore", "StorageError", "StoredAsset", "UnsupportedFileType"]
