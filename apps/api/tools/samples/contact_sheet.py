#!/usr/bin/env python3
"""Build a contact sheet of every split card, so the type truth can be read off
in ONE image instead of 34 separate ones.

Writes _look/sheet.jpg  and prints an index listing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.split import split_page

OUT = Path("_look")
PAGES = ["更多样本/002.jpg", "更多样本/003.jpg", "更多样本/004.jpg", "更多样本/005.jpg"]
WORK = 2000
CELL_W = 190
CELL_H = 300
COLS = 10


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[4]
    tiles: list[tuple[str, np.ndarray]] = []

    for rel in PAGES:
        p = repo / "docs" / "need" / "pic" / rel
        if not p.exists():
            print(f"missing {rel}")
            continue
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        H, W = img.shape[:2]
        s = WORK / max(H, W)
        small = cv2.resize(img, (int(W * s), int(H * s)), interpolation=cv2.INTER_AREA)
        sp = split_page(small, dpi=600, detection_scale=s)
        print(f"{rel}: {len(sp.candidates)} cards")
        for i, c in enumerate(sp.candidates):
            tiles.append((f"{Path(rel).stem}-{i}", c.ortho))

    n = len(tiles)
    rows = (n + COLS - 1) // COLS
    sheet = np.full((rows * CELL_H, COLS * CELL_W, 3), 245, np.uint8)
    for idx, (label, ortho) in enumerate(tiles):
        r, c = divmod(idx, COLS)
        h, w = ortho.shape[:2]
        sc = min(CELL_W / w, (CELL_H - 24) / h)
        thumb = cv2.resize(ortho, (max(1, int(w * sc)), max(1, int(h * sc))),
                           interpolation=cv2.INTER_AREA)
        th, tw = thumb.shape[:2]
        y0 = r * CELL_H + 24
        x0 = c * CELL_W + (CELL_W - tw) // 2
        sheet[y0:y0 + th, x0:x0 + tw] = thumb
        cv2.putText(sheet, f"{idx}", (c * CELL_W + 4, r * CELL_H + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 220), 2)
        cv2.rectangle(sheet, (c * CELL_W, r * CELL_H),
                      (c * CELL_W + CELL_W - 1, r * CELL_H + CELL_H - 1), (200, 200, 200), 1)

    cv2.imwrite(str(OUT / "sheet.jpg"), sheet)
    print(f"\n{ n } tiles -> {OUT}/sheet.jpg  ({sheet.shape[1]}x{sheet.shape[0]})")
    print("index:")
    for idx, (label, ortho) in enumerate(tiles):
        print(f"  {idx:>3}  {label:<12} {ortho.shape[1]}x{ortho.shape[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
