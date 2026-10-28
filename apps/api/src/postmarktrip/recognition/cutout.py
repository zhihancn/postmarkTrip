"""抠图：戳贴片与票贴片（spec v1.0 §3.8）。

## 三条硬规定（都写成测试）

| 规定 | 为什么 |
| --- | --- |
| **绝不灰度化** | 彩色戳的颜色本身就是记录对象。实测样本里的彩色戳已经很淡（粉/绿/洋红、低饱和），任何灰度化都不可逆地丢掉信息 |
| **绝不自动校色** | 同上的反面：自动白平衡/锐化会改变"这枚戳盖得淡不淡"这个**事实**，而那个事实是鉴定依据 |
| **不缩放到固定尺寸** | 缩放会抹掉 DPI，而 DPI 是 mm↔px 换算的唯一依据（N7）。**保留原始像素 + 记录 dpi** |

## 为什么戳贴片要带 alpha 通道

"贴片"的用途是**叠在画廊/详情页上**。如果保留方形背景，贴片会带一圈
纸面颜色，叠在同色背景上还看不出来、叠在异色背景上就是一个方块。
所以戳贴片输出 **BGRA**，圆外透明。

票贴片则**保持矩形**——邮票是矩形，而且**齿孔必须保留**
（spec §3.4：不沿齿孔裁切。齿孔既是"这是邮票"的证据，也是不裁切的理由）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, Literal

import cv2
import numpy as np

from postmarktrip.recognition.postmark_type import Circle

logger = logging.getLogger("postmarktrip.recognition.cutout")

#: 写进元数据的管线版本
CUTOUT_PIPELINE_VERSION: Final = "p2-0.1.0"

CutoutKind = Literal["postmark", "stamp"]


@dataclass(slots=True)
class Cutout:
    """一张贴片。"""

    kind: CutoutKind
    #: **原始分辨率**的图。戳贴片是 BGRA（圆外透明），票贴片是 BGR。
    image: np.ndarray
    #: 在**原图**里的来源包围盒 (x, y, w, h)
    source_bbox: tuple[int, int, int, int]
    #: 采集时的 DPI。**必须记**：没有它就无法把贴片的像素换算回物理尺寸
    dpi: int | None
    #: 贴片自身的物理尺寸（mm）。由像素 + dpi 推算；dpi 未知时为 None。
    size_mm: tuple[float, float] | None
    method: str
    pipeline_version: str = CUTOUT_PIPELINE_VERSION

    @property
    def has_alpha(self) -> bool:
        return self.image.ndim == 3 and self.image.shape[2] == 4


def _mm(px: int, dpi: int | None) -> float | None:
    if not dpi:
        return None
    return round(px / dpi * 25.4, 2)


def cut_postmark(
    image: np.ndarray,
    circle: Circle,
    *,
    dpi: int | None = None,
    margin: float = 1.06,
    feather_px: int = 2,
) -> Cutout | None:
    """抠出圆形戳贴片（BGRA，圆外透明）。

    ``margin`` 略大于 1 是为了**含住外圈那一圈墨**（实测外圈在 r 处密度
    可达 1.00）；但也不能大太多，否则会带进卡片上的印刷内容。

    ``feather_px`` 给圆形边缘做 2px 羽化：**硬边会在叠图时产生锯齿**，
    而锯齿在深色背景上非常显眼（样本里就有黑色卡纸的用法）。
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
        # 灰度输入也要补成 3 通道 —— 但**不反过来做**（不把彩色变灰度）
        crop = np.stack([crop] * 3, axis=-1)
    if crop.shape[2] == 4:
        crop = crop[:, :, :3]
    bgr = np.ascontiguousarray(crop)

    # 圆形掩膜（带羽化）
    ch, cw = bgr.shape[:2]
    mask = np.zeros((ch, cw), np.uint8)
    cv2.circle(
        mask,
        (int(round(circle.cx - x0)), int(round(circle.cy - y0))),
        int(round(r)),
        255,
        -1,
    )
    if feather_px > 0:
        k = feather_px * 2 + 1
        # 显式 astype：GaussianBlur 的 stub 返回更宽的 dtype，
        # 而 alpha 通道必须是 uint8。不写的话 mypy 报错，
        # 写 `# type: ignore` 则会把"类型确实变宽了"这件事藏起来。
        mask = cv2.GaussianBlur(mask, (k, k), 0).astype(np.uint8)

    bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = mask
    return Cutout(
        kind="postmark",
        image=bgra,
        source_bbox=(x0, y0, x1 - x0, y1 - y0),
        dpi=dpi,
        size_mm=((_mm(bgra.shape[1], dpi) or 0.0, _mm(bgra.shape[0], dpi) or 0.0) if dpi else None),
        method=f"circular alpha mask, margin={margin}, feather={feather_px}px",
    )


