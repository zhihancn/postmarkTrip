#!/usr/bin/env python3
"""切分诊断：把每条通路的中间态落盘，并打印连通域统计。

**为什么需要它**：切分失败的形态是"数字不对"，而数字不对的原因有十几种
（阈值错了、面积过滤掉了、闭运算把卡粘在一起了……）。只看最终张数无法区分。
这个工具把每一步的掩膜/边缘/连通域统计摊开。

用法::

    python tools/samples/debug_split.py 黑色背景.jpg
    python tools/samples/debug_split.py 多张卡混排扫描图.jpg --long-side 1400
    # 输出写到 /tmp/splitdebug/<样本名>/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.split import (
    MM_PER_INCH,
    SplitParams,
    mm_to_px,
    sauvola_threshold,
)


def load(path: Path) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"读不出：{path}")
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("name", help="样本文件名（在 docs/need/pic/ 下）")
    ap.add_argument("--long-side", type=int, default=1400)
    ap.add_argument(
        "--samples", type=Path, default=None, help="样本目录（默认仓库的 docs/need/pic）"
    )
    ap.add_argument("--out", type=Path, default=Path("/tmp/splitdebug"))
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[4]
    samples = args.samples or repo / "docs" / "need" / "pic"
    path = samples / args.name
    if not path.is_file():
        print(f"✗ 找不到 {path}", file=sys.stderr)
        return 1

    outdir = args.out / path.stem
    outdir.mkdir(parents=True, exist_ok=True)

    img = load(path)
    h, w = img.shape[:2]
    scale = args.long_side / max(h, w)
    small = (
        cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else img
    )
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    p = SplitParams()
    dpi = 600.0  # 诊断时用名义值；真实值和它差几倍只影响像素阈值

    print(f"样本：{args.name}")
    print(f"  原图      {w}×{h}")
    print(f"  检测尺寸  {small.shape[1]}×{small.shape[0]}（缩放 {scale:.4f}）")
    print(f"  名义 DPI  {dpi:.0f}")
    print()

    # ---- 灰度直方图峰值（判断底色与是否有双峰）----
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    top = np.argsort(hist)[::-1][:5]
    print("  灰度直方图峰值 top5：", ", ".join(f"{int(v)}({int(hist[v])})" for v in top))
    print()

    # ---- 通路 A：Otsu ----
    otsu_val, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cv2.imwrite(str(outdir / "A1_otsu_mask.png"), otsu)
    k_close = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (max(3, mm_to_px(p.close_kernel_mm, dpi) * 4) | 1,) * 2
    )
    closed = cv2.morphologyEx(otsu, cv2.MORPH_CLOSE, k_close, iterations=2)
    cv2.imwrite(str(outdir / "A2_otsu_closed.png"), closed)

    n, _labels, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    expected_area = (p.card_w_mm / MM_PER_INCH * dpi) * (p.card_h_mm / MM_PER_INCH * dpi) * scale**2
    print(f"  通路A Otsu 阈值={otsu_val:.0f}  闭运算核={k_close.shape[1]}px")
    print(f"  期望单卡面积（检测尺度）={expected_area:,.0f}px")
    print(f"  连通域 {n - 1} 个（按面积降序，只列前 12）：")
    order = sorted(range(1, n), key=lambda i: stats[i, 4], reverse=True)
    for rank, i in enumerate(order[:12], 1):
        x, y, bw, bh, area = stats[i]
        ratio = area / expected_area
        aspect = max(bw, bh) / max(1, min(bw, bh))
        flag = ""
        if not (p.area_min_ratio <= ratio <= p.area_max_ratio):
            flag += " ✗面积"
        if not (1.0 / p.aspect_max <= min(bw, bh) / max(bw, bh) <= 1.0):
            flag += " ✗比例"
        print(
            f"    #{rank:<2} area={area:>10,} ({ratio:>6.2f}×卡) "
            f"bbox={bw}×{bh} fill={area / (bw * bh):.2f} {flag}"
        )
    print()

    # ---- 通路 B：Canny ----
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    med = float(np.median(enhanced))
    edges = cv2.Canny(enhanced, int(max(0, 0.6 * med)), int(min(255, 1.6 * med)), apertureSize=3)
    cv2.imwrite(str(outdir / "B1_canny.png"), edges)
    kb = cv2.getStructuringElement(cv2.MORPH_RECT, (mm_to_px(p.close_kernel_mm * 2, dpi),) * 2)
    b_closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kb, iterations=2)
    cv2.imwrite(str(outdir / "B2_canny_closed.png"), b_closed)

    contours, _ = cv2.findContours(b_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    print(
        f"  通路B Canny 阈值=({int(max(0, 0.6 * med))},{int(min(255, 1.6 * med))}) "
        f"中位数={med:.0f}  闭运算核={kb.shape[1]}px"
    )
    print(f"  轮廓 {len(contours)} 个（按面积降序，只列前 12）：")
    big = sorted(contours, key=cv2.contourArea, reverse=True)[:12]
    for rank, cnt in enumerate(big, 1):
        area = cv2.contourArea(cnt)
        ratio = area / expected_area
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        rect = cv2.minAreaRect(cnt)
        (_, _), (rw, rh), _ = rect
        fill = area / (rw * rh) if rw * rh > 0 else 0
        flags = []
        if len(approx) != 4:
            flags.append(f"顶点{len(approx)}")
        elif not cv2.isContourConvex(approx):
            flags.append("非凸")
        if not (p.area_min_ratio <= ratio <= p.area_max_ratio):
            flags.append("✗面积")
        if fill < p.min_rectangularity:
            flags.append("✗填充")
        print(
            f"    #{rank:<2} area={area:>10,.0f} ({ratio:>6.2f}×卡) "
            f"fill={fill:.2f} {' '.join(flags) or '✓'}"
        )

    # ---- Sauvola（备选路径）----
    sw = mm_to_px(p.card_w_mm * p.sauvola_window_ratio, dpi)
    sau = sauvola_threshold(gray, sw)
    cv2.imwrite(str(outdir / "C1_sauvola.png"), sau)

    print()
    print(f"  中间态已落盘：{outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
