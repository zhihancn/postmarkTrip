"""`/api/assets/{id}/card-crop` 的**输入校验**测试（`docs/ok/19` §三 3.2 路径 C）。

## 为什么单独测这几条

这个端点是"弹层不再有黑边"的依靠（用户 2026-10-12 的裁定：几何交给服务端，
而不是前端 canvas）。它的**几何来自 URL**，所以坏输入只有两种下场：

* 校验太松 → 裁出一块歪的/黑的图，而**看起来只是"这张卡不好看"**（静默的错图）；
* 校验太严/抛 500 → 弹层直接白屏。

两种都不是"崩了"那么显眼，所以把四条拒绝分支逐条钉住。纯函数 `_parse_quad()`
负责这些判断 —— 它不 import cv2，所以测试不需要识别栈。

⚠️ **没做 HTTP 层测试**（按 FastAPI 的依赖求解顺序，鉴权可能先于查询参数校验跑，
那样拿到的是 401 而不是 422，测出来的东西和想测的不是同一件事）。这里测的是
**判断本身**；HTTP 层由手工 E2E 覆盖（实测 200 / 954x1600 / 163 KB）。
"""

from __future__ import annotations

import pytest

from postmarktrip.api.routes_assets import _parse_quad


def test_quad_is_scaled_back_to_the_original_resolution() -> None:
    """工作尺度 → 原图：`1 / work_scale` 的换算必须对（`ok/15` 第 2 条的老教训）。"""
    points = _parse_quad("10,20,110,20,110,220,10,220", 0.5)
    assert points == [(20.0, 40.0), (220.0, 40.0), (220.0, 440.0), (20.0, 440.0)]


def test_quad_tolerates_spaces_and_work_scale_one() -> None:
    assert _parse_quad(" 1, 2, 3, 4, 5, 6, 7, 8 ", 1.0) == [
        (1.0, 2.0),
        (3.0, 4.0),
        (5.0, 6.0),
        (7.0, 8.0),
    ]


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("1,2,3,4", "点数不够"),
        ("1,2,3,4,5,6,7,8,9,10", "点数太多"),
        ("1,2,3,4,5,6,7,abc", "有非数字"),
        ("1,2,3,4,5,6,nan,8", "NaN 也要挡"),
        ("1,2,3,4,5,6,inf,8", "inf 也要挡"),
    ],
)
def test_bad_quads_are_rejected_with_a_readable_reason(raw: str, why: str) -> None:
    with pytest.raises(ValueError) as exc:
        _parse_quad(raw, 1.0)
    assert str(exc.value), f"{why}：必须给出理由（会回给界面）"


@pytest.mark.parametrize("bad", [0.0, -0.5, float("nan"), float("inf")])
def test_work_scale_must_be_a_positive_finite_number(bad: float) -> None:
    """`work_scale` 坏了就换算不出原图坐标 —— 宁可 422，也不要裁出一块错的图。"""
    with pytest.raises(ValueError):
        _parse_quad("1,2,3,4,5,6,7,8", bad)
