#!/usr/bin/env python3
"""Inspect the cloud vs local pages side by side, field by field."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

d = Path(sys.argv[1]) / "docs/frontDemo"
html = (d / "index.html").read_text(encoding="utf-8")
data = json.loads(re.search(r"const DEMO = (\{.?*?\n\});\n", html, re.S).group(1)) if False else None
m = re.search(r"const DEMO = (\{.*?\n\});\n", html, re.S)
data = json.loads(m.group(1))

by_slug = {Path(p["image"]).stem: p for p in data["pages"]}

for local_slug, cloud_slug in (("p005", "p005_cloud"), ("ink_too_much", "ink_too_much_cloud")):
    lo, cl = by_slug.get(local_slug), by_slug.get(cloud_slug)
    if not lo or not cl:
        print(f"missing {local_slug} or {cloud_slug}")
        continue
    print(f"\n{'=' * 78}\n{local_slug}  vs  {cloud_slug}\n{'=' * 78}")
    for i in range(max(len(lo["cards"]), len(cl["cards"]))):
        lc = lo["cards"][i] if i < len(lo["cards"]) else None
        cc = cl["cards"][i] if i < len(cl["cards"]) else None
        if lc is None or cc is None:
            continue
        lf = {f["field_name"]: f["value"] for f in lc["fields"]}
        cf = {f["field_name"]: f["value"] for f in cc["fields"]}
        keys = ["date", "place", "office", "office_no", "postmark_type"]
        print(f"\n-- card {i}   本地来源={lc['field_source']}  云端来源={cc['field_source']}")
        print(f"   本地戳型={lc['postmark_type']:<8} 云端戳型={cc['postmark_type']}")
        for k in keys:
            a, b = lf.get(k, ""), cf.get(k, "")
            mark = "  <== 不同" if a != b else ""
            print(f"   {k:<13} 本地={a!r:<18} 云端={b!r}{mark}")
        # 云端原始 OCR 行，看它到底读到了什么
        if cc["field_source"] == "vlm":
            print(f"   云端原文行：{[t for t, _ in cc['ocr_lines']]}")
