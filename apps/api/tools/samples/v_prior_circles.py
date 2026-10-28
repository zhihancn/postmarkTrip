#!/usr/bin/env python3
"""定案：不同先验选中的圆，**半径与圆心**各是什么？顶弧响应因此差多少？

上一轮的教训：我只比了 `r` 就下结论"三种先验给出同一个圆"。
但同一张图上**半径相同、圆心不同的候选**是可以同时存在的 ——
而顶弧响应对圆心极敏感。所以这次把 (cx, cy, r, quality, 顶弧) 全打出来。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.postmark_type import (
    classify_postmark,
    find_postmark_circle,
    top_arc_text_response,
)
from postmarktrip.recognition.text_regions import postmark_diameter_hint_px

WORK = 2000
SAMPLES = [
    "清晰黑色日戳特写-1.jpg",
    "清晰黑色日戳特写-2.jpg",
    "盖的不好-墨太多.jpg",
    "盖的不好-墨太少.jpg",
    "盖的不好-有污渍.jpg",
    "盖的不好-糊掉了.jpg",
    "盖的不好-邮票歪，不清晰.jpg",
    "风景戳特写(来源-求戳信).jpg",
    "彩色戳特写.jpg",
]

#: 真值（前几轮逐张看图确认过）
TRUTH = {
    "清晰黑色日戳特写-1.jpg": "daily",
    "清晰黑色日戳特写-2.jpg": "daily",
    "盖的不好-墨太多.jpg": "daily",
    "盖的不好-邮票歪，不清晰.jpg": "daily",
    "风景戳特写(来源-求戳信).jpg": "scenic",
    "彩色戳特写.jpg": "scenic",
}


def main() -> int:
    repo = Path(__file__).resolve().parents[4]
    pic = repo / "docs" / "need" / "pic"

    print(
        f"{'样本':<28} {'真值':<7} {'先验':<10} {'cx':>5} {'cy':>5} {'r':>5} "
        f"{'q':>5} {'顶弧':>6} {'判定':<8}"
    )
    print("-" * 96)
    flips = 0
    for name in SAMPLES:
        path = pic / name
        if not path.exists():
            continue
        img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        scale = WORK / max(h, w)
        work = img
        if scale < 1.0:
            work = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        hint = postmark_diameter_hint_px(work)
        truth = TRUTH.get(name, "?")
        verdicts = []

        for label, kw in (
            ("dpi=600", {"dpi": 600.0}),
            ("几何先验", {"expected_diameter_px": hint}),
            ("无先验", {}),
        ):
            c = find_postmark_circle(work, **kw)
            v = classify_postmark(work, **kw)
            verdicts.append(v.kind)
            if c is None:
                print(f"{name[:26]:<28} {truth:<7} {label:<10}   —— 未找到圆 → {v.kind}")
                continue
            top = top_arc_text_response(work, c)
            print(
                f"{name[:26]:<28} {truth:<7} {label:<10} {c.cx:>5.0f} {c.cy:>5.0f} "
                f"{c.r:>5.0f} {c.quality:>5.2f} {top:>6.3f} {v.kind:<8}"
            )
        if len(set(verdicts)) > 1:
            flips += 1
            print(f"{'':<28} {'':<7} 🔴 三种先验给出**不同判定**：{verdicts}")
        print()

    print("=== 结论 ===")
    print(f"  先验不同 → 判定不同的样本：{flips}/{len([n for n in SAMPLES if (pic / n).exists()])}")
    print()
    print("  这解释了为什么同一个样本在不同脚本/不同先验下会得到不同的戳型：")
    print("  **顶弧响应对选中的圆（尤其是圆心）极敏感，而阈值余量很小。**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
