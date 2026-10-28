#!/usr/bin/env python3
"""A3: does the STROKE-level feature separate text from line drawings?

Uses the labels read off the 380x620 range sheet by eye
(`tools/samples/a3_range_sheet.py`). Only the cards a human could actually
judge are labelled; the rest stay unlabelled rather than guessed.

Compares, on the SAME cards:
  * `top_arc_text_response`  -- the current feature (ink structure)
  * `top_arc_stroke_score`   -- the candidate (block-size distribution)
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
    top_arc_stroke_score,
    top_arc_text_response,
)
from postmarktrip.recognition.split import split_page

WORK = 2000
PAGES = ["更多样本/002.jpg", "更多样本/003.jpg", "更多样本/004.jpg", "更多样本/005.jpg"]

#: 人工判读（380x620 分辨率下逐张看）。只列能判定的。
LABELS = {
    "005-3": "daily",  # 顶弧「福州」+ 福屿1
    "003-6": "daily",  # 顶弧「江西玉山」
    "005-6": "daily",  # 顶弧「福建南平」+ 滨江北路1
    "002-1": "daily",  # 有日戳（顶弧被邮票遮住一部分）
    "003-3": "scenic",  # 线描图 司春女神
    "004-0": "scenic",  # 线描图
    "002-6": "scenic",  # 线描图 猴王献宝
    "003-8": "scenic",  # 线描图 文峰塔
    "002-4": "scenic",  # 线描图 老道拜月
    # 005-1 无邮戳，不参与
}


def main() -> int:
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
            key = f"{Path(rel).stem}-{i}"
            card = c.ortho
            v = classify_postmark(card, dpi=600 * s)
            if v.circle is None:
                continue
            band = find_text_band(card, v.circle)
            v1 = top_arc_text_response(card, v.circle, band=band)
            v2 = top_arc_stroke_score(card, v.circle, band=band)
            bw = (band.r_outer - band.r_inner) / v.circle.r if band else 0.0
            rows.append(
                {
                    "key": key,
                    "label": LABELS.get(key),
                    "v1": v1,
                    "v2": v2,
                    "band_w": bw,
                    "local": v.kind,
                    "r": v.circle.r,
                }
            )

    print(
        f"{'card':<9} {'label':<8} {'local':<8} {'band_w':>7} {'old(ink)':>9} {'new(stroke)':>12}"
    )
    print("-" * 62)
    for r in sorted(rows, key=lambda x: x["key"]):
        print(
            f"{r['key']:<9} {r['label'] or '-'!s:<8} {r['local']:<8} "
            f"{r['band_w']:>7.2f} {r['v1']:>9.3f} {r['v2']:>12.3f}"
        )

    lab = [r for r in rows if r["label"]]
    print()
    print(f"有标签的卡片：{len(lab)} 张")
    for fname, name in (("v1", "old(ink structure)"), ("v2", "new(stroke blocks)")):
        d = sorted(r[fname] for r in lab if r["label"] == "daily")
        s = sorted(r[fname] for r in lab if r["label"] == "scenic")
        print()
        print(f"=== {name} ===")
        print(f"  daily  n={len(d)}  {[round(x, 3) for x in d]}")
        print(f"  scenic n={len(s)}  {[round(x, 3) for x in s]}")
        if d and s:
            sep = (min(d) - max(s)) if min(d) > max(s) else 0.0
            print(
                f"  最大 scenic = {max(s):.3f}   最小 daily = {min(d):.3f}   "
                f"间隔 = {sep:.3f}  {'✓ 可分' if sep > 0 else '✗ 重叠'}"
            )
            # 最佳单阈值准确率
            cands = sorted({round(x, 3) for x in d + s})
            best = (0.0, 0.0)
            for thr in cands:
                acc = sum(1 for r in lab if (r[fname] > thr) == (r["label"] == "daily")) / len(lab)
                if acc > best[0]:
                    best = (acc, thr)
            print(f"  最佳单阈值 {best[1]:.3f} → 准确率 {best[0]:.1%} ({len(lab)} 张)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
