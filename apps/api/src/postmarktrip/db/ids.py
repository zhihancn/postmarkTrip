"""UUID v7 生成器。

为什么要 v7 而不是 v4：v7 的前 48 位是毫秒时间戳，因此主键**按时间单调递增**，
在 B-tree 索引里是顺序追加而不是随机插入。对一个"持续追加、偶尔查询"的日志型
数据（本项目的核心用法）来说，这能显著减少索引页分裂与写放大。

Python 3.12 的标准库还没有 uuid.uuid7（3.14 才有），所以这里自己实现。
参考 RFC 9562 §5.7。
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

__all__ = ["uuid7", "uuid7_from_datetime", "uuid7_timestamp_ms"]


def uuid7() -> uuid.UUID:
    """生成一个 UUID v7。"""
    return uuid7_from_datetime(datetime.now(UTC))


def uuid7_from_datetime(dt: datetime) -> uuid.UUID:
    if dt.tzinfo is None:
        raise ValueError("uuid7_from_datetime 需要带时区的 datetime")
    ts_ms = int(dt.timestamp() * 1000) & ((1 << 48) - 1)

    # rand_a: 12 bits (RFC 9562 允许用作亚毫秒精度或随机；这里用随机)
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    # rand_b: 62 bits
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)

    value = (ts_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """从 UUID v7 取回毫秒时间戳（用于调试与排序校验）。"""
    return value.int >> 80
