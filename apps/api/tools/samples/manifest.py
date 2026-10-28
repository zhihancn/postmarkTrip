#!/usr/bin/env python3
"""生成样本清单：逐图实测尺寸 / DPI / 花色等，作为准确率判据的基准。

**为什么先做这一步**：spec 的 A1（单卡切分 ≥95%）/ A2（多卡混排 ≥85%）都是
**在样本集上统计出来的数字**。没有一份明确的"这批样本是什么、期望切出几张卡"，
后面的准确率就无从计算 —— 只能靠"看起来不错"。

这份清单是**判据基准**：切分算法报出 N 张卡，与清单里的 expected_cards 对比。

输出：tests/fixtures/samples.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

#: A4 尺寸（mm），用来从像素尺寸反推实际扫描 DPI
A4_W_MM, A4_H_MM = 210.0, 297.0
MM_PER_INCH = 25.4

#: "整页扫描"的比例判定区间。
#:
#: ⚠️ 标准 A4 的长短边比是 297/210 = **1.4142**，但**实测这批样本是 1.3829**
#: —— 页面被裁掉了一条边（很可能是扫描软件的自动裁边）。
#: 所以不能按 1.4142±0.02 判，那样 4 张真正的整页扫描会全部被判成"非 A4"，
#: 于是 DPI 全部变成"未知"（第一版就是这样，白算了一遍）。
#:
#: 区间取 [1.30, 1.45]：能容纳"整页 + 轻微裁边"，又能可靠排除
#: 卡片特写（实测比例 1.53~1.78）。
PAGE_RATIO_MIN = 1.30
PAGE_RATIO_MAX = 1.45


def is_page_scan(size: tuple[int, int]) -> bool:
    """这张图是不是**整页扫描**（而不是裁剪过的卡片特写）。

    **这个判断不能跳过**，因为 DPI 是管线里所有毫米↔像素换算的基准（N7）。
    样本集里有两个来源不同的图：

    * 整页扫描（5072×7014、10144×14028）—— 比例 1.3829
    * **裁剪过的卡片特写**（1222×2062 等）—— 比例 1.53~1.78

    对后者套"整页反推 DPI"会得到 161~175 这种**看起来像真、其实无意义**的
    数字（第一版的输出）。所以非整页时如实返回 None，
    由管线回落到 ``DEFAULT_SCAN_DPI`` 并在结果里标注 —— 而不是猜一个。

    > 对 A4 扫描，**更可靠的做法是从切出的卡片尺寸反推 DPI**
    > （卡片实测 54×90mm，``px / mm × 25.4``）。那是切分器要做的事：
    > 它本来就得先量出卡片的像素尺寸。这里只做"是/不是整页"的粗判。
    """
    w, h = size
    ratio = max(w, h) / min(w, h)
    return PAGE_RATIO_MIN <= ratio <= PAGE_RATIO_MAX


def infer_dpi(size: tuple[int, int]) -> int | None:
    """从像素尺寸反推整页扫描的实际 DPI；**不是整页时返回 None**。

    比读元数据可靠：实测这些图 EXIF 里写的是 144（明显的垃圾值），
    而按整页反推得到 600 / 1200，与用户的操作一致。
    """
    if not is_page_scan(size):
        return None
    w, h = size
    short_px, long_px = min(w, h), max(w, h)
    by_w = short_px / (A4_W_MM / MM_PER_INCH)
    by_h = long_px / (A4_H_MM / MM_PER_INCH)
    # 取两者平均后四舍五入到最接近的 50，避免"607"这种假精度
    raw = (by_w + by_h) / 2
    return int(round(raw / 50) * 50)


def gray_stats(im: Image.Image) -> dict[str, float | int]:
    """灰度分布的粗统计，用来判断底色与对比度。"""
    g = im.convert("L")
    g.thumbnail((1200, 1200), Image.Resampling.BOX)
    arr = np.asarray(g, dtype=np.uint8).ravel()
    bright = arr[arr >= 128]
    dark = arr[arr < 128]
    return {
        "min": int(arr.min()),
        "max": int(arr.max()),
        "mean": round(float(arr.mean()), 1),
        "std": round(float(arr.std()), 1),
        "bright_pct": round(float(bright.size) / arr.size * 100, 1),
        "dark_mean": round(float(dark.mean()), 1) if dark.size else 0.0,
        "bright_mean": round(float(bright.mean()), 1) if bright.size else 0.0,
        "contrast": round(float(bright.mean()) - float(dark.mean()), 1)
        if dark.size and bright.size
        else 0.0,
    }


#: 逐图的人工判据。expected_cards 由**看图数出来**，不是猜的。
#: 每加一张样本都必须在这里登记，否则清单生成器会拒绝运行。
#:
#: kind 决定这张图**能用来验证什么**（见 spec 的验收项）：
#:   a4_scan      —— 整张 A4，用来验 A1 / A2（卡片切分）
#:   card_closeup —— 裁剪过的单卡特写，用来验 A3 / A4（戳型判别、日期抽取）
#:   postmark_only—— 只有戳的极近特写，同上
KNOWN: dict[str, dict[str, object]] = {
    "单张卡整张扫描图.jpg": {
        "kind": "a4_scan",
        "expected_cards": 1,
        "background": "white",
        "note": "单张卡放在 A4 右上角，其余全空白。**A1 的判据样本。**",
    },
    "多张卡混排扫描图.jpg": {
        "kind": "a4_scan",
        "expected_cards": 8,
        "background": "white",
        "note": "一张 A4 放 8 张卡，3+3+2 排布。**A2 的白底样本。**",
    },
    "清晰黑色日戳特写-1.jpg": {
        "kind": "card_closeup",
        "expected_cards": 1,
        "background": "white",
        "note": "福建南平 / 2026.09.25.15 / 建溪 1。标准日戳。**A3/A4 判据样本。**",
    },
    "清晰黑色日戳特写-2.jpg": {
        "kind": "card_closeup",
        "expected_cards": 1,
        "background": "white",
        "note": "标准日戳，第二张。",
    },
    "风景戳特写(来源-求戳信).jpg": {
        "kind": "card_closeup",
        "expected_cards": 1,
        "background": "white",
        "note": "江西玉山纪念碑风景戳：**顶弧空、名称横排在中部**。"
        "spec §3.3「先判戳型再定文字区域」的直接证据。",
    },
    "彩色戳特写.jpg": {
        "kind": "card_closeup",
        "expected_cards": 1,
        "background": "white",
        "note": "晋祠天龙山主题邮局，彩色，书法融入图案。**B 类文字**（以图搜戳的正解）。",
    },
    "盖的不好-有污渍.jpg": {
        "kind": "card_closeup",
        "expected_cards": 1,
        "background": "white",
        "note": "污渍在戳圈外。",
    },
    "盖的不好-墨太多.jpg": {
        "kind": "card_closeup",
        "expected_cards": 1,
        "background": "white",
        "note": "",
    },
    "盖的不好-墨太少.jpg": {
        "kind": "card_closeup",
        "expected_cards": 1,
        "background": "white",
        "note": "",
    },
    "盖的不好-糊掉了.jpg": {
        "kind": "card_closeup",
        "expected_cards": 1,
        "background": "white",
        "note": "",
    },
    "盖的不好-邮票歪，不清晰.jpg": {
        "kind": "card_closeup",
        "expected_cards": 1,
        "background": "white",
        "note": "",
    },
    "1200dpi.jpg": {
        "kind": "a4_scan",
        "expected_cards": 1,
        "background": "white",
        "note": "V5 用：与 单张卡整张扫描图.jpg 同一张卡（三清山），10144×14028。"
        "**不参与 A1/A2 统计**（它与 600dpi 那张是同一场景，计入会重复）。",
    },
    "黑色背景.jpg": {
        "kind": "a4_scan",
        "expected_cards": 9,
        "background": "black",
        "note": "9 张卡垫黑色鼠标垫，3×3 排布，彼此分开。"
        "**A1/A2 唯一能验证「深色垫底」前提的样本。** 卡片延伸到画面外被裁切。",
    },
}


def main() -> int:
    pic = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/need/pic")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "tests/fixtures/samples.json")

    if not pic.is_dir():
        print(f"✗ 样本目录不存在：{pic}", file=sys.stderr)
        return 1

    found = sorted(p.name for p in pic.glob("*.jpg"))
    unregistered = [n for n in found if n not in KNOWN]
    if unregistered:
        print("✗ 有样本还没登记判据（expected_cards / background）：", file=sys.stderr)
        for n in unregistered:
            print(f"    {n}", file=sys.stderr)
        print("\n请在 KNOWN 里登记后再运行 —— 判据缺失时算出来的准确率没有意义。", file=sys.stderr)
        return 1

    entries = []
    for name in found:
        path = pic / name
        with Image.open(path) as im:
            size = im.size
            mode = im.mode
            exif_dpi = im.info.get("dpi")
            stats = gray_stats(im)

        known = KNOWN[name]
        entries.append(
            {
                "file": name,
                "kind": known["kind"],
                "bytes": path.stat().st_size,
                "width": size[0],
                "height": size[1],
                "aspect_ratio": round(max(size) / min(size), 4),
                "is_page_scan": is_page_scan(size),
                "mode": mode,
                "exif_dpi": int(exif_dpi[0]) if isinstance(exif_dpi, tuple) and exif_dpi else None,
                "inferred_dpi": infer_dpi(size),
                "expected_cards": known["expected_cards"],
                "background": known["background"],
                "note": known["note"],
                **stats,
            }
        )

    payload = {
        "_comment": (
            "由 apps/api/tools/samples/manifest.py 生成。"
            "expected_cards 是**人工看图数出来的**判据，不是算法输出 —— "
            "准确率 = 切分报出的卡数与此处一致的比例。"
            "inferred_dpi 只在 is_full_a4 为真时有意义（见 infer_dpi 的说明）。"
        ),
        "sample_count": len(entries),
        "images": entries,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"✓ 清单已写：{out}（{len(entries)} 张）")
    print()
    print(
        f"  {'文件':<32} {'像素':>13} {'比例':>6} {'整页':>4} {'DPI':>6} {'卡':>3} {'底':>6} {'类':>13}"
    )
    for e in entries:
        dpi = e["inferred_dpi"] if e["inferred_dpi"] is not None else "—"
        print(
            f"  {e['file']:<32} {e['width']}×{e['height']:<7} {e['aspect_ratio']:>6} "
            f"{'是' if e['is_page_scan'] else '否':>4} {dpi!s:>6} {e['expected_cards']:>3} "
            f"{e['background']:>6} {e['kind']:>13}"
        )
    print()
    seg = [e for e in entries if e["kind"] == "a4_scan"]
    print(
        f"  可用于 A1/A2（切分）：{len(seg)} 张，共 {sum(int(e['expected_cards']) for e in seg)} 张卡"
    )
    dark = [e for e in seg if e["background"] == "black"]
    print(
        f"  其中**深色垫底**（A1/A2 的前提）：{len(dark)} 张，"
        f"{sum(int(e['expected_cards']) for e in dark)} 张卡"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
