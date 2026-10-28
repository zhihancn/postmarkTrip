#!/usr/bin/env python3
"""能否把**尾随的戳号**捞回来？逐个试廉价修法。

已确认的事实（见 diag_office_no.py 的输出与区域图）：

* 那个数字**在裁剪框里**（区域图 `福屿1` 清晰可见）→ **不是裁剪问题**
* 但 OCR 的**行列表里根本没有它**（不是被置信度过滤掉的 —— 是检测阶段没提出框）
* 它是**单个、旋转、与局名有间隔**的小字 —— 这类目标最容易在检测阶段被漏掉

所以这里试四类廉价变体，看哪一种能让它出现：

  1. 放大（小字检测通常随放大改善）
  2. 只取**底弧**再极坐标展开（把旋转的字摆平）
  3. 展开 + 放大
  4. 展开后**裁到局名那一段**（减少无关内容）

判据：输出的行里出现**独立的 1~2 位数字**，且不与日期同行。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "apps/api/src"))

from postmarktrip.recognition.ocr import recognize  # noqa: E402
from postmarktrip.recognition.postmark_type import (  # noqa: E402
    arc_angles,
    classify_postmark,
    find_text_band,
    polar_unwrap,
)
from postmarktrip.recognition.split import split_page  # noqa: E402
from postmarktrip.recognition.text_regions import extract_postmark_region  # noqa: E402

WORK = 2000
#: (样本, 卡序号, 期望出现的局名, 期望的戳号)
CASES = [
    ("更多样本/005.jpg", 3, "福屿", "1"),
    ("更多样本/005.jpg", 0, "中亭街", "1"),
    ("更多样本/005.jpg", 4, "西洪", "1"),
    ("更多样本/005.jpg", 6, "滨江北路", "1"),
    ("清晰黑色日戳特写-1.jpg", 0, "建溪", ""),
]


def up(img: np.ndarray, k: float) -> np.ndarray:
    if k <= 1.0:
        return img
    h, w = img.shape[:2]
    return cv2.resize(img, (int(w * k), int(h * k)), interpolation=cv2.INTER_CUBIC)


def lone_digits(lines: list[str]) -> list[str]:
    """找出「独立的小数字」—— 排除日期行（含 . 或长度 > 3）。"""
    out = []
    for t in lines:
        s = t.strip()
        if s.isdigit() and 1 <= len(s) <= 2:
            out.append(s)
    return out


def variants(card: np.ndarray, circle: object) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    got = extract_postmark_region(card, circle, margin=1.10)
    if got is None:
        return out
    region, _ = got
    out["1 区域原样"] = region
    out["2 区域放大2x"] = up(region, 2.0)
    out["3 区域放大3x"] = up(region, 3.0)

    band = find_text_band(card, circle)
    lo = band.r_inner / circle.r if band else 0.55
    hi = band.r_outer / circle.r if band else 0.98
    a0, a1 = arc_angles("bottom")
    strip = polar_unwrap(
        card, circle, angle_from_deg=a0, angle_to_deg=a1,
        r_from_ratio=lo, r_to_ratio=hi, out_height=110,
    )
    out["4 底弧展开"] = strip
    out["5 底弧展开2x"] = up(strip, 2.0)
    out["6 底弧展开3x"] = up(strip, 3.0)
    # 展开后裁右半（局名通常在底弧的一侧、戳号在其外侧）
    w = strip.shape[1]
    out["7 展开右半2x"] = up(strip[:, w // 2 :], 2.0)
    out["8 展开左半2x"] = up(strip[:, : w // 2], 2.0)
    return out


def main() -> int:
    for rel, idx, office, want_no in CASES:
        p = REPO / "docs" / "need" / "pic" / rel
        if not p.exists():
            print(f"missing {rel}")
            continue
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        H, W = img.shape[:2]
        s = WORK / max(H, W)
        small = cv2.resize(img, (int(W * s), int(H * s)), interpolation=cv2.INTER_AREA)
        work_dpi = 600.0 * min(s, 1.0)
        if rel.startswith("更多样本"):
            sp = split_page(small, dpi=600, detection_scale=s)
            if idx >= len(sp.candidates):
                continue
            card = sp.candidates[idx].ortho
        else:
            card = small

        v = classify_postmark(card, dpi=work_dpi)
        if v.circle is None:
            continue
        print(f"\n=== {rel} card{idx}  局名={office}  期望戳号={want_no!r}  r={v.circle.r:.0f} ===")
        base = recognize(card)
        base_office_hit = any(office in line.text for line in base.lines)
        print(f"  整卡（基线）：局名命中={base_office_hit} "
              f"独立数字={lone_digits([l.text for l in base.lines])}")
        for name, im in variants(card, v.circle).items():
            if im.size == 0:
                continue
            res = recognize(im)
            lines = [l.text for l in res.lines]
            hit = any(office[:2] in t for t in lines)
            digits = lone_digits(lines)
            mark = ""
            if want_no and want_no in digits:
                mark = "  ★★★ 捞到了！"
            elif not want_no and not digits:
                mark = "  （该样本本就无戳号，符合预期）"
            print(f"  {name:<14} {im.shape[1]:>5}x{im.shape[0]:<5} "
                  f"局名≈{hit}  独立数字={digits}{mark}")
            print(f"                 行：{[(l.text, round(l.confidence, 2)) for l in res.lines]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
