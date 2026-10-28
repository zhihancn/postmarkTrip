#!/usr/bin/env python3
"""V4：三个识别通道的 A/B + **幻觉测试**（spec §八 V4）。

## 方法论与 V1/V2 一致（否则数字不可比）

同一块戳区域、**同一个字段抽取器**、**同一份人工誊写真值**，只换识别通道。

| 通道 | 位置 | 成本 |
| --- | --- | --- |
| rapidocr | 本地 | 0.54s |
| paddleocr | 本地 | 3.73s |
| vlm(deepseek) | 云端 | 网络往返 + tokens |

## 两件事分开量

1. **准确率**：日期是否命中真值（与 V2 同一批 6 张，数字可直接对比）
2. **幻觉率**：**这是 V4 真正要问的** —— 给它"只有邮票、没有邮戳"的卡，
   看它会不会**凭空造出**一枚邮戳（日期 + 地名）。
   诚实的回答是「无邮戳」；编造的回答会给出一个**看起来完全合理的**日期与地名，
   而那正是规则判别不出来的错值。

## 用法

    python tools/samples/v4_vlm_ab.py --accuracy
    python tools/samples/v4_vlm_ab.py --hallucination
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
from postmarktrip.recognition.vlm import (
    PROMPT_HAS_POSTMARK,
    PROMPT_READ_POSTMARK,
    ask,
    resolve_config,
)

WORK_LONG_SIDE = 2000

TRUTH_DATE = {
    "清晰黑色日戳特写-1.jpg": "2026-09-25",
    "清晰黑色日戳特写-2.jpg": "2026-09-25",
    "盖的不好-墨太多.jpg": "2026-10-06",
    "盖的不好-邮票歪，不清晰.jpg": "2026-09-25",
    "单张卡整张扫描图.jpg": "2026-10-01",
    "盖的不好-糊掉了.jpg": "2026-09-25",
}

#: 005.jpg 里两枚"只有邮票、没有邮戳"的卡（用户告知：从左往右第 2、3 张）。
#: 布局是 3×3，所以是第 1 行的第 2、3 格。
STAMP_ONLY_005 = ((0, 1), (0, 2))


def load_env_key() -> str:
    """从 WSL 仓库的 .env 取 Key（那里是用户放的地方）。

    为什么不从 D: 的 .env：`.env` 被 gitignore，同步脚本不碰它，
    所以两份仓库各有一份 —— 用户把 Key 放在了 WSL 那份（E-05）。
    """
    import os

    for cand in (
        Path.home() / "myCode/postmarkTrip/.env",
        Path(".env"),
    ):
        if not cand.exists():
            continue
        for line in cand.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("DEEPSEEK_API_KEY=") and not s.startswith("#"):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("DEEPSEEK_API_KEY", "")


def vlm_recognise(cfg, canvas: np.ndarray) -> tuple[list[tuple[str, float, float]], str, dict]:
    """把 VLM 的自由文本**转成与 OCR 等价的"行"**，好让同一个字段抽取器评分。

    归一化规则（刻意保守）：
    * 按行切开，去掉「顶弧：」这类**提示词回声**前缀
    * 置信度给一个固定的名义值 —— VLM 不返回字级置信度，
      而**编造一个假的置信度会让 confidence 防线失效**。
      所以这里给 0.75（落在"需人工确认"区间），并**在报告里说明**。
    """
    import re

    ans = ask(cfg, canvas, PROMPT_READ_POSTMARK)
    if not ans.ok:
        return [], ans.reason, {"latency": ans.latency_s, "tokens": 0}
    lines: list[tuple[str, float, float]] = []
    for i, raw in enumerate(ans.text.splitlines()):
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^(顶弧|中部|底弧|上弧|下弧)\s*[:：]\s*", "", s)
        if not s or s in ("无", "无邮戳", "看不清"):
            continue
        lines.append((s, 0.75, float(i)))
    return (
        lines,
        "",
        {
            "latency": ans.latency_s,
            "tokens": ans.prompt_tokens + ans.completion_tokens,
            "text": ans.text,
        },
    )


def ocr_lines(engine: str, canvas: np.ndarray):
    from postmarktrip.recognition.ocr import recognize

    res = recognize(canvas, orientation="horizontal")
    if not res.available:
        return [], res.reason
    return [
        (ln.text, ln.confidence, float(np.mean([p[1] for p in ln.box]))) for ln in res.lines
    ], ""


def run_accuracy(args) -> int:
    repo = Path(__file__).resolve().parents[4]
    samples = args.samples or repo / "docs" / "need" / "pic"

    cfg, why = resolve_config(
        provider="deepseek", api_key=load_env_key(), enabled=True, max_tokens=512
    )
    if cfg is None:
        print(f"⚠️ VLM 不可用：{why}")
    else:
        print(f"VLM 配置：{cfg.describe()}")

    channels = ["rapidocr", "paddleocr"] + (["vlm"] if cfg else [])
    hits: dict[str, list[bool]] = {c: [] for c in channels}
    lats: dict[str, list[float]] = {c: [] for c in channels}

    for path in sorted(samples.glob("*.jpg")):
        truth = TRUTH_DATE.get(path.name)
        if truth is None:
            continue
        img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        s = WORK_LONG_SIDE / max(h, w)
        if s < 1.0:
            img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        v = classify_postmark(img, expected_diameter_px=postmark_diameter_hint_px(img))
        print(f"=== {path.name}  真值={truth} ===")
        if v.circle is None:
            print("  无戳圈 → 全部记未命中")
            for c in channels:
                hits[c].append(False)
                lats[c].append(0.0)
            continue
        reg = extract_postmark_region(img, v.circle)
        if reg is None:
            continue
        canvas = reg[0]

        for ch in channels:
            t0 = time.perf_counter()
            if ch == "vlm":
                lines, err, meta = vlm_recognise(cfg, canvas)
                dt = meta.get("latency", time.perf_counter() - t0)
                toks = meta.get("tokens", 0)
                raw_text = meta.get("text", "")
            else:
                lines, err = ocr_lines(ch, canvas)
                dt = time.perf_counter() - t0
                toks = 0
                raw_text = " | ".join(t for t, _, _ in lines)
            if err:
                print(f"  [{ch:<9}] 不可用：{err[:70]}")
                hits[ch].append(False)
                lats[ch].append(dt)
                continue
            parsed = parse_fields(
                [(t, c) for t, c, _ in lines],
                region_height=canvas.shape[0],
                line_y_positions=[y for _, _, y in lines],
            )
            got = parsed.date.value[:10] if parsed.date else ""
            ok = got == truth
            hits[ch].append(ok)
            lats[ch].append(dt)
            extra = f" {toks}tok" if toks else ""
            print(f"  [{ch:<9}] {dt:5.2f}s{extra} 日期={got or '-':<10} {'✓' if ok else '✗'}")
            if ch == "vlm" and raw_text:
                for ln in raw_text.splitlines():
                    if ln.strip():
                        print(f"       | {ln.strip()}")
        print()

    print("=== V4 准确率（日期命中真值）===")
    for ch in channels:
        n = len(hits[ch])
        if not n:
            continue
        print(
            f"  {ch:<9} {sum(hits[ch])}/{n} = {sum(hits[ch]) / n:5.1%}   "
            f"平均耗时 {np.mean(lats[ch]):5.2f}s"
        )
    print()
    print("  ⚠️ **VLM 的置信度是名义值 0.75** —— 它不返回字级置信度。")
    print("     所以 confidence 那条防线对它无效，必须靠**词表**那条；")
    print("     而编造出来的真实地名连词表也拦不住 —— 这正是下面 --hallucination 要量的。")
    return 0


def run_hallucination(args) -> int:
    """**V4 的核心**：给"只有邮票、没有邮戳"的卡，看它会不会编出邮戳。"""
    repo = Path(__file__).resolve().parents[4]
    page = args.page or repo / "docs/need/pic/更多样本/005.jpg"
    img = cv2.imdecode(np.fromfile(str(page), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        print(f"读不出 {page}")
        return 1
    cfg, why = resolve_config(
        provider="deepseek", api_key=load_env_key(), enabled=True, max_tokens=256
    )
    if cfg is None:
        print(f"⚠️ VLM 不可用：{why}")
        return 1

    H, W = img.shape[:2]
    print(f"{page.name}  {W}x{H}  → 按 3×3 网格切，取第 1 行第 2、3 格（纯邮票卡）")
    rows, cols = 3, 3
    for ri, ci in STAMP_ONLY_005:
        y0, y1 = H * ri // rows, H * (ri + 1) // rows
        x0, x1 = W * ci // cols, W * (ci + 1) // cols
        cell = img[y0:y1, x0:x1]
        # 加白边（与 ocr._prepare 同理：区域被内容填满时模型会误判）
        ch, cw = cell.shape[:2]
        py, px = int(ch * 0.08), int(cw * 0.08)
        canvas = np.full((ch + 2 * py, cw + 2 * px, 3), 255, np.uint8)
        canvas[py : py + ch, px : px + cw] = cell
        cv2.imwrite(f"/tmp/stamponly_{ri}{ci}.jpg", canvas)

        print()
        print(f"--- 格 ({ri},{ci})  裁剪 {cw}x{ch}  真值 = 无邮戳 ---")
        for label, prompt in (("是/否", PROMPT_HAS_POSTMARK), ("读文字", PROMPT_READ_POSTMARK)):
            ans = ask(cfg, canvas, prompt)
            first = (ans.text or "").replace("\n", " / ")[:160]
            print(
                f"  [{label}] ok={ans.ok} {ans.latency_s:.2f}s "
                f"{ans.prompt_tokens + ans.completion_tokens}tok → {first!r}"
            )

        # 判定：二值提问是否说了「无邮戳」
        ans = ask(cfg, canvas, PROMPT_HAS_POSTMARK)
        verdict = (
            "诚实（无邮戳）"
            if "无邮戳" in ans.text
            else ("🔴 编造（说有邮戳）" if "有邮戳" in ans.text else "含糊")
        )
        print(f"  ⇒ 判定：{verdict}")

    print()
    print("=== V4 幻觉结论 ===")
    print("  上面每格的真值都是「无邮戳」。")
    print("  「有邮戳」= 编造；「无邮戳」/「看不清」= 没有编造。")
    print("  这一步的样本量只有 2，所以只能作为**方向性证据**，不足以定阈值。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, default=None)
    ap.add_argument("--page", type=Path, default=None)
    ap.add_argument("--accuracy", action="store_true")
    ap.add_argument("--hallucination", action="store_true")
    args = ap.parse_args()
    if not (args.accuracy or args.hallucination):
        args.accuracy = args.hallucination = True
    rc = 0
    if args.accuracy:
        rc |= run_accuracy(args)
    if args.hallucination:
        rc |= run_hallucination(args)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
