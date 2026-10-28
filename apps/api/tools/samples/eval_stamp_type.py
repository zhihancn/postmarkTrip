#!/usr/bin/env python3
"""戳型判别的样本标注 + 阈值标定 + 准确率统计（spec §3.3 / A3）。

## 用法

::

    # 1) 先看每张样本的特征值（人工据此确认标注是否合理）
    python tools/samples/eval_stamp_type.py --features

    # 2) 用标注统计准确率，并给出每个特征维度的最佳单阈值
    python tools/samples/eval_stamp_type.py

    # 3) 扫描阈值网格，找使准确率最高的一组
    python tools/samples/eval_stamp_type.py --sweep

## 为什么标注写在文件里而不是"自动判定"

**用算法输出当真值去算准确率是自证。** 所以这里的人工标注是独立的一份数据，
与算法的特征值分开。spec §七 A3 要求"在 11 张样本 + 补充样本上统计，
**数字写进实现说明**" —— 这份标注就是那个"统计"的依据。

## 标注来源

* `need/03` §三.1 与 §三.3 的实测记录（日戳样本布局、风景戳样本布局）
* `spec/v1.0` §3.3 的判据表
* 对样本图的逐张查看

**未看图确认的样本一律标 ``None``（不计入统计）**，而不是猜一个 ——
标注是准确率的分母，猜出来的分母会让数字失去意义。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.postmark_type import (
    TYPE_DAILY,
    TYPE_SCENIC,
    TypeThresholds,
    center_image_ness,
    classify_postmark,
    find_postmark_circle,
    top_arc_text_response,
)

#: 人工标注。``None`` = 未确认，不计入准确率统计。
#:
#: 依据（逐条可追溯）：
#:   * 清晰黑色日戳特写-1 —— `need/03` §三.3 明确为"标准日戳"。**看图确认**：
#:     顶弧「福建南平」+ `2026.09.25.15` + 底弧「建溪 1」，无中心图案。
#:   * 清晰黑色日戳特写-2 —— 同上布局。**看图确认。**
#:   * 风景戳特写(来源-求戳信) —— `need/03` §三.3 明确为"顶弧空、名称横排在中部"。
#:     **看图确认**：中部横排「中国工农红军北上抗日先遣队纪念碑」+ 线描图，
#:     底弧「江西玉山 1」。**顶弧确实为空。**
#:   * 彩色戳特写 —— 晋祠天龙山主题邮局戳。`need/03` §三.3 归为风景/主题邮局一类
#:     （顶弧空、名称在右侧竖弧、书法融入图案）。**B 类文字**（以图搜戳的正解场景）。
#:   * 盖的不好-墨太多 —— **看图确认**：顶弧「福建厦门」+ `2026.10.06.17` +
#:     底弧「四号航站楼 2」，无中心图案 → 日戳。
#:   * 盖的不好-邮票歪，不清晰 —— **看图确认**：顶弧「福建南平」+
#:     `2026.09.25.15` + 底弧「茶山 1」→ 日戳。
#:
#: ⚠️ **刻意留 None 的样本**（看图后仍不确定，不猜）：
#:   * 盖的不好-有污渍 —— 顶弧「江西玉山」+ `2026.10.01.09` + 底弧「三清乡 1」。
#:     结构上是日戳，但「三清」这个地名与那枚三清山**风景戳**同名，
#:     而中心区被邮票压住、无法确认有没有图案。**标 None。**
#:   * 盖的不好-墨太少 / 糊掉了 —— 图形太弱，看图无法可靠区分。
#:
#: 判据缺失时**宁可留空**：标注是准确率的分母，猜出来的分母让数字失去意义。
LABELS: dict[str, str | None] = {
    # --- 已看图确认 ---
    "清晰黑色日戳特写-1.jpg": TYPE_DAILY,
    "清晰黑色日戳特写-2.jpg": TYPE_DAILY,
    "风景戳特写(来源-求戳信).jpg": TYPE_SCENIC,
    "彩色戳特写.jpg": TYPE_SCENIC,
    "盖的不好-墨太多.jpg": TYPE_DAILY,
    "盖的不好-邮票歪，不清晰.jpg": TYPE_DAILY,
    # --- 未确认（看图后仍不确定，或尚未逐张看图）---
    # 整页扫描里有多枚戳，而本模块一次只判一枚 —— 它们不适合单枚判别的统计
    "单张卡整张扫描图.jpg": None,
    "多张卡混排扫描图.jpg": None,
    "黑色背景.jpg": None,
    "1200dpi.jpg": None,
    # 图形太弱或中心被邮票压住，看图无法可靠区分
    "盖的不好-有污渍.jpg": None,
    "盖的不好-墨太少.jpg": None,
    "盖的不好-糊掉了.jpg": None,
}


def load_gray(path: Path) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"读不出：{path}")
    return img


#: 处理时把长边缩到这个尺寸。**不是可选优化**：整页扫描是 5072×7014，
#: 在整幅上跑 HoughCircles 要几十秒；而戳的定位不需要满分辨率
#: （600dpi 下戳径约 590px，缩到 2000px 后仍有约 230px，足够）。
#: 代价是半径的数字不再等于"原图上的像素"，所以报告里要标明这一层缩放。
WORK_LONG_SIDE = 2000


def prepare(path: Path) -> tuple[np.ndarray, float]:
    """读图并**按需缩小**，返回 (图, 缩放比)。"""
    img = load_gray(path)
    h, w = img.shape[:2]
    scale = WORK_LONG_SIDE / max(h, w)
    if scale >= 1.0:
        return img, 1.0
    return (
        cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA),
        scale,
    )


def features(path: Path) -> dict[str, object]:
    img, scale = prepare(path)
    # 名义 600dpi：这批样本里整页扫描与卡片特写的实际 dpi 都接近它
    # （见 ok/10 的分析），而"戳径 25mm"是物理常数，所以这个先验是可用的。
    dpi = 600.0 * scale
    circle = find_postmark_circle(img, dpi=dpi)
    out: dict[str, object] = {
        "file": path.name,
        "size": list(img.shape[:2]),
        "scale": round(scale, 4),
        "label": LABELS.get(path.name),
    }
    if circle is None:
        out |= {"circle": None, "top_arc_text": None, "center_image": None}
        return out
    out |= {
        "circle": {
            "cx": round(circle.cx, 1),
            "cy": round(circle.cy, 1),
            "r": round(circle.r, 1),
            "r_mm": round(circle.r / (dpi / 25.4), 1),
            "quality": round(circle.quality, 3),
        },
        "top_arc_text": round(top_arc_text_response(img, circle), 4),
        "center_image": round(center_image_ness(img, circle), 4),
    }
    return out


def accuracy(rows: list[dict[str, object]], t: TypeThresholds) -> dict[str, object]:
    """在**有标注**的样本上算准确率。"""
    labelled = [r for r in rows if r.get("label")]
    if not labelled:
        return {"n": 0, "correct": 0, "accuracy": 0.0, "details": []}

    correct = 0
    details = []
    for r in labelled:
        path = Path(str(r["_path"]))
        img = load_gray(path)
        v = classify_postmark(img, thresholds=t)
        ok = v.kind == r["label"]
        correct += ok
        details.append(
            {
                "file": r["file"],
                "label": r["label"],
                "predicted": v.kind,
                "correct": ok,
                "confidence": v.confidence,
                "top_arc_text": round(v.top_arc_text, 3),
                "center_image": round(v.center_image, 3),
                "reason": v.reason,
            }
        )
    return {
        "n": len(labelled),
        "correct": correct,
        "accuracy": round(correct / len(labelled), 4),
        "details": details,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, default=None)
    ap.add_argument("--features", action="store_true", help="只打印特征值")
    ap.add_argument("--sweep", action="store_true", help="网格搜索阈值")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[4]
    samples = args.samples or repo / "docs" / "need" / "pic"

    rows: list[dict[str, object]] = []
    for path in sorted(samples.glob("*.jpg")):
        r = features(path)
        r["_path"] = str(path)
        rows.append(r)

    labelled_n = sum(1 for r in rows if r.get("label"))
    print(f"样本 {len(rows)} 张，其中有标注 {labelled_n} 张")
    print()
    print(f"{'文件':<32} {'标注':>7} {'戳径mm':>8} {'质量':>6} {'顶弧文字':>9} {'中心图像':>9}")
    print("-" * 80)
    for r in rows:
        c = r.get("circle")
        r_txt = f"{c['r_mm']:.1f}" if isinstance(c, dict) else "—"
        q_txt = f"{c['quality']:.2f}" if isinstance(c, dict) else "—"
        top = f"{r['top_arc_text']:.3f}" if r.get("top_arc_text") is not None else "—"
        ctr = f"{r['center_image']:.3f}" if r.get("center_image") is not None else "—"
        print(
            f"{r['file']:<32} {r.get('label') or '?'!s:>7} {r_txt:>8} {q_txt:>6} {top:>9} {ctr:>9}"
        )

    if args.features:
        return 0

    print()
    t = TypeThresholds()
    res = accuracy(rows, t)
    print(f"=== 当前阈值 {t} ===")
    print(f"  准确率 {res['correct']}/{res['n']} = {res['accuracy']:.1%}")
    print(
        "  ⚠️ 分母很小（只有已看图确认标注的样本才计入）。spec §7 的 A3 要求"
        "『在 11 张样本 + 补充样本上统计』，所以**这个百分比目前不构成 A3 的验证** ——\n"
        "     它只说明判据在已确认的这几张上方向正确。分母要扩大，"
        "办法是逐张看图确认 LABELS 里那些 None。"
    )
    for d in res["details"]:
        mark = "✓" if d["correct"] else "✗"
        print(
            f"  {mark} {d['file']:<32} 标注={d['label']:<7} 判定={d['predicted']:<8} "
            f"conf={d['confidence']:.2f}  顶弧={d['top_arc_text']:.3f} 中心={d['center_image']:.3f}"
        )
        if not d["correct"]:
            print(f"      {d['reason']}")

    if args.sweep:
        print()
        print("=== 阈值网格搜索（只扫有标注的样本）===")
        best: tuple[float, float, float] | None = None
        for top_thr in np.arange(0.10, 0.70, 0.02):
            for ctr_thr in np.arange(0.15, 0.75, 0.02):
                tt = TypeThresholds(
                    top_arc_text=round(float(top_thr), 3),
                    center_image=round(float(ctr_thr), 3),
                )
                a = accuracy(rows, tt)["accuracy"]
                if best is None or a > best[0]:
                    best = (float(a), tt.top_arc_text, tt.center_image)
        if best:
            print(f"  最佳：准确率 {best[0]:.1%} @ top_arc_text={best[1]}, center_image={best[2]}")
            print("  ⚠️ 标注样本数少时网格搜索会过拟合 —— 这两个数字要连同 n 一起看。")

    if args.json:
        args.json.write_text(
            json.dumps(
                {"rows": rows, "thresholds": asdict(t), "accuracy": res},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nJSON → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
