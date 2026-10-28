#!/usr/bin/env python3
"""生成 `docs/frontDemo/index.html` —— 一个**双击就能打开**的确认界面原型。

## 为什么是静态 HTML

它**完全隔离**：不需要 WSL、不需要起服务、不需要 npm、不需要网络。
双击 `index.html` 即可。数据和图都在同一个文件夹里。

## 数据是**真实识别结果**，不是编的

`recognize_page` 在真实样本上跑一遍，把 `quad` / `circle` / `decision` /
`reason` / `queue` / `circle_page` 原样导出。所以界面上看到的问题**都是真的问题**。

## 本地 + 云端两套（可 A/B）

* 默认生成的 4 页是**纯本地**（云端默认关：花钱且结果不可复现）
* 另外 2 页是**开启云端**的同一样本，标签带 ☁，用于直接对比。
  云端要读 `.env` 里的 `DEEPSEEK_API_KEY`（`.env` 是 gitignored，不会同步过来）

## 词表是特意配上的

为了让"词表那道防线"在界面上看得见，本脚本把样本的**真值**当作已登记词表
传入（行政区划与邮局）。这不是造假 —— 它演示的正是"字典填好之后会怎样"。
每一页的 `demo_note` 会说明这一点。

用法：

    python docs/frontDemo/generate.py
    python docs/frontDemo/validate.py .
    python docs/frontDemo/_check_overlay.py .
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "apps/api/src"))

from postmarktrip.recognition.pipeline import recognize_page

OUT = Path(__file__).resolve().parent
PIC = REPO / "docs/need/pic"

#: 预览图的最大宽度。再大只会让文件变胖，界面用不上。
PREVIEW_W = 1100

#: 样本的**真值** —— 同时充当"已登记词表"，让词表防线在界面上可见。
TRUTH: dict[str, dict[str, list[str]]] = {
    "更多样本/005.jpg": {
        "places": ["福建南平", "福州", "江西玉山", "江西三清山"],
        "offices": ["福屿", "中亭街", "西洪", "滨江北路", "茶山", "建溪", "三清乡"],
    },
    "盖的不好-墨太多.jpg": {"places": ["福建厦门"], "offices": ["四号航站楼"]},
    "清晰黑色日戳特写-1.jpg": {"places": ["福建南平"], "offices": ["建溪"]},
    "黑色背景-单张.jpg": {
        "places": ["福建南平", "福州", "江西玉山"],
        "offices": ["建溪", "茶山", "三清乡"],
    },
}

#: (相对路径, 文件名 slug, 标签, 这一页演示什么)
PAGES: list[tuple[str, str, str, str]] = [
    (
        "更多样本/005.jpg",
        "p005",
        "9 张卡整页（本地）",
        "这一页演示「整页折叠成摘要 + 只让可疑卡自动展开」。"
        "底行的卡在扫描时略有倾斜，所以四边形（quad）比轴对齐的框准。"
        "词表按样本真值配置，所以「在词表里」的地名会显示绿勾。",
    ),
    (
        "盖的不好-墨太多.jpg",
        "ink_too_much",
        "失败样本（本地）",
        "**这是真实的失败**：圆定位落在了明信片印刷抬头「中国邮政 CHINA POST 邮戳卡」上，"
        "半径明显偏大（看『圆可疑』标记），于是日期抽不到、局名抽成了「邮政」。"
        "这种错**最难从字段值看出来** —— 所以界面上把圆画出来。",
    ),
    (
        "清晰黑色日戳特写-1.jpg",
        "closeup_1",
        "词表防线（本地）",
        "OCR 给地名读成「福建南火」，**置信度 0.90**，真值是「福建南平」。"
        "只看置信度会把它显示成可信；这里因为**不在词表里**被标成待确认。"
        "这就是为什么颜色只能按 decision 上色，不能按 confidence。",
    ),
    (
        "黑色背景-单张.jpg",
        "dark_single",
        "A1 单卡（本地）",
        "深色垫底的单卡整页 —— A1 用的就是这类样本。"
        "这一页字段基本都对，用来对比「一页全是绿勾时你能多快扫完」。",
    ),
]

#: 额外生成的**云端**版本，用于 A/B 对比。只挑最能说明问题的两张。
CLOUD_EXTRA: list[tuple[str, str, str, str]] = [
    (
        "更多样本/005.jpg",
        "p005_cloud",
        "☁ 同上一页（云端）",
        "**与本地版逐字对比**：戳型由整卡 VLM 判定（本地特征实测只有 44.4%），"
        "地名也由云端读（本地会给出「福建南火」这类错字）。"
        "另外留意**局名的戳号**（如「中亭街1」的 1）—— 本地 OCR 的检测阶段"
        "从不给那个孤立小数字提出候选框（放大与极坐标展开都试过，救不回来）。",
    ),
    (
        "盖的不好-墨太多.jpg",
        "ink_too_much_cloud",
        "☁ 同上一张（云端）",
        "本地在这里**失败**了：圆落在明信片印刷抬头上，日期抽不到、局名抽成「邮政」。"
        "云端可以自己找邮戳，所以这一张的差别最大。",
    ),
]


def _load_key() -> str:
    """取 DEEPSEEK_API_KEY。`.env` 是 gitignored，所以 WSL 仓与 D: 仓各自一份。"""
    for cand in (Path.home() / "myCode/postmarkTrip/.env", REPO / ".env"):
        if not cand.exists():
            continue
        for line in cand.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("DEEPSEEK_API_KEY="):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _save_preview(work: np.ndarray, slug: str) -> None:
    h, w = work.shape[:2]
    if w > PREVIEW_W:
        s = PREVIEW_W / w
        work = cv2.resize(
            work, (PREVIEW_W, max(1, int(h * s))), interpolation=cv2.INTER_AREA
        )
    (OUT / "images").mkdir(exist_ok=True)
    cv2.imwrite(
        str(OUT / "images" / f"{slug}.jpg"), work, [cv2.IMWRITE_JPEG_QUALITY, 82]
    )


def build_page(
    rel: str,
    slug: str,
    label: str,
    note: str,
    vlm_config: object | None,
    *,
    vlm_primary: bool = False,
) -> dict[str, object] | None:
    path = PIC / rel
    if not path.exists():
        print(f"⚠️ 样本不存在，跳过：{rel}")
        return None
    img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    truth = TRUTH.get(rel, {})
    res = recognize_page(
        img,
        dpi=600,
        known_places=frozenset(truth.get("places", [])),
        known_offices=frozenset(truth.get("offices", [])),
        vlm_config=vlm_config,  # type: ignore[arg-type]
        vlm_primary=vlm_primary,
    )
    # 四边形/圆的坐标都在**工作尺度**上算的，所以预览图必须是同一张工作图，
    # 否则叠加会整体错位（同族问题见 ok/09 E-14）
    work = cv2.resize(img, res.work_size, interpolation=cv2.INTER_AREA)
    _save_preview(work, slug)

    cards = []
    for c in res.cards:
        fields, queue = [], []
        if c.bundle is not None:
            fields = [
                {
                    "field_name": o.field_name,
                    "value": o.normalized_value or o.raw_value,
                    "confidence": round(o.confidence, 3),
                    "decision": o.decision,
                    "reason": o.reason,
                    "pipeline_version": o.pipeline_version,
                }
                for o in c.bundle.observations
            ]
            queue = [
                {
                    "field_name": q.field_name,
                    "value": q.value,
                    "confidence": round(q.confidence, 3),
                    "why": q.why,
                }
                for q in c.bundle.queue
            ]
        circle = None
        # 用**页面坐标**的圆（管线已变换好）。曾直接用 verdict.circle（卡局部），
        # 结果 7 张卡的圆全部堆在页面左上角。
        if c.circle_page is not None and c.verdict is not None and c.verdict.circle:
            cw = max(1, c.card_size[0])
            circle = {
                "cx": round(c.circle_page[0], 1),
                "cy": round(c.circle_page[1], 1),
                "r": round(c.circle_page[2], 1),
                "radius_over_card_width": round(c.verdict.circle.r / cw, 3),
            }
        cards.append(
            {
                "index": c.index,
                "bbox": list(c.bbox),
                "quad": [[round(x, 1), round(y, 1)] for x, y in c.quad],
                "card_size": list(c.card_size),
                "size_suspect": c.size_suspect,
                "postmark_type": c.verdict.kind if c.verdict else "",
                "postmark_reason": c.verdict.reason if c.verdict else "",
                "circle": circle,
                "fields": fields,
                "queue": queue,
                "ocr_lines": [[t, round(v, 3)] for t, v in c.ocr_lines],
                "field_source": c.field_source,
                "notes": c.notes,
            }
        )

    srcs = sorted({c["field_source"] for c in cards})
    nrev = sum(
        1 for c in cards if any(f["decision"] != "auto_accept" for f in c["fields"])
    )
    offices = []
    for c in cards:
        got = {f["field_name"]: f["value"] for f in c["fields"]}
        if got.get("office"):
            offices.append(f"{got['office']}{got.get('office_no') or ''}")
    print(
        f"  {slug:<18} {len(cards):>2} 张卡  待确认 {nrev:>2}  "
        f"来源={','.join(srcs):<6} 局名={offices}"
    )
    return {
        "label": label,
        "image": f"images/{slug}.jpg",
        "pipeline_version": res.pipeline_version,
        "work_scale": round(res.work_scale, 4),
        "work_size": list(res.work_size),
        "stage_notes": res.stage_notes,
        "demo_note": note,
        "cards": cards,
    }


def main() -> int:
    from postmarktrip.recognition.vlm import resolve_config

    cfg, why = resolve_config(
        provider="deepseek", api_key=_load_key(), enabled=True, max_tokens=512
    )
    if cfg is None:
        print(f"⚠️ 云端不可用（{why}）—— 只生成本地版\n")
    else:
        print(f"云端：{cfg.describe()}\n")

    jobs: list[tuple[str, str, str, str, object | None, bool]] = [
        (a, b, c, d, None, False) for a, b, c, d in PAGES
    ]
    if cfg is not None:
        # 云端页走**形态②**：vlm_primary=True，字段与戳型都由云端给
        jobs += [(a, b, c, d, cfg, True) for a, b, c, d in CLOUD_EXTRA]

    pages = []
    for rel, slug, label, note, vlm, primary in jobs:
        page = build_page(rel, slug, label, note, vlm, vlm_primary=primary)
        if page is not None:
            pages.append(page)

    data = {"pages": pages, "generated_by": "docs/frontDemo/generate.py"}
    tpl = (OUT / "template.html").read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False, indent=1)
    # 内联进 <script> 的 JSON 必须转义 "</"，否则字符串里出现 "</script>"
    # 会提前结束脚本块 —— 那种失败表现为整页白屏，很难看出原因
    blob = blob.replace("</", "<\\/")
    (OUT / "index.html").write_text(tpl.replace("/*__DATA__*/", blob), encoding="utf-8")
    print(f"\n-> {OUT / 'index.html'}  （双击即可打开）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
