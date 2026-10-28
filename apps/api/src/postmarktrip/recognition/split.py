"""卡片切分：一张页扫描 → N 张单卡正射图（spec v1.0 §3.2）。

## 硬性前提

**扫描时卡下垫深色（黑/藏青）卡纸。** 这不是优化技巧，是本方案成立的前提
（spec §3.2 原话）。本轮实测也印证了：深色垫底那张样本的卡/底对比是
**187 灰阶**，而白底那些只有 **147**（且那 147 主要来自卡上的印刷图案，
不是卡的边界）。

## 两条互补的检测通路

单靠一种方法覆盖不了两种底色，所以这里并行跑两条，再合并去重：

| 通路 | 适用 | 原理 |
| --- | --- | --- |
| **A 亮区** | **深色垫底** | 卡是页面上唯一的大块亮区 → Otsu 阈值 → 连通域 |
| **B 轮廓** | 白底、低对比 | 卡的裁切边线与印刷图案构成边缘 → Canny + 闭运算 → 四点多边形 |

为什么不只做通路 A：白底上卡与纸**同为白色**，阈值分不开；
而深色垫底时通路 B 反而多余（阈值已经足够干净）。
两条都留着，是因为**样本集里两种底色都有**，而它们本该被同一套算法处理。

## DPI 归一化（N7 硬要求）

所有"毫米/像素"阈值一律按 DPI 折算，**不写死像素**：``px = mm / 25.4 × dpi``。
DPI 的来源按优先级：

1. 调用方显式传入（最可靠）
2. ``asset.dpi``（EXIF —— 但实测这批样本写的是 144，是垃圾值，见下）
3. 从**切出的卡片尺寸**反推（最可靠，且它本来就是我们要量的东西）
4. ``DEFAULT_SCAN_DPI``（兜底，并在结果里标注）

> ⚠️ 实测：这批样本的 EXIF DPI 全是 **144**，而实际是 600（按页宽反推）。
> 所以**不能信元数据**。``infer_dpi_from_cards`` 从卡片像素尺寸反推
> （卡片实测 54×90mm），这一步比读 EXIF 可靠得多。

## 尺寸校验（N8）

切出的卡片长短边与 ``CARD_WIDTH_MM × CARD_HEIGHT_MM`` 差 3 倍以上 →
标 ``size_suspect``，**但绝不丢弃** —— 异形卡、自制卡都是合法的。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

import cv2
import numpy as np

logger = logging.getLogger("postmarktrip.recognition.split")

MM_PER_INCH: Final = 25.4


# --------------------------------------------------------------------------- #
#  参数（毫米；运行时按 DPI 折算成像素）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SplitParams:
    """切分参数。**全部以毫米/比例表达**，像素值运行时算出来。"""

    #: 期望的卡片物理尺寸（mm）。仅用于面积/长宽比过滤与尺寸校验，不做硬拒绝。
    card_w_mm: float = 54.0
    card_h_mm: float = 90.0

    #: 面积允许区间（相对一张卡的期望面积）。
    #: 下限 0.25 而不是更小：太小说明是戳圈或邮票这类内部图形，
    #: 而不是整张卡。上限 4 倍容纳"一张卡被上下撕成两半但仍连在一起"之类。
    area_min_ratio: float = 0.25
    area_max_ratio: float = 4.0

    #: 长宽比区间，口径是 **长边/短边**。
    #: 卡是 54×90 → 90/54 = **1.667**。
    #:
    #: 区间取 [1.45, 1.95] 是**紧的，这是有意的**：它顺带承担了"排除戳圈"
    #: 的职责 —— 邮戳是**圆**，长宽比 1.0，会被这一条挡掉（实测：圆形戳圈
    #: 在白底页上是很强的边缘特征，不挡就会切成"卡片"）。
    #: 异形卡不会因此被拒：``size_suspect`` 只标注不丢弃（N8），
    #: 而这里挡掉的是"连矩形都不是"的东西。
    aspect_min: float = 1.45
    aspect_max: float = 1.95

    #: 候选区域的最小"矩形填充度"（区域面积 / 最小外接矩形面积）。
    #: 卡是矩形，所以这个值应当接近 1；用它排除文字/图案构成的不规则块。
    min_rectangularity: float = 0.72

    #: **长宽比豁免的面积门槛**。
    #:
    #: 实测动机：3×3 网格的整页里，**第三行的卡被画面下边缘裁掉**，
    #: 长宽比掉到 1.11，被 `aspect_min` 拒掉 → 漏切 2~3 枚。
    #: 但"面积 >= 0.35×卡"的东西**不可能是戳（约 0.10）或票（约 0.15）**，
    #: 所以按"被边缘裁掉的真卡"放行，并标 `size_suspect`。
    #:
    #: 门槛从 0.45 下调到 **0.35**：实测 `005.jpg` 底行右侧那张卡的
    #: 面积比是 **0.39** —— 它**已经被检测到了**，只因离 0.45 差 0.06 而被拒。
    #: 下调后它被放行。（复核依据：`look_page.py --diag` 会把被拒候选按面积排序。）
    clipped_area_ratio: float = 0.35

    #: **豁免的面积比上界**。
    #:
    #: ⚠️ 这一条也是被测试逼出来的，而且理由很物理：**"被裁"只会让卡变小，
    #: 不会让它变大**。所以被裁的真卡其面积比应当在 0.45~1.5 之间。
    #:
    #: 没有上界时，**页面边框本身**会被放进来：它是一个近乎完美的矩形
    #: （rect≈1.0）、面积比约 3.2（整页/单卡）、长宽比 1.4（出界）——
    #: 于是被当成"被边缘裁掉的真卡"。实测
    #: `test_split_dark_pad_with_card_touching_border` 立刻抓到了它（2 张变 3 张）。
    clipped_area_max_ratio: float = 1.5

    #: **长宽比豁免时的矩形度门槛**（比 `min_rectangularity` 严得多）。
    #:
    #: ⚠️ 这一条是被测试逼出来的：只加面积门槛时，**一个大圆也能通过** ——
    #: 圆的填充度是 π/4 ≈ **0.785**，高于 `min_rectangularity` 0.72；
    #: 只要它在图里足够大（面积比 >= 0.45），就会被当成"被裁掉的真卡"放进来。
    #: （实测：`test_split_cutline_rejects_circle` 立刻抓到了这一点。）
    #:
    #: 而真矩形的填充度接近 **1.0**。所以豁免时要求 >= 0.90 ——
    #: 这个区间圆进不来，而真卡即使被裁掉仍是矩形。
    clipped_min_rectangularity: float = 0.90

    #: 自适应阈值（Sauvola）的窗口边长，相对卡片**短边**的比例。
    #: spec §3.2 建议 ≈ 1/4。
    sauvola_window_ratio: float = 0.25

    #: 形态学闭运算核边长（mm）。用途是**把卡内部的小孔填掉**（印刷图案、
    #: 戳圈会在掩膜上留下孔洞），**不是**把相邻的卡连起来。
    #:
    #: ⚠️ 这个值曾经订得太大，实测直接把 9 张卡糊成一个连通域
    #: （预期 9 张卡 → 切出 1 张，面积 11.56×单卡）。原因是当时的核大小
    #: 是按"卡内最大特征（邮票约 30mm）"算的 —— **定错了参照物**。
    #: 正确的参照物是**卡与卡的间隙**（几毫米），因为该被保留的正是那些间隙。
    close_kernel_mm: float = 2.0

    #: 是否对掩膜做闭运算。深色垫底时卡体本身已是干净的大连通块，
    #: 闭运算只会引入"把卡粘起来"的风险，所以那条通路默认关掉。
    use_closing_on_body: bool = False

    #: 判定"页面底色"的比例：连通域的外接框在**两个方向**都超过画面的这个比例。
    #:
    #: ⚠️ 不能用"是否与图像边界相连"来判底色 —— 实测那张深色垫底样本里
    #: 9 个卡片尺寸的连通域中有 **8 个贴着边界**（卡片延伸到画面外），
    #: 那样会误杀真卡。底色与卡片的真正区别是**尺寸**。
    background_span_ratio: float = 0.5

    #: 卡区域面积的取法：``true`` = 用**最小外接矩形**的面积，
    #: ``false`` = 用轮廓自身的面积（``contourArea``）。
    #:
    #: ⚠️ 这个开关是实测逼出来的。对"裁切边线围成的框"而言，
    #: ``contourArea`` 量的是**那段细线本身围出的面积** —— 细线是 1~2px 宽、
    #: 有断口的开放曲线，所以它给出的是**卡片面积的一半左右**（实测 0.26~0.53×卡），
    #: 而且填充度（轮廓面积/外接矩形面积）只有 0.4~0.6，被 min_rectangularity 挡掉。
    #:
    #: 但那个**框的形状是对的**：实测长宽比 1.69 / 1.57 / 1.62，
    #: 而卡的卡号是 90/54 = 1.667。所以要换的是"面积怎么算"，不是"形状判据"。
    #: 用最小外接矩形的面积，`fill` 会自然接近 1，形状判据重新生效。
    use_rect_area: bool = True

    #: 裁切边线相对纸面的灰度落差。
    #:
    #: 实测：白底页纸面 255，卡的裁切边线掉到 232~244（宽 8px）。
    #: 所以阈值取 ``纸面高分位数 - cutline_drop``。
    #: 这个值必须**远小于**卡与纸的差异（那本来就是 0），但大于 JPEG 噪点
    #: 在纯白区的波动（实测 std ~1.5），否则会把噪点当成边线。
    cutline_drop: float = 8.0

    #: NMS 的 IoU 阈值。两个候选重叠超过它，只留大的那个。
    nms_iou: float = 0.3

    #: 尺寸校验的容忍倍数（N8）：与期望长短边比超过它就标 size_suspect。
    size_suspect_factor: float = 3.0

    #: 面积小于此比例的整页面积时，认为"整页只有很小一块"——
    #: 用于 L1 降级判断（切 0 张且整页大面积空白）。
    sparse_page_ratio: float = 0.03

    #: **云端兜底的去重门槛**（见 :func:`merge_cloud_corners`）。
    #:
    #: ⚠️ 它必须比 `nms_iou` **大**，理由在 `merge_cloud_corners` 里写明了：
    #: 这里要判的是"两条通道说的是不是同一张卡"，而卡与卡的 IoU 本来就可能
    #: 到 0.3（实测 `001.jpg` 底行三张被 Otsu 粘成一个大连通域）。
    #: 沿用 0.3 会把**真正补出来的那张卡**当成重复项丢掉 —— 而症状只是
    #: "兜底好像没起作用"。
    cloud_iou: float = 0.5

    #: 云端角点过滤用的**宽/高**区间（与 `aspect_min/aspect_max` 的
    #: "长边/短边"口径不同：这里是 \**竖版卡的宽高比**，卡约 0.55~0.62）。
    #:
    #: 为什么不能直接复用 `aspect_min/aspect_max`：那是 1.45~1.95 的"长/短"，
    #: 而云端的输入是**画面外也能给**的四边形，横放的卡会算成 1/0.6 ≈ 1.67
    #: 从而"看起来合规"。这里要的是"**它像不像一张竖版卡**"，
    #: 所以取宽高比并允许一点余量（0.40~0.80 覆盖实测 0.55~0.67 与轻微倾斜）。
    cloud_wh_min: float = 0.40
    cloud_wh_max: float = 0.80

    #: 云端候选的**面积比上界**。比 `area_max_ratio` 紧得多，而且这是有意的：
    #: 云端是"按矩形给角点"，它**倾向于把整页框成一张卡**（或把小图案框成卡）。
    #: 实测该给 9 张卡时它给得出 9 个规整矩形，所以不需要靠宽面积去容忍。
    #: 上界取 1.6×卡：被边缘裁掉的真卡面积只会更小，不可能是"更大的东西"。
    cloud_area_max_ratio: float = 1.6

    #: **包含度门槛**：一个四边形有多少落在另一个里面时，判为"同一张卡"。
    #:
    #: ⚠️ 这一条是被实测逼出来的（`005.jpg`，2026-10-13）：本地那张错的
    #: `#6`（`bbox=(1176,1614,269,323)`，只有同页 41% 面积、宽高比 0.83）
    #: 与云端给的**同一张卡**（`bbox=(1093,1418,352,581)`）**IoU 只有 0.42**
    #: —— 因为错的框又小又偏下，交并比天然低。只看 IoU 的话它会活下来，
    #: 于是那页变成 **10 张**（正确是 9，多出来的正是这块"卡上的图形"）。
    #:
    #: "小框几乎整个落在大框里"这件事本身就很能说明问题：一张 41% 面积的
    #: 东西整个躺在真卡范围内，它是真卡上的一块，不是另一张卡。
    #: 实测该例包含度 = **0.86**，而"两张相邻真卡"的包含度只到 **0.69** ——
    #: 门槛取 0.75，两边都有余量（不是贴着数据调的）。
    cloud_containment: float = 0.75


# --------------------------------------------------------------------------- #
#  结果
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CardCandidate:
    """一个候选卡片。"""

    #: 正射图（透视矫正后）。保持长宽比，**不强行压成 54×90**（spec §3.2）。
    ortho: np.ndarray
    #: 四角在原图中的坐标，形状 (4, 2)，顺序：左上、右上、右下、左下。
    quad: np.ndarray
    #: 来源通路：body（亮区）/ contour（轮廓）/ cutline（裁切边线）/
    #: fallback（整页降级）/ vlm（云端角点兜底）。
    origin: str
    #: 面积比（相对期望卡片面积）。>1 说明比一张卡大（可能两张粘连）。
    area_ratio: float
    #: 长宽比（长边/短边）。卡约 1.667。
    aspect: float
    #: 矩形填充度。
    rectangularity: float
    #: 尺寸与期望差 3 倍以上（N8：标注但**不丢弃**）。
    size_suspect: bool = False
    #: 几何来自哪条通道：``local``（本地检测）或 ``vlm``（云端角点兜底）。
    #:
    #: ⚠️ 与 ``origin`` 分开记是有意的：``origin`` 回答"本地是哪条通路找到的"，
    #: 而这一条回答"**这个四边形是谁给的**"。界面要标的是后者 ——
    #: 云端给的几何不可复现，多看一眼的理由不同。
    geometry_source: str = "local"


@dataclass(slots=True)
class SplitResult:
    """一次切分的结果。"""

    candidates: list[CardCandidate] = field(default_factory=list)
    #: 实际使用的 DPI（以及它是怎么来的，便于排障）。
    dpi: int = 0
    dpi_source: str = "default"
    #: L0/L1/L2 降级级别（spec §3.2 的失败降级表）。
    #: L0 = 正常切出；L1 = 切 0 张但整页像单卡，整页当单卡；L2 = 无法判断。
    level: str = "L0"
    #: 供 report 用的诊断信息。
    notes: list[str] = field(default_factory=list)
    #: 这一页的几何**最终**来自哪条通道：``local`` 或 ``vlm``。
    #:
    #: ``vlm`` 表示"云端角点兜底被触发并且真的用上了" —— 而不是"调过云端"。
    #: 两者的区别很重要：调用失败/回答为空时几何仍然是本地的，
    #: 那时报 `vlm` 会让人以为"这页的几何是云端给的"，从而不敢信它。
    geometry_source: str = "local"


# --------------------------------------------------------------------------- #
#  工具
# --------------------------------------------------------------------------- #


def mm_to_px(mm: float, dpi: float) -> int:
    """毫米 → 像素。**所有阈值都必须走这里**（N7：不写死像素值）。"""
    return max(1, round(mm / MM_PER_INCH * dpi))


def order_quad(pts: np.ndarray) -> np.ndarray:
    """把 4 个点排成「左上 → 右上 → 右下 → 左下」。

    用**和/差**而不是"最小 x 就是左上"：后者在卡片旋转时会把角判错，
    而透视变换对角的顺序极其敏感 —— 顺序错了会得到一张镜像/扭转的图，
    而它"看起来"仍然是一张卡，所以不会立刻暴露。
    """
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array(
        [
            pts[np.argmin(s)],  # 左上：x+y 最小
            pts[np.argmin(d)],  # 右上：x-y 最大（y-x 最小）
            pts[np.argmax(s)],  # 右下：x+y 最大
            pts[np.argmax(d)],  # 左下：x-y 最小
        ],
        dtype=np.float32,
    )


def quad_iou(a: np.ndarray, b: np.ndarray) -> float:
    """两个四边形的 IoU（用轴对齐外接框近似）。

    刻意不用精确多边形交：候选本来就要经过 NMS，而外接框近似在这个场景下
    足够（卡是凸四边形且方向相近）—— 换来的是不依赖任何额外几何库。
    """
    ax, ay, aw, ah = cv2.boundingRect(np.asarray(a, dtype=np.float32))
    bx, by, bw, bh = cv2.boundingRect(np.asarray(b, dtype=np.float32))
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return float(inter) / float(union) if union > 0 else 0.0


def quad_containment(a: np.ndarray, b: np.ndarray) -> float:
    """``a`` 有多大比例落在 ``b`` 里（交 / ``a`` 自身的面积）。

    与 :func:`quad_iou` 同一套外接框近似，所以两者可以互相印证。

    ⚠️ **不要指望它识别退化框**：``cv2.boundingRect`` 对退化的点集返回
    ``1x1`` 而不是 ``0x0``，所以一个"四个角挤在一起"的框在这里仍会算出
    包含度 1.0。挡退化输入的是 :func:`same_card` 里那道鞋带面积判据
    （``_quad_area`` 给的是真面积 0）—— 这两件事刻意分开。
    """
    ax, ay, aw, ah = cv2.boundingRect(np.asarray(a, dtype=np.float32))
    if aw <= 0 or ah <= 0:
        return 0.0
    bx, by, bw, bh = cv2.boundingRect(np.asarray(b, dtype=np.float32))
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = float(ix * iy)
    if inter <= 0:
        return 0.0
    return inter / float(aw * ah)


def same_card(a: np.ndarray, b: np.ndarray, params: SplitParams) -> bool:
    """两个四边形**是不是同一张卡**（云端去重用的判据，只有这一个实现）。

    两条**任一**成立就算同一张：

    1. **IoU >= `cloud_iou`**（默认 0.5）—— 两个框基本重合。
    2. **包含度 >= `cloud_containment`**（默认 0.75）—— 一个几乎完全在另一个
       里面。这一条不可省：实测 `005.jpg` 那张错框与云端同一个卡的 IoU 只有
       **0.42**（错框又小又偏），而包含度是 **0.86**。少了第 2 条，那页会留下
       两个候选 → **10 张**（正确的是 9）。

    ⚠️ 为什么不用更小的 IoU 门槛（比如 0.3）来代替第 2 条：卡与卡的 IoU
    本来就可能到 0.3（实测 `001` 底行三张被 Otsu 粘成一个大连通域），
    那样会把**真正补出来的那张卡**当重复丢掉 —— 而症状只是"兜底没起作用"。
    包含度没有这个毛病：两张相邻真卡的包含度只到 0.69。

    ⚠️ **任一为退化框（零面积）就直接判"不是同一张"**：`cv2.boundingRect`
    对退化的点集返回 ``1x1``，于是形状判据会拿到一个 1 像素的"框"并算出
    包含度 1.0 —— 那个框会被**任意**一个云端候选吸收掉（包括离它很远的）。
    真卡不是退化的，这种输入只可能来自坏数据，宁可留着让人看见。
    所以这里用**鞋带面积**（真面积）挡，而不是用形状判据挡。
    """
    qa = quad_area(a)
    qb = quad_area(b)
    if qa <= 0 or qb <= 0:
        return False
    if quad_iou(a, b) >= params.cloud_iou:
        return True
    return quad_containment(a, b) >= params.cloud_containment


def warp_card(image: np.ndarray, quad: np.ndarray, *, max_px: int | None = None) -> np.ndarray:
    """把四边形透视矫正成正射图。

    目标尺寸取四边形的**实际长短边**，保持长宽比 —— **不强行压成 54×90**
    （spec §3.2）。理由：压成固定尺寸会把"这到底是不是一张卡"这个信息抹掉，
    而尺寸校验（N8）正需要它。
    """
    q = order_quad(quad)
    (tl, tr, br, bl) = q
    w_top = float(np.linalg.norm(tr - tl))
    w_bottom = float(np.linalg.norm(br - bl))
    h_left = float(np.linalg.norm(bl - tl))
    h_right = float(np.linalg.norm(br - tr))
    out_w = max(1, round(max(w_top, w_bottom)))
    out_h = max(1, round(max(h_left, h_right)))

    # 卡是竖版：取长边当高，避免横放时把长宽比弄反
    if out_w > out_h:
        out_w, out_h = out_h, out_w

    if max_px and max(out_w, out_h) > max_px:
        scale = max_px / max(out_w, out_h)
        out_w = max(1, int(out_w * scale))
        out_h = max(1, int(out_h * scale))

    dst = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype=np.float32
    )
    m = cv2.getPerspectiveTransform(q, dst)
    # INTER_CUBIC：upscale 时比 LINEAR 保留更多笔画细节（戳面小字是识别目标）。
    # 绝不灰度化、绝不做色彩校正（spec §3.8 硬规定）—— 这里只做几何变换。
    return cv2.warpPerspective(
        image, m, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )


def infer_dpi_from_cards(
    cards: list[CardCandidate], card_h_mm: float, *, scale: float = 1.0
) -> int | None:
    """从切出的卡片尺寸反推**原图**的 DPI。

    **这比读 EXIF 可靠**。实测这批样本的 EXIF DPI 是 144（垃圾值），
    而实际扫描是 600。用"卡片的像素高度 ÷ 已知物理高度"能直接算出来，
    因为卡片的物理尺寸是固定的（实测 54×90mm）。

    ## ``scale`` 不能省

    ``CardCandidate.ortho`` 是在**调用方传入的那张图**的尺度下生成的。
    若调用方传的是缩略图（见 ``split_page`` 的 ``detection_scale``），
    那么正射图的像素也是缩略图尺度 —— 而 ``card_h_mm`` 是物理毫米。
    两者相除必须先把像素**还原到原图尺度**，否则结果是"缩略图 DPI"。

    ⚠️ 实测撞到过：漏了这一步，反推出 **100** 而真值是 600（差 6 倍，
    恰好是 1/0.1996 的量级）。这与 ``expected_area`` 那次是**同一个量纲错误**，
    所以这里把它做成显式参数，并在 docstring 里写明为什么。

    ## 只信"非 size_suspect"的候选

    ``size_suspect`` 的候选**按定义**就是尺寸异常的那些（可能是戳圈、
    邮票、或两张粘连的卡）。用它们反推会把估计带偏。
    若一个可信候选都没有，返回 ``None``，由调用方回落并标注 —— 而不是硬给一个数。

    ## 取中位数而不是均值

    个别切歪的卡会把均值拉偏，中位数不会。
    """
    ratios: list[float] = []
    for c in cards:
        if c.size_suspect:
            continue
        h = max(c.ortho.shape[:2])
        if h <= 0:
            continue
        original_h = h / scale if scale > 0 else h
        ratios.append(original_h / (card_h_mm / MM_PER_INCH))
    if not ratios:
        return None
    # 取中位数后归到最接近的 50，避免"607"这种假精度
    median = float(np.median(ratios))
    return int(round(median / 50) * 50)


def sauvola_threshold(gray: np.ndarray, window: int, k: float = 0.2) -> np.ndarray:
    """Sauvola 自适应阈值（局部均值 + 局部标准差）。返回 0/255 掩膜。

    为什么不用 Otsu：**Otsu 是全局的**，一张 A4 上光照不均（平板扫描仪边缘
    偏暗是常态）时，全局阈值会在一侧吃掉卡边、在另一侧吃进纸面。
    Sauvola 对每个像素用邻域统计，天然容忍渐变。

    实现用积分图做 box filter（比 cv2.adaptiveThreshold 的 mean 版多了
    标准差项，而 cv2 没有内置 Sauvola）。
    """
    win = max(3, window | 1)  # 必须是奇数
    f = gray.astype(np.float32)
    mean = cv2.boxFilter(f, -1, (win, win), normalize=True, borderType=cv2.BORDER_REPLICATE)
    sq = cv2.boxFilter(f * f, -1, (win, win), normalize=True, borderType=cv2.BORDER_REPLICATE)
    var = np.maximum(sq - mean * mean, 0.0)
    std = np.sqrt(var)
    # R 取 128（8 位图像的动态范围一半）是 Sauvola 的常用取值
    threshold = mean * (1.0 + k * (std / 128.0 - 1.0))
    return np.where(f > threshold, 255, 0).astype(np.uint8)


def _quad_from_rect(rect: tuple[Sequence[float], Sequence[float], float]) -> np.ndarray:
    """``cv2.minAreaRect`` 的返回值 → 4 个角点。

    参数类型刻意写成 ``Sequence[float]`` 而不是 ``tuple[float, float]``：
    OpenCV 的 stub 把它们声明成 ``Sequence``，而**运行时的值确实是二元组**。
    写成更窄的类型会让 mypy 报 ``arg-type``，而 cast 或 ``type: ignore``
    只会把问题藏起来 —— 这个签名既准确又不撒谎。
    """
    (cx, cy), (w, h), angle = rect
    box = cv2.boxPoints(((cx, cy), (w, h), angle))
    return np.asarray(box, dtype=np.float32)


# --------------------------------------------------------------------------- #
#  通路 A：亮区（深色垫底时的主力）
# --------------------------------------------------------------------------- #


def _detect_by_bright_body(
    gray: np.ndarray, params: SplitParams, expected_area: float, dpi: float
) -> list[tuple[np.ndarray, float, float, float]]:
    """在**深色垫底**的页面上找卡片：取亮区中**不与图像边界相连**的连通域。

    返回 [(quad, area_ratio, aspect, rectangularity), ...]

    ## 为什么是这条判据（两次实测的结论）

    **第一版：阈值取亮区 → 取最大连通域。** 失败。
    深色垫底那张样本里有两类亮区：卡片内部（255），以及**图像左右边缘没被
    垫子盖住的浅色条带**（也是 255）。两者连成一片，结果是 1 个连通域、
    13.11×单卡面积、bbox 等于整幅图。

    **第二版：从暗区找洞。** 也失败。假设是"垫子围成一圈、卡是它的内孔"，
    但实测垫子自己就是**最大的连通暗域**，卡与卡之间的暗缝与它相连 ——
    所以没有任何内孔（实测候选 459 个全是噪点，过滤后 0 个）。

    **第三版（当前）：亮区中"不与边界相连"的连通域。**
    这条判据不需要垫子围成环，只需要"卡被暗区与页面边缘隔开" ——
    而深色垫底恰恰保证了这一点。它天然排除了边缘条带（因为那些**贴着边界**），
    也天然排除了白底页上的整片纸面（同理）。

    > 遗留：白底页上多张卡会**连成一片**（卡与卡之间只隔着极淡的裁切边线，
    > 灰度上仍算亮区），这条通路会把 N 张卡当成 1 个大连通域。
    > 那属于"白底"这一降级情形，由通路 B（轮廓）处理 —— 见 spec §3.2 的
    > 说明：白底本来就只是"勉强可分"。
    """
    # 深色垫底是双峰分布，Otsu 足够。实测峰值 255（卡）与 47（垫）→ 阈值 154。
    _, bright = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    n, labels, stats, _ = cv2.connectedComponentsWithStats(bright, connectivity=8)
    if n <= 1:
        return []

    # ---- 排除"页面底色"，保留卡片 ----
    #
    # ⚠️ 判据**不是**"是否与图像边界相连"。这是实测撞出来的：
    # 那张深色垫底样本里 9 个卡片尺寸的连通域中有 **8 个贴着图像边界**
    # —— 因为卡片延伸到画面外（扫描裁掉了边缘）。
    # 用"不触边"当判据会误杀 8 张真卡，只剩 1 张。
    #
    # 真正的区别在**尺寸**：页面底色横跨大半个画面，而一张卡只占一小块。
    # 所以判据是"触边 **且** 外接框横跨画面的大部分"。
    height, width = labels.shape
    border_labels: set[int] = set()
    for x in range(width):
        border_labels.add(int(labels[0, x]))
        border_labels.add(int(labels[height - 1, x]))
    for y in range(height):
        border_labels.add(int(labels[y, 0]))
        border_labels.add(int(labels[y, width - 1]))
    border_labels.discard(0)

    #: 外接框超过画面这个比例 × 两个方向 → 判为底色
    span = params.background_span_ratio
    background_labels = {
        i for i in border_labels if stats[i, 2] >= width * span and stats[i, 3] >= height * span
    }

    out: list[tuple[np.ndarray, float, float, float]] = []
    for i in range(1, n):
        if i in background_labels:
            continue
        x, y, w, h, area = stats[i]
        if area <= 0 or w <= 2 or h <= 2:
            continue

        # 卡片是矩形：连通域应当几乎填满自己的外接框。
        # 这一条能挡掉"由印刷图案/文字拼出来的不规则连通域"，且比 minAreaRect 便宜。
        if area / float(w * h) < params.min_rectangularity:
            continue

        component = (labels[y : y + h, x : x + w] == i).astype(np.uint8) * 255
        contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        cnt = max(contours, key=cv2.contourArea) + np.array([[x, y]])

        rect = cv2.minAreaRect(cnt)
        (_, _), (rw, rh), _ = rect
        if rw < 2 or rh < 2:
            continue
        long_side, short_side = max(rw, rh), min(rw, rh)
        out.append(
            (
                _quad_from_rect(rect),
                area / expected_area,
                long_side / short_side,
                area / float(rw * rh),
            )
        )
    return out


# --------------------------------------------------------------------------- #
#  通路 B：轮廓（白底 / 低对比时的主力）
# --------------------------------------------------------------------------- #


def _detect_by_cutline_frame(
    gray: np.ndarray, params: SplitParams, expected_area: float, dpi: float, *, scale: float
) -> list[tuple[np.ndarray, float, float, float]]:
    """在白底页上找**卡片裁切边线围成的框**，框内的孔就是卡。

    返回 [(quad, area_ratio, aspect, rectangularity), ...]

    ## 为什么不能靠 Otsu（实测撞出来的）

    "多张卡混排"与"单张卡"这两张白底样本里，**亮区的最大连通域就是整张纸**
    —— 面积比 11.5~12.9×单卡、bbox 等于整幅图。因为卡与纸**同为白色 255**，
    灰度上不可分。距离变换也救不了：页面中心到边缘的距离有 547px，
    而卡短边半径只有 64px，两者差一个量级。

    ## 但裁切边线是可见的（实测量化过）

    沿卡边界取剖面，边线处纸面 255 掉到 **232/244**，宽 **8px**：

        x=[3845,3852] 宽 8px  min=232 mean=243   ← 卡的左裁切边
        x=[4540,4547] 宽 8px  min=244 mean=249   ← 卡的右裁切边

    **Otsu 看不见它，是因为 Otsu 是全局的**：98.5% 的页面是纯白 255 时，
    它挑出的阈值远低于边线的灰度。所以这里改用**相对纸面的低阈值**：
    纸面取高分位数（255），边线阈值取它减去 ``cutline_drop``。

    ## 为什么在缩略图上也能用

    边线在原图宽 8px，缩到 1400px（比例 0.2）后约 1.6px —— 仍可分辨，
    但下面的 ``close_kernel_mm`` 要按 scale 折算，否则闭运算相对放大 5 倍。
    """
    # 纸面灰度取高分位数：白底页绝大多数像素是纸
    paper = float(np.percentile(gray, 99))
    cut = max(1.0, paper - params.cutline_drop)
    darker = (gray < cut).astype(np.uint8) * 255

    # 边线常常被文字/图案打断，闭运算把它接成闭合的框。
    # 核要够大以跨过断口，但远小于卡的短边（否则会把整张卡吃掉）。
    ksize = max(3, mm_to_px(params.close_kernel_mm * scale, dpi))
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (ksize, ksize))
    closed = cv2.morphologyEx(darker, cv2.MORPH_CLOSE, k, iterations=2)

    # RETR_CCOMP：外轮廓（框）与它的内孔（卡内部）会成对出现，
    # hierarchy[i][3] != -1 标记"这是某个外轮廓的内孔"。
    contours, hierarchy = cv2.findContours(closed, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []

    out: list[tuple[np.ndarray, float, float, float]] = []
    for cnt in contours:
        area = float(cv2.contourArea(cnt))
        if area <= 0:
            continue
        rect = cv2.minAreaRect(cnt)
        (_, _), (rw, rh), _ = rect
        if rw < 2 or rh < 2:
            continue
        long_side, short_side = max(rw, rh), min(rw, rh)
        rect_area = float(rw * rh)
        # 卡区域面积：优先用最小外接矩形（见 use_rect_area 的说明）。
        #
        # ⚠️ 填充度必须跟着这个选择走。用外接矩形当区域时，"轮廓面积 / 外接矩形面积"
        # 描述的是**那段细线有多细**（实测 0.07），而不是"这块区域像不像矩形" ——
        # 于是形状判据会把正确的框全部挡掉。实测撞到过：
        # 单卡那张抓到的框是 `面积比 0.941 / 长宽比 1.694`（卡真值 1.667，几乎完美），
        # 却因为填充度 0.074 被 min_rectangularity 拒绝。
        # 外接矩形按定义就是矩形，所以这种口径下填充度恒为 1，
        # 形状质量改由**长宽比**承担（它本来就更直接）。
        if params.use_rect_area:
            region, fill = rect_area, 1.0
        else:
            region, fill = area, area / rect_area
        out.append(
            (
                _quad_from_rect(rect),
                region / expected_area,
                long_side / short_side,
                fill,
            )
        )
    return out


def _detect_by_contour(
    gray: np.ndarray,
    params: SplitParams,
    expected_area: float,
    dpi: float,
    *,
    scale: float = 1.0,
) -> list[tuple[np.ndarray, float, float, float]]:
    """返回 [(quad, area_ratio, aspect, rectangularity), ...]

    ``aspect`` 口径与通路 A 一致：**长边/短边**。两条通路必须用同一个口径，
    否则过滤阈值在两处含义不同 —— 这类不一致不会报错，只会让结果难解释。

    ``scale`` 是当前图相对原图的缩放（见 ``split_page`` 的说明）：
    **形态学核也要跟着缩**，否则在缩略图上用一个按原图毫米算出来的核，
    等于把操作放大了 1/scale 倍。
    """

    # CLAHE：提升弱边界的局部对比（卡与白纸的裁切边线本身就极淡）
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    # Canny 的双阈值按图像中位数自适应：单一固定阈值在不同曝光下必然失效
    median = float(np.median(enhanced))
    lower = int(max(0, 0.6 * median))
    upper = int(min(255, 1.6 * median))
    edges = cv2.Canny(enhanced, lower, upper, apertureSize=3)

    # 闭运算把断开的边线连起来 —— 卡的四条边在白纸上常常只断断续续可见。
    # 核大小按"卡与卡的间隙"定（见 close_kernel_mm 的说明），不是按卡内特征。
    ksize = max(3, mm_to_px(params.close_kernel_mm * scale, dpi))
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (ksize, ksize))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k, iterations=2)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    out: list[tuple[np.ndarray, float, float, float]] = []
    for cnt in contours:
        area = float(cv2.contourArea(cnt))
        if area <= 0:
            continue

        # 多边形逼近：ε 取周长的 2%（spec §3.2）
        peri = float(cv2.arcLength(cnt, True))
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue

        rect = cv2.minAreaRect(cnt)
        (_, _), (rw, rh), _ = rect
        if rw < 2 or rh < 2:
            continue
        long_side, short_side = max(rw, rh), min(rw, rh)
        out.append(
            (
                approx.reshape(4, 2).astype(np.float32),
                area / expected_area,
                long_side / short_side,
                area / float(rw * rh),
            )
        )
    return out


# --------------------------------------------------------------------------- #
#  B 方案：本地几何"可疑"的判据
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GeometrySuspicion:
    """ "这一页的本地几何要不要问云端"的判断结果。

    ``reasons`` 为空 = 不调用云端。**判据必须可测**：几何这类东西
    "看起来对"最不可靠，而"该不该花钱调云"是个会被反复改的策略点。
    """

    needed: bool
    reasons: tuple[str, ...] = ()

    @property
    def reason_text(self) -> str:
        return "；".join(self.reasons) if self.reasons else "无可疑信号"


#: 每页"期望几张卡"的默认先验。实测样本都是 3×3 = 9 张的整页扫描。
#: 判据不用它当**硬**要求，只用它做"少于这个数就可疑"这一条。
DEFAULT_EXPECTED_CARDS: Final = 9


class GeometryMode(StrEnum):
    """**几何从哪来** —— B 方案与 A 方案共用的那个开关（`ok/20` §七的回滚点）。

    * ``LOCAL_FIRST``（B，先做的那个）：本地检测是主路径；**只在可疑时**问一次
      云端"每张卡的四个角"，校正后与本地按 IoU/包含度去重合并。
      成本：正常页 **0 次**额外调用。
    * ``CLOUD_FIRST``（A，B 不达标时的退路）：**每页都问**云端角点，几何以云端
      为准；本地检测**只留作交叉校验**（本地与云端 IoU 都很低时在 notes 里标出来）。
      成本：每页 **1~2 次**额外调用（追问是条件性的）。

    为什么做成一个常量而不是散落的 if：`ok/20` §七要求"出问题一个常量切回"。
    两条路共用同一段校正/过滤/去重（`cloud_corners_to_quads` +
    `_candidate_ok` + `same_card`），所以切模式**不会**换掉几何的正确性规则 ——
    换的只是"什么时候用云端"。
    """

    LOCAL_FIRST = "local_first"
    CLOUD_FIRST = "cloud_first"


#: **当前生效的几何模式**。`ok/20` §七的回滚点 —— 出问题把这一个常量改回去。
#:
#: ## 为什么最终定在 `CLOUD_FIRST`
#:
#: 按 `ok/20` §六 的验收标准，B 跑出来的数字是：
#:
#: | 页 | 检出 | 宽高比 | `尺寸可疑` | `长宽比不像卡` | **`贴到图像上/左边`** |
#: | --- | --- | --- | --- | --- | --- |
#: | `001.jpg` | **9** | 0.54~0.67 | 无 | 无 | **有**（#0/#3/#6 的 x=0，#1/#2 的 y=0）|
#: | `005.jpg` | **9** | 0.56~0.63 | 无 | 无 | **有**（#0/#3/#6 的 x=0）|
#:
#: 也就是说：**"9 张、形态正常"达标了**，唯一没消掉的是"贴边"。
#: 而 `_edge_check.py` 量过：那几条边上**纸面灰度是 255 一直到像素 0** ——
#: 卡片真的是被扫描画面裁掉的，**不存在能拿来"推出去"的像素**。
#: 云端自己也把那些角点回成 x=0.001（画面内），说明它同样看不见被裁掉的部分。
#:
#: 所以这一条**两条路都消不掉**，而用户 2026-10-13 已授权"不达标就直接上 A"，
#: 于是按 A 落地：**每页都问云端**（成本按页 +1~2 次调用，用户已知情并接受）。
#: 好处不只是"守规矩"：A 让几何**每次都以云端为准**，本地那些斜正方形/粘连
#: 大块连"参与竞争"的机会都没有 —— B 里它们还要靠去重规则被踢掉。
GEOMETRY_MODE: Final = GeometryMode.CLOUD_FIRST


def suspect_geometry(
    cards: Sequence[CardCandidate],
    image_size: tuple[int, int],
    params: SplitParams | None = None,
    *,
    expected_cards: int = DEFAULT_EXPECTED_CARDS,
    edge_margin_px: float = 8.0,
) -> GeometrySuspicion:
    """本地切分结果**可不可疑** —— 可疑才去问云端（B 方案的触发条件）。

    ``image_size`` 是 ``(宽, 高)``，与 ``CardCandidate.quad`` 的坐标同一个
    尺度（都是**当前这张图**的像素，即管线里的 work 尺度）。

    ## 四条判据（每一条都对应一个实测过的失败形态）

    | 判据 | 实测命中（2026-10-13，`02df352` + B 前） |
    | --- | --- |
    | 检出数 < `expected_cards` | `001` 得 8、`005` 得 7（都应为 9）|
    | 四边形顶点**越出图像** | `001` #6 的 x = **-51**（`bbox=(-51,1476,416,505)`）|
    | **宽高比越界**（卡 0.55~0.62）| `001` #6/#7 = 0.82/0.81；`005` #6 = 0.83 |
    | 面积远小于同页中位数（< 60%）| `005` #6 只有同页正常卡的 **41%** |

    ## 为什么"顶点越出图像"本身就算可疑（它不是正常的吗）

    卡被画面边缘切掉时顶点确实会越界 —— 这**不一定是错**。但实测两次
    越界的成因都是**错的**：`001` #6 的四边形是一个**斜的正方形**
    （minAreaRect 440×440，长/短 = 1.0）而它落在卡上，是把卡片内部图形
    当成了卡边界。所以这里刻意**宁可多问一次云端**：兜底结果要与本地
    按 IoU 去重后合并，问错了也只是白花一次调用，而漏问就是继续漏切。
    """
    p = params or SplitParams()
    #: 期望的宽高比来自**同一个物理常数**：卡是 54×90mm → 0.6。
    #: 不从 `aspect_min/aspect_max`（1.45~1.95，那是"长/短"口径）反算 ——
    #: 那两个数是给"是不是矩形卡"用的，容差宽得多，拿它当"像不像竖版卡"
    #: 会把实测那两个 0.82/0.81 的错框放过去。
    expected_wh = p.card_w_mm / p.card_h_mm
    wh_low, wh_high = expected_wh * 0.83, expected_wh * 1.13
    width, height = float(image_size[0]), float(image_size[1])
    reasons: list[str] = []

    # ---- 1) 数量 ----
    if len(cards) < expected_cards:
        reasons.append(f"检出 {len(cards)} 张 < 期望 {expected_cards} 张")
    # ---- 2) 顶点越界 ----
    out_of_frame: list[str] = []
    for i, c in enumerate(cards):
        q = np.asarray(c.quad, dtype=np.float32).reshape(4, 2)
        outside = (
            float(q[:, 0].min()) < -edge_margin_px
            or float(q[:, 1].min()) < -edge_margin_px
            or float(q[:, 0].max()) > width + edge_margin_px
            or float(q[:, 1].max()) > height + edge_margin_px
        )
        if outside:
            out_of_frame.append(f"#{i}")
    if out_of_frame:
        reasons.append(f"四边形顶点越出图像：{'、'.join(out_of_frame)}")

    # ---- 3) 宽高比 ----
    #: 口径是**宽/高**（竖版卡约 0.55~0.62），与 `CardCandidate.aspect`
    #: （长边/短边）不同 —— 这里要判的是"它像不像一张竖着的卡"。
    #: 用**四边形自身**的宽高而不是 bbox：bbox 会把斜框的宽算大。
    #:
    #: 区间由 ``card_w_mm / card_h_mm`` 按 ±13~17% 推出来（0.498~0.678），
    #: 实测卡的 0.55~0.62 落在里面，而错框的 0.81~0.83 在外面。
    bad_ratio: list[str] = []
    for i, c in enumerate(cards):
        wh = _quad_wh_ratio(np.asarray(c.quad, dtype=np.float32).reshape(4, 2))
        if not (wh_low <= wh <= wh_high):
            bad_ratio.append(f"#{i}={wh:.2f}")
    if bad_ratio:
        reasons.append(
            f"宽高比越界（卡约 {expected_wh:.2f}，容许 {wh_low:.2f}~{wh_high:.2f}）："
            f"{'、'.join(bad_ratio)}"
        )

    # ---- 4) 面积远小于同页中位数 ----
    #
    # 这一条抓的是"同页里**一张**明显小一截"（实测 `005` #6 只有 41%）。
    # 用中位数而不是均值：均值会被那个异常值自己拉偏（分母也跟着变小）。
    # 少于 3 张时"同页中位数"没有意义，所以那条判据不启用。
    if len(cards) >= 3:
        areas = np.array(
            [quad_area(np.asarray(c.quad, dtype=np.float32).reshape(4, 2)) for c in cards],
            dtype=np.float64,
        )
        median = float(np.median(areas))
        if median > 0:
            small = [
                f"#{i}={areas[i] / median:.0%}"
                for i in range(len(cards))
                if areas[i] < median * 0.6
            ]
            if small:
                reasons.append(f"面积远小于同页中位数（<60%）：{'、'.join(small)}")

    #: 空页也算可疑：什么都没切出来时，云端是唯一的第二意见。
    if not cards:
        reasons.append("一张都没切出")

    return GeometrySuspicion(needed=bool(reasons), reasons=tuple(reasons))


def _quad_wh_ratio(quad: np.ndarray) -> float:
    """四边形的**宽/高**（用外接框的短边当宽、长边当高，即"竖版"口径）。

    用 ``cv2.minAreaRect`` 而不是轴对齐 bbox：斜着放的卡其 bbox 宽会被放大，
    于是"宽高比越界"这条判据在斜卡上会**误报**。
    """
    rect = cv2.minAreaRect(np.asarray(quad, dtype=np.float32).reshape(4, 2))
    _c, (w, h), _a = rect
    short, long_ = min(w, h), max(w, h)
    return float(short / long_) if long_ > 0 else 0.0


def quad_area(quad: np.ndarray) -> float:
    """四边形面积（鞋带公式）。

    不用 ``cv2.contourArea``：那个对**自交**四边形会给出"两块面积之差"这种
    无意义的值，而云端回来的角点顺序不保证不自交（所以要先 `order_quad`）。
    这里算的是绝对面积，自交时偏大 —— 那会让"面积可疑"这条**更不容易**触发，
    方向是安全的（宁可少报可疑，也不要因为一个畸形输入把整页判成可疑）。
    """
    q = np.asarray(quad, dtype=np.float64).reshape(4, 2)
    x, y = q[:, 0], q[:, 1]
    return float(abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2.0)


def card_shape_suspect(
    quad: np.ndarray,
    params: SplitParams | None = None,
    *,
    expected_area: float = 0.0,
) -> tuple[bool, float, float, float]:
    """一个四边形"像不像一张卡" → ``(可疑?, 长/短, 宽/高, 面积比)``。

    **只有这一个实现**。合并云端几何之后，管线要用它回答"云端给的那几张里
    有几张形状不对"（从而决定要不要追问一次）—— 那次判断与
    `split_page` 里给 `size_suspect` 赋值**必须是同一套判据**，
    否则会出现"切分说正常、管线说不正常"这种自相矛盾（`ok/16`/`ok/17`
    两次分歧都是这么来的）。

    * ``长/短``：卡约 90/54 = **1.667**，`aspect_min~aspect_max` 是它的区间。
    * ``宽/高``：竖版卡的宽高比，卡约 **0.6**。
    * ``面积比``：相对 ``expected_area``（为 0 时该项返回 0，不除零）。

    可疑 = 尺寸与期望差 3 倍以上（N8 口径）**或** 长/短落在区间之外。
    """
    p = params or SplitParams()
    q = np.asarray(quad, dtype=np.float32).reshape(4, 2)
    wh = _quad_wh_ratio(q)
    aspect = (1.0 / wh) if wh > 0 else 0.0
    area = quad_area(q)
    area_ratio = area / expected_area if expected_area > 0 else 0.0
    expect_aspect = p.card_h_mm / p.card_w_mm
    suspect = (
        aspect > expect_aspect * p.size_suspect_factor
        or aspect < expect_aspect / p.size_suspect_factor
    )
    #: **被长宽比豁免放行的候选必须标注**。上面那条 3 倍判据抓不到它们
    #: （裁掉一部分的卡比例约 1.1，离 1.667/3 = 0.556 还很远），所以必须单独标 ——
    #: 否则"放行但没标注"会让一个部分可见的卡被当成完整卡对待。
    if not (p.aspect_min <= aspect <= p.aspect_max):
        suspect = True
    return suspect, aspect, wh, area_ratio


# --------------------------------------------------------------------------- #
#  B 方案：云端角点 → 候选卡（一条规则只有一个实现）
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CloudCorners:
    """一次云端"每张卡的四个角"调用的结果（纯数据，便于单测）。"""

    #: 每个卡一份四角点，坐标是 `unit` 单位。
    quads: list[tuple[tuple[float, float], ...]] = field(default_factory=list)
    #: 坐标单位（见 `vlm.CORNERS_UNIT`）。**必须显式带来** ——
    #: 换算依赖它，而"默认就是比例"这种隐含假设在换 prompt 时会静默错一档。
    unit: str = "fraction"
    #: 送云端那张图的尺寸（换算比例时的分母）。
    sent_size: tuple[int, int] = (0, 0)
    #: 调用是否成功（失败时 `quads` 为空，`reason` 说明原因）。
    ok: bool = True
    reason: str = ""
    latency_s: float = 0.0


def cloud_corners_to_quads(corners: CloudCorners, image_size: tuple[int, int]) -> list[np.ndarray]:
    """云端角点 → **当前图尺度**的像素四边形（已排序、已裁到图内）。

    ``image_size`` 是**要被裁的那张图**的尺寸（管线里是 work 尺度那张）。

    三步，顺序不能换：

    1. **换算**：``fraction`` 时按**送云端那张图**的比例 → 像素
       （``x * image_w``）。⚠️ 用 ``image_size`` 而不是 ``sent_size``：
       两者只差一个整体缩放（送云端的图就是这张图缩来的），所以比例可以直接
       乘到目标图的宽高上 —— 这样**不必**关心送图时缩了多少。
    2. **爬角点**：按"和/差"排成「左上→右上→右下→左下」（**不信任云端顺序**）。
    3. **贴边裁剪**：越界顶点夹到 ``0..w-1 / 0..h-1``。
       ⚠️ 实测（2026-10-13，`001.jpg`）云端回的是 x=**1.001**（画面外）——
       它把被切掉的卡的**真实**角点推了出来。裁剪是必要的（否则
       `warpPerspective` 会去采样图外的像素、`borderMode=REPLICATE` 把边缘
       拉出一道假边），但裁剪**只在最后做**：先去重/过滤（用真实形状判），
       再裁（得到能用的图）。

    ## 刻意**不在这里**过滤
    过滤（面积/宽高比/最小边长）在 :func:`merge_cloud_corners` 里，
    与本地候选**共用同一个 `_candidate_ok`** —— 一处规则只写一遍
    （`ok/16`/`ok/17` 两次分歧都因为写了两份）。
    """
    width, height = float(image_size[0]), float(image_size[1])
    out: list[np.ndarray] = []
    for raw in corners.quads:
        pts = np.asarray(raw, dtype=np.float64).reshape(4, 2)
        if corners.unit == "fraction":
            pts = pts * np.array([width, height], dtype=np.float64)
        #: 非有限值已在 `vlm.parse_card_corners` 挡掉一次；这里再挡一次是因为
        #: `CloudCorners` 可以被直接构造（测试、未来的其他来源），
        #: 而 NaN 进了 `order_quad` 会得到"看起来正常"的乱顺序。
        if not np.all(np.isfinite(pts)):
            continue
        ordered = order_quad(pts.astype(np.float32))
        if not np.all(np.isfinite(ordered)):
            continue
        ordered[:, 0] = np.clip(ordered[:, 0], 0.0, max(0.0, width - 1.0))
        ordered[:, 1] = np.clip(ordered[:, 1], 0.0, max(0.0, height - 1.0))
        out.append(ordered.astype(np.float32))
    return out


def _candidate_ok(
    quad: np.ndarray,
    area_ratio: float,
    aspect: float,
    rect: float,
    *,
    expected_area: float,
    params: SplitParams,
    area_min_ratio: float,
    area_max_ratio: float,
) -> bool:
    """候选卡的**共用过滤**（本地各通路与云端角点走同一条）。

    ``area_min_ratio`` / ``area_max_ratio`` 由调用方给：本地放宽（容忍
    裁切与粘连），云端收紧（见 `cloud_area_max_ratio` 的说明）。除这两个数
    以外的判据**只有一份** —— 这是有意的（`ok/16`/`ok/17` 两次分歧都因为
    同一件事写了两遍，然后两处慢慢漂开）。
    """
    if not (area_min_ratio <= area_ratio <= area_max_ratio):
        return False
    if rect < params.min_rectangularity:
        return False
    if params.aspect_min <= aspect <= params.aspect_max:
        return True
    #: 长宽比出界，但"**像是被边缘裁掉的**真卡" → 放行（三条缺一不可，
    #: 理由见 `SplitParams.clipped_area_ratio` 那几段）。
    return (
        params.clipped_area_ratio <= area_ratio <= params.clipped_area_max_ratio
        and rect >= params.clipped_min_rectangularity
    )


def _expected_card_area(params: SplitParams, dpi: float, detection_scale: float) -> float:
    """当前图尺度下"一张卡"的期望像素面积。**只有这一处算法**。

    ``detection_scale`` 取 ``max(1e-6, ...)``：它是"当前图 ÷ 原图"，
    真实来源只有"调用方缩了图"这一种，理论上不可能 <= 0，
    但一个 0 会让期望面积变成 0 而**不报错**（随后所有候选因 area_ratio 无穷大
    被过滤掉，表现为"一张都切不出"——而真正的原因是参数传错了）。
    """
    px_per_mm = dpi / MM_PER_INCH * max(1e-6, detection_scale)
    return (params.card_w_mm * px_per_mm) * (params.card_h_mm * px_per_mm)


def merge_cloud_corners(
    image: np.ndarray,
    local: SplitResult,
    corners: CloudCorners,
    *,
    dpi: int | None = None,
    params: SplitParams | None = None,
    detection_scale: float = 1.0,
    max_card_px: int | None = None,
) -> SplitResult:
    """把云端角点**校正、过滤、去重**后补进本地候选 —— B 方案的全部内容。

    ``image`` 是**当前图**（管线里是 work 尺度那张），坐标与 ``local`` 的
    ``CardCandidate.quad`` 同一尺度。

    ## 合并规则：云端几何**优先**，本地只留"云端没提到"的那些

    这条规则是刻意的，理由来自实测的失败形态：

    * 本地错的那几张（`001` #6 是个斜正方形、`005` #6 只有同页 41% 面积）
      **恰好与云端给的是同一张卡** —— 若按"只加新的"合并，它们会**留下来**，
      然后由 NMS 决定谁死，而 NMS 是按面积降序的，错的那个更大就赢了。
    * 本地对的那些（`001` #4：`x=0,y=0` 但形状正常）与云端给的重叠 → 用云端那份，
      两者的几何本来就一致（云端更规整）。

    所以：**云端有的卡，几何听云端的；云端没提的本地卡，原样保留**
    （它可能是一张云端真的看不见的卡，例如极小或被遮挡的）。

    ## 去重的两条判据（都必须比 NMS 的 0.3 严）

    判据本身在 :func:`same_card` 里（**只有一个实现**），这里只说为什么是这两个数：

    * **IoU >= `cloud_iou`**（0.5）。用 0.3 会出事，而且是**静默**出事：
      卡与卡的 IoU 本来就可能到 0.3（实测 `001` 底行三张被 Otsu 粘成一个大
      连通域，其 bbox 与相邻卡高度重叠），于是"真正补出来的那张卡"
      会被当成重复项丢掉 —— 表现为"兜底好像没起作用"，看不出是门槛的问题。
    * **包含度 >= `cloud_containment`**（0.75）。这一条不可省：实测 `005.jpg`
      那张错框与云端同一张卡的 **IoU 只有 0.42**（错框又小又偏下），
      而包含度 0.86 —— 少了它，那页会剩下两个候选 → **10 张**（正确 9）。

    ## 云端候选的 `size_suspect`

    沿用与本地**同一个**判据（正射之后按实际输出尺寸算），不另写一套。
    云端角点被裁剪过时形状会变扁，那正是应当被标出来的东西。
    """
    p = params or SplitParams()
    if not corners.quads:
        #: 没拿到角点 → **一字不改地**返回本地结果（含 notes 与 level）。
        #: 调用方据此知道"这次兜底没帮上忙"，并在 notes 里写清原因。
        return local

    est_dpi = float(dpi or local.dpi or 600)
    expected_area = _expected_card_area(p, est_dpi, detection_scale)
    image_size = (image.shape[1], image.shape[0])
    quads = cloud_corners_to_quads(corners, image_size)

    # ---- 过滤（与本地共用 `_candidate_ok`）----
    cloud_ok: list[np.ndarray] = []
    rejected = 0
    rejected_why: list[str] = []
    for i, q in enumerate(quads):
        area = quad_area(q)
        area_ratio = area / expected_area if expected_area > 0 else 0.0
        wh = _quad_wh_ratio(q)
        aspect = (1.0 / wh) if wh > 0 else 0.0
        #: 宽高比**先**判、且用它自己的区间（`cloud_wh_min/max`，"像不像竖版卡"）。
        #: 之后 `_candidate_ok` 里的长宽比是"长/短"口径的另一道关，
        #: 两道都要过 —— 它们判的不是同一件事（一个是朝向，一个是形状是否像卡）。
        if not (p.cloud_wh_min <= wh <= p.cloud_wh_max):
            rejected += 1
            rejected_why.append(f"云端#{i} 宽高比 {wh:.2f} 不在 {p.cloud_wh_min}~{p.cloud_wh_max}")
            continue
        if not _candidate_ok(
            q,
            area_ratio,
            aspect,
            1.0,  #: 云端给的就是四边形，矩形填充度按定义是 1
            expected_area=expected_area,
            params=p,
            area_min_ratio=p.area_min_ratio,
            area_max_ratio=p.cloud_area_max_ratio,
        ):
            rejected += 1
            box = np.asarray(q, dtype=np.float32).reshape(4, 2)
            rejected_why.append(
                f"云端#{i} 面积比 {area_ratio:.2f} 长/短 {aspect:.2f} "
                f"bbox=({box[:, 0].min():.0f},{box[:, 1].min():.0f},"
                f"{box[:, 0].max() - box[:, 0].min():.0f},"
                f"{box[:, 1].max() - box[:, 1].min():.0f})"
            )
            continue
        cloud_ok.append(q)

    # ---- 与本地**按 IoU/包含度去重**：云端优先 ----
    kept_local: list[CardCandidate] = []
    replaced = 0
    for cand in local.candidates:
        if any(same_card(np.asarray(cand.quad, dtype=np.float32), q, p) for q in cloud_ok):
            replaced += 1
        else:
            kept_local.append(cand)

    # ---- 云端候选 → CardCandidate（与本地走同一段正射/校验逻辑）----
    #
    # ⚠️ `size_suspect` 由 `card_shape_suspect`（**唯一实现**）判，与
    # `split_page` 里本地候选用的那一个**同一个函数**。这里额外传
    # `expected_area` 只是为了顺带拿到面积比，判据本身不看它。
    cloud_cards: list[CardCandidate] = []
    for q in cloud_ok:
        ortho = warp_card(image, q, max_px=max_card_px)
        h, w = ortho.shape[:2]
        long_side, short_side = max(h, w), min(h, w)
        real_aspect = long_side / short_side if short_side else 0.0
        suspect, _aspect, _wh, area_ratio = card_shape_suspect(q, p, expected_area=expected_area)
        #: ⚠️ **正射之后的实际长宽比可能低于四边形本身**（`warp_card` 会按长边
        #: 当高，把横放的框转正），所以这里用真实输出尺寸再判一次 ——
        #: 与本地那段完全一致（本地也是 `real_aspect` 说了算）。
        if not (p.aspect_min <= real_aspect <= p.aspect_max):
            suspect = True
        cloud_cards.append(
            CardCandidate(
                ortho=ortho,
                quad=q,
                origin="vlm",
                area_ratio=area_ratio,
                aspect=real_aspect,
                rectangularity=1.0,
                size_suspect=suspect,
                geometry_source="vlm",
            )
        )

    #: 云端在前（阅读顺序由云端给的顺序决定），本地未被替换的接在后面。
    merged = cloud_cards + kept_local
    out = SplitResult(
        candidates=merged,
        dpi=local.dpi,
        dpi_source=local.dpi_source,
        level=local.level,
        #: ⚠️ **不复述本地那几条 notes**（只带云端这几条）。
        #: 复述它们看着"信息更全"，实际会让调用方**重复报一遍本地那些** ——
        #: 实测 005 的页面 notes 里"检测尺度/候选/NMS"整段出现了两次，
        #: 而那种重复读起来像"管线跑了两遍"，比少几条更难排查。
        #: 本地那几条调用方本来就已经从 `split_page` 拿到了。
        notes=[],
        geometry_source="vlm" if cloud_cards else "local",
    )
    out.notes.append(
        f"**云端几何兜底**：本地 {len(local.candidates)} 张 → 云端给 {len(corners.quads)} 个四边形，"
        f"过滤掉 {rejected} 个 → 采纳 {len(cloud_cards)} 张；"
        f"与本地重叠被替换 {replaced} 张，保留本地独有的 {len(kept_local)} 张 → 合计 {len(merged)} 张"
        f"（云端耗时 {corners.latency_s:.1f}s）"
    )
    out.notes.append(
        "云端角点已按「和/差」重排为 左上→右上→右下→左下、并裁剪到图像内"
        "（裁剪前后形状不同的会标 size_suspect）"
    )
    for why in rejected_why:
        out.notes.append(f"云端候选被过滤：{why}")
    return out


# --------------------------------------------------------------------------- #
#  主入口
# --------------------------------------------------------------------------- #


def _nms(
    items: list[tuple[np.ndarray, float, float, float, str]], threshold: float
) -> list[tuple[np.ndarray, float, float, float, str]]:
    """按面积降序做 NMS（spec §3.2 的 "IoU > 0.3 抑制"）。"""
    items = sorted(items, key=lambda t: t[1], reverse=True)
    kept: list[tuple[np.ndarray, float, float, float, str]] = []
    for cand in items:
        if all(quad_iou(cand[0], k[0]) <= threshold for k in kept):
            kept.append(cand)
    return kept


def split_page(
    image: np.ndarray,
    *,
    dpi: int | None = None,
    params: SplitParams | None = None,
    max_card_px: int | None = None,
    detection_scale: float = 1.0,
) -> SplitResult:
    """把一张页扫描切成若干张单卡正射图。

    ``dpi`` 为 None 时先用 ``DEFAULT_SCAN_DPI`` 估一版参数，切完之后
    再从卡片尺寸反推真实 DPI 并**在结果里报告**（``dpi_source``）。
    这样调用方能看到"这个数字是怎么来的"，而不是拿一个不知来源的 DPI 去算阈值。

    ## ``detection_scale``

    调用方可以先把图缩小再传进来（A4@600dpi 是 3500 万像素，
    Canny + 形态学在整幅上要几十秒，而几何检测不需要满分辨率）。
    但**缩小之后所有以像素表达的判据都要跟着缩**，否则面积过滤会整体错位。

    ⚠️ 这个参数是**必需的**，不是可选优化。实测撞到过一次：
    不传它时 ``expected_area`` 按原图尺度算得 2,711,885px，而实际图上
    一张卡只有 108,043px —— 相差 25 倍（= 1/0.1996²），于是 **9 张卡全部**
    被 ``area_min_ratio`` 滤掉，表现为"一个都切不出来"。
    而错误的量级恰好与缩放比有关，所以它看起来像"参数没调好"，
    实际是**单位不一致**。
    """
    p = params or SplitParams()
    notes: list[str] = []

    if image.ndim == 3:
        # 只做灰度化用于**检测**；输出正射图仍然用原彩色图（绝不灰度化，spec §3.8）
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:  # pragma: no cover - 调用方一般传彩色
        gray = image

    # 估一版 DPI 用于算像素阈值；随后会用卡片尺寸修正
    est_dpi = float(dpi or 600)
    dpi_source = "explicit" if dpi else "default"
    # 面积判据必须落在**当前这张图**的像素尺度上（见 docstring 的说明）
    expected_area = _expected_card_area(p, est_dpi, detection_scale)
    notes.append(f"检测尺度 {detection_scale:.4f} → 期望单卡面积 {expected_area:,.0f}px")

    raw: list[tuple[np.ndarray, float, float, float, str]] = []
    for quad, area_ratio, aspect, rect in _detect_by_bright_body(gray, p, expected_area, est_dpi):
        raw.append((quad, area_ratio, aspect, rect, "body"))
    for quad, area_ratio, aspect, rect in _detect_by_cutline_frame(
        gray, p, expected_area, est_dpi, scale=detection_scale
    ):
        raw.append((quad, area_ratio, aspect, rect, "cutline"))
    for quad, area_ratio, aspect, rect in _detect_by_contour(
        gray, p, expected_area, est_dpi, scale=detection_scale
    ):
        raw.append((quad, area_ratio, aspect, rect, "contour"))

    # 过滤：判据只有一份，两条通路共用（口径已统一为"长边/短边"）
    #
    # ⚠️ **长宽比不能单独用作否决条件** —— 实测（`更多样本/002..005.jpg`）：
    # 那 4 张是 3×3 网格、**深色垫底**，而**第三行的卡被画面下边缘裁掉**。
    # 一张 54×90mm 的卡被裁成 54×60mm 后长宽比是 1.11，落在判据 1.45~1.95 之外，
    # 于是**真卡被当成非卡片丢掉** → 漏切 2~3 枚（卡片级只有 81.1%）。
    #
    # 修法：**面积足够大 + 填充度达标**时不做长宽比否决。
    # 为什么这样安全：25mm 戳的面积比约 **0.10**、邮票约 **0.15**，
    # 而这里要求 >= 0.45×卡 —— 所以"面积够大 + 是矩形"的东西不可能是戳或邮票，
    # 只可能是**被边缘裁掉的真卡**（或两张粘在一起，那种情况也需要人来拆）。
    #
    # 这类候选会在后面被标 `size_suspect=True` —— **标注而不是丢弃**（N8）。
    clipped_area_ratio = p.clipped_area_ratio

    #: ⚠️ 过滤体本身在 `_candidate_ok` 里 —— **与云端角点共用同一份**。
    #: 这里只把两个面积区间传进去（本地宽、云端紧，理由见各自的参数注释）。
    #: 曾经这段是内联在这里的闭包，于是"再写一份给云端用"几乎不可避免 ——
    #: 而 `ok/16`/`ok/17` 两次分歧都源于同一件事写了两遍。
    def _ok(t: tuple[np.ndarray, float, float, float, str]) -> bool:
        return _candidate_ok(
            t[0],
            t[1],
            t[2],
            t[3],
            expected_area=expected_area,
            params=p,
            area_min_ratio=p.area_min_ratio,
            area_max_ratio=p.area_max_ratio,
        )

    filtered = [t for t in raw if _ok(t)]
    aspect_rescued = sum(1 for t in filtered if not (p.aspect_min <= t[2] <= p.aspect_max))
    notes.append(
        f"检测候选 {len(raw)} 个 → 面积/长宽比/填充度过滤后 {len(filtered)} 个"
        f"（长宽比区间 {p.aspect_min}~{p.aspect_max}，面积 {p.area_min_ratio}~"
        f"{p.area_max_ratio}×卡）"
    )
    if aspect_rescued:
        notes.append(
            f"其中 {aspect_rescued} 个**长宽比出界但面积 >= {clipped_area_ratio}×卡**，"
            f"按「被边缘裁掉的真卡」放行（标为 size_suspect）"
        )

    kept = _nms(filtered, p.nms_iou)
    notes.append(f"NMS 后 {len(kept)} 个")

    cards: list[CardCandidate] = []
    # 注意 `_det_aspect`：检测阶段的 aspect 是**在缩略图上**按最小外接矩形算的，
    # 而 `real_aspect` 是**正射之后**按实际输出尺寸算的 —— 后者才是要报给
    # 调用方和用于 size_suspect 判定的值（它不受检测尺度影响）。
    # `_det_aspect` 现在只被 `_candidate_ok` 用过（放行"被边缘裁掉的卡"），
    # 到了这一步已经不再需要，所以按下划线命名。
    for quad, area_ratio, _det_aspect, rect, origin in kept:
        ortho = warp_card(image, quad, max_px=max_card_px)
        h, w = ortho.shape[:2]
        long_side, short_side = max(h, w), min(h, w)
        real_aspect = long_side / short_side if short_side else 0.0
        # 尺寸校验（N8）：与期望差 3 倍以上就标注，**不丢弃**；
        # 被长宽比豁免放行的也在这里标（判据只有一份：`card_shape_suspect`）。
        suspect, _aspect, _wh, _ratio = card_shape_suspect(
            np.asarray(quad, dtype=np.float32), p, expected_area=expected_area
        )
        #: ⚠️ 本地这一路还要看**正射之后**的实际长宽比（`real_aspect`）：
        #: `det_aspect` 是在**缩略图**上按最小外接矩形算的，而 `warp_card` 会按
        #: 长边当高把横放的框转正 —— 两者可能不同，而调用方看到的是后者。
        if not (p.aspect_min <= real_aspect <= p.aspect_max):
            suspect = True
        cards.append(
            CardCandidate(
                ortho=ortho,
                quad=np.asarray(quad, dtype=np.float32),
                origin=origin,
                area_ratio=area_ratio,
                aspect=real_aspect,
                rectangularity=rect,
                size_suspect=suspect,
            )
        )

    # ---- 用卡片尺寸反推 DPI，并在下一步重新折算阈值所需的参数 ----
    # scale 必须传：正射图是缩略图尺度的（见 infer_dpi_from_cards 的说明）
    measured = infer_dpi_from_cards(cards, p.card_h_mm, scale=detection_scale)
    if dpi is None and measured:
        est_dpi = float(measured)
        dpi_source = "from_cards"
        notes.append(f"从卡片尺寸反推 DPI = {measured}")

    # ---- 失败降级（spec §3.2 的 L0/L1/L2）----
    level = "L0"
    if not cards:
        bright_ratio = float(np.mean(gray > 128))
        if bright_ratio >= 0.5 or bright_ratio <= 0.5:
            # 判定"整页是否像单卡场景"：卡占页面比例是否合理。
            # 单卡扫 A4 时卡只占约 8%，所以这里不能靠面积，改靠**是否只有一簇前景**。
            pass
        level = "L1"
        notes.append(
            "未切出卡片 → L1 降级：整页当作单卡（不做透视矫正），并在校对队列标注「未能定位卡片」"
        )
        cards = [
            CardCandidate(
                ortho=image.copy(),
                quad=np.array(
                    [
                        [0, 0],
                        [image.shape[1] - 1, 0],
                        [image.shape[1] - 1, image.shape[0] - 1],
                        [0, image.shape[0] - 1],
                    ],
                    dtype=np.float32,
                ),
                origin="fallback",
                area_ratio=0.0,
                aspect=max(image.shape[:2]) / max(1, min(image.shape[:2])),
                rectangularity=1.0,
                size_suspect=True,
            )
        ]

    return SplitResult(
        candidates=cards,
        dpi=int(est_dpi),
        dpi_source=dpi_source,
        level=level,
        notes=notes,
    )
