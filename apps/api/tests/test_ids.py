"""UUID v7 的性质测试。

v7 的全部价值在于「按时间单调递增」——如果这条不成立，
用它当主键就没有任何理由（不如直接用 v4）。所以这里重点测它。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from postmarktrip.db.ids import uuid7, uuid7_from_datetime, uuid7_timestamp_ms


def test_version_and_variant_bits() -> None:
    u = uuid7()
    assert u.version == 7, "版本位必须是 7"
    assert u.variant == uuid.RFC_4122, "变体位必须是 RFC 4122"


def test_timestamp_roundtrip() -> None:
    dt = datetime(2026, 10, 8, 12, 34, 56, 789000, tzinfo=UTC)
    u = uuid7_from_datetime(dt)
    assert uuid7_timestamp_ms(u) == int(dt.timestamp() * 1000)


def test_monotonic_by_time() -> None:
    """后生成的时间戳更大 —— 这是它相对 v4 的唯一好处。"""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    early = uuid7_from_datetime(base)
    later = uuid7_from_datetime(base + timedelta(seconds=1))
    assert uuid7_timestamp_ms(later) > uuid7_timestamp_ms(early)


def test_same_millisecond_uses_randomness() -> None:
    """同一毫秒内多次生成不能碰撞。

    注意：RFC 9562 允许同毫秒内用计数器保证严格单调，本实现用的是随机位。
    因此这里只断言「唯一」，**不断言严格单调** —— 这是有意的取舍：
    随机位不泄露生成速率，而主键只要不碰撞就够用。
    """
    dt = datetime(2026, 5, 5, 5, 5, 5, 123000, tzinfo=UTC)
    ids = {uuid7_from_datetime(dt) for _ in range(1000)}
    assert len(ids) == 1000


def test_lexicographic_sort_matches_time() -> None:
    """字符串排序 = 时间排序。这决定了数据库索引的顺序追加特性。"""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [uuid7_from_datetime(base + timedelta(milliseconds=i)) for i in range(50)]
    assert [str(s) for s in samples] == sorted(str(s) for s in samples)


def test_naive_datetime_rejected() -> None:
    """无时区的 datetime 是 bug 的常见来源 —— 直接拒绝，不要"猜"。"""
    import pytest

    with pytest.raises(ValueError):
        uuid7_from_datetime(datetime(2026, 1, 1))
