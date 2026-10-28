#!/usr/bin/env python3
"""Look at a full page: mark what the splitter found, AND list rejected candidates.

Why list rejected ones: the real cause of under-segmentation only shows up in
the rejected set. "How many came out" cannot distinguish "never detected" from
"detected but filtered out" -- and those two have completely different fixes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.split import (
    MM_PER_INCH,
    SplitParams,
    _detect_by_bright_body,
    _detect_by_contour,
    _detect_by_cutline_frame,
    split_page,
)

OUT = Path("_look")
WORK = 1400


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[4]
    rel = sys.argv[1] if len(sys.argv) > 1 else "更多样本/005.jpg"
    want_diag = "--diag" in sys.argv
    img = cv2.imdecode(
        np.fromfile(str(repo / "docs" / "need" / "pic" / rel), np.uint8), cv2.IMREAD_COLOR
    )
    H, W = img.shape[:2]
    s = WORK / max(H, W)
    small = cv2.resize(img, (int(W * s), int(H * s)), interpolation=cv2.INTER_AREA)
    print(f"{rel}  {W}x{H}  -> work {small.shape[1]}x{small.shape[0]}  scale={s:.4f}")

    sp = split_page(small, dpi=600, detection_scale=s)
    print(f"  split: {len(sp.candidates)} candidates, level={sp.level}")
    for n in sp.notes:
        print(f"    - {n}")
    vis = small.copy()
    for i, c in enumerate(sp.candidates):
        q = c.quad.astype(int)
        cv2.polylines(vis, [q.reshape(-1, 1, 2)], True, (0, 0, 255), 3)
        cx, cy = int(q[:, 0].mean()), int(q[:, 1].mean())
        cv2.putText(vis, str(i), (cx - 10, cy + 10), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 255), 3)
        print(
            f"    [{i}] {c.origin:<8} area={c.area_ratio:.2f} aspect={c.aspect:.2f} "
            f"suspect={c.size_suspect} centre=({cx},{cy})"
        )
    cv2.imwrite(str(OUT / "overlay.jpg"), vis)
    print(f"  -> {OUT}/overlay.jpg")

    if not want_diag:
        return 0

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    p = SplitParams()
    dpi = 600.0
    px_per_mm = dpi / MM_PER_INCH * s
    expected_area = (p.card_w_mm * px_per_mm) * (p.card_h_mm * px_per_mm)
    raw: list[tuple[np.ndarray, float, float, float, str]] = []
    for quad, ar, asp, rect in _detect_by_bright_body(gray, p, expected_area, dpi):
        raw.append((quad, ar, asp, rect, "body"))
    for name, fn in (("cutline", _detect_by_cutline_frame), ("contour", _detect_by_contour)):
        for quad, ar, asp, rect in fn(gray, p, expected_area, dpi, scale=s):
            raw.append((quad, ar, asp, rect, name))

    print()
    print("=== rejected candidates (the real cause lives here) ===")
    print(
        f"  rule: area {p.area_min_ratio}~{p.area_max_ratio}  "
        f"aspect {p.aspect_min}~{p.aspect_max}  fill >= {p.min_rectangularity}"
    )
    print(
        f"  exempt: area {p.clipped_area_ratio}~{p.clipped_area_max_ratio} "
        f"and fill >= {p.clipped_min_rectangularity}"
    )
    print()
    print(f"{'#':>3} {'route':<8} {'area':>6} {'asp':>6} {'fill':>6} {'centre':>11}  reason")
    print("-" * 82)
    n_rej = 0
    vis2 = small.copy()
    for i, (quad, ar, asp, rect, src) in enumerate(raw):
        cx, cy = int(quad[:, 0].mean()), int(quad[:, 1].mean())
        bad = []
        if not (p.area_min_ratio <= ar <= p.area_max_ratio):
            bad.append(f"area {ar:.2f} out of range")
        if rect < p.min_rectangularity:
            bad.append(f"fill {rect:.2f} < {p.min_rectangularity}")
        in_band = p.aspect_min <= asp <= p.aspect_max
        exempt = (
            p.clipped_area_ratio <= ar <= p.clipped_area_max_ratio
            and rect >= p.clipped_min_rectangularity
        )
        if not in_band and not exempt:
            if ar < p.clipped_area_ratio:
                bad.append(f"aspect {asp:.2f} out AND area {ar:.2f} < {p.clipped_area_ratio}")
            elif ar > p.clipped_area_max_ratio:
                bad.append(f"aspect {asp:.2f} out AND area {ar:.2f} > {p.clipped_area_max_ratio}")
            else:
                bad.append(
                    f"aspect {asp:.2f} out AND fill {rect:.2f} < {p.clipped_min_rectangularity}"
                )
        if bad:
            n_rej += 1
            cv2.polylines(vis2, [quad.astype(int).reshape(-1, 1, 2)], True, (0, 165, 255), 2)
            cv2.putText(vis2, str(i), (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 165, 255), 2)
            print(
                f"{i:>3} {src:<8} {ar:>6.2f} {asp:>6.2f} {rect:>6.2f} ({cx:>4},{cy:>4})  "
                f"{'; '.join(bad)}"
            )
    print()
    print(f"rejected {n_rej} (orange boxes) -> {OUT}/rejected.jpg")
    cv2.imwrite(str(OUT / "rejected.jpg"), vis2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
