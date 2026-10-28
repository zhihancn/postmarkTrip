#!/usr/bin/env python3
"""V1：极坐标展开到底有没有用？（spec §八 V1）

spec 把这一项标为「**本版最大的一处可能不需要做的工作**，不允许跳过验证直接实现」。
现在两条路都实现了，所以可以真正地量它。

## 方法

同一枚戳，两条路各跑一次，用**同一个字段抽取器**评分：

* **A 整块**：`extract_postmark_region` → OCR
* **B 展开**：`extract_arc_regions`（顶弧 + 底弧条带）→ 逐条 OCR → 合并行

指标：
* ``lines`` —— OCR 返回的行数
* ``chars`` —— 总字符数（"读出了多少字"）
* ``conf`` —— 平均置信度
* ``fields`` —— 抽到几个字段（date/place/office）
* **date 是否命中真值** —— A4 的核心指标，最有说服力

## 为什么用"抽到几个字段"而不是"字符准确率"

字符准确率需要逐字标注（这笔人工成本很高）。而"日期是否抽对"
是一个**端到端的、二值的、与业务直接相关**的指标 ——
它同时反映了"读到了"和"字段规则работ"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.fields import parse_fields
from postmarktrip.recognition.ocr import recognize
from postmarktrip.recognition.postmark_type import classify_postmark
from postmarktrip.recognition.text_regions import (
    extract_arc_regions,
    extract_postmark_region,
)

WORK_LONG_SIDE = 2000

#: 日期真值（与 eval_fields.py 一致，看图人工誊写）
TRUTH_DATE = {
    "清晰黑色日戳特写-1.jpg": "2026-09-25",
    "清晰黑色日戳特写-2.jpg": "2026-09-25",
    "盖的不好-墨太多.jpg": "2026-10-06",
    "盖的不好-邮票歪，不清晰.jpg": "2026-09-25",
    "单张卡整张扫描图.jpg": "2026-10-01",
    "盖的不好-糊掉了.jpg": "2026-09-25",
}

MARGINS = (1.00, 1.15, 1.30)


def evaluate(img: np.ndarray, verdict, truth_date: str) -> dict[str, object]:
    """两条路各跑一次，返回可比指标。"""
    assert verdict.circle is not None

    # ---- A：整块 ----
    best_a: dict[str, object] | None = None
    for margin in MARGINS:
        reg = extract_postmark_region(img, verdict.circle, margin=margin)
        if reg is None:
            continue
        canvas, _bbox = reg
        res = recognize(canvas, orientation="horizontal")
        lines = [(ln.text, ln.confidence) for ln in res.lines]
        ys = [float(np.mean([p[1] for p in ln.box])) for ln in res.lines]
        parsed = parse_fields(lines, region_height=canvas.shape[0], line_y_positions=ys)
        cand = {
            "lines": len(lines),
            "chars": sum(len(t) for t, _ in lines),
            "conf": res.mean_confidence,
            "fields": len(parsed.found),
            "date": parsed.date.value[:10] if parsed.date else "",
        }
        if best_a is None or cand["fields"] > best_a["fields"]:  # type: ignore[operator]
            best_a = cand
    a = best_a or {"lines": 0, "chars": 0, "conf": 0.0, "fields": 0, "date": ""}

    # ---- B：极坐标展开 ----
    regions = extract_arc_regions(img, verdict.circle)
    lines_b: list[tuple[str, float]] = []
    ys_b: list[float] = []
    for r in regions:
        res = recognize(r.image, orientation=r.orientation)
        for ln in res.lines:
            lines_b.append((ln.text, ln.confidence))
            ys_b.append(float(np.mean([p[1] for p in ln.box])))
    # 条带之间没有共同的 y 坐标系，所以用"序号顺序"当位置：
    # 展开顺序是 top 先于 bottom（extract_arc_regions 的产出顺序），
    # 而 sort 会按响应重排 —— 这里直接用响应序，只对"能否抽到日期"有意义
    ys_seq = [float(i) for i in range(len(lines_b))]
    parsed_b = parse_fields(
        lines_b,
        region_height=max(1, len(lines_b) * 2),
        line_y_positions=ys_seq,
    )
    b = {
        "lines": len(lines_b),
        "chars": sum(len(t) for t, _ in lines_b),
        "conf": float(np.mean([c for _, c in lines_b])) if lines_b else 0.0,
        "fields": len(parsed_b.found),
        "date": parsed_b.date.value[:10] if parsed_b.date else "",
    }

    for d in (a, b):
        d["date_ok"] = d["date"] == truth_date
    return {"A_whole": a, "B_unwrap": b}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, default=None)
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[4]
    samples = args.samples or repo / "docs" / "need" / "pic"

    totals = {"A_whole": 0, "B_unwrap": 0}
    n = 0
    print(
        f"{'样本':<30} {'路':<9} {'行':>3} {'字':>4} {'置信':>6} {'字段':>5} {'日期':<12} {'命中':>4}"
    )
    print("-" * 88)
    for path in sorted(samples.glob("*.jpg")):
        truth = TRUTH_DATE.get(path.name)
        if truth is None:
            continue
        img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        scale = WORK_LONG_SIDE / max(h, w)
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        verdict = classify_postmark(img, dpi=600.0 * min(1.0, scale))
        if verdict.circle is None:
            print(f"{path.name:<30} 无戳圈，跳过")
            continue
        n += 1
        res = evaluate(img, verdict, truth)
        for key, label in (("A_whole", "整块"), ("B_unwrap", "展开")):
            d = res[key]
            totals[key] += int(d["date_ok"])
            print(
                f"{path.name if key == 'A_whole' else '':<30} {label:<9} "
                f"{d['lines']:>3} {d['chars']:>4} {d['conf']:>6.2f} {d['fields']:>5} "
                f"{d['date']!s:<12} {'✓' if d['date_ok'] else '✗':>4}"
            )
        print()

    print("=== V1 结论（日期命中率）===")
    for key, label in (("A_whole", "A 整块 OCR"), ("B_unwrap", "B 极坐标展开 + 逐条 OCR")):
        print(f"  {label:<28} {totals[key]}/{n} = {totals[key] / n:.1%}" if n else "  n=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
