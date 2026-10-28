"""戳型判别：日戳 / 风景戳（spec v1.0 §3.3）。

## 为什么这一项必须先做

实测推翻了初版设计的一个假设：**风景戳的文字可能不在圆环上**
（`need/03` §三.3：玉山纪念碑戳的名称横排在中部、晋祠天龙山戳在右侧竖弧）。
所以管线必须是「**先判戳型 → 再按戳型选文字区域提取策略**」，
而不是无脑极坐标展开。

## 判据（按实测可靠性排序）

| # | 判据 | 为什么可靠 | 本模块的函数 |
| --- | --- | --- | --- |
| **1** | **顶弧区域是否有文字** | 日戳必有省市名；风景戳顶弧为空（实测 3/3 成立） | `top_arc_text_response` |
| **2** | **中心区域的"图像性"** | 日戳中心是纯数字；风景戳是大面积线稿/色块 | `center_image_ness` |
| 3 | 时间码是否存在 | 日戳通常有；主题邮局戳无（有例外，仅作辅助） | 由 OCR 阶段提供 |

## 本模块的分工

* **几何**（圆定位、极坐标展开、区域掩膜）—— 纯 numpy/cv2，可单测
* **特征**（文字响应、图像性）—— 纯函数，输出可解释的数值
* **判定**（阈值）—— 单独一层，阈值由 `tools/samples/eval_stamp_type.py`
  在真实样本上标定，并把**准确率数字写进报告**（spec §3.3 明确要求
  "不能只说效果不错"）

## 绝对不做的事

**不用多模态大模型做这个判别**（spec §3.3 的硬规定）：
VLM 输出不可复现、给不出可信置信度，而且 `ok/02` §4.3 实测中文识别只有
24.4–72 分。判别是个分类任务，用 VLM 是杀鸡用牛刀且更不可靠。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

import cv2
import numpy as np

logger = logging.getLogger("postmarktrip.recognition.postmark_type")

#: 实测的邮戳直径（mm）。`need/03` §三.6 第 9 条：约 25mm。
POSTMARK_DIAMETER_MM: Final = 25.0

#: 圆校验的容差：``找到的半径 / 期望半径`` 必须落在这个区间内。
#:
#: 取 0.55~1.60 而不是更紧的值，理由是**实测样本的扫描质量差异很大**：
#: 盖得糊/缺墨时外圈会被侵蚀或晕开，半径估计本就有几个百分点的误差；
#: 而不同厂家/年份的邮戳直径也不是严格 25mm。
#: 更紧的区间会在真实样本上误杀（把好的圆判成 None），
#: 而更松则挡不住"抓到了邮票"（邮票与戳的尺寸可以很接近）。
DIAMETER_TOLERANCE_MIN: Final = 0.55
DIAMETER_TOLERANCE_MAX: Final = 1.60

#: 日戳 / 风景戳 / 无法判定
TYPE_DAILY: Final = "daily"
TYPE_SCENIC: Final = "scenic"
TYPE_UNKNOWN: Final = "unknown"


# --------------------------------------------------------------------------- #
#  圆定位
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Circle:
    """一个圆。``cx, cy, r`` 都是像素。"""

    cx: float
    cy: float
    r: float
    #: 拟合质量（0..1）。越高表示边界越连续 —— 盖得糊/缺墨的戳会低。
    quality: float

    @property
    def diameter(self) -> float:
        return self.r * 2


def find_postmark_circle(
    image: np.ndarray,
    *,
    expected_diameter_px: float | None = None,
    dpi: float | None = None,
) -> Circle | None:
    """找出邮戳的外圆。

    ## ``expected_diameter_px`` / ``dpi`` 不是可选优化，是必需的

    实测撞到过：不给期望直径时，在整页扫描图上会抓到 **r=1736px** 的圆 ——
    那不是戳，是**整张卡**（卡的轮廓比戳更"圆"也更完整）。
    而戳的直径只有 25mm，在 600dpi 下约 590px。

    所以必须有一个**尺寸先验**把搜索限制在戳的量级。给 ``dpi`` 时会自动
    按 ``POSTMARK_DIAMETER_MM`` 折算；也可以直接给像素值。

    ## 两条通路，取质量更高的

    * **Hough 圆**：对完整圆有效，但对缺墨断环敏感
    * **暗轮廓的圆拟合**：对断环更宽容

    为什么两条都要：邮戳是**盖印**不是印刷，缺墨、断环、边缘晕染都常见
    （样本里"盖的不好"那几张就是），而 Hough 对这类残缺很敏感。
    """
    gray = _to_gray(image)
    h, w = gray.shape[:2]

    if expected_diameter_px is None and dpi:
        expected_diameter_px = POSTMARK_DIAMETER_MM / 25.4 * float(dpi)

    # 戳是深色墨迹 → 二值化取暗区。Otsu 在"白纸 + 深墨"上是合适的
    # （与切分器不同：那里的困难是卡与纸**同色**，这里墨与纸差别很大）
    _, dark = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    candidates: list[Circle] = []

    # --- 路 1：Hough 圆 ---
    if expected_diameter_px:
        r_min = max(6, int(expected_diameter_px * 0.35))
        r_max = max(r_min + 4, int(expected_diameter_px * 0.75))
    else:
        r_min = max(8, int(min(h, w) * 0.12))
        r_max = max(r_min + 4, int(min(h, w) * 0.55))
    blurred = cv2.medianBlur(gray, 5)
    circles = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=max(20, min(h, w) // 3),
        param1=120,
        param2=45,
        minRadius=r_min,
        maxRadius=r_max,
    )
    if circles is not None:
        for cx, cy, r in circles[0]:
            q = _ring_ink_ratio(dark, cx, cy, r)
            candidates.append(Circle(float(cx), float(cy), float(r), q))

    # --- 路 2：暗轮廓的圆拟合（**只考虑尺寸在期望范围内的轮廓**）---
    #
    # ⚠️ 这里以前是"取面积最大的暗轮廓" —— 那在整页扫描上会拿到整张卡。
    # 现在改为"在尺寸合理的轮廓里取圆度最高的"。
    contours, _ = cv2.findContours(dark, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    best_fit: Circle | None = None
    for cnt in contours:
        area = float(cv2.contourArea(cnt))
        if area <= 0:
            continue
        (cx, cy), r = cv2.minEnclosingCircle(cnt)
        if r < r_min * 0.7 or (expected_diameter_px and r > r_max * 1.3):
            continue
        circularity = area / (np.pi * r * r) if r > 0 else 0.0
        # 圆环的"墨"只占外接圆的一小部分，所以圆度本身不会很高；
        # 真正区分戳与卡的是**圆周墨迹比例**，所以这里以它为主。
        ring = _ring_ink_ratio(dark, cx, cy, r)
        quality = float(np.clip(0.4 * circularity + 0.6 * ring, 0, 1))
        cand = Circle(float(cx), float(cy), float(r), quality)
        if best_fit is None or cand.quality > best_fit.quality:
            best_fit = cand
    if best_fit is not None:
        candidates.append(best_fit)

    if not candidates:
        return None

    def score(c: Circle) -> float:
        s = c.quality
        if expected_diameter_px:
            target = expected_diameter_px / 2
            # 半径偏离期望越多，扣得越狠：这一条就是"不要把整张卡当戳"的保证
            s -= abs(c.r - target) / max(target, 1) * 0.5
        return s

    best = max(candidates, key=score)
    if best is None or best.quality <= 0.02:
        return None

    # ---- 用几何不变量**校验**找到的圆（不只是拿它当搜索范围）----
    #
    # 实测教训：在这些"特写"样本上圆定位有时抓到**邮票**或别的圆，
    # 而不是邮戳。而戳型判别与文字区域**全都建立在"戳圈在哪"之上**，
    # 所以一个错的圆会让错误一路传下去，最后表现为"戳型判错"。
    #
    # 但"戳径 = 卡短边 × 25/54"是一条**硬几何**（两边都是物理常量），
    # 所以可以用它判断"这个圆到底是不是一枚戳"。
    #
    # **返回 None 比返回一个错的圆好得多**：None 是明确且可行动的
    # （"没找到戳" → 人工框选），而错的圆会被下游当成事实。
    if expected_diameter_px and expected_diameter_px > 0:
        target_r = expected_diameter_px / 2.0
        ratio = best.r / target_r
        if not (DIAMETER_TOLERANCE_MIN <= ratio <= DIAMETER_TOLERANCE_MAX):
            logger.debug(
                "圆被几何校验拒绝：r=%.0f 期望 %.0f（比值 %.2f 不在 %.2f~%.2f 内）",
                best.r,
                target_r,
                ratio,
                DIAMETER_TOLERANCE_MIN,
                DIAMETER_TOLERANCE_MAX,
            )
            return None
    return best


#: ⚠️ **未解决**：戳径的毫米换算不可信。
#:
#: 实测（2026-10-08）：圆定位在**像素层面是对的** —— 在
#: `清晰黑色日戳特写-1.jpg` 上判出 r=279px，而"12.5mm @600dpi"的期望值是
#: 273px，几乎重合。但那些**特写样本是裁剪过的卡片照**，其有效 DPI
#: 约 360（卡片 90mm 只占约 1275px）而不是 600，所以把 12.8mm 当成
#: "戳太细"是**误读**。
#:
#: 试过的修法与其代价（记在这里免得重复试）：
#:   * 「沿半径找最外侧环形峰」—— 让部分样本的半径变大（10.6→18.3mm），
#:     但**破坏了 6/6 的判别基线**（`盖的不好-墨太多` 的顶弧响应
#:     0.487→0.197，因为取到外圈后展开条带位置变了）。
#:     在没有更多标注的情况下**不能靠调参把它救回来** —— 那正是
#:     6 个标注样本上的过拟合。**已回退。**
#:
#: 结论：**要正确地报告物理尺寸，必须先知道每张图的真实 DPI**。
#: 对整页扫描可以从卡片尺寸反推（切分器已经这么做），对裁剪过的特写
#: 则没有可靠依据 —— 除非让用户告知扫描参数，或从图里再找一个已知尺寸的参照物。
#: 在那之前，`TypeVerdict` 的报告只用于**同一张图内的相对比较**，
#: 不能当物理量读。


def _ring_ink_ratio(dark: np.ndarray, cx: float, cy: float, r: float) -> float:
    """圆周上"有墨"的比例。

    沿圆周采样，看落在暗区的比例。完整实心圆环接近 1；
    断环/缺墨会低。这是**拟合质量**的直接度量，也是"这到底是不是一枚戳"
    的判据 —— 卡片上的邮票齿孔、二维码之类也会给出圆，但圆周墨迹比例很低。
    """
    if r < 3:
        return 0.0
    n = max(36, int(2 * np.pi * r / 2))
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False)
    xs = (cx + r * np.cos(angles)).round().astype(int)
    ys = (cy + r * np.sin(angles)).round().astype(int)
    h, w = dark.shape[:2]
    inside = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
    if not inside.any():
        return 0.0
    return float((dark[ys[inside], xs[inside]] > 0).mean())


# --------------------------------------------------------------------------- #
#  元素定位（spec 3.4）
#
#  spec 把"戳区域"与"票区域"分开处理。戳区域的定位**复用**上面的圆检测结果，
#  所以它不需要新算法；票区域则需要另一条判据（见 detect_stamp_region）。
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TextBand:
    """戳上"文字环"的径向范围（像素），以及它的墨迹密度。

    这个区间由 :func:`find_text_band` 从**实测的径向分布**得出，
    而不是写死一个比例 —— 因为戳有同心结构（内圈文字环 / 中部 / 外圈），
    而不同戳种的层次位置并不一致。
    """

    r_inner: float
    r_outer: float
    #: 该区间内的平均墨迹密度（0..1）。太低说明这段不是文字环。
    density: float


def find_text_band(
    image: np.ndarray,
    circle: Circle,
    *,
    inner_start: float = 0.95,
    inner_limit: float = 0.45,
    max_density: float = 0.45,
    min_density: float = 0.03,
) -> TextBand | None:
    """找出戳上**文字环**所处的半径区间。

    ## 两个被实测推翻的版本（写在这里免得再走一遍）

    **v1：写死比例 0.55~1.15 × 外圆半径。** 不行 —— 戳有同心结构
    （内圈文字环 / 中部 / 外圈），不同戳种的层次位置不一致。

    **v2：从外向内找第一段"密度 >= 0.25"的连续区段。** 也不行，而且错得很隐蔽：
    实测 `清晰黑色日戳特写-1.jpg` 的径向密度是
    「**实心外圈** r≈275 密度 **1.00**」+「**文字环** 密度 **0.04~0.15**」。
    于是 v2 抓到的是**那圈外圈本身**（r_inner=268, r_outer=289），
    展开出来的顶弧响应只有 **0.008** —— 取到的是一圈实心墨，不是文字。

    **根本原因**：我假设"文字环的周向墨迹密度高"。错了。
    文字只在圆周的**一部分角度上**有墨（一个省市名 4~6 个字），
    所以它的**周向平均密度反而低**；而实心外圈整周都是墨，密度接近 1。

    ## v3（当前）：外圈**内侧**、密度**中等**的那一段

    正确的判据是三个条件一起：

    1. 在实心外圈的**内侧** —— 先找密度最高的那个半径当外圈基准，
       再从 ``inner_start`` × r 开始往内找（不再从最外圈开始）
    2. 密度**不高** —— 排除实心外圈（``max_density``）
    3. 密度**不为零** —— 排除空白区（``min_density``）

    实测在 `清晰黑色日戳特写-1.jpg` 上：
    0.60~0.95×r 的顶弧响应 **0.741**，而 v2 抓的区间只有 **0.008**。
    """
    gray = _to_gray(image)
    _, dark = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    r_hi = max(4.0, circle.r * 1.10)
    r_lo = max(2.0, circle.r * inner_limit)
    radii = np.arange(r_lo, r_hi + 1.0, 1.0)
    if radii.size < 6:
        return None

    n_angles = 360
    angles = np.linspace(0, 2 * np.pi, n_angles, endpoint=False)
    cos_a, sin_a = np.cos(angles), np.sin(angles)
    h, w = dark.shape[:2]

    density = np.empty(radii.size, dtype=np.float64)
    for i, r in enumerate(radii):
        xs = (circle.cx + r * cos_a).round().astype(int)
        ys = (circle.cy + r * sin_a).round().astype(int)
        ok = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
        density[i] = float((dark[ys[ok], xs[ok]] > 0).mean()) if ok.any() else 0.0

    # 平滑，避免单像素噪声造成假区段
    smooth = np.convolve(density, np.ones(3) / 3.0, mode="same")

    # 先定位**实心外圈**：密度最高的那个半径。它给出"文字环从哪往内找"。
    ring_idx = int(np.argmax(smooth))
    if smooth[ring_idx] < 0.5:
        # 没有明显的实心外圈 —— 这枚戳可能盖得很淡，或者圆定位抓错了。
        # 此时退化为"整段找中等密度"，而不是硬套外圈内侧的逻辑。
        ring_idx = smooth.size - 1

    start = min(ring_idx - 1, int(np.searchsorted(radii, circle.r * inner_start)))
    start = max(0, min(start, smooth.size - 1))

    # 从 start 往内找第一段满足 min_density <= 密度 <= max_density 的连续区段
    i = start
    while i >= 0 and not (min_density <= smooth[i] <= max_density):
        i -= 1
    if i < 0:
        return None
    j = i
    while j > 0 and min_density <= smooth[j - 1] <= max_density:
        j -= 1
    if i == j:
        return None

    return TextBand(
        r_inner=float(radii[j]),
        r_outer=float(radii[i]),
        density=float(smooth[j : i + 1].mean()),
    )


def detect_stamp_region(
    image: np.ndarray, *, min_area_ratio: float = 0.004
) -> tuple[int, int, int, int] | None:
    """定位**邮票**区域，返回 ``(x, y, w, h)``。

    ## 判据：齿孔

    spec §3.4 给的是"矩形 + **齿孔特征**：沿候选矩形边界的一维投影呈周期性缺口"。
    **齿孔是邮票的判据，也是不裁切的理由** —— 这两个作用值得放在一起看：
    齿孔既是"这是邮票"的证据，又是"不能沿齿孔裁掉"的原因。

    ## 但它不是本模块的主力

    实测样本里邮票通常被**戳骑缝压住**（`need/03` §三.6 第 3 条，
    实测 3/3 样本如此），所以齿孔常常只有**两三边**可见。
    一个"要求四边都有齿孔"的实现会大面积失败。

    所以这里的策略是**降级**的：先找"面积合理 + 填满外接框 + 长宽比像邮票"
    的亮区候选，再看它边界上的周期性。做不到就返回 ``None`` ——
    由调用方（校对队列）让用户手动画框，而不是给一个错框。

    ⚠️ **这是本模块里唯一"未在真实样本上标定过"的部分**：样本里没有
    只含邮票的干净图，无法算准确率。所以它按"宁可返回 None"设计。
    """
    gray = _to_gray(image)
    h, w = gray.shape[:2]
    _, bright = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    page_area = float(h * w)

    best: tuple[float, tuple[int, int, int, int]] | None = None
    for cnt in contours:
        area = float(cv2.contourArea(cnt))
        if area < page_area * min_area_ratio or area > page_area * 0.6:
            continue
        x, y, bw, bh = cv2.boundingRect(cnt)
        if bw < 8 or bh < 8:
            continue
        fill = area / float(bw * bh)
        if fill < 0.75:
            continue
        portion = _perforation_score(gray, x, y, bw, bh)
        if portion <= 0:
            continue
        score = fill * portion * min(1.0, area / (page_area * 0.05))
        if best is None or score > best[0]:
            best = (score, (x, y, bw, bh))
    return best[1] if best else None


def _perforation_score(gray: np.ndarray, x: int, y: int, w: int, h: int) -> float:
    """边界上的"周期性缺口"程度（0..1）。

    沿四条边取一条带，看灰度沿边方向的起伏是否**周期性强**。
    齿孔的缺口间距大致均匀，而普通矩形边界是平的（无起伏）或随机（无周期）。

    做法：对每条边算一维剖面的方差，再算它的自相关在"常见齿孔间距"
    范围内是否有明显峰。这里用一个便宜且够用的近似：**起伏的方差 ×
    过零次数落在合理区间的程度**。
    """
    pad = 3
    y0, y1 = max(0, y - pad), min(gray.shape[0], y + h + pad)
    x0, x1 = max(0, x - pad), min(gray.shape[1], x + w + pad)
    band = gray[y0:y1, x0:x1]
    if band.size < 64:
        return 0.0

    edges = [
        band[0, :].astype(np.float64),
        band[-1, :].astype(np.float64),
        band[:, 0].astype(np.float64),
        band[:, -1].astype(np.float64),
    ]
    scores: list[float] = []
    for prof in edges:
        if prof.size < 16:
            continue
        prof = prof - prof.mean()
        std = prof.std()
        if std < 2.0:
            # 完全平直 —— 没有齿孔起伏
            continue
        # 过零次数：齿孔会带来规则的交替
        crossings = int(np.sum(np.diff(np.signbit(prof)) != 0))
        per_100 = crossings / max(1, prof.size) * 100
        # 经验区间：6~40 次/100px。落在区间内给分，越靠中间越高
        lo, hi = 6.0, 40.0
        if per_100 < lo or per_100 > hi:
            continue
        mid = (lo + hi) / 2
        shape = 1.0 - abs(per_100 - mid) / mid
        scores.append(float(np.clip(shape, 0, 1)) * min(1.0, std / 30.0))
    if not scores:
        return 0.0
    # 要求至少两条边有齿孔迹象（被戳压住的邮票常常只剩两三条边）
    if len(scores) < 2 and max(scores) < 0.5:
        return 0.0
    return float(np.mean(sorted(scores, reverse=True)[:3]))


# --------------------------------------------------------------------------- #
#  极坐标展开
# --------------------------------------------------------------------------- #


def _to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return image


def polar_unwrap(
    image: np.ndarray,
    circle: Circle,
    *,
    angle_from_deg: float = 0.0,
    angle_to_deg: float = 360.0,
    r_from_ratio: float = 0.55,
    r_to_ratio: float = 1.15,
    out_height: int = 96,
    out_width: int | None = None,
    max_width: int = 2400,
) -> np.ndarray:
    """把圆环的一段展开成矩形（极坐标 → 笛卡尔）。

    * ``angle_from/to``：角度区间。**0° 是正右方，90° 是正下方**
      （图像坐标 y 向下，与 cv2 的绘图约定一致）。所以：
        顶弧 = [225°, 315°]（以 270° 为中心，见 :func:`arc_angles`）
        底弧 = [45°, 135°]（以 90° 为中心）
    * ``r_from/to_ratio``：半径区间，相对 ``circle.r``。
    * ``out_width``：列数。**None 表示按弧长自动定**（见下）。

    ## ⚠️ 列数不能等于角度跨度（这是我犯过的错，两轮才定位）

    第一版令 ``列数 = 角度跨度``（90° → 90 列）。看着合理，实际是错的：
    **弧长 = 半径 × 弧度**，所以 90° 在 r=275px 处的弧长是 **431px**。
    90 列意味着**水平压缩约 5 倍**，字被压成一团。

    后果非常隐蔽：区域本身完全正常（几何对、位置对、响应值高），
    只是 **OCR 读不出任何东西** —— 看起来像"模型不行"，实际是采样率不足。

    修法：``列数 = 角度跨度(度) × π/180 × 半径``，即"**每像素一个采样**"。

    ## 行数也按物理尺寸给（同样重要）

    ``out_height`` 由调用方给，但调用方应当传 **band 的径向像素跨度** ——
    也就是"每像素一个采样"。实测：传固定的 96 而 band 跨 96px 时恰好等值，
    但如果 band 更宽就会被压缩，字会被挤扁（同样只表现为 OCR 读不出）。

    ## 为什么行数是调用方给的

    半径方向的跨度由调用方选的 band 决定，所以它最清楚该给多少行。
    这里不做二次推断，避免与 band 的语义打架。
    """
    gray = _to_gray(image)
    span_deg = abs(angle_to_deg - angle_from_deg)
    r0 = circle.r * r_from_ratio
    r1 = circle.r * r_to_ratio
    r_mid = max(1.0, (r0 + r1) / 2.0)

    if out_width is None:
        # 每像素一个采样：弧长 = r × θ
        out_width = int(round(np.deg2rad(span_deg) * r_mid))
    n_cols = int(np.clip(out_width, 16, max_width))

    angles = np.deg2rad(np.linspace(angle_from_deg, angle_to_deg, n_cols))
    radii = np.linspace(r0, r1, out_height)

    # 广播成 (out_height, n_cols) 的采样网格
    rr, aa = np.meshgrid(radii, angles, indexing="ij")
    xs = circle.cx + rr * np.cos(aa)
    ys = circle.cy + rr * np.sin(aa)

    h, w = gray.shape[:2]
    map_x = np.clip(xs, 0, w - 1).astype(np.float32)
    map_y = np.clip(ys, 0, h - 1).astype(np.float32)
    return cv2.remap(
        gray, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    )


def arc_angles(which: str) -> tuple[float, float]:
    """取某个弧的角度区间（度）。

    角度约定：**0° 正右，90° 正下**（图像坐标 y 向下）。
    所以顶弧的中心是 **270°**，底弧的中心是 **90°**。
    这个约定容易搞反 —— 搞反的后果是"日戳被看成风景戳"，
    而它不会报错，只会让准确率莫名偏低，所以单独做成函数并配测试。
    """
    if which == "top":
        return (225.0, 315.0)
    if which == "bottom":
        return (45.0, 135.0)
    raise ValueError(f"未知的弧：{which!r}（只支持 'top' / 'bottom'）")


# --------------------------------------------------------------------------- #
#  特征 1：顶弧的"文字响应"
# --------------------------------------------------------------------------- #


def text_response(strip: np.ndarray) -> float:
    """一条条带"像文字"的程度。

    spec §3.3 给的定义是「二值化后的**水平投影方差** + 连通域数量」。
    这里实现成两者的**归一化**组合，让输出的量级稳定、可跨样本比较：

    * ``投影方差`` —— 文字沿展开方向排布时，垂直投影会呈现周期性的峰谷；
      纯色区域或纯噪声的投影是平的。取归一化方差（除以均值平方）。
    * ``连通域数量`` —— 文字笔画会被二值化切成若干独立小块；
      空白区域没有，而大面积色块只有 1 个巨大块。

    **为什么必须归一化**：不归一化的话，"墨多"本身就会让方差大，
    于是风景戳里深色的图案会被判成文字。归一化后才是在问
    "这些墨是不是**排布成行**"。
    """
    if strip.size == 0:
        return 0.0

    # 条带内的局部对比度归一化：不同样本的墨色深浅差别很大。
    # 用 numpy 手算而不是 cv2.normalize —— 后者的重载签名把 dst 声明成
    # `Mat | ndarray`，传 None 会让 mypy 报错；而这里本来就只是一个
    # 线性拉伸，手写既清楚又不必绕类型。
    lo, hi = float(strip.min()), float(strip.max())
    if hi - lo < 1e-6:
        return 0.0  # 完全平坦：不可能是文字
    strip = ((strip.astype(np.float32) - lo) * (255.0 / (hi - lo))).astype(np.uint8)
    _, binary = cv2.threshold(strip, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # 沿**垂直**方向投影（每一列有多少墨）——
    # 展开后文字是水平排布，所以它的墨在垂直方向上是分散的、在水平方向上有节奏
    projection = (binary > 0).sum(axis=0).astype(np.float64)
    if projection.size < 4:
        return 0.0
    mean = projection.mean()
    if mean <= 0:
        return 0.0
    projection_variance = float(projection.var() / (mean * mean))

    # 连通域数量：只数"尺寸像笔画"的块（太大的是色块，太小的是噪声）
    n_labels, _labels, stats, _ = cv2.connectedComponentsWithStats(
        (binary > 0).astype(np.uint8), connectivity=8
    )
    component_area = binary.size
    strokes = 0
    for i in range(1, n_labels):
        a = stats[i, cv2.CC_STAT_AREA]
        if component_area * 0.0002 <= a <= component_area * 0.08:
            strokes += 1
    # 归一化成"每个字符大小的区域大约几个笔画"：用条带面积估字符数
    normalised_strokes = min(1.0, strokes / 12.0)

    # 方差项也压到 0..1：经验上"排布成行"的条带方差 > 0.5
    variance_term = min(1.0, projection_variance / 1.5)

    return float(0.6 * variance_term + 0.4 * normalised_strokes)


def top_arc_text_response(
    image: np.ndarray, circle: Circle, *, band: TextBand | None = None
) -> float:
    """判据 1：顶弧区域是否有文字。

    ⚠️ **实测推翻了这条判据的原始表述**（见 ok/10 §七之十五）：

    在 31 张真实卡片上逐张判读后发现，**这些风景戳的线描图正好占在顶弧位置**。
    而本函数量的是"顶弧有没有**墨迹结构**" —— 线描图的结构比文字还丰富，
    于是风景戳得到**高分**并被判成日戳。

    > "顶弧有文字"这条判据，**无法用"顶弧有墨迹结构"来实现**。
    > 这是判据的可实现性问题，不是阈值问题。

    所以本函数现在的定位是"顶弧的文字**密度**"，
    而它**不足以**单独区分文字与线描图。真正的区分需要
    "笔画级连通域的尺寸/数量分布"（文字是许多小、等高的块；
    线描图是少数大而不规则的块）—— 那是下一步的工作。

    ``band`` 可以传入已算好的文字环，避免重复计算（``classify_postmark``
    已经在判退化之前算过一次）。
    """
    if band is None:
        band = find_text_band(image, circle)
    if band is None:
        # 没有可靠的环 → 返回 0，由调用方负责把它当成"不确定"
        return 0.0
    a0, a1 = arc_angles("top")
    strip = polar_unwrap(
        image,
        circle,
        angle_from_deg=a0,
        angle_to_deg=a1,
        r_from_ratio=band.r_inner / max(circle.r, 1e-6),
        r_to_ratio=band.r_outer / max(circle.r, 1e-6),
    )
    return text_response(strip)


# --------------------------------------------------------------------------- #
#  特征 2：中心区域的"图像性"
# --------------------------------------------------------------------------- #


def center_image_ness(image: np.ndarray, circle: Circle, *, radius_ratio: float = 0.42) -> float:
    """判据 2：中心区域有多"像图像"（而不是像数字）。

    日戳中心是**一串数字**：笔画细、数量少、色相单一（黑）。
    风景戳中心是**线描图或色块**：连通域多、边缘方向杂、颜色多。

    三个子量，取归一化后的加权和：

    * ``颜色数`` —— 日戳的黑墨在灰度上只有一个峰；彩色风景戳有多个色相。
    * ``边缘方向熵`` —— 线描图各方向都有边缘；数字的笔画方向更集中。
    * ``填充率`` —— 色块/线稿的墨覆盖率高；一串数字的覆盖率低。
    """
    gray = _to_gray(image)
    h, w = gray.shape[:2]
    r = max(3.0, circle.r * radius_ratio)
    x0, x1 = int(max(0, circle.cx - r)), int(min(w, circle.cx + r + 1))
    y0, y1 = int(max(0, circle.cy - r)), int(min(h, circle.cy + r + 1))
    roi = gray[y0:y1, x0:x1]
    if roi.size < 16:
        return 0.0

    # 圆形掩膜：角落不属于中心区
    mask = np.zeros(roi.shape, np.uint8)
    cv2.circle(
        mask,
        (int(circle.cx) - x0, int(circle.cy) - y0),
        int(r),
        255,
        -1,
    )
    inside = mask > 0
    if inside.sum() < 16:
        return 0.0

    _, binary = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink = (binary > 0) & inside

    # --- 填充率 ---
    fill_ratio = float(ink.sum()) / float(inside.sum())

    # --- 边缘方向熵 ---
    edges = cv2.Canny(roi, 50, 150)
    edges[~inside] = 0
    ys, xs = np.nonzero(edges)
    entropy = 0.0
    if ys.size >= 8:
        gy = cv2.Sobel(roi, cv2.CV_32F, 0, 1, ksize=3)
        gx = cv2.Sobel(roi, cv2.CV_32F, 1, 0, ksize=3)
        angles = np.arctan2(gy[ys, xs], gx[ys, xs])
        # 方向折成 8 个箱（180° 周期性）
        bins = ((angles + np.pi) % np.pi / np.pi * 8).astype(int).clip(0, 7)
        counts = np.bincount(bins, minlength=8).astype(np.float64)
        p = counts / counts.sum()
        p = p[p > 0]
        entropy = float(-(p * np.log2(p)).sum() / 3.0)  # 归一化：log2(8)=3

    # --- 色相数（彩色戳）---
    hue_count = 0.0
    if image.ndim == 3:
        hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        sat = hsv[:, :, 1]
        coloured = (sat > 40) & inside
        if coloured.sum() > inside.sum() * 0.02:
            hues = hsv[:, :, 0][coloured]
            hist = np.bincount((hues // 20).astype(int).clip(0, 8), minlength=9).astype(np.float64)
            hist /= hist.sum()
            # 显著色相数：占比 > 8% 的箱
            hue_count = float(min(1.0, (hist > 0.08).sum() / 3.0))

    fill_term = min(1.0, fill_ratio / 0.35)
    return float(0.45 * fill_term + 0.35 * entropy + 0.20 * hue_count)


# --------------------------------------------------------------------------- #
#  判定层：阈值
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TypeThresholds:
    """判定的阈值。**必须对着真实样本标定**（spec §3.3 的硬要求）。

    下面这组是初始值，由 ``tools/samples/eval_stamp_type.py`` 标定后替换。
    标定出来的准确率数字必须写进报告 —— 「不能只说效果不错」。
    """

    #: 顶弧文字响应 > 它 → 判日戳
    top_arc_text: float = 0.34
    #: 顶弧没有文字、且中心图像性 > 它 → 判风景戳
    center_image: float = 0.42
    #: **文字环的最小有效宽度**（相对外圆半径）。窄于它就算"环定位不可靠"，
    #: 此时返回 `unknown` 而不是在薄片上做阈值判断。
    #:
    #: 实测依据：31 张真实卡片里 `find_text_band` 有 **7 张**返回退化值
    #: （None，或只有 `0.94~0.96 x r` 约 1% 半径宽的薄片）。
    #: 在那种薄片上算出的顶弧响应是**任意数字**，却会照常参与阈值比较
    #: → 产出一个"看起来正常、实际基于噪声"的判定。
    min_text_band_ratio: float = 0.12


@dataclass(frozen=True, slots=True)
class TypeVerdict:
    """一次判别的结果。**带全部中间量**，因为标定与排障都要看它们。"""

    kind: str
    #: 置信度 0..1。不等同于"准确率"，只用于 UI 高亮排序。
    confidence: float
    top_arc_text: float
    center_image: float
    #: 圆心与半径（像素）；None 表示没找到戳圈
    circle: Circle | None
    #: 人类可读的判定依据
    reason: str

    @property
    def is_unknown(self) -> bool:
        return self.kind == TYPE_UNKNOWN


def classify_postmark(
    image: np.ndarray,
    *,
    thresholds: TypeThresholds | None = None,
    dpi: float | None = None,
    expected_diameter_px: float | None = None,
) -> TypeVerdict:
    """判别一枚戳是日戳还是风景戳。

    决策顺序**就是判据的可靠性顺序**（spec §3.3）：
    先看顶弧有没有文字，再看中心像不像图像。
    两条都不成立时返回 ``unknown`` —— 而不是硬选一个。
    **"不确定"是一个合法的输出**：它让 UI 能把这一枚送去人工确认，
    而强行二选一只会把错误藏进统计里。

    ## 尺寸先验怎么给（二选一，``expected_diameter_px`` 优先）

    * ``expected_diameter_px`` —— **推荐**。当输入是"已知物理尺寸的卡片
      正射图"时，戳径是卡片尺寸的固定比例（25mm 戳 / 54mm 卡宽 ≈ 0.46），
      所以**不需要知道 DPI** 就能算出期望像素
    * ``dpi`` —— 按 ``POSTMARK_DIAMETER_MM`` 折算。**只在确实知道 DPI 时用**

    ## ⚠️ 给错 DPI 会让判别出错（实测踩到）

    实测：管线给"特写"样本传了 `dpi=600`，而该特写的有效 DPI 约 360。
    后果是戳圈搜索范围偏窄、找到的圆不同、**顶弧/中心特征值跟着变** ——
    一张明明是日戳的样本被判成 `scenic(0.56)`（用正确的尺度是 `daily(0.69)`）。

    错法很有迷惑性：**看起来像"判别器不准"，实际是尺度输入错了**。
    所以调用方应当优先用 ``expected_diameter_px``（它只依赖几何比例），
    只在 DPI 确实可信时才用 ``dpi``。
    """
    t = thresholds or TypeThresholds()
    circle = find_postmark_circle(image, dpi=dpi, expected_diameter_px=expected_diameter_px)
    if circle is None:
        return TypeVerdict(
            kind=TYPE_UNKNOWN,
            confidence=0.0,
            top_arc_text=0.0,
            center_image=0.0,
            circle=None,
            reason=(
                "未能定位戳的外圆（缺墨/断环严重，或戳不在画面里）。"
                "若调用方既没给 dpi 也没给 expected_diameter_px，"
                "搜索范围可能覆盖到整张卡。"
            ),
        )

    # ---- 文字环必须真的定位到了，否则后面的阈值判断是在噪声上做 ----
    #
    # ⚠️ 实测（31 张真实卡片，见 ok/10 §七之十五）：`find_text_band` 会返回
    # **退化**结果 —— 要么 None，要么宽到只有 `0.94~0.96 x r`（约 1% 半径宽）
    # 这样的薄片。31 张里有 **7 张**是这种。
    #
    # 在薄片上算 `top_arc_text` 得到的是一个**任意数字**，而它会照常参与
    # 阈值比较 → 产出一个**看起来正常、实际基于噪声**的型别判定。
    #
    # 所以：环定位不可靠时返回 `unknown`。**"不知道"比"基于噪声的答案"好** ——
    # 前者会让 UI 送去人工确认，后者会把错误藏进统计里。
    band = find_text_band(image, circle)
    min_band_ratio = t.min_text_band_ratio
    if band is None or (band.r_outer - band.r_inner) < circle.r * min_band_ratio:
        have = (
            "未找到"
            if band is None
            else (
                f"仅 {band.r_outer - band.r_inner:.0f}px 宽"
                f"（{band.r_inner / max(circle.r, 1e-6):.2f}~{band.r_outer / max(circle.r, 1e-6):.2f} x r）"
            )
        )
        return TypeVerdict(
            kind=TYPE_UNKNOWN,
            confidence=0.0,
            top_arc_text=0.0,
            center_image=0.0,
            circle=circle,
            reason=(
                f"文字环定位不可靠（{have}，要求 >= {min_band_ratio:.2f} x r）—— "
                f"在未知宽度的环上算顶弧响应只会得到任意数字，故返回「不确定」"
            ),
        )

    top = top_arc_text_response(image, circle, band=band)
    center = center_image_ness(image, circle)

    if top > t.top_arc_text:
        # 置信度：离阈值越远越有把握，封顶 0.95（判别本身不该宣称 100%）
        margin = min(1.0, (top - t.top_arc_text) / max(t.top_arc_text, 1e-6))
        return TypeVerdict(
            kind=TYPE_DAILY,
            confidence=round(0.5 + 0.45 * margin, 3),
            top_arc_text=top,
            center_image=center,
            circle=circle,
            reason=f"顶弧有文字（{top:.2f} > {t.top_arc_text}）→ 日戳",
        )

    if center > t.center_image:
        margin = min(1.0, (center - t.center_image) / max(t.center_image, 1e-6))
        return TypeVerdict(
            kind=TYPE_SCENIC,
            confidence=round(0.5 + 0.45 * margin, 3),
            top_arc_text=top,
            center_image=center,
            circle=circle,
            reason=(
                f"顶弧无文字（{top:.2f} ≤ {t.top_arc_text}）"
                f"且中心图像性强（{center:.2f} > {t.center_image}）→ 风景戳"
            ),
        )

    return TypeVerdict(
        kind=TYPE_UNKNOWN,
        confidence=0.0,
        top_arc_text=top,
        center_image=center,
        circle=circle,
        reason=(
            f"两条判据都不成立：顶弧文字 {top:.2f} ≤ {t.top_arc_text}，"
            f"中心图像性 {center:.2f} ≤ {t.center_image} → 交人工确认"
        ),
    )


__all__ = [
    "POSTMARK_DIAMETER_MM",
    "TYPE_DAILY",
    "TYPE_SCENIC",
    "TYPE_UNKNOWN",
    "Circle",
    "TextBand",
    "TypeThresholds",
    "TypeVerdict",
    "arc_angles",
    "center_image_ness",
    "classify_postmark",
    "detect_stamp_region",
    "find_postmark_circle",
    "find_text_band",
    "polar_unwrap",
    "text_response",
    "top_arc_text_response",
]


# --------------------------------------------------------------------------- #
#  笔画级特征：区分"文字"与"线描图"（A3 的实现性修复）
# --------------------------------------------------------------------------- #


def stroke_text_score(strip: np.ndarray) -> float:
    """条带里"像文字笔画"的程度（0..1）。

    # ⚠️ 试过了，**没有帮上忙** —— 保留是为了让这个否定结论可复现

    实测（`tools/samples/a3_feature_ab.py`，9 张我逐张判读过标签的卡片）：

    | 特征 | daily | scenic | 最佳单阈值准确率 |
    | --- | --- | --- | --- |
    | `text_response`（旧的墨迹结构） | 0.394 / 0.777 / 0.833 / 0.904 | 0.524 / 0.539 / 0.633 / 0.633 / 0.800 | **77.8%** |
    | `stroke_text_score`（本函数） | 0.335 / 0.55 / 0.65 / 0.65 | 0.35 / 0.577 / 0.65 / 0.65 / 0.745 | **55.6%** |

    **两者都重叠，而本函数更差。**

    ## 为什么它没帮上忙（我的判断，未进一步验证）

    1. **值堆积**：输出里大量卡片恰好等于 `0.650`，说明三个子项在真实数据上
       同时饱和 —— 满足条件的卡都落到同一个值上，**区分信息丢了**
    2. **前提可能不成立**：我假设"文字 = 许多小等高块、线描图 = 少数大块"，
       但这些风景戳是**线描画**（细线条组成），它的连通域**也是许多小块** ——
       所以按"块的尺寸分布"区分不了**线描画**与文字。
       真正能区分的可能是"**笔画走向的一致性**"（汉字有横竖撇捺的规律方向，
       线描画的线条方向更自由），那是另一个方向的特征

    **所以 A3 仍未解决**，而这次尝试排除了一条路。若要继续，
    应当换到"笔画方向分布"而不是"块的尺寸分布"。

    ---

    下面是原设计说明（**其前提已被上表推翻，留作记录**）：

    ## 为什么必须换（实测，见 ok/10 §七之十五 与 ok/09 E-15）

    `text_response` 量的是"有没有墨迹结构"，而**风景戳的线描图正好占在顶弧**，
    它的结构比文字还丰富 → 风景戳得高分 → 被判成日戳。

    **"顶弧有文字"无法用"顶弧有结构"来实现。** 换任何阈值都无解，
    因为实测日戳的该特征分布在 0.41~0.90，与风景戳的线描图区间重叠。

    ## 判据：**块的尺寸分布**，不是块的多少

    | | 文字 | 线描图 |
    | --- | --- | --- |
    | 连通域数量 | **多** | 少 |
    | 单个尺寸 | **小** | 大 |
    | 高度一致性 | **高**（同一字号） | 低（线条、色块大小不一） |
    | 宽高比 | 接近 1（方块字） | 变化大 |

    所以本函数组合三个量：

    1. ``块数量`` —— 尺寸合理的连通域个数（归一化到"一行大约几个字"）
    2. ``高度一致性`` —— 这些块高度的**变异系数倒数**（文字接近同高）
    3. ``小块的面积占比`` —— 文字由许多小块组成，线描图由少数大块组成

    **为什么用"变异系数倒数"而不是方差**：不同样本的字号不同，
    绝对方差不可比；除以均值就变成了尺度无关的"齐不齐"。
    """
    if strip.size == 0:
        return 0.0

    # 与 text_response 同样的对比度归一化（墨色深浅跨样本差别大）
    lo, hi = float(strip.min()), float(strip.max())
    if hi - lo < 1e-6:
        return 0.0
    norm = ((strip.astype(np.float32) - lo) * (255.0 / (hi - lo))).astype(np.uint8)
    _, binary = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    n_labels, _labels, stats, _ = cv2.connectedComponentsWithStats(
        (binary > 0).astype(np.uint8), connectivity=8
    )
    band_area = float(binary.size)
    heights: list[float] = []
    small_area = 0.0
    big_area = 0.0
    for i in range(1, n_labels):
        a = float(stats[i, cv2.CC_STAT_AREA])
        h = float(stats[i, cv2.CC_STAT_HEIGHT])
        # 太小的是噪点（不是笔画），太大的是整片图/大色块
        if a < band_area * 0.0005:
            continue
        if a > band_area * 0.30:
            big_area += a
            continue
        heights.append(h)
        small_area += a

    if not heights:
        return 0.0

    # 1) 块数量：一行展开后的顶弧大约容纳 4~8 个汉字，取 8 作归一
    count_term = min(1.0, len(heights) / 8.0)

    # 2) 高度一致性：变异系数越小越像同一字号。CV=0 → 1；CV>=0.6 → 0
    mean_h = float(np.mean(heights))
    cv = float(np.std(heights)) / mean_h if mean_h > 0 else 1.0
    consistency_term = float(np.clip(1.0 - cv / 0.6, 0.0, 1.0))

    # 3) 小块占比：文字几乎全由小块组成；线描图会有大块
    total_ink = small_area + big_area
    small_term = small_area / total_ink if total_ink > 0 else 0.0

    return float(0.40 * count_term + 0.35 * consistency_term + 0.25 * small_term)


def top_arc_stroke_score(
    image: np.ndarray, circle: Circle, *, band: TextBand | None = None
) -> float:
    """顶弧的**笔画级**文字得分（:func:`stroke_text_score` 的顶弧版本）。"""
    if band is None:
        band = find_text_band(image, circle)
    if band is None:
        return 0.0
    a0, a1 = arc_angles("top")
    strip = polar_unwrap(
        image,
        circle,
        angle_from_deg=a0,
        angle_to_deg=a1,
        r_from_ratio=band.r_inner / max(circle.r, 1e-6),
        r_to_ratio=band.r_outer / max(circle.r, 1e-6),
    )
    return stroke_text_score(strip)
