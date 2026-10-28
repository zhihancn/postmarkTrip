#!/usr/bin/env python3
"""在样本集上实测切分准确率（A1 / A2）。

用法::

    python tools/samples/eval_split.py                     # 全部整页样本
    python tools/samples/eval_split.py --only 黑色背景.jpg
    python tools/samples/eval_split.py --dump /tmp/out     # 把切出的卡片落盘

判据来自 ``tests/fixtures/samples.json`` 的 ``expected_cards`` ——
那是**人工看图数出来的**，不是算法输出。

## 输出的数字是什么意思

* **card recall** = 切出的卡数 / 期望卡数。**>1 意味着过切**（把邮票、戳圈或
  纸面噪点当成卡），**<1 意味着漏切**。
* **image hit** = 图像级"张数完全正确"的比例。spec 的 A1/A2 说的成功率，
  严格讲是这一项（切出的张数对得上）。

两个都报，因为它们的故障含义不同：过切与漏切需要不同的修法。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.split import SplitParams, split_page

Image = np.ndarray


def load(path: Path) -> Image:
    """读图。中文路径用 imdecode（cv2.imread 在 Windows/某些 locale 下会失败）。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"读不出图像：{path}")
    return img


def downscale_for_detection(img: Image, long_side: int) -> tuple[Image, float]:
    """把整页缩到便于检测的尺寸。返回 (缩略图, 缩放比)。

    为什么要缩：A4@600dpi 是 5072×7014 = 3500 万像素。Canny + 形态学在整幅上
    跑要几十秒，而且**边缘检测本来就不需要满分辨率**。
    几何检测在小图上做，随后把角点映射回原尺寸再对原图做透视变换 ——
    变换只作用在卡片那一小块上，所以最终输出仍是原始像素质量。
    """
    h, w = img.shape[:2]
    scale = long_side / max(h, w)
    if scale >= 1.0:
        return img, 1.0
    small = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return small, scale


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, default=None)
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--only", default=None, help="只测文件名包含此子串的样本")
    ap.add_argument("--detect-long-side", type=int, default=1400)
    ap.add_argument("--dump", type=Path, default=None, help="把切出的卡片写到这个目录")
    ap.add_argument("--json", type=Path, default=None, help="把结果写到 JSON")
    args = ap.parse_args()

    # 本文件位于 <repo>/apps/api/tools/samples/ → parents[4] 才是仓库根
    repo = Path(__file__).resolve().parents[4]
    samples = args.samples or repo / "docs" / "need" / "pic"
    manifest_path = args.manifest or repo / "tests" / "fixtures" / "samples.json"

    if not manifest_path.is_file():
        print(f"✗ 找不到清单：{manifest_path}", file=sys.stderr)
        print("  先运行：python tools/samples/manifest.py", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    rows = []
    print(f"检测缩略图长边 = {args.detect_long_side}px")
    print()
    header = (
        f"{'文件':<30} {'期望':>4} {'切出':>4} {'召回':>6} {'级别':>5} "
        f"{'DPI':>6} {'来源':>10} {'秒':>6}"
    )
    print(header)
    print("-" * len(header))

    for entry in manifest["images"]:
        if entry["kind"] != "a4_scan":
            continue
        name = entry["file"]
        if args.only and args.only not in name:
            continue

        path = samples / name
        if not path.is_file():
            print(f"  ✗ 缺文件：{path}")
            continue

        img = load(path)
        small, scale = downscale_for_detection(img, args.detect_long_side)

        t0 = time.perf_counter()
        result = split_page(
            small,
            dpi=None,
            params=SplitParams(
                card_w_mm=54.0,
                card_h_mm=90.0,
            ),
            # ⚠️ 必须传：检测跑在缩略图上，所有像素判据都要跟着缩
            detection_scale=scale,
        )
        elapsed = time.perf_counter() - t0

        expected = int(entry["expected_cards"])
        got = len(result.candidates)
        recall = got / expected if expected else 0.0
        print(
            f"{name:<30} {expected:>4} {got:>4} {recall:>6.2f} {result.level:>5} "
            f"{result.dpi:>6} {result.dpi_source:>10} {elapsed:>6.1f}"
        )
        for note in result.notes:
            print(f"    · {note}")
        # 诊断：把每张正射图的尺寸打出来 —— DPI 反推错时，第一件要看的就是它
        sizes = sorted(
            (max(c.ortho.shape[:2]), min(c.ortho.shape[:2]), c.size_suspect)
            for c in result.candidates
        )
        if sizes:
            shown = ", ".join(f"{h}×{w}{'?' if susp else ''}" for h, w, susp in sizes[:6])
            more = f" …共{len(sizes)}张" if len(sizes) > 6 else ""
            print(f"    · 正射尺寸(长×短)：{shown}{more}")

        if args.dump:
            outdir = args.dump / name.replace(".jpg", "")
            outdir.mkdir(parents=True, exist_ok=True)
            for i, cand in enumerate(result.candidates):
                # 把检测缩略图上的角点映射回原图，再对**原图**做变换
                quad = cand.quad / scale
                h, w = img.shape[:2]
                quad[:, 0] = np.clip(quad[:, 0], 0, w - 1)
                quad[:, 1] = np.clip(quad[:, 1], 0, h - 1)
                from postmarktrip.recognition.split import warp_card

                full = warp_card(img, quad)
                cv2.imwrite(str(outdir / f"card_{i:02d}.jpg"), full)

                vis = img.copy()
                cv2.polylines(vis, [quad.astype(np.int32)], True, (0, 0, 255), 12)
                cv2.imwrite(str(outdir / "overlay.jpg"), vis)
            print(f"    → 落盘 {len(result.candidates)} 张到 {outdir}")

        rows.append(
            {
                "file": name,
                "kind": entry["kind"],
                "background": entry["background"],
                "expected": expected,
                "got": got,
                "level": result.level,
                "dpi": result.dpi,
                "dpi_source": result.dpi_source,
                "seconds": round(elapsed, 2),
                "notes": result.notes,
                "origins": [c.origin for c in result.candidates],
            }
        )

    if not rows:
        print("没有可测的整页样本", file=sys.stderr)
        return 1

    total_expected = sum(r["expected"] for r in rows)
    total_got = sum(r["got"] for r in rows)
    hits = sum(1 for r in rows if r["got"] == r["expected"])

    print()
    print("=" * 60)
    print(f"  图像级命中（张数完全正确）：{hits}/{len(rows)} = {hits / len(rows) * 100:.1f}%")
    print(f"  卡片级召回：{total_got}/{total_expected} = {total_got / total_expected * 100:.1f}%")
    print()
    # 按底色分组 —— spec 的 A1/A2 标准是**以深色垫底为前提**的
    for bg in ("black", "white"):
        sub = [r for r in rows if r["background"] == bg]
        if not sub:
            continue
        e = sum(r["expected"] for r in sub)
        g = sum(r["got"] for r in sub)
        h = sum(1 for r in sub if r["got"] == r["expected"])
        label = "深色垫底（spec 前提）" if bg == "black" else "白底（spec 的降级情形）"
        print(f"  {label}：图像级 {h}/{len(sub)}，卡片级 {g}/{e}")
    print("=" * 60)

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "rows": rows,
                    "summary": {
                        "images": len(rows),
                        "hits": hits,
                        "expected": total_expected,
                        "got": total_got,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  JSON → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
