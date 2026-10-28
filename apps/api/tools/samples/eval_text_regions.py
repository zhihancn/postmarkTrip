#!/usr/bin/env python3
"""在样本上检查文字区域提取：每张图提出哪些区域、响应多少。

**这一步没有"准确率"可算** —— 文字区域的真值标注需要逐块框选，
而现在没有。所以这里的验收方式是**可读的数量检查**：

* 日戳 → 应当提出 **2 个环区域**（顶弧 + 底弧）
* 风景戳 → 应当提出**若干名称行**，且响应明显高于"环区域"（因为它的文字不在环上）
* 如果风景戳提出的一堆区域响应都很低，说明名称行没找到
* 可选的 ``--dump`` 会把区域图落盘，供人肉看一眼"取到的到底是不是文字"

用法::

    python tools/samples/eval_text_regions.py
    python tools/samples/eval_text_regions.py --dump /tmp/regions
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.postmark_type import classify_postmark
from postmarktrip.recognition.text_regions import extract_text_regions

WORK_LONG_SIDE = 2000

#: 与 eval_stamp_type 用同一份标注（看图确认过的）
LABELS: dict[str, str | None] = {
    "清晰黑色日戳特写-1.jpg": "daily",
    "清晰黑色日戳特写-2.jpg": "daily",
    "风景戳特写(来源-求戳信).jpg": "scenic",
    "彩色戳特写.jpg": "scenic",
    "盖的不好-墨太多.jpg": "daily",
    "盖的不好-邮票歪，不清晰.jpg": "daily",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, default=None)
    ap.add_argument("--dump", type=Path, default=None)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[4]
    samples = args.samples or repo / "docs" / "need" / "pic"

    for path in sorted(samples.glob("*.jpg")):
        if args.only and args.only not in path.name:
            continue
        img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        scale = WORK_LONG_SIDE / max(h, w)
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        dpi = 600.0 * min(1.0, scale)

        verdict = classify_postmark(img, dpi=dpi)
        regions = extract_text_regions(img, verdict)

        label = LABELS.get(path.name)
        print(f"=== {path.name}  标注={label or '?'}  判定={verdict.kind} ===")
        if verdict.circle is None:
            print("  未找到戳圈 → 无法提取")
            print()
            continue
        print(f"  戳圈 r={verdict.circle.r:.0f}px  提出 {len(regions)} 个区域")
        for i, r in enumerate(regions):
            bx, by, bw, bh = r.bbox
            print(
                f"    [{i}] {r.kind:<12} 响应={r.text_response:.3f} "
                f"{r.orientation:<10} bbox=({bx},{by},{bw},{bh}) img={r.image.shape}"
            )
        if not regions:
            print("    ⚠️ 一个区域都没提出")

        if args.dump:
            outdir = args.dump / path.stem
            outdir.mkdir(parents=True, exist_ok=True)
            for i, r in enumerate(regions):
                cv2.imwrite(str(outdir / f"{i:02d}_{r.kind}.png"), r.image)
            vis = img.copy()
            for i, r in enumerate(regions):
                bx, by, bw, bh = r.bbox
                cv2.rectangle(vis, (bx, by), (bx + bw, by + bh), (0, 0, 255), 3)
                cv2.putText(
                    vis,
                    str(i),
                    (bx + 4, by + 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.0,
                    (0, 0, 255),
                    2,
                )
            cv2.imwrite(str(outdir / "_overlay.jpg"), vis)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
