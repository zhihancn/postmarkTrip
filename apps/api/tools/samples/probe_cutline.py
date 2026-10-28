#!/usr/bin/env python3
"""白底页的裁切边线到底可不可见。

spec §三.2 说「卡片边界有一条细灰线（裁切边），在纯白 A4 上是可见的」。
这个脚本量化它：在卡边界附近取一条水平剖面，打印灰度值，
并给出"边线与纸面的对比度"。

**这件事决定白底切分是否可做**：如果边线与纸面的灰度差只有 1~2 个灰阶，
那么任何阈值/边缘方法都会同时被 JPEG 噪点淹没 —— 那属于**扫描条件问题**，
不是算法问题，该如实写进报告而不是继续调参。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

repo = Path(__file__).resolve().parents[4]
name = sys.argv[1] if len(sys.argv) > 1 else "单张卡整张扫描图.jpg"
img = cv2.imdecode(
    np.fromfile(str(repo / "docs" / "need" / "pic" / name), np.uint8), cv2.IMREAD_COLOR
)
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
h, w = gray.shape

print(f"=== {name} ===")
print(f"原图 {w}×{h}  灰度 min={gray.min()} max={gray.max()} mean={gray.mean():.1f}")

# 灰度直方图的最暗部分：纸面 250+，边线应当落在 200~250 之间的一个窄峰
hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
nonzero = [(int(v), int(hist[v])) for v in range(256) if hist[v] > 0]
print("\n灰度分布（只列 <255 且计数 > 全图 0.01% 的档）：")
thresh = gray.size * 0.0001
for v, c in nonzero[:40]:
    if c >= thresh:
        print(f"  {v:>3}: {c:>12,}  ({c / gray.size * 100:.4f}%)")

# 找"暗于纸面"的像素占比 —— 它们就是边线与印刷内容
for cut in (250, 245, 240, 230, 220, 200):
    frac = float((gray < cut).mean()) * 100
    print(f"  灰度 < {cut} 的像素占比：{frac:.3f}%")

# 取一条穿过卡片边界的水平剖面（卡在右上角，约 y = 12% 处）
print("\n水平剖面（y = 12% 高度处，从左到右抽样）：")
y = int(h * 0.12)
row = gray[y]
# 只打印"偏离纸面"的位置附近
paper = int(np.median(row))
print(f"  该行纸面中位灰度 = {paper}")
deviating = np.where(np.abs(row.astype(int) - paper) > 2)[0]
if deviating.size:
    print(f"  偏离纸面 >2 灰阶的列数：{deviating.size} / {w}")
    # 聚成区间
    groups = []
    start = prev = deviating[0]
    for c in deviating[1:]:
        if c - prev > 5:
            groups.append((start, prev))
            start = c
        prev = c
    groups.append((start, prev))
    print("  区间（前 10 个）：")
    for a, b in groups[:10]:
        seg = row[a : b + 1]
        print(
            f"    x=[{a:>5},{b:>5}] 宽 {b - a + 1:>4}px  "
            f"灰度 min={seg.min()} mean={seg.mean():.0f}（纸面 {paper}）"
        )
else:
    print("  该行**没有任何**偏离纸面 >2 灰阶的像素")

# 垂直剖面
print("\n垂直剖面（x = 88% 处）：")
x = int(w * 0.88)
col = gray[:, x]
paper_c = int(np.median(col))
dev = np.where(np.abs(col.astype(int) - paper_c) > 2)[0]
print(f"  该列纸面中位灰度 = {paper_c}；偏离 >2 灰阶的行数 = {dev.size} / {h}")
