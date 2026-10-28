#!/usr/bin/env python3
"""戳型判负的真因：是**切分器的正射图**改变了判别输入吗？

## 背景（我把这条线索追错了两轮）

* 第 10~11 轮：我说"管线给特写传 dpi=600 而实际约 360" → **DPI 审计证明是 600，此说错**
* 第 12 轮：我说"不同先验会选中不同的圆" → **审计证明三种先验给出同一个圆，此说也错**

但事实仍然在：`清晰黑色日戳特写-1` 在 `eval_stamp_type`（直接喂原图）里是
`daily(0.69)`，而在 `pipeline` 里是 `scenic(0.56)`。

**区别只可能在"喂进去的图不同"** —— `pipeline.recognize_page` 会先跑切分，
把**正射图**交给判别器，而不是原图。这个脚本直接对照这一条。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.postmark_type import (
    center_image_ness,
    classify_postmark,
    find_text_band,
    top_arc_text_response,
)
from postmarktrip.recognition.split import split_page
from postmarktrip.recognition.text_regions import postmark_diameter_hint_px

WORK = 2000


def probe(label: str, img: np.ndarray) -> None:
    h, w = img.shape[:2]
    aspect = max(h, w) / max(1, min(h, w))
    hint = postmark_diameter_hint_px(img)
    v = classify_postmark(img, expected_diameter_px=hint)
    circle = v.circle
    if circle is None:
        print(
            f"  {label:<22} {w:>5}x{h:<5} 比例{aspect:>5.2f}  hint={hint:>5.0f}  未找到圆 → {v.kind}"
        )
        return
    band = find_text_band(img, circle)
    top = top_arc_text_response(img, circle)
    ctr = center_image_ness(img, circle)
    br = f"{band.r_inner:.0f}~{band.r_outer:.0f}" if band else "无"
    print(
        f"  {label:<22} {w:>5}x{h:<5} 比例{aspect:>5.2f}  hint={hint:>5.0f}  "
        f"r={circle.r:>4.0f} q={circle.quality:.2f}  "
        f"顶弧={top:.3f} 中心={ctr:.3f} band={br:<11} → {v.kind}({v.confidence:.2f})"
    )


def main() -> int:
    repo = Path(__file__).resolve().parents[4]
    pic = repo / "docs" / "need" / "pic"

    for name in ("清晰黑色日戳特写-1.jpg", "盖的不好-墨太多.jpg", "清晰黑色日戳特写-2.jpg"):
        path = pic / name
        if not path.exists():
            continue
        img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        scale = WORK / max(h, w)
        work = img
        if scale < 1.0:
            work = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

        print(f"######## {name} ########")
        probe("A 原图（直接喂）", work)

        # B: 走切分器，拿它的正射图
        try:
            sp = split_page(work, detection_scale=scale)
        except Exception as exc:
            print(f"  切分失败：{type(exc).__name__}: {exc}")
            print()
            continue
        print(
            f"  （切分：{len(sp.candidates)} 个候选，级别 {sp.level}，dpi_source={sp.dpi_source}）"
        )
        for i, cand in enumerate(sp.candidates[:3]):
            probe(f"B{i} 切分正射图", cand.ortho)

        # C: 用"卡片几何比例"先把图裁成一张卡，再喂（模拟用户手工裁单卡）
        H, W = work.shape[:2]
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
