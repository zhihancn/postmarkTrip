#!/usr/bin/env python3
"""在真实样本上跑"文字区域 → OCR"，看读出来什么。

**这一步没有"准确率"可算** —— 识别的真值要先人工誊写。所以先做
**可读性检查**：把每张图提出来的区域与 OCR 文本并排打出来，
人看一眼就知道判据与 OCR 是否可用。

`--truth` 可以喂一份人工誊写的真值（`文件名|字段|期望`），
有真值时才算准确率 —— 没真值的百分比是自证。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.ocr import recognize
from postmarktrip.recognition.postmark_type import classify_postmark
from postmarktrip.recognition.text_regions import extract_text_regions

WORK_LONG_SIDE = 2000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, default=None)
    ap.add_argument("--only", default=None)
    ap.add_argument("--min-conf", type=float, default=0.30)
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[4]
    samples = args.samples or repo / "docs" / "need" / "pic"

    ok_any = False
    for path in sorted(samples.glob("*.jpg")):
        if args.only and args.only not in path.name:
            continue
        img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        scale = WORK_LONG_SIDE / max(h, w)
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        verdict = classify_postmark(img, dpi=600.0)
        regions = extract_text_regions(img, verdict)
        if not regions:
            continue
        ok_any = True
        print(f"=== {path.name}  判定={verdict.kind}  区域 {len(regions)} ===")
        for r in regions:
            res = recognize(r.image, orientation=r.orientation, min_confidence=args.min_conf)
            status = "可用" if res.available else f"不可用({res.reason})"
            texts = " | ".join(f"{ln.text}({ln.confidence:.2f})" for ln in res.lines)
            print(
                f"  {r.kind:<11} 区域响应={r.text_response:.3f} 图={r.image.shape}  "
                f"OCR[{status}] → {texts or '（空）'}"
            )
        print()
    if not ok_any:
        print("没有任何样本提出文字区域")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