def cut_stamp(
    image: np.ndarray,
    bbox: tuple[int, int, int, int],
    *,
    dpi: int | None = None,
    min_side: int = 16,
) -> Cutout | None:
    """抠出矩形票贴片（BGR，**保留齿孔**）。

    ## 为什么不沿齿孔裁切

    spec §3.4 明确要求保留齿孔。齿孔有**双重作用**：
    它既是"这是邮票"的证据，又是**不裁切的理由** ——
    沿齿孔裁掉后，贴片就与"随便一块矩形纸"无法区分。

    所以这里只按外接矩形裁，**不做任何沿边界的抠形**。
    """
    x, y, bw, bh = bbox
    h, w = image.shape[:2]
    x0 = int(max(0, x))
    y0 = int(max(0, y))
    x1 = int(min(w, x + bw))
    y1 = int(min(h, y + bh))
    if x1 - x0 < min_side or y1 - y0 < min_side:
        return None

    crop = image[y0:y1, x0:x1]
    if crop.ndim == 2:
        crop = np.stack([crop] * 3, axis=-1)
    if crop.shape[2] == 4:
        crop = crop[:, :, :3]
    bgr = np.ascontiguousarray(crop)
    return Cutout(
        kind="stamp",
        image=bgr,
        source_bbox=(x0, y0, x1 - x0, y1 - y0),
        dpi=dpi,
        size_mm=((_mm(bgr.shape[1], dpi) or 0.0, _mm(bgr.shape[0], dpi) or 0.0) if dpi else None),
        method="rectangular crop (perforations preserved)",
    )


def assert_colour_preserved(before: np.ndarray, after: np.ndarray) -> None:
    """自检：贴片**必须仍是彩色**。

    这个函数给管线做运行期断言用。判据是"三个通道是否**不完全相同**" ——
    灰度化后的图三通道逐像素相等，所以这是一个充分判据
    （反过来不成立：一张本来就是灰的戳会误报，所以只当断言不当过滤器）。

    它存在的理由：灰度化是一个**静默且不可逆**的操作。
    代码里加一行 ``cvtColor(..., GRAY2BGR)`` 就能毁掉所有彩色戳的证据，
    而测试如果不专门查这个，不会有任何东西报错。
    """
    a = before[:, :, :3] if before.ndim == 3 else before
    b = after[:, :, :3] if after.ndim == 3 else after
    if b.ndim != 3 or b.shape[2] < 3:
        raise AssertionError("贴片不是三通道 —— 彩色已被破坏")
    # 只在"原图确实是彩色"时检查
    if a.ndim == 3 and a.shape[2] >= 3:
        src_colour = not bool(np.all(a[:, :, 0] == a[:, :, 1]))
    else:
        src_colour = False
    if not src_colour:
        return
    dst_grey = bool(np.all(b[:, :, 0] == b[:, :, 2]))
    if dst_grey:
        raise AssertionError("原图是彩色但贴片变成了灰度 —— 违反 spec §3.8")
