#!/usr/bin/env python3
"""诊断 A：局名末尾的**戳号**（如「中亭街1」的 1）为什么丢了？

两种可能的修法完全不同，所以必须先定位：

  (a) **区域裁剪把它切掉了** → 放宽取景框即可
  (b) **OCR 没认出那个小字**   → 要么换引擎/预处理，要么接受

判据：如果**在某个 margin 下它出现了**，就是 (a)；如果**所有 margin 都没有**，
就是 (b)（或需要更强的预处理）。

同时把每次裁剪的区域**落盘成图**，这样"1 到底在不在框里"可以直接看。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "apps/api/src"))

from postmarktrip.recognition.ocr import recognize  # noqa: E402
from postmarktrip.recognition.postmark_type import classify_postmark  # noqa: E402
from postmarktrip.recognition.split import split_page  # noqa: E402
from postmarktrip.recognition.text_regions import extract_postmark_region  # noqa: E402

OUT = Path("_look")
WORK = 2000
MARGINS = [1.00, 1.15, 1.30, 1.50]

#: 我们想验证的目标：**卡里含有哪个已知带戳号的局名**
TARGETS = {
    "更多样本/005.jpg": ["中亭街", "福屿", "西洪", "滨江北路"],
    "清晰黑色日戳特写-1.jpg": ["建溪"],
    "清晰黑色日戳特写-2.jpg": ["八一一路"],
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for rel, targets in TARGETS.items():
        p = REPO / "docs" / "need" / "pic" / rel
        if not p.exists():
            print(f"missing {rel}")
            continue
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        H, W = img.shape[:2]
        s = WORK / max(H, W)
        small = cv2.resize(img, (int(W * s), int(H * s)), interpolation=cv2.INTER_AREA)
        work_dpi = 600.0 * min(s, 1.0)

        cards = []
        if rel.startswith("更多样本"):
            sp = split_page(small, dpi=600, detection_scale=s)
            cards = [(i, c.ortho) for i, c in enumerate(sp.candidates)]
        else:
            cards = [(0, small)]

        for idx, card in cards:
            v = classify_postmark(card, dpi=work_dpi)
            if v.circle is None:
                continue
            ocr = recognize(card)
            if not ocr.available:
                print(f"{rel} card{idx}: OCR 不可用：{ocr.reason}")
                continue
            joined = " ".join(line.text for line in ocr.lines)
            hit = [t for t in targets if t in joined]
            if not hit:
                continue

            print(f"\n=== {rel} card{idx}  目标={hit} ===")
            print(f"  戳型={v.kind}  r={v.circle.r:.0f}  卡={card.shape[1]}x{card.shape[0]}")
            print(f"  整卡 OCR 全文：{joined}")
            print(f"  整卡 OCR 行：{[(line.text, round(line.confidence, 2)) for line in ocr.lines]}")

            for margin in MARGINS:
                got = extract_postmark_region(card, v.circle, margin=margin)
                if got is None:
                    print(f"    margin={margin}: 区域为空")
                    continue
                region, rbbox = got
                r_ocr = recognize(region)
                lines = [(line.text, round(line.confidence, 2)) for line in r_ocr.lines]
                txt = " ".join(line.text for line in r_ocr.lines)
                has_digit_at_end = any(
                    line.text and line.text[-1].isdigit() for line in r_ocr.lines
                )
                flag = "★ 末尾有数字" if has_digit_at_end else ""
                print(f"    margin={margin}: {region.shape[1]}x{region.shape[0]} bbox={rbbox}  {lines} {flag}")
                print(f"        全文：{txt}")
                fn = OUT / f"region_{Path(rel).stem}_{idx}_m{margin:.2f}.jpg"
                cv2.imwrite(str(fn), region, [cv2.IMWRITE_JPEG_QUALITY, 90])
            print(f"    → 区域图落在 {OUT}/region_*.jpg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
