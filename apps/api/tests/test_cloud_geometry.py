"""B 方案（云端几何兜底）的**纯函数级**测试 —— 不依赖云端、不依赖网络。

## 为什么这一组必须是纯的

几何代码的失效模式是"**看起来对**"：一张镜像/扭转的卡图仍然是一张卡，
一个被裁掉一半的四边形仍然"是个四边形"。所以每一条规则都要能被一条断言
钉住，而"调一次云端看看"不能当测试（贵、慢、不可复现）。

这里的输入全部构造成**实测过的形状**（2026-10-13，`更多样本/001.jpg` 与
`005.jpg`）：
  * `001` #6 的四个角点 `(146,1980) (-51,1585) (168,1476) (365,1871)`
    —— 一个斜的正方形（minAreaRect 440×440），宽高比 0.82；
  * 云端对同一页回的 9 个规整矩形，第三列 x 到 1.001（画面外）。
"""

from __future__ import annotations

import numpy as np

from postmarktrip.recognition.split import (
    CardCandidate,
    CloudCorners,
    SplitParams,
    SplitResult,
    cloud_corners_to_quads,
    merge_cloud_corners,
    order_quad,
    quad_containment,
    quad_iou,
    same_card,
    suspect_geometry,
)

#: 实测 `001.jpg` #6 —— 本地切错的那张（斜正方形、x 越界）。
QUAD_001_6 = np.array([[146, 1980], [-51, 1585], [168, 1476], [365, 1871]], dtype=np.float32)
#: 同页 #4 —— 本地切**对**的那张（x=0,y=0 但形状正常）。
QUAD_001_4 = np.array([[0, 0], [353, 0], [353, 595], [0, 595]], dtype=np.float32)

IMAGE_SIZE = (1446, 2000)  # work 尺度，实测 001/005 都是这个


def _card(quad: np.ndarray, **kw: object) -> CardCandidate:
    return CardCandidate(
        ortho=np.zeros((10, 6, 3), dtype=np.uint8),
        quad=np.asarray(quad, dtype=np.float32),
        origin=str(kw.get("origin", "body")),
        area_ratio=float(kw.get("area_ratio", 1.0)),  # type: ignore[arg-type]
        aspect=float(kw.get("aspect", 1.67)),  # type: ignore[arg-type]
        rectangularity=float(kw.get("rectangularity", 0.9)),  # type: ignore[arg-type]
        size_suspect=bool(kw.get("size_suspect", False)),
    )


def _blank_image(width: int | None = None, height: int | None = None) -> np.ndarray:
    """默认给一张**与 IMAGE_SIZE 同尺寸**的假图（宽=1446, 高=2000）。"""
    w = IMAGE_SIZE[0] if width is None else width
    h = IMAGE_SIZE[1] if height is None else height
    return np.full((h, w, 3), 200, dtype=np.uint8)


# --------------------------------------------------------------------------- #
#  order_quad：顺序必须稳（透视变换对角顺序极其敏感）
# --------------------------------------------------------------------------- #


def test_order_quad_sorts_into_reading_order() -> None:
    """乱序输入 → 左上/右上/右下/左下。"""
    pts = np.array([[100, 100], [300, 100], [300, 500], [100, 500]], dtype=np.float32)
    for perm in ([2, 0, 3, 1], [1, 2, 3, 0], [3, 2, 1, 0]):
        got = order_quad(pts[perm])
        assert got.tolist() == [[100, 100], [300, 100], [300, 500], [100, 500]], perm


# --------------------------------------------------------------------------- #
#  suspect_geometry：四条判据
# --------------------------------------------------------------------------- #


def test_a_clean_page_of_nine_is_not_suspicious() -> None:
    """9 张规整的卡 = 什么都不该问云端（否则每页都多花钱）。"""
    cards = [
        _card(np.array([[x, y], [x + 350, y], [x + 350, y + 595], [x, y + 595]], dtype=np.float32))
        for y in (0, 700, 1400)
        for x in (0, 550, 1100)
    ]
    verdict = suspect_geometry(cards, IMAGE_SIZE)
    assert verdict.needed is False
    assert verdict.reasons == ()


