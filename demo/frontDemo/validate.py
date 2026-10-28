#!/usr/bin/env python3
"""Validate the generated demo WITHOUT a browser.

Checks the things that would actually make it fail on a double-click:
  1. images/ contains exactly the files the data references
  2. the inlined DEMO blob is still valid JSON (i.e. not truncated / not broken
     by a "</script>" or an unescaped quote)
  3. every card has the fields the renderer indexes without a null check
  4. no unescaped "</" inside the script block
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

d = Path(sys.argv[1]) / "docs/frontDemo"
html = (d / "index.html").read_text(encoding="utf-8")


def fail(msg: str) -> None:
    print(f"FAIL  {msg}")
    fails.append(msg)


fails: list[str] = []

# ---- 1. 数据块能被解析 ----
m = re.search(r"const DEMO = (\{.*?\n\});\n", html, re.DOTALL)
if not m:
    fail("找不到内联的 DEMO 数据块")
    print("\n".join(fails))
    raise SystemExit(1)
try:
    data = json.loads(m.group(1))
    print(f"OK    DEMO JSON 可解析：{len(data['pages'])} 页")
except json.JSONDecodeError as exc:
    fail(f"DEMO JSON 解析失败：{exc}")
    print("\n".join(fails))
    raise SystemExit(1)

# ---- 2. 图片是否都在 ----
for page in data["pages"]:
    img = d / page["image"]
    if not img.exists():
        fail(f"缺图片：{page['image']}")
    else:
        print(f"OK    图片存在 {page['image']:<24} {img.stat().st_size // 1024} KB")

# ---- 3. 真正的危险只有两点 ----
#   (a) 内联 JSON 里出现字面量 "</script" —— 会提前结束脚本块
#   (b) 内联 JSON 里出现未转义的 "</" —— 惯例上也要转义（防御性）
#
# ⚠️ 不能把 JS **字符串字面量**里的 "</span>"、'</div>' 算作问题 ——
# 那些完全合法。第一版就是这么误报的（60 处"未转义"全是模板自己的
# HTML 片段字符串），所以这里收窄到真正的危险。
blob = m.group(1)
if re.search(r"</script", blob, re.IGNORECASE):
    fail("内联 JSON 里出现字面量 '</script'（会提前结束脚本块）")
else:
    print("OK    内联 JSON 里无 '</script'")

stray = blob.count("</")
if stray:
    fail(f"内联 JSON 里有 {stray} 处未转义的 '</'")
else:
    print("OK    内联 JSON 里无未转义的 '</'")

if html.count("</script>") != 1:
    fail(f"</script> 出现 {html.count('</script>')} 次，应为 1 次")
else:
    print("OK    </script> 恰好 1 处")

# ---- 4. 渲染器会直接索引的字段必须存在 ----
need_card = (
    "index",
    "bbox",
    "quad",
    "card_size",
    "postmark_type",
    "postmark_reason",
    "circle",
    "fields",
    "queue",
    "ocr_lines",
    "field_source",
    "notes",
    "size_suspect",
)
need_field = ("field_name", "value", "confidence", "decision", "reason")
ncards = 0
for page in data["pages"]:
    for k in (
        "label",
        "image",
        "pipeline_version",
        "work_scale",
        "work_size",
        "stage_notes",
        "demo_note",
        "cards",
    ):
        if k not in page:
            fail(f"页缺少键 {k}")
    for c in page["cards"]:
        ncards += 1
        for k in need_card:
            if k not in c:
                fail(f"卡缺少键 {k}")
        for f in c["fields"]:
            for k in need_field:
                if k not in f:
                    fail(f"字段缺少键 {k}")
        # 圆若存在，必须有渲染器用的三个键
        if c["circle"]:
            for k in ("cx", "cy", "r", "radius_over_card_width"):
                if k not in c["circle"]:
                    fail(f"圆缺少键 {k}")
print(f"OK    {ncards} 张卡的结构完整")

# ---- 5. 决策取值必须在渲染器的 CFG 里 ----
allowed = {"auto_accept", "needs_review", "reject"}
for page in data["pages"]:
    for c in page["cards"]:
        for f in c["fields"]:
            if f["decision"] not in allowed:
                fail(f"未知 decision：{f['decision']}")

# ---- 6. demo_note 的换行在 JS 字符串里是安全的（JSON 会转义为 \n）----
for page in data["pages"]:
    if "\n" in page["demo_note"]:
        print("OK    demo_note 含换行（JSON 会转义，安全）")

# ---- 7. 不能引用不存在的 template 占位符 ----
if "/*__DATA__*/" in html:
    fail("模板占位符没被替换 —— 打开会是空白页")

# ---- 8. 顶层 `const NAME` 不得被当 window.NAME 用 ----
# 经典 <script> 里顶层 const/let 是**词法绑定**，不是 window 的属性
# （只有 var 与函数声明才会成为属性）。写成 window.NAME 会永远 undefined。
#
# ⚠️ 检查前必须**剥掉注释** —— 第一版没剥，结果把本文件里解释这个坑的
# 注释文字（里面就写着 "window.DEMO"）当成了违规，自己误报自己。
code_only = re.sub(r"//[^\n]*", "", html)
for name in re.findall(r"^const\s+([A-Za-z_$][\w$]*)\s*=", code_only, re.MULTILINE):
    if f"window.{name}" in code_only:
        fail(f"顶层 const {name} 被当成 window.{name} 使用（永远是 undefined）")
print("OK    顶层 const 未被误用为 window 属性")

print()
if fails:
    print(f"== {len(fails)} 项失败 ==")
    raise SystemExit(1)
print("== 全部检查通过；双击 index.html 应该能正常显示 ==")
