#!/usr/bin/env python3
"""A1 / A2 在新样本上的实测。

用户新给：

* ``黑色背景-单张.jpg`` —— **深色垫底 + 单卡** → 这是 A1（≥95%）缺的那张
* ``更多样本/002..005.jpg`` —— 每张 **9 枚**（005 的第 2、3 格只有邮票、无戳）
  → A2 的更多整页，且 005 可兼作邮票定位标定

真值是**用户告知的**（"每张图片都有9枚戳"、"005 第二三张只有邮票"）。

用法：
    python tools/samples/eval_a1a2_new.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.pipeline import recognize_page

#: 真值：路径 → (期望卡片数, 说明)
CASES = [
    ("黑色背景-单张.jpg", 1, "A1：深色垫底单卡"),
    ("更多样本/002.jpg", 9, "A2：整页 9 枚"),
    ("更多样本/003.jpg", 9, "A2：整页 9 枚"),
    ("更多样本/004.jpg", 9, "A2：整页 9 枚"),
    ("更多样本/005.jpg", 9, "A2：整页 9 枚（第 2/3 格无戳）"),
]


def main() -> int:
    repo = Path(__file__).resolve().parents[4]
    pic = repo / "docs" / "need" / "pic"

    rows = []
    for rel, expect, note in CASES:
        path = pic / rel
        if not path.exists():
            print(f"⚠️ 不存在：{rel}")
            continue
        img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            print(f"⚠️ 读不出：{rel}")
            continue
        H, W = img.shape[:2]
        # dpi=600：用户告知这些是从 600dpi 扫描/裁剪而来
        res = recognize_page(img, dpi=600)
        got = res.card_count
        ok = "✓" if got == expect else "✗"
        rows.append((rel, expect, got, res.split.level, res.work_scale, note))

        print(f"=== {rel}  {W}x{H}  （{note}）===")
        print(
            f"  期望 {expect} 枚 → 切出 {got} 枚 {ok}   切分级={res.split.level} "
            f"工作尺度={res.work_scale:.3f}"
        )
        for n in res.stage_notes[-2:]:
            if n.strip():
                print(f"    · {n[:110]}")
        for c in res.cards[:3]:
            d = c.parsed.date.value[:10] if c.parsed and c.parsed.date else "-"
            p = c.parsed.place.value if c.parsed and c.parsed.place else "-"
            print(f"    [{c.index}] {c.postmark_type:<7} date={d:<10} place={p}")
        if got > 3:
            print(f"    … 其余 {got - 3} 枚略")
        print()

    print("=== A1 / A2 汇总（分母 = 用户告知的真值）===")
    print(f"{'样本':<24} {'期望':>4} {'切出':>4} {'级别':>5} {'判定':>5}")
    for rel, expect, got, lvl, sc, _ in rows:
        print(f"{rel:<24} {expect:>4} {got:>4} {lvl:>5} {'✓' if got == expect else '✗':>5}")

    exact = sum(1 for _, e, g, _, _, _ in rows if e == g)
    cards_expect = sum(e for _, e, _, _, _, _ in rows)
    cards_got = sum(g for _, _, g, _, _, _ in rows)
    print()
    print(f"  图像级：{exact}/{len(rows)} = {exact / len(rows):.1%}")
    print(f"  卡片级：{cards_got}/{cards_expect} = {cards_got / cards_expect:.1%}")
    print()
    print("  注：图像级要求张数**恰好相等**（多切一张也算错），")
    print("      卡片级只看总数，所以会出现「图像级错但卡片级对」的情形。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
