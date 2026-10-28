#!/usr/bin/env python3
"""V2：RapidOCR vs PaddleOCR 在中文邮戳上的 A/B（spec §八 V2）。

## 方法论（与 V1 故意保持一致）

同一块戳区域、**同一个字段抽取器**、**同一份人工誊写真值**，
只换 OCR 引擎。主指标用 **日期命中率** ——
它同时反映"读到了"与"字段规则работа"，是端到端且与业务直接相关的二值指标。

这样才能回答"该选哪个引擎"，而不是"哪个引擎输出的字多"。

## 引擎适配层

两个引擎的 API 完全不同（RapidOCR 直接 `engine(img)` 返回 `[box,text,conf]`；
PaddleOCR 3.x 是 `engine.predict(img)` 返回带 `rec_texts`/`rec_scores` 的结果对象）。
所以这里做一层薄适配，**把差异挡在适配器里**，让评分逻辑与引擎无关。

## 用法

    python tools/samples/v2_ocr_engines_ab.py              # 两个引擎都跑（缺的跳过并说明）
    python tools/samples/v2_ocr_engines_ab.py --only rapidocr
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.fields import parse_fields
from postmarktrip.recognition.postmark_type import classify_postmark
from postmarktrip.recognition.text_regions import (
    extract_postmark_region,
    postmark_diameter_hint_px,
)

WORK_LONG_SIDE = 2000

#: 人工誊写的日期真值（与 eval_fields.py / v1_unwrap_ab.py 一致）
TRUTH_DATE = {
    "清晰黑色日戳特写-1.jpg": "2026-09-25",
    "清晰黑色日戳特写-2.jpg": "2026-09-25",
    "盖的不好-墨太多.jpg": "2026-10-06",
    "盖的不好-邮票歪，不清晰.jpg": "2026-09-25",
    "单张卡整张扫描图.jpg": "2026-10-01",
    "盖的不好-糊掉了.jpg": "2026-09-25",
}


def _pad(img: np.ndarray, ratio: float = 0.12) -> np.ndarray:
    """给区域加白边。实测这一步对两个引擎都关键（见 ocr._prepare 的对照表）。"""
    h, w = img.shape[:2]
    py, px = max(8, int(h * ratio)), max(8, int(w * ratio))
    canvas = np.full((h + 2 * py, w + 2 * px, 3), 255, np.uint8)
    canvas[py : py + h, px : px + w] = img
    return canvas


# --------------------------------------------------------------------------- #
#  引擎适配器：每个返回 [(text, confidence, y_center), ...]
# --------------------------------------------------------------------------- #


def run_rapidocr(canvas: np.ndarray) -> tuple[list[tuple[str, float, float]], str]:
    from postmarktrip.recognition.ocr import recognize

    res = recognize(canvas, orientation="horizontal")
    if not res.available:
        return [], res.reason
    out = []
    for ln in res.lines:
        y = float(np.mean([p[1] for p in ln.box])) if ln.box else 0.0
        out.append((ln.text, ln.confidence, y))
    return out, ""


_PADDLE_ENGINE: object | None = None


def _paddle_engine() -> tuple[object | None, str]:
    """构造 PaddleOCR 引擎（缓存）。3.x 与 2.x 的参数名不同，都试一遍。"""
    global _PADDLE_ENGINE
    if _PADDLE_ENGINE is not None:
        return _PADDLE_ENGINE, ""
    try:
        from paddleocr import PaddleOCR
    except Exception as exc:
        return None, f"无法导入 paddleocr：{type(exc).__name__}: {exc}"
    # ⚠️ `enable_mkldnn: False` **不是可选优化，是必需的**。
    #
    # 实测：paddlepaddle 3.3.1 的 oneDNN 后端在 predict() 时抛
    #   NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support
    #     [pir::ArrayAttribute<pir::DoubleAttribute>]  (onednn_instruction.cc:116)
    # 这是**库级 bug**（PIR 属性在 oneDNN 路径上没实现），不是我们的代码问题。
    # 关掉 oneDNN 后同一张图上它读出了 RapidOCR 读错的字：
    #   PaddleOCR: 福建南平(0.999)   RapidOCR: 福建南火(0.87)
    # 环境变量 FLAGS_use_mkldnn=0 也有效，但写在构造参数里更明确、不依赖外部设置。
    attempts = (
        {
            "lang": "ch",
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "enable_mkldnn": False,
        },  # 3.x + 关 oneDNN
        {"lang": "ch", "enable_mkldnn": False},  # 最小参数 + 关 oneDNN
        {"lang": "ch", "use_angle_cls": False, "show_log": False, "enable_mkldnn": False},
    )
    last = ""
    for kwargs in attempts:
        try:
            _PADDLE_ENGINE = PaddleOCR(**kwargs)
            return _PADDLE_ENGINE, ""
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
    return None, f"PaddleOCR 构造失败（试了 3 组参数）：{last}"


def run_paddleocr(canvas: np.ndarray) -> tuple[list[tuple[str, float, float]], str]:
    engine, err = _paddle_engine()
    if engine is None:
        return [], err

    raw = None
    for call in ("predict", "ocr"):
        fn = getattr(engine, call, None)
        if fn is None:
            continue
        try:
            raw = fn(canvas)
            break
        except Exception as exc:
            err = f"{call}() 失败：{type(exc).__name__}: {exc}"
    if raw is None:
        return [], err or "PaddleOCR 没有可用的调用入口"

    out: list[tuple[str, float, float]] = []
    # 3.x：结果对象带 rec_texts / rec_scores / rec_polys（或 dt_polys）
    for item in raw if isinstance(raw, list) else [raw]:
        texts = getattr(item, "get", lambda *_: None)("rec_texts") if hasattr(item, "get") else None
        if texts is None and isinstance(item, dict):
            texts = item.get("rec_texts")
        if texts:
            scores = None
            if hasattr(item, "get"):
                scores = item.get("rec_scores")
            scores = scores if scores is not None else [1.0] * len(texts)
            polys = None
            if hasattr(item, "get"):
                polys = item.get("rec_polys") or item.get("dt_polys")
            for i, t in enumerate(texts):
                y = 0.0
                if polys is not None and i < len(polys):
                    try:
                        y = float(np.mean([p[1] for p in polys[i]]))
                    except Exception:
                        y = 0.0
                c = float(scores[i]) if i < len(scores) else 1.0
                if str(t).strip():
                    out.append((str(t).strip(), c, y))
            continue
        # 2.x：[[box, (text, score)], ...]
        if isinstance(item, (list, tuple)):
            for entry in item:
                try:
                    box, (text, score) = entry[0], entry[1]
                    y = float(np.mean([p[1] for p in box]))
                    if str(text).strip():
                        out.append((str(text).strip(), float(score), y))
                except Exception:
                    continue
    return out, ""


ENGINES = {"rapidocr": run_rapidocr, "paddleocr": run_paddleocr}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, default=None)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[4]
    samples = args.samples or repo / "docs" / "need" / "pic"
    engines = [args.only] if args.only else list(ENGINES)

    stats: dict[str, list[bool]] = {e: [] for e in engines}
    chars: dict[str, list[int]] = {e: [] for e in engines}
    times: dict[str, list[float]] = {e: [] for e in engines}

    for path in sorted(samples.glob("*.jpg")):
        truth = TRUTH_DATE.get(path.name)
        if truth is None:
            continue
        img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        scale = WORK_LONG_SIDE / max(h, w)
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        # 用几何比例定戳径先验（不依赖 DPI）
        hint = postmark_diameter_hint_px(img)
        verdict = classify_postmark(img, expected_diameter_px=hint)
        print(f"=== {path.name}  真值={truth}  型={verdict.kind} ===")
        if verdict.circle is None:
            print("  无戳圈 → 两引擎都记未命中\n")
            for e in engines:
                stats[e].append(False)
                chars[e].append(0)
                times[e].append(0.0)
            continue
        region = extract_postmark_region(img, verdict.circle)
        if region is None:
            print("  戳区域为空\n")
            continue
        canvas = _pad(region[0])

        for name in engines:
            t0 = time.perf_counter()
            lines, err = ENGINES[name](canvas)
            dt = time.perf_counter() - t0
            if err:
                print(f"  [{name}] 不可用：{err}")
                stats[name].append(False)
                chars[name].append(0)
                times[name].append(dt)
                continue
            parsed = parse_fields(
                [(t, c) for t, c, _ in lines],
                region_height=canvas.shape[0],
                line_y_positions=[y for _, _, y in lines],
            )
            got = parsed.date.value[:10] if parsed.date else ""
            ok = got == truth
            stats[name].append(ok)
            chars[name].append(sum(len(t) for t, _, _ in lines))
            times[name].append(dt)
            joined = " | ".join(f"{t}({c:.2f})" for t, c, _ in lines) or "（空）"
            print(
                f"  [{name:<9}] {dt:5.2f}s 日期={got or '-':<10} {'✓' if ok else '✗'}  → {joined}"
            )
        print()

    print("=== V2 结论 ===")
    for name in engines:
        n = len(stats[name])
        if not n:
            print(f"  {name:<9} 没有可比样本")
            continue
        hit = sum(stats[name])
        avg_c = np.mean(chars[name])
        avg_t = np.mean(times[name])
        print(
            f"  {name:<9} 日期命中 {hit}/{n} = {hit / n:5.1%}   "
            f"平均字符 {avg_c:5.1f}   平均耗时 {avg_t:5.2f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