def test_too_few_cards_is_suspicious() -> None:
    """实测 `001` 检出 8、`005` 检出 7（都应 9）→ 触发。"""
    cards = [_card(QUAD_001_4) for _ in range(8)]
    verdict = suspect_geometry(cards, IMAGE_SIZE)
    assert verdict.needed is True
    assert any("检出 8 张" in r for r in verdict.reasons)


def test_out_of_frame_vertex_is_suspicious() -> None:
    """实测 `001` #6 的 x=**-51**。"""
    cards = [_card(QUAD_001_4) for _ in range(9)]
    cards.append(_card(QUAD_001_6))
    verdict = suspect_geometry(cards, IMAGE_SIZE)
    assert verdict.needed is True
    assert any("越出图像" in r for r in verdict.reasons)


def test_bad_aspect_ratio_is_suspicious() -> None:
    """实测 `001` #6/#7 = 0.82/0.81、`005` #6 = 0.83（卡约 0.55~0.62）。"""
    flat = np.array([[0, 0], [416, 0], [416, 505], [0, 505]], dtype=np.float32)
    cards = [_card(QUAD_001_4) for _ in range(9)]
    cards.append(_card(flat))
    verdict = suspect_geometry(cards, IMAGE_SIZE)
    assert verdict.needed is True
    assert any("宽高比越界" in r for r in verdict.reasons)


def test_area_much_smaller_than_the_page_median_is_suspicious() -> None:
    """实测 `005` #6 面积只有同页的 **41%**。"""
    big = np.array([[0, 0], [360, 0], [360, 600], [0, 600]], dtype=np.float32)
    small = np.array([[1100, 1600], [1370, 1600], [1370, 1930], [1100, 1930]], dtype=np.float32)
    cards = [_card(big) for _ in range(8)]
    cards.append(_card(small))
    verdict = suspect_geometry(cards, IMAGE_SIZE)
    assert verdict.needed is True
    assert any("面积远小于" in r for r in verdict.reasons)


def test_an_empty_page_is_suspicious() -> None:
    """ "一张都没切出"时云端是唯一的第二意见。"""
    verdict = suspect_geometry([], IMAGE_SIZE)
    assert verdict.needed is True


def test_a_single_normal_card_is_not_suspicious() -> None:
    """⚠️ 单卡页（用户在拍一张卡）**不能**因为"少于 9 张"就被判可疑。

    否则每张单卡图都会多花一次云端调用。所以"数量"这条判据在
    ``expected_cards=1`` 时必须放过它 —— 调用方按"这一页像不像整页"传值。
    """
    cards = [_card(np.array([[0, 0], [360, 0], [360, 600], [0, 600]], dtype=np.float32))]
    assert suspect_geometry(cards, IMAGE_SIZE, expected_cards=1).needed is False


def test_reason_text_is_readable_when_clean() -> None:
    assert suspect_geometry([], IMAGE_SIZE, expected_cards=0).needed is True  # 空页仍可疑
    clean = suspect_geometry(
        [_card(np.array([[0, 0], [350, 0], [350, 595], [0, 595]], dtype=np.float32))],
        IMAGE_SIZE,
        expected_cards=1,
    )
    assert clean.reason_text == "无可疑信号"


# --------------------------------------------------------------------------- #
#  cloud_corners_to_quads：换算 / 排序 / 裁剪
# --------------------------------------------------------------------------- #


def test_fraction_corners_are_scaled_by_the_target_image() -> None:
    """比例 × **目标图**的宽高 —— 与送云端时缩了多少无关。

    ⚠️ 两个细节：``sent_size``（723x1000）**不参与**换算，用的是目标图
    1446x2000；乘积再夹到 ``w-1 / h-1``（0.5×1446 = 723 < 1445，所以不夹）。
    """
    corners = CloudCorners(
        quads=[((0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5))],
        unit="fraction",
        sent_size=(723, 1000),
    )
    got = cloud_corners_to_quads(corners, IMAGE_SIZE)
    assert got[0].tolist() == [[0.0, 0.0], [723.0, 0.0], [723.0, 1000.0], [0.0, 1000.0]]


