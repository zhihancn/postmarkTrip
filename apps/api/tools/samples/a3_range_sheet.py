#!/usr/bin/env python3
"""A3: is `top_arc_text` pointing the right way?

Builds a HIGH-RESOLUTION sheet of cards sampled across the whole top_arc_text
range, sorted by that value, so a human can judge whether the low-scoring cards
really are scenic stamps (empty top arc) and the high ones really are daily
stamps (place name in the top arc).

The earlier contact sheet was 190x300 per card, and at that size the
discriminator is not readable -- which is why this one uses 380x600.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.postmark_type import (
    classify_postmark,
    find_text_band,
)
from postmarktrip.recognition.split import split_page

OUT = Path("_look")
PAGES = ["更多样本/002.jpg", "更多样本/003.jpg", "更多样本/004.jpg", "更多样本/005.jpg"]
WORK = 2000
CELL_W, CELL_H = 380, 620
COLS = 5
PICK = 10


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[4]
    rows = []
    for rel in PAGES:
        p = repo / "docs" / "need" / "pic" / rel
        if not p.exists():
            continue
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        H, W = img.shape[:2]
        s = WORK / max(H, W)
        small = cv2.resize(img, (int(W * s), int(H * s)), interpolation=cv2.INTER_AREA)
        sp = split_page(small, dpi=600, detection_scale=s)
        for i, c in enumerate(sp.candidates):
            card = c.ortho
            v = classify_postmark(card, dpi=600 * s)
            if v.circle is None:
                continue
            band = find_text_band(card, v.circle)
            rows.append({
                "key": f"{Path(rel).stem}-{i}",
                "card": card,
                "top": v.top_arc_text,
                "centre": v.center_image,
                "r": v.circle.r,
                "band": (band.r_inner / v.circle.r, band.r_outer / v.circle.r) if band else None,
                "kind": v.kind,
            })

    rows.sort(key=lambda r: r["top"])
    n = len(rows)
    print(f"{n} cards with a located circle, sorted by top_arc_text:")
    for r in rows:
        b = f"{r['band'][0]:.2f}~{r['band'][1]:.2f}" if r["band"] else "-"
        print(f"  {r['key']:<9} top={r['top']:.3f} centre={r['centre']:.3f} "
              f"r={r['r']:>5.0f} band={b:<11} local={r['kind']}")

    # 沿整个区间均匀取样（含两端）
    if n > PICK:
        idx = [round(i * (n - 1) / (PICK - 1)) for i in range(PICK)]
        picked = [rows[i] for i in idx]
    else:
        picked = rows

    r_count = (len(picked) + COLS - 1) // COLS
    sheet = np.full((r_count * CELL_H, COLS * CELL_W, 3), 245, np.uint8)
    for k, r in enumerate(picked):
        rr, cc = divmod(k, COLS)
        card = r["card"]
        h, w = card.shape[:2]
        sc = min(CELL_W / w, (CELL_H - 40) / h)
        th = cv2.resize(card, (max(1, int(w * sc)), max(1, int(h * sc))),
                        interpolation=cv2.INTER_AREA)
        y0 = rr * CELL_H + 36
        x0 = cc * CELL_W + (CELL_W - th.shape[1]) // 2
        sheet[y0:y0 + th.shape[0], x0:x0 + th.shape[1]] = th
        cv2.putText(sheet, f"{r['key']} top={r['top']:.2f}", (cc * CELL_W + 4, rr * CELL_H + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.58, (0, 0, 200), 2)
        cv2.putText(sheet, f"local={r['kind']} c={r['centre']:.2f}",
                    (cc * CELL_W + 4, rr * CELL_H + 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.48, (60, 60, 60), 1)
        cv2.rectangle(sheet, (cc * CELL_W, rr * CELL_H),
                      (cc * CELL_W + CELL_W - 1, rr * CELL_H + CELL_H - 1), (200, 200, 200), 1)
    cv2.imwrite(str(OUT / "a3_range.jpg"), sheet)
    print(f"\n-> {OUT}/a3_range.jpg  ({sheet.shape[1]}x{sheet.shape[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
