"""卡片列表派生字段的单元测试（`_summaries`）。

**这个文件存在的直接原因是一个真实 bug**：`_summaries` 曾经把累加器写成
`out = {}` 之后直接 `out.get(cid)` —— 每次都是 `None`、每个分支都 `continue`，
于是函数**静默返回空字典**。

后果不是报错，而是卡片列表页上戳面原文、票名·志号·面值、地点面包屑、
彩色与戳种角标**全部为空**。它不会被"端点是否注册""模型能否编译"这类
结构测试发现，只有真的看一眼界面、或者像这样用一个假 session 直接喂数据
才会暴露。

所以这里的核心断言是：**返回的字典必须为每一个传入的 card_id 都有条目**
（哪怕那张卡没有任何戳和票）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from postmarktrip.api.routes_cards import _summaries

# --------------------------------------------------------------------------- #
#  测试替身
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class _StubSession:
    """只实现 `_summaries` 用到的那一个方法：``execute(stmt).all()``。

    刻意按语句里出现的列名分派，而不是按调用顺序 —— 顺序分派会在别人
    调整查询顺序时给出误导性的失败。
    """

    def __init__(
        self,
        *,
        postmarks: list[tuple[Any, ...]] | None = None,
        stamps: list[tuple[Any, ...]] | None = None,
        places: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self._postmarks = postmarks or []
        self._stamps = stamps or []
        self._places = places or []
        self.seen: list[str] = []

    def execute(self, stmt: Any) -> _Result:  # noqa: ANN401
        sql = str(stmt)
        self.seen.append(sql)
        if "postmark.inscription" in sql:
            return _Result(self._postmarks)
        if "stamp.catalog_ref" in sql:
            return _Result(self._stamps)
        if "place.level" in sql:
            return _Result(self._places)
        raise AssertionError(f"意料之外的查询（测试替身需要更新）：{sql[:160]}")


# --------------------------------------------------------------------------- #
#  回归测试
# --------------------------------------------------------------------------- #


def test_every_card_id_gets_an_entry() -> None:
    """★ 回归测试：没有任何戳与票的卡，也必须拿到一条（空的）摘要。

    这正是当初那个 bug：函数返回 `{}`，于是列表页的所有派生字段都是空的。
    """
    ids = [uuid.uuid4() for _ in range(3)]
    out = _summaries(_StubSession(), ids)  # type: ignore[arg-type]

    assert set(out) == set(ids), "有 card_id 没有拿到摘要条目 —— 列表页的派生字段会全空"
    for cid in ids:
        s = out[cid]
        assert s.postmark_summary is None
        assert s.stamp_summary is None
        assert s.is_color is False
        assert s.postmark_type is None
        assert s.place_names == []


def test_empty_input_returns_empty() -> None:
    assert _summaries(_StubSession(), []) == {}  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
#  邮戳
# --------------------------------------------------------------------------- #


def test_postmark_summary_from_first_postmark() -> None:
    cid = uuid.uuid4()
    session = _StubSession(
        postmarks=[
            (cid, "福建南平 建溪1", "daily", False),
            (cid, "江西玉山 三清1", "scenic", True),
        ]
    )
    out = _summaries(session, [cid])  # type: ignore[arg-type]
    s = out[cid]
    # 列表页只反映第一个戳 —— 这是有意为之（概览，不是全部内容）
    assert s.postmark_summary == "福建南平 建溪1"
    assert s.postmark_type == "daily"
    assert s.is_color is False


def test_color_flag_picked_up_when_first_postmark_is_colored() -> None:
    cid = uuid.uuid4()
    session = _StubSession(postmarks=[(cid, "晋祠天龙山主题邮局", "scenic", True)])
    out = _summaries(session, [cid])  # type: ignore[arg-type]
    assert out[cid].is_color is True


# --------------------------------------------------------------------------- #
#  邮票
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ((uuid.uuid4(), "保护森林", "普30", None, None), "保护森林 · 普30"),
        ((uuid.uuid4(), None, "普30", None, None), "普30"),
        ((uuid.uuid4(), "保护森林", None, None, None), "保护森林"),
    ],
)
def test_stamp_summary_without_face_value(row: tuple[Any, ...], expected: str) -> None:
    cid = row[0]
    session = _StubSession(stamps=[row])
    out = _summaries(session, [cid])  # type: ignore[arg-type]
    assert out[cid].stamp_summary == expected


def test_stamp_summary_includes_face_value_and_currency() -> None:
    from decimal import Decimal

    cid = uuid.uuid4()
    session = _StubSession(stamps=[(cid, "保护森林", "普30", Decimal("0.10"), "CNY")])
    out = _summaries(session, [cid])  # type: ignore[arg-type]
    # 面值以 Decimal 存库；拼摘要时直接 str()，避免浮点误差
    assert out[cid].stamp_summary == "保护森林 · 普30 · 0.10CNY"


def test_only_first_stamp_is_used() -> None:
    cid = uuid.uuid4()
    session = _StubSession(
        stamps=[
            (cid, "保护森林", "普30", None, None),
            (cid, "防治大气污染", "普30", None, None),
        ]
    )
    out = _summaries(session, [cid])  # type: ignore[arg-type]
    assert out[cid].stamp_summary == "保护森林 · 普30"


def test_stamp_with_no_usable_fields_yields_none() -> None:
    cid = uuid.uuid4()
    session = _StubSession(stamps=[(cid, None, None, None, None)])
    out = _summaries(session, [cid])  # type: ignore[arg-type]
    assert out[cid].stamp_summary is None


# --------------------------------------------------------------------------- #
#  地点
# --------------------------------------------------------------------------- #


def test_place_names_keep_level_order() -> None:
    """面包屑要从粗到细：国家 → 省级 → 地级市 → 县级。

    查询已按 level 升序，这里要保持那个顺序而不是去重时打乱它。
    """
    cid = uuid.uuid4()
    session = _StubSession(
        places=[
            (cid, 0, "中国"),
            (cid, 1, "福建省"),
            (cid, 2, "南平市"),
            (cid, 3, "建阳区"),
        ]
    )
    out = _summaries(session, [cid])  # type: ignore[arg-type]
    assert out[cid].place_names == ["中国", "福建省", "南平市", "建阳区"]


def test_place_names_deduped() -> None:
    """多张卡共用同一地点、或一张卡多个戳指向同一地点时不要重复。"""
    cid = uuid.uuid4()
    session = _StubSession(places=[(cid, 1, "福建省"), (cid, 1, "福建省")])
    out = _summaries(session, [cid])  # type: ignore[arg-type]
    assert out[cid].place_names == ["福建省"]


# --------------------------------------------------------------------------- #
#  混合
# --------------------------------------------------------------------------- #


def test_summaries_are_isolated_per_card() -> None:
    """一张卡的数据不能串到另一张卡上。"""
    a, b = uuid.uuid4(), uuid.uuid4()
    session = _StubSession(
        postmarks=[(a, "A 的戳", "daily", False)],
        stamps=[(b, "B 的票", "普30", None, None)],
        places=[(a, 1, "福建省")],
    )
    out = _summaries(session, [a, b])  # type: ignore[arg-type]

    assert out[a].postmark_summary == "A 的戳"
    assert out[a].stamp_summary is None
    assert out[a].place_names == ["福建省"]

    assert out[b].postmark_summary is None
    assert out[b].stamp_summary == "B 的票 · 普30"
    assert out[b].place_names == []


def test_three_queries_are_issued_regardless_of_card_count() -> None:
    """这是这个函数存在的理由：一次批量取，**不随卡片数增长**。

    如果哪天有人把它改回"每张卡查一次"，这条会失败。
    """
    ids = [uuid.uuid4() for _ in range(50)]
    session = _StubSession()
    _summaries(session, ids)  # type: ignore[arg-type]
    assert len(session.seen) == 3, f"查询次数应为 3（戳/票/地点各一），实际 {len(session.seen)}"