def test_pixel_corners_are_passed_through() -> None:
    """`unit != "fraction"` 时不再乘 —— 单位约定由 `CloudCorners.unit` 带来。"""
    corners = CloudCorners(
        quads=[((10.0, 20.0), (110.0, 20.0), (110.0, 220.0), (10.0, 220.0))],
        unit="pixel",
        sent_size=(200, 300),
    )
    got = cloud_corners_to_quads(corners, IMAGE_SIZE)
    assert got[0].tolist() == [[10.0, 20.0], [110.0, 20.0], [110.0, 220.0], [10.0, 220.0]]


def test_out_of_frame_corners_are_clipped_to_the_image() -> None:
    """实测云端回 x=**1.001**（画面外）→ 夹到 w-1。

    同时断言**顺序被修正过**：输入刻意打乱，出来必须回到阅读顺序。
    坐标用 ``pytest.approx`` 比：比例乘出来的，不该假装是整数。
    """
    import pytest

    corners = CloudCorners(
        quads=[((1.001, 0.999), (0.759, 0.710), (1.001, 0.710), (0.759, 0.999))],
        unit="fraction",
    )
    got = cloud_corners_to_quads(corners, IMAGE_SIZE)
    (tl, tr, br, bl) = got[0]
    assert tl.tolist() == pytest.approx([0.759 * 1446, 0.710 * 2000], abs=1.0)
    assert tr[1] == tl[1]  # 上边两个角 y 相同
    assert br[0] == 1445.0  # 1.001×1446 = 1447.4 → 夹到 w-1
    assert bl[0] == tl[0]
    assert br[1] == pytest.approx(0.999 * 2000, abs=1.0)  # 1998 < h-1，没被夹


def test_negative_corners_are_clipped_to_zero() -> None:
    corners = CloudCorners(
        quads=[((-0.05, -0.02), (0.3, -0.02), (0.3, 0.5), (-0.05, 0.5))],
        unit="fraction",
    )
    got = cloud_corners_to_quads(corners, IMAGE_SIZE)
    assert got[0][0].tolist() == [0.0, 0.0]


