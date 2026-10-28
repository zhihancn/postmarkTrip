#!/usr/bin/env python3
"""Is the top-arc strip actually showing the top arc?

Dumps the strip that `top_arc_text_response` measures, for a few cards where the
feature looks wrong (high response but labelled scenic, and vice versa).
If the strip does not contain the top arc, the FEATURE is broken.
If it does contain text, then the label (or the premise) is wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.postmark_type import (
    arc_angles,
    classify_postmark,
    find_text_band,
    polar_unwrap,
)
from postmarktrip.recognition.split import split_page

OUT = Path("_look")
PAGES = ["更多样本/002.jpg", "更多样本/003.jpg", "更多样本/005.jpg"]
WORK = 2000
WANT = {"002-0", "002-3", "003-2", "003-5", "005-2", "005-6"}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[4]
    tiles = []
    for rel in PAGES:
        p = repo / "docs" / "need" / "pic" / rel
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        H, W = img.shape[:2]
        s = WORK / max(H, W)
        small = cv2.resize(img, (int(W * s), int(H * s)), interpolation=cv2.INTER_AREA)
        sp = split_page(small, dpi=600, detection_scale=s)
        for i, c in enumerate(sp.candidates):
            key = f"{Path(rel).stem}-{i}"
            if key not in WANT:
                continue
            card = c.ortho
            # 正射图是 work 尺度的，所以 dpi 也必须折算到这个尺度
            v = classify_postmark(card, dpi=600 * s)
            if v.circle is None:
                print(f"{key}: no circle")
                continue
            band = find_text_band(card, v.circle)
            a0, a1 = arc_angles("top")
            lo = band.r_inner / v.circle.r if band else 0.6
            hi = band.r_outer / v.circle.r if band else 0.95
            strip = polar_unwrap(card, v.circle, angle_from_deg=a0, angle_to_deg=a1,
                                 r_from_ratio=lo, r_to_ratio=hi, out_height=120)
            # 并排：卡片 + 展开条带
            ch, cw = card.shape[:2]
            sc = 260 / max(ch, cw)
            card_t = cv2.resize(card, (int(cw * sc), int(ch * sc)))
            sh, sw = strip.shape[:2]
            strip_t = cv2.resize(strip, (max(1, int(sw * 0.6)), max(1, int(sh * 0.6))))
            canvas = np.full((max(card_t.shape[0], strip_t.shape[0]) + 30,
                              card_t.shape[1] + strip_t.shape[1] + 20, 3), 245, np.uint8)
            canvas[30:30 + card_t.shape[0], :card_t.shape[1]] = card_t
            x0 = card_t.shape[1] + 10
            canvas[30:30 + strip_t.shape[0], x0:x0 + strip_t.shape[1]] = cv2.cvtColor(strip_t, cv2.COLOR_GRAY2BGR) if strip_t.ndim == 2 else strip_t
            cv2.putText(canvas, f"{key} top={v.top_arc_text:.2f} {v.kind}",
                        (4, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 1)
            tiles.append(canvas)
            print(f"{key}: r={v.circle.r:.0f} band={lo:.2f}~{hi:.2f} "
                  f"top={v.top_arc_text:.3f} kind={v.kind}")

    if tiles:
        wmax = max(t.shape[1] for t in tiles)
        hsum = sum(t.shape[0] for t in tiles) + 6 * len(tiles)
        sheet = np.full((hsum, wmax, 3), 245, np.uint8)
        y = 0
        for t in tiles:
            sheet[y:y + t.shape[0], :t.shape[1]] = t
            y += t.shape[0] + 6
        cv2.imwrite(str(OUT / "strips.jpg"), sheet)
        print(f"-> {OUT}/strips.jpg ({sheet.shape[1]}x{sheet.shape[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
