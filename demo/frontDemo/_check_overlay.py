#!/usr/bin/env python3
"""把界面要画的叠加**用 Python 重画一遍**，用来验证坐标对齐。

为什么需要：我没有浏览器，不能双击看。但叠加错位是这类界面最常见的失败
（坐标空间不一致 —— 四边形是在**工作尺度**上算的，而预览图若不是同一张
工作图就会整体错位）。所以在提交前用同一套数据、同一个 viewBox 逻辑画一遍，
肉眼确认四边形真的框住了卡、圆真的落在邮戳上。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np

d = Path(sys.argv[1]).resolve() / "docs/frontDemo"
html = (d / "index.html").read_text(encoding="utf-8")
data = json.loads(re.search(r"const DEMO = (\{.*?\n\});\n", html, re.S).group(1))

OUT = d / "_check"
OUT.mkdir(exist_ok=True)

for page in data["pages"]:
    img = cv2.imread(str(d / page["image"]))
    if img is None:
        print(f"cannot read {page['image']}")
        continue
    h, w = img.shape[:2]
    W, H = page["work_size"]
    # 预览图相对工作图的缩放（界面里 SVG 用 viewBox=work_size 自动处理这件事）
    sx, sy = w / W, h / H

    for c in page["cards"]:
        worst = "auto_accept"
        if any(f["decision"] == "reject" for f in c["fields"]):
            worst = "reject"
        elif any(f["decision"] == "needs_review" for f in c["fields"]) or c["queue"]:
            worst = "needs_review"
        col = {"reject": (51, 54, 218), "needs_review": (34, 153, 210)}.get(worst, (67, 160, 46))
        if len(c["quad"]) == 4:
            pts = np.array([[int(x * sx), int(y * sy)] for x, y in c["quad"]], np.int32)
            cv2.polylines(img, [pts], True, col, 3)
            cv2.putText(img, str(c["index"]), (pts[:, 0].min(), pts[:, 1].min() + 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, col, 2)
        if c["circle"]:
            cv2.circle(img, (int(c["circle"]["cx"] * sx), int(c["circle"]["cy"] * sy)),
                       int(c["circle"]["r"] * sx), (253, 139, 56), 2)

    name = Path(page["image"]).stem
    cv2.imwrite(str(OUT / f"{name}_overlay.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    print(f"{name}: {len(page['cards'])} 卡 -> {OUT / (name + '_overlay.jpg')}")