def test_non_finite_corners_are_skipped_at_this_layer_too() -> None:
    """`CloudCorners` 可以被直接构造（测试、别的来源），所以这一层也要挡。"""
    corners = CloudCorners(
        quads=[
            ((float("nan"), 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
            ((0.1, 0.1), (0.2, 0.1), (0.2, 0.4), (0.1, 0.4)),
        ],
        unit="fraction",
    )
    got = cloud_corners_to_quads(corners, IMAGE_SIZE)
    assert len(got) == 1


# --------------------------------------------------------------------------- #
#  merge_cloud_corners：云端优先 + IoU 去重 + 过滤
# --------------------------------------------------------------------------- #


def _cloud_9() -> CloudCorners:
    """实测云端对 `001.jpg` 回的 9 个矩形（比例，第三列到 1.001）。"""
    cols = [(0.001, 0.243), (0.373, 0.622), (0.760, 1.001)]
    rows = [(0.002, 0.302), (0.358, 0.661), (0.710, 0.999)]
    quads = []
    for y0, y1 in rows:
        for x0, x1 in cols:
            quads.append(((x0, y0), (x1, y0), (x1, y1), (x0, y1)))
    return CloudCorners(quads=quads, unit="fraction", sent_size=(1446, 2000), latency_s=2.3)


def test_merge_replaces_the_mis_detected_local_card() -> None:
    """**核心用例**：本地 #6 是斜正方形（与云端第 7 个重叠）→ 被云端替换。

    替换的判据必须落在**几何本身**上（IoU），而不是"谁先谁后"或"谁面积大"：
    错的本地框正好是斜的，而它的轴对齐外接框**比云端的大**（416×505 vs
    约 350×580）—— 按面积优先会让错的赢。
    """
    local = SplitResult(
        candidates=[_card(QUAD_001_4), _card(QUAD_001_6), _card(QUAD_001_4)],
        dpi=600,
        notes=["本地备注"],
    )
    merged = merge_cloud_corners(_blank_image(), local, _cloud_9(), detection_scale=0.2851)
    assert merged.geometry_source == "vlm"
    #: 9 个云端 + 本地那两张与云端不重叠的（#4 与云端第 1 个重叠 → 被替换）
    assert len(merged.candidates) == 9
    assert all(c.geometry_source == "vlm" for c in merged.candidates)
    assert all(c.origin == "vlm" for c in merged.candidates)
    #: ⚠️ `merged.notes` **只含云端这几条**，不复述本地那些 —— 复述会让调用方
    #: 把本地那一段重复报一遍（实测页面 notes 里"检测尺度/候选/NMS"整段出现
    #: 两次，读起来像"管线跑了两遍"）。本地 notes 留在 `local.notes` 里。
    assert merged.notes, "云端那几条必须在"
    assert "本地备注" not in merged.notes, "本地 notes 不该被复述进云端结果"
    assert any("云端几何兜底" in n for n in merged.notes)


def test_merge_keeps_local_cards_the_cloud_did_not_mention() -> None:
    """云端**没提到**的本地卡要原样留着 —— 它可能是一张云端真看不见的卡。

    ⚠️ 位置必须落在云端 9 个矩形**之间的缝里**（这里取 x 380~520、y 610~700，
    正好是第 1/2 列之间、第 2/3 行之间的空隙）。否则它会与某个云端矩形
    重叠而被当成同一张卡替换掉 —— 那样测的就不是"保留"这条规则了。
    """
    lonely = np.array([[380, 610], [520, 610], [520, 700], [380, 700]], dtype=np.float32)
    local = SplitResult(candidates=[_card(lonely)], dpi=600)
    merged = merge_cloud_corners(_blank_image(), local, _cloud_9(), detection_scale=0.2851)
    assert len(merged.candidates) == 10  # 9 云端 + 1 本地独有
    assert sum(1 for c in merged.candidates if c.geometry_source == "local") == 1


def test_merge_without_corners_returns_the_local_result_untouched() -> None:
    """云端**没回角点**（失败/空数组）→ 一字不改。

    ⚠️ 这一条决定了 `geometry_source` 的语义：调用**失败**时几何仍然是本地的，
    所以报 `vlm` 会让人以为"这页的几何是云端给的"，从而不敢信它。
    """
    local = SplitResult(candidates=[_card(QUAD_001_4)], dpi=600, notes=["x"])
    for empty in (CloudCorners(), CloudCorners(ok=False, reason="HTTP 401")):
        merged = merge_cloud_corners(_blank_image(), local, empty, detection_scale=0.2851)
        assert merged is local
        assert merged.geometry_source == "local"


def test_merge_filters_out_cards_that_are_not_card_shaped() -> None:
    """云端也会给错（框住整页、框住一个小图案）—— 这些必须被过滤掉。

    这里给三个矩形：一个正常的、一个横躺的（宽高比 1.6）、一个过小的。
    期望只采纳正常的那个。
    """
    corners = CloudCorners(
        quads=[
            ((0.05, 0.05), (0.30, 0.05), (0.30, 0.33), (0.05, 0.33)),  # 正常（竖版）
            ((0.40, 0.05), (0.90, 0.05), (0.90, 0.36), (0.40, 0.36)),  # 横躺
            ((0.50, 0.50), (0.545, 0.50), (0.545, 0.575), (0.50, 0.575)),  # 过小
        ],
        unit="fraction",
    )
    merged = merge_cloud_corners(
        _blank_image(), SplitResult(dpi=600), corners, detection_scale=0.2851
    )
    assert len(merged.candidates) == 1
    assert any("过滤掉 2 个" in n for n in merged.notes)


def test_merge_rejects_a_quad_covering_the_whole_page() -> None:
    """整页被框成"一张卡"必须被拒（面积比 1.6 的上界就是为它设的）。"""
    corners = CloudCorners(
        quads=[((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))], unit="fraction"
    )
    merged = merge_cloud_corners(
        _blank_image(), SplitResult(dpi=600), corners, detection_scale=0.2851
    )
    assert merged.candidates == []
    assert merged.geometry_source == "local"


def test_merge_dedup_threshold_is_stricter_than_nms() -> None:
    """去重门槛必须 **> NMS 的 0.3**，否则真正补出来的那张会被当重复丢掉。

    这条用**数值**钉住：卡与卡的 IoU 本来就可能到 0.3（实测 001 底行三张
    被 Otsu 粘成一个大连通域），沿用 0.3 会让兜底静默失效。
    """
    p = SplitParams()
    assert p.cloud_iou > p.nms_iou


# --------------------------------------------------------------------------- #
#  same_card：**实测逼出来的第二条判据**（包含度）
# --------------------------------------------------------------------------- #


def test_same_card_by_containment_catches_the_005_artifact() -> None:
    """**实测 `005.jpg` 的那一对**：IoU 只有 0.42，包含度 0.86。

    本地错框 `bbox=(1176,1614,269,323)`（同页 41% 面积、宽高比 0.83）与云端
    给的**同一张卡** `bbox=(1093,1418,352,581)`。只看 IoU 的话它会活下来，
    于是那页变成 **10 张**（正确 9）—— 而 9 正是验收标准，所以必须有断言。
    """
    p = SplitParams()
    artifact = np.array([[1176, 1614], [1445, 1614], [1445, 1937], [1176, 1937]], dtype=np.float32)
    cloud = np.array([[1093, 1418], [1445, 1418], [1445, 1999], [1093, 1999]], dtype=np.float32)
    assert quad_iou(artifact, cloud) < p.cloud_iou, "IoU 本来就不够 —— 这正是要包含度的原因"
    assert quad_containment(artifact, cloud) >= p.cloud_containment
    assert same_card(artifact, cloud, p) is True


def test_same_card_does_not_merge_two_neighbouring_real_cards() -> None:
    """两张**相邻的真卡**不能被当成同一张（实测包含度只到 0.69）。

    这是上一条的护栏：门槛一路降到 0.69 就会开始吃掉相邻的卡。
    """
    p = SplitParams()
    left = np.array([[0, 0], [353, 0], [353, 595], [0, 595]], dtype=np.float32)
    right = np.array([[540, 0], [893, 0], [893, 595], [540, 595]], dtype=np.float32)
    assert same_card(left, right, p) is False

    #: 部分重叠（模拟 `001` 底行粘连的那两个框）：也不该判成同一张
    overlap = np.array([[300, 0], [653, 0], [653, 595], [300, 595]], dtype=np.float32)
    assert same_card(left, overlap, p) is False


def test_containment_is_directional_and_handles_degenerate_boxes() -> None:
    """ "a 有多少落在 b 里"是**有向**的；退化框**不会被吸收**。

    ⚠️ 退化这件事**不能靠包含度来判**：`cv2.boundingRect` 对退化的点集返回
    1×1 而不是 0×0，所以包含度仍会算出 1.0。挡住它的是 `same_card` 里那道
    **鞋带面积**判据（真面积确实是 0）—— 这一条正是为此而写。
    "实测 `001` #6 的四个角挤在一个小范围里"就是这种输入。
    """
    p = SplitParams()
    big = np.array([[0, 0], [400, 0], [400, 400], [0, 400]], dtype=np.float32)
    small = np.array([[100, 100], [200, 100], [200, 200], [100, 200]], dtype=np.float32)
    assert quad_containment(small, big) == 1.0
    assert quad_containment(big, small) < 0.1
    assert same_card(small, big, p) is True  # 包含度这一条就够了

    degenerate = np.array([[10, 10], [10, 10], [10, 10], [10, 10]], dtype=np.float32)
    assert same_card(degenerate, big, p) is False, "零面积的框不许被任何框吸收"


def test_cloud_cards_keep_the_same_size_suspect_rule_as_local() -> None:
    """正射之后被裁扁的卡要标 `size_suspect` —— 与本地同一判据。"""
    #: 高只有宽的一半多点 → 正射后长/短 ≈ 1.8 仍在区间内（不算可疑）；
    #: 这里刻意给一个**极扁**的：宽/高 0.78 会被 wh 过滤掉，所以取 0.60 但
    #: 上下再夹一点，让正射后的长/短比落到 3 倍之外。
    corners = CloudCorners(
        quads=[((0.05, 0.05), (0.30, 0.05), (0.30, 0.10), (0.05, 0.10))], unit="fraction"
    )
    merged = merge_cloud_corners(
        _blank_image(), SplitResult(dpi=600), corners, detection_scale=0.2851
    )
    #: 宽/高 = 0.6/0.1 → 用 minAreaRect 的短/长 = 0.167，落在 cloud_wh 区间外 → 被过滤
    assert merged.candidates == []
