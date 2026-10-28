"""文字区域提取：按戳型分支（spec v1.0 §3.5）。

## 为什么必须分两条路（这是本版最重要的管线决定）

实测推翻了初版假设：**风景戳的文字可能不在圆环上**
（`need/03` §三.3：玉山纪念碑戳的名称横排在中部、晋祠天龙山戳在右侧竖弧）。
所以无脑做极坐标展开，对风景戳会**展开出一片空白** —— 而且不会报错，
只会让 OCR 拿到没有文字的图，表现为"识别率莫名很低"。

| 戳型 | 策略 | 依据 |
| --- | --- | --- |
| **日戳** | 顶弧 + 底弧**极坐标展开** | 日戳的文字确实在环上（顶弧省市名、底弧局名戳号） |
| **风景戳** | **名称行定位**（中部/右侧的横向或竖向文字块） | 实测其文字不在环上 |

## 关于"DPI 不可信"（见 postmark_type 的说明）

这一步**不受那个问题影响**：文字环用 `find_text_band` 的**像素**半径、
展开也用像素，两者同一尺度，比值不受 DPI 影响。
毫米数字只用于对外报告，不参与这里的计算。

## 产物

每个区域都带：
* ``image`` —— 展开后的条带（喂给 OCR 的东西）
* ``bbox`` —— 在**原图**里的位置。**必须有**：校对队列要能画出
  "这一块是从哪取的"，否则用户无法判断 OCR 结果该不该信
* ``text_response`` —— 该区域像文字的程度，用于排序与"存疑高亮"（D2）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, Literal

import cv2
import numpy as np

from postmarktrip.recognition.postmark_type import (
    POSTMARK_DIAMETER_MM,
    TYPE_DAILY,
    TYPE_SCENIC,
    Circle,
    TextBand,
    TypeVerdict,
    _to_gray,
    arc_angles,
    find_text_band,
    polar_unwrap,
    text_response,
)

logger = logging.getLogger("postmarktrip.recognition.text_regions")

#: 区域的语义标签。**故意做得细**：校对队列要按标签分组显示，
#: 而"顶弧省市名"与"底弧局名戳号"对用户的含义完全不同。
RegionKind = Literal[
    "top_arc",  # 日戳顶弧（省市名）
    "bottom_arc",  # 日戳底弧（局名 + 戳号）
    "name_line_h",  # 风景戳：横向名称行
    "name_line_v",  # 风景戳：竖向名称（右侧或左侧竖弧文字）
]

#: 卡片物理宽度（mm）。用于**从卡片像素尺寸反推戳径先验** ——
#: 这样就不需要知道 DPI（见 :func:`postmark_diameter_hint_px`）。
CARD_W_MM: Final = 54.0
CARD_H_MM: Final = 90.0

#: 卡片长宽比的容差。正射图的比例接近 90/54 = 1.667 时，
#: 才认为"这是一张完整的卡"，戳径比例才成立。
CARD_ASPECT: Final = CARD_H_MM / CARD_W_MM
CARD_ASPECT_TOL: Final = 0.18


def postmark_diameter_hint_px(image: np.ndarray, *, fallback_fraction: float = 0.46) -> float:
    """从**图像自身的尺寸**估出戳的外径（像素）。

    ## 为什么不需要 DPI

    如果输入是"一张完整的卡片"，那它的物理尺寸是已知的（54×90mm），
    而邮戳直径也是已知的（``POSTMARK_DIAMETER_MM`` = 25mm）。
    于是：

        dpi = 卡宽像素 / 54mm
        戳径像素 = 25mm × dpi = 卡宽像素 × 25/54 ≈ 卡宽像素 × 0.463

    **D 因子被消掉了。** 这解决了实测那个"传错 DPI 导致戳型判错"的问题
    （见 ``classify_postmark`` 的说明）—— 只要输入是卡片正射图，
    尺寸先验就可以完全不依赖 DPI。

    输入不是完整卡（比例偏离）时退回 ``fallback_fraction × 短边``：
    那是一个更松的先验，但比"完全不设范围"好得多（后者会在整页扫描上
    抓到整张卡，实测 r=1736px）。
    """
    h, w = image.shape[:2]
    if h < 8 or w < 8:
        return 0.0
    aspect = max(h, w) / max(1, min(h, w))
    if abs(aspect - CARD_ASPECT) <= CARD_ASPECT_TOL:
        # 竖卡：宽是短边；横卡：高是短边。戳径按**短边**折算
        return float(min(h, w)) * (POSTMARK_DIAMETER_MM / CARD_W_MM)
    return float(min(h, w)) * fallback_fraction


#: 风景戳里"找名称行"时，中心区取多大比例（相对外圆半径）。
#: 名称通常落在中部，所以取中间一段而不是整个圆。
SCENIC_CENTER_RATIO: Final = 0.85

#: 风景戳名称行定位是否已验证通过。
#:
#: **当前是 False**，`find_text_lines` 因此直接返回空 —— 见它的 docstring：
#: 试过的两版判据在真实风景戳上都不可用，而留下的版本只会产出**假区域**。
#: 假区域比空结果有害：OCR 会去读图案、产出乱码、污染置信度统计（spec §3.7）。
#:
#: 改成 True 之前必须先满足：在真实风景戳样本上量出准确率，且假阳性可接受。
_SCENIC_LINES_VERIFIED: Final = False

#: **主路径**：把整个戳区域（外圆外扩一点 + 白边）整块交给 OCR。
#:
#: ## 为什么这才是主路径（实测推翻了"必须先切条带"的设计）
#:
#: 原设计是"极坐标展开成顶弧/底弧条带，再逐条 OCR"。实测它能取到对的像素
#: （肉眼看展开图能读出「福建南平」），但 **OCR 只读出 2 个字**：「平南」「山」。
#: 试过加宽窗口（±45~±105）、逐列剪切去旋转（k=0~0.9）、放大 2 倍、二值化
#: —— **全都不行**。
#:
#: 换成"整块戳区域 + 白边，一次 OCR"，同一批样本：
#:
#: | 样本 | 整块 OCR 结果 |
#: | --- | --- |
#: | 清晰黑色日戳特写-1 | `福建南火`(0.90) · **`2026.09.25.15`(0.96)** · `建溪`(0.99) |
#: | 盖的不好-墨太多 | `牡丹江`(0.89) · **`2026.10.06.17`(0.98)** · `四号航站耀`(0.75) |
#: | 盖的不好-糊掉了 | `老南平`(0.73) · **`20 26 09.25.16`(0.97)** |
#: | 单张卡整张扫描图 | **`2026.10.01.09`(0.99)** · `三清`(1.00) |
#:
#: **日期字段几乎全对。** 根因：条带把字切成碎片后，检测网络失去了
#: "这是一枚戳、这些字属于同一枚戳"的上下文；整块喂进去时，
#: 检测器能在**自然比例**下把行找出来。
#:
#: 所以条带路径**降级为辅助**（也保留给"以图搜戳"取规整贴片用）。
POSTMARK_MARGIN: Final = 1.10

#: 整块区域的白色留白比例。与 ``ocr._prepare`` 同理：
#: 区域被字填满时检测网络会把笔画当成巨大块。
POSTMARK_PAD_RATIO: Final = 0.12


def extract_postmark_region(
    image: np.ndarray,
    circle: Circle,
    *,
    margin: float = POSTMARK_MARGIN,
    pad_ratio: float = POSTMARK_PAD_RATIO,
) -> tuple[np.ndarray, tuple[int, int, int, int]] | None:
    """把整个戳区域（外圆外扩 ``margin`` 倍 + 白色留白）交出来。

    返回 ``(带留白的图, 原图 bbox)``。

    **``margin`` 的取舍**（实测）：1.0~1.15 干净；**1.4 会把明信片上印刷的
    "中国邮政 CHINA POST"/"美丽中国"一起读进来** —— 会污染字段抽取
    （"中国邮政"可能被误当成地名）。所以默认 1.10。
    """
    if circle.r < 4:
        return None
    h, w = image.shape[:2]
    r = circle.r * margin
    x0 = int(max(0, circle.cx - r))
    x1 = int(min(w, circle.cx + r))
    y0 = int(max(0, circle.cy - r))
    y1 = int(min(h, circle.cy + r))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    crop = image[y0:y1, x0:x1]
    if crop.ndim == 2:
        crop = np.stack([crop] * 3, axis=-1)

    ch, cw = crop.shape[:2]
    py = max(8, int(ch * pad_ratio))
    px = max(8, int(cw * pad_ratio))
    canvas = np.full((ch + 2 * py, cw + 2 * px, 3), 255, np.uint8)
    canvas[py : py + ch, px : px + cw] = crop
    return canvas, (x0, y0, x1 - x0, y1 - y0)


@dataclass(slots=True)
class TextRegion:
    """一块被判为"含文字"的区域。"""

    kind: RegionKind
    #: 展开/裁剪后的图像（喂给 OCR）
    image: np.ndarray
    #: 在**原图**里的包围盒 (x, y, w, h)。校对队列用它画"取自何处"。
    bbox: tuple[int, int, int, int]
    #: 像文字的程度（0..1）
    text_response: float
    #: 该区域是**横向**还是**竖向**排布。OCR 据此决定要不要转 90°。
    #: 展开后的环是横向的；风景戳的竖弧文字是竖向的。
    orientation: Literal["horizontal", "vertical"]
    note: str = ""


def _arc_bbox(
    circle: Circle, a0: float, a1: float, r0: float, r1: float, shape: tuple[int, int]
) -> tuple[int, int, int, int]:
    """一个环形扇区在**原图**里的包围盒。

    做法是把扇形外接矩形的四个角点算出来再取包围盒 —— 比"按角度范围取
    x/y 极值"简单，而且在跨 0°/360° 时不会算错。
    """
    angles = np.deg2rad([a0, a1, (a0 + a1) / 2.0, a0 - 2.0, a1 + 2.0])
    xs: list[float] = []
    ys: list[float] = []
    for a in angles:
        for r in (r0, r1):
            xs.append(circle.cx + r * np.cos(a))
            ys.append(circle.cy + r * np.sin(a))
    h, w = shape
    x0 = int(np.clip(min(xs), 0, w - 1))
    x1 = int(np.clip(max(xs) + 1, 1, w))
    y0 = int(np.clip(min(ys), 0, h - 1))
    y1 = int(np.clip(max(ys) + 1, 1, h))
    return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))


def extract_arc_regions(
    image: np.ndarray, circle: Circle, *, band: TextBand | None = None
) -> list[TextRegion]:
    """日戳：把顶弧与底弧分别展开成横向条带。

    半径区间取 ``band``（由 :func:`find_text_band` 从实测分布得出）。
    拿不到 band 时返回空列表 —— **而不是回落到一个写死的比例**：
    写死比例在同心结构不同的戳上会让条带偏到圈外，
    而那种错误产出的"空白条带"会被当成"这枚戳没文字"。
    """
    band = band or find_text_band(image, circle)
    if band is None:
        return []

    # ⚠️ 这里曾经把 band 的半径区间原样当 out_height 传下去（默认 96），
    # 结果在 `清晰黑色日戳特写-1.jpg` 上展开出 90×96 的**一片灰噪声** ——
    # 而那个环上明明有「福建南平」。
    #
    # 根因不是采样密度，而是**圆心偏了**：检测到的圆心落在邮票下沿附近，
    # 半径也比真实外圈小（叠加图上能直接看到）。截取位置跟着圆心错，
    # 所以取到的是一块没有文字的灰度区域。
    # 详见 docs/ok/09 的 E-09 与 ok/10 §七之四。
    #
    # 采样密度仍然按"每像素至少一个采样"给足 —— 这是**必要但不充分**的修正：
    # 它让几何正确的输入不被降采样毁掉，但不能补救错的圆心。
    out_rows = max(64, int(round(band.r_outer - band.r_inner)) + 1)

    out: list[TextRegion] = []
    for which, kind in (("top", "top_arc"), ("bottom", "bottom_arc")):
        a0, a1 = arc_angles(which)
        strip = polar_unwrap(
            image,
            circle,
            angle_from_deg=a0,
            angle_to_deg=a1,
            r_from_ratio=band.r_inner / max(circle.r, 1e-6),
            r_to_ratio=band.r_outer / max(circle.r, 1e-6),
            out_height=out_rows,
        )
        out.append(
            TextRegion(
                kind=kind,  # type: ignore[arg-type]
                image=strip,
                bbox=_arc_bbox(circle, a0, a1, band.r_inner, band.r_outer, image.shape[:2]),
                text_response=text_response(strip),
                orientation="horizontal",
                note=(
                    f"极坐标展开：{which}弧，半径 {band.r_inner:.0f}~{band.r_outer:.0f}px"
                    f"（由径向墨迹分布得出，密度 {band.density:.2f}）"
                ),
            )
        )
    return out


def find_text_lines(
    image: np.ndarray,
    circle: Circle,
    *,
    center_ratio: float = SCENIC_CENTER_RATIO,
    min_response: float = 0.45,
) -> list[tuple[tuple[int, int, int, int], float]]:
    """在圆的中心区里找**成行**的文字块，返回 ``[(bbox, response), ...]``。

    ## ⚠️ 这个函数当前**不可用**（未在真实风景戳上成功过）

    实测：两张风景戳样本（`风景戳特写(来源-求戳信)`、`彩色戳特写`）
    走这条路都得到 **0 个区域**。也就是说**风景戳的文字区域提取还没做通**。

    ### 试过的判据与它为什么错

    **v1："中心区里连续的高密度带就是文字行"** ——
    在合成图上它会把**整个 ROI 的一半**当成一个带（因为环形边缘在行/列投影上
    形成两个高值，而它们之间被连续填满），响应高达 0.667。
    在真实风景戳上则一个带都找不到 —— 同一个判据在两种情形下都不对。

    根因：**风景戳的中心是图案，图案在投影上也是"连续的高值"**。
    所以"连续高值"无法区分"文字行"与"图案"。

    ### 下一步该怎么做（写在这里，避免下一轮重复摸索）

    spec §3.5 对 B 类文字（融入图案的书法）给的方向是
    **"以图搜戳"而不是 OCR**（`need/03` §三.5）。也就是说：
    晋祠天龙山那类戳的名称**本来就不该指望用投影切出来**，
    它该走向量检索。而玉山纪念碑那类"横排名称"是真正的 OCR 目标 ——
    对它的判据应当是**笔画的连通域尺寸分布**（汉字有稳定的
    宽高比与笔画数），而不是投影的连续性。

    ### 所以现在**直接返回空**（``return []``）

    上面那条判据的两个版本实测都不可用：

    * v1（中位数一半）在真实风景戳上找不到带，在合成图上把 ROI 的一半当成带
    * v2（0.75×中位数 + 带长 <= 22% ROI）在**空白区+细圆环**的合成图上
      仍然产出假带（响应 0.667）—— 因为细圆环在列投影上是两个稀疏高值，
      它们之间的空档被算成了"带"

    与其留一个**只会产出假区域**的版本，不如明确返回空：
    空结果让校对队列显示"未找到文字区域"、用户手动画框；
    假区域会让 OCR 去读图案，产出乱码并污染置信度统计（spec §3.7）。

    ``min_response`` 与投影逻辑**保留**（下面那段不删），
    因为下一轮实现"连通域尺寸分布"判据时还要用它做基线对照。
    """
    gray = _to_gray(image)
    h, w = gray.shape[:2]
    r = max(4.0, circle.r * center_ratio)
    x0 = int(max(0, circle.cx - r))
    x1 = int(min(w, circle.cx + r + 1))
    y0 = int(max(0, circle.cy - r))
    y1 = int(min(h, circle.cy + r + 1))
    roi = gray[y0:y1, x0:x1]
    if roi.size < 64:
        return []

    #: 明确的开关：判据未验证通过前不放行任何区域。
    #: 改成 True 之前必须先在高阈值下量出真实风景戳上的准确率。
    if not _SCENIC_LINES_VERIFIED:
        logger.debug(
            "风景戳名称行定位尚未验证（_SCENIC_LINES_VERIFIED=False），返回空。ROI=%dx%d",
            roi.shape[1],
            roi.shape[0],
        )
        return []

    _, dark = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink = dark > 0

    results: list[tuple[tuple[int, int, int, int], float]] = []
    for orientation in ("h", "v"):
        oriented = ink if orientation == "h" else ink.T
        profile = oriented.sum(axis=1).astype(np.float64)
        if profile.max() < 3:
            continue
        smooth = np.convolve(profile, np.ones(3) / 3.0, mode="same")
        positive = smooth[smooth > 0]
        if positive.size == 0:
            continue
        thr = max(2.0, 0.75 * float(np.median(positive)))
        on = smooth >= thr

        i = 0
        n = int(on.size)
        while i < n:
            if not on[i]:
                i += 1
                continue
            end = i
            while end + 1 < n and on[end + 1]:
                end += 1
            start = i
            length = end - start + 1
            i = end + 1
            # 带必须"窄而长"：太宽的是整片图案，太窄的是笔画
            if length < 6 or length > oriented.shape[0] * 0.22:
                continue
            if orientation == "h":
                band = roi[start : end + 1, :]
                bbox = (x0, y0 + start, roi.shape[1], length)
            else:
                band = roi[:, start : end + 1]
                bbox = (x0 + start, y0, length, roi.shape[0])
            resp = text_response(band)
            if resp >= min_response:
                results.append((bbox, resp))
    return results


def extract_text_regions(
    image: np.ndarray,
    verdict: TypeVerdict,
    *,
    band: TextBand | None = None,
) -> list[TextRegion]:
    """**主入口**：按戳型挑提取策略，返回按"像文字程度"降序的区域。

    分派表就是本模块开头那张表。``TYPE_UNKNOWN`` 时**两个策略都跑**，
    并把结果按响应排序 —— 因为"判别不出来"恰恰是最需要多给几个候选的时候；
    此时谎称"没文字"比多给两块区域有害得多。
    """
    if verdict.circle is None:
        return []

    circle = verdict.circle
    regions: list[TextRegion] = []

    # 判别不出来时**两个策略都跑** —— 那正是最需要多给几个候选的时候。
    # 谎称"没文字"比多给两块区域有害得多（多给的可以人工忽略，漏掉的找不到）。
    dual = verdict.kind not in (TYPE_DAILY, TYPE_SCENIC)

    if verdict.kind == TYPE_DAILY or dual:
        regions.extend(extract_arc_regions(image, circle, band=band))

    if verdict.kind == TYPE_SCENIC or dual:
        for bbox, resp in find_text_lines(image, circle):
            bx, by, bw, bh = bbox
            sub = _to_gray(image)[by : by + bh, bx : bx + bw]
            h, w = sub.shape[:2]
            regions.append(
                TextRegion(
                    kind="name_line_h" if w >= h else "name_line_v",
                    image=sub,
                    bbox=bbox,
                    text_response=resp,
                    # 竖向文字要告诉 OCR 转 90°：不转的话它会把每个字当成
                    # 一个独立的噪声块 —— 而它不会报错，只会给空结果
                    orientation="horizontal" if w >= h else "vertical",
                    note="风景戳名称行定位（中心区投影 + 连续带）",
                )
            )

    regions.sort(key=lambda r: r.text_response, reverse=True)
    return regions
