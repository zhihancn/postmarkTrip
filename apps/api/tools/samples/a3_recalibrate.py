#!/usr/bin/env python3
"""A3 recalibration: labels from an INDEPENDENT source (the VLM), then measure
the local classifier's feature separation and pick a threshold with real margin.

Why the VLM can serve as the label source: it is a completely different method
from the local circle+arc-feature classifier, so it is not circular to evaluate
one against the other. The labels are recorded as VLM-derived, and a sample was
verified visually.

Why not hand-label: the contact sheet is 190x300 per card, and at that size the
discriminator (text in the top arc vs an empty top arc) is not reliably readable.
Unreliable labels would make the calibration look precise while being wrong.
"""

from __future__ import annotations

import json
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
from postmarktrip.recognition.vlm import ask, resolve_config

OUT = Path("_look")
PAGES = ["更多样本/002.jpg", "更多样本/003.jpg", "更多样本/004.jpg", "更多样本/005.jpg"]
WORK = 2000

PROMPT = (
    "这是一枚中国邮戳。请判断它的戳型，只回复一个词：\n"
    "日戳\n"
    "风景戳\n"
    "无邮戳\n"
    "看不清\n"
    "判据（务必按此判据，不要凭印象）：\n"
    "- 日戳：**顶弧（上半圆）有省市地名文字**，中部是日期数字，底弧是局名+戳号。\n"
    "- 风景戳：**顶弧是空的**（没有地名文字），中部或侧面是图案/线描/书法名称。\n"
    "只回复那一个词，不要解释。"
)


def load_key() -> str:
    for cand in (Path.home() / "myCode/postmarkTrip/.env",
                 Path(".env")):
        if not cand.exists():
            continue
        for line in cand.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("DEEPSEEK_API_KEY="):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[4]
    cfg, why = resolve_config(provider="deepseek", api_key=load_key(), enabled=True,
                              max_tokens=64)
    if cfg is None:
        print(f"VLM unavailable: {why}")
        return 1
    print(f"VLM: {cfg.describe()}")

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
            # 正射图是 work 尺度的；不折算会让戳圈搜索去抓整张卡（实测过）
            v = classify_postmark(card, dpi=600 * s)
            circle = v.circle
            top = center = float("nan")
            band = ""
            if circle is not None:
                top = top_arc_text_response(card, circle)
                center = center_image_ness(card, circle)
                b = find_text_band(card, circle)
                band = f"{b.r_inner:.0f}~{b.r_outer:.0f}" if b else "-"
            ans = ask(cfg, card, PROMPT)
            label = (ans.text or "").strip().split()[0] if ans.text else "?"
            rows.append({
                "card": f"{Path(rel).stem}-{i}",
                "vlm": label,
                "local": v.kind,
                "top": round(top, 4) if top == top else None,
                "center": round(center, 4) if center == center else None,
                "band": band,
                "r": round(circle.r, 1) if circle else None,
            })
            print(f"  {rows[-1]['card']:<10} vlm={label:<6} local={v.kind:<7} "
                  f"top={rows[-1]['top']} centre={rows[-1]['center']} r={rows[-1]['r']}")

    (OUT / "a3_rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                     encoding="utf-8")

    # ---- 分布与一致性 ----
    print()
    print("=== VLM 标注的分布 ===")
    from collections import Counter
    print(" ", dict(Counter(r["vlm"] for r in rows)))
    print()
    print("=== 本地判别与 VLM 标注的一致数 ===")
    lab = [r for r in rows if r["vlm"] in ("日戳", "风景戳")]
    agree = sum(1 for r in lab if (r["vlm"] == "日戳") == (r["local"] == "daily"))
    print(f"  {agree}/{len(lab)} = {agree / len(lab):.1%}" if lab else "  no labelled rows")

    print()
    print("=== top_arc_text 的分布（按 VLM 标注分组）===")
    for want, name in (("日戳", "daily"), ("风景戳", "scenic")):
        vals = sorted(r["top"] for r in rows if r["vlm"] == want and r["top"] is not None)
        if vals:
            print(f"  {name:<7} n={len(vals)}  min={vals[0]:.3f}  max={vals[-1]:.3f}  "
                  f"中位={vals[len(vals) // 2]:.3f}")
            print(f"          {[round(v, 3) for v in vals]}")
        else:
            print(f"  {name}: none")
    allv = sorted(r["top"] for r in rows if r["top"] is not None)
    if allv:
        gaps = [(allv[i + 1] - allv[i], allv[i], allv[i + 1]) for i in range(len(allv) - 1)]
        gaps.sort(reverse=True)
        print()
        print("=== top_arc_text 排序后最大的三个间隔（阈值应当落在最大的那个里）===")
        for g, lo, hi in gaps[:3]:
            print(f"  间隔 {g:.3f}  在 {lo:.3f} ~ {hi:.3f}  中点 {(lo + hi) / 2:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
