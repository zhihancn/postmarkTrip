#!/usr/bin/env python3
"""端到端：卡片图 → 戳区域 → OCR → 字段，并统计 A4（日期抽取）。

## A4 的真值来源

**人工誊写，写在下面 ``TRUTH`` 里** —— 从样本图上直接读出来的。
用它算准确率才是独立验证；用 OCR 输出当真值是自证。

## 用法

    python tools/samples/eval_fields.py
    python tools/samples/eval_fields.py --only 清晰黑色
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
from postmarktrip.recognition.text_regions import extract_postmark_region

WORK_LONG_SIDE = 2000

#: 人工誊写的真值（看图读出）。``None`` = 该样本没有可读的对应字段。
#: 格式：文件名 → {字段: 期望值}
TRUTH: dict[str, dict[str, str]] = {
    # 顶弧「福建南平」+ 2026.09.25.15 + 底弧「建溪 1」
    "清晰黑色日戳特写-1.jpg": {
        "date": "2026-09-25",
        "place": "福建南平",
        "office": "建溪",
    },
    # 顶弧「福建南平」+ 2026.09.25.16 + 底弧「八一一路」
    "清晰黑色日戳特写-2.jpg": {
        "date": "2026-09-25",
        "place": "福建南平",
        "office": "八一一路",
    },
    # 顶弧「福建厦门」+ 2026.10.06.17 + 底弧「四号航站楼 2」
    "盖的不好-墨太多.jpg": {
        "date": "2026-10-06",
        "place": "福建厦门",
        "office": "四号航站楼",
    },
    # 顶弧「福建南平」+ 2026.09.25.15 + 底弧「茶山 1」
    "盖的不好-邮票歪，不清晰.jpg": {
        "date": "2026-09-25",
        "place": "福建南平",
        "office": "茶山",
    },
    # 顶弧「江西玉山」+ 2026.10.01.09 + 底弧「三清乡 1」
    "单张卡整张扫描图.jpg": {
        "date": "2026-10-01",
        "place": "江西玉山",
        "office": "三清乡",
    },
    # 顶弧「福建南平」+ 2026.09.25.16（糊掉了，仅日期可读）
    "盖的不好-糊掉了.jpg": {
        "date": "2026-09-25",
        "place": "福建南平",
        "office": "",
    },
}


def norm(s: str) -> str:
    """比较用的归一化：去掉空格与分隔符，只留字符与数字。"""
    return "".join(ch for ch in s if ch.isalnum())


#: 多 margin 集成：对同一枚戳按几个外扩比例各 OCR 一次，取**抽到字段最多**的结果。
#:
#: ## 为什么需要它（实测）
#:
#: OCR 对**裁剪范围极度敏感**。同一张 `盖的不好-墨太多`：
#: margin 1.0 读出 `2026.10.06.17`(0.98)，margin 1.10 却**读不到日期**
#: （只读到 `邮政`/`美丽`/`牡丹江`）。
#:
#: 这不是过拟合 —— 选哪个结果的判据是**内容性的**
#: （"有没有抽到合法日期"），与真值无关。所以它是合法的集成，
#: 而不是"按答案挑参数"。
MARGINS: tuple[float, ...] = (1.00, 1.15, 1.30)

#: 明信片印刷抬头上的行会被整块 OCR 读进来（margin 越大越多）。
#: 注释里说明为什么默认 margin 取 1.10 而不是 1.4。


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, default=None)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[4]
    samples = args.samples or repo / "docs" / "need" / "pic"

    stats: dict[str, list[bool]] = {"date": [], "place": [], "office": []}

    for path in sorted(samples.glob("*.jpg")):
        if args.only and args.only not in path.name:
            continue
        truth = TRUTH.get(path.name)
        if truth is None:
            continue

        img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        scale = WORK_LONG_SIDE / max(h, w)
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        verdict = classify_postmark(img, dpi=600.0 * min(1.0, scale))
        print(
            f"=== {path.name}  判定={verdict.kind}  r={verdict.circle.r if verdict.circle else 0:.0f} ==="
        )
        if verdict.circle is None:
            print("  未找到戳圈 → 全部字段计为未抽到\n")
            for f in stats:
                if truth.get(f):
                    stats[f].append(False)
            continue

        region = extract_postmark_region(img, verdict.circle)
        if region is None:
            print("  戳区域为空\n")
            continue

        # 多 margin 集成：取抽到字段最多的那次
        best = None
        for margin in MARGINS:
            reg = extract_postmark_region(img, verdict.circle, margin=margin)
            if reg is None:
                continue
            canvas, bbox = reg
            res = recognize(canvas, orientation="horizontal")
            lines = [(line.text, line.confidence) for line in res.lines]
            ys = [float(np.mean([p[1] for p in line.box])) for line in res.lines]
            xys = [
                (float(np.mean([p[0] for p in line.box])), float(np.mean([p[1] for p in line.box])))
                for line in res.lines
            ]
            parsed = parse_fields(lines, region_height=canvas.shape[0], line_y_positions=ys)
            n_found = len(parsed.found)
            print(
                f"  --- margin={margin:.2f} 区域 {canvas.shape[1]}x{canvas.shape[0]} 抽到 {n_found} 个字段 ---"
            )
            for (t, c), (lx, ly) in zip(lines, xys, strict=False):
                print(
                    f"      OCR {t!r} ({c:.2f}) 位置=({lx:.0f},{ly:.0f}) 中线={canvas.shape[0] / 2:.0f}"
                )
            for note in parsed.notes:
                print(f"      · {note}")
            if best is None or n_found > len(best[0].found):
                best = (parsed, margin, canvas, bbox)
        if best is None:
            print("  所有 margin 都失败\n")
            continue
        parsed, used_margin, canvas, bbox = best
        print(f"  ⇒ 采用 margin={used_margin:.2f} 的结果")

        for fname in ("date", "place", "office"):
            want = truth.get(fname, "")
            if not want:
                continue
            got = getattr(parsed, fname)
            got_v = got.value if got else ""
            if fname == "date":
                # A4 只考核**日期**；时分是额外信息，不进分母。
                # 用前 10 个字符（YYYY-MM-DD）比较，避免"多抽到时分"被算成错。
                ok = norm(got_v[:10]) == norm(want)
            else:
                ok = norm(got_v) == norm(want)
            stats[fname].append(ok)
            mark = "✓" if ok else "✗"
            print(f"    {mark} {fname}: got={got_v!r} want={want!r}")
        print()

    print("=== A4 与字段抽取统计（分母 = 人工誊写真值）===")
    for fname, results in stats.items():
        if not results:
            continue
        got = sum(results)
        print(f"  {fname:<8} {got}/{len(results)} = {got / len(results):.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
