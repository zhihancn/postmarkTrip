#!/usr/bin/env python3
"""A3: can the VLM classify stamp type from the WHOLE CARD?

The A4 fix showed the pattern: when the local pass fails, escalate the
GRANULARITY (give the VLM the whole card) rather than swapping the recogniser
on the same (possibly wrongly-cropped) input.

The VLM's earlier type labels looked unstable (16/12 then 18/10 across runs) --
but those were computed on the local pipeline's crop, which we now know can be
centred on the printed postcard header rather than the postmark. So re-ask on
the whole card.

Compares, on the cards a human actually judged:
  * local  (top_arc_text + centre_image features)
  * vlm    (whole card, thinking disabled)
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.pipeline import _pad_for_vlm
from postmarktrip.recognition.postmark_type import classify_postmark
from postmarktrip.recognition.split import split_page
from postmarktrip.recognition.vlm import ask, resolve_config

WORK = 2000
PAGES = ["更多样本/002.jpg", "更多样本/003.jpg", "更多样本/004.jpg", "更多样本/005.jpg"]

PROMPT = (
    "这是一张邮戳卡。请判断卡上那枚**邮戳**的戳型，只回复一个词：\n"
    "日戳 / 风景戳 / 无邮戳 / 看不清\n"
    "判据（务必按此，不要凭印象）：\n"
    "- 日戳：**顶弧（上半圆）有省市地名文字**，中部是日期数字，底弧是局名+戳号。\n"
    "- 风景戳：**顶弧是空的**（没有地名文字），中部或侧面是图案/线描/书法名称。\n"
    "注意：卡上印刷的「中国邮政 CHINA POST 邮戳卡」是卡片抬头，**不是邮戳**。\n"
    "只回复那一个词。"
)

LABELS = {
    "005-3": "daily",
    "003-6": "daily",
    "005-6": "daily",
    "002-1": "daily",
    "003-3": "scenic",
    "004-0": "scenic",
    "002-6": "scenic",
    "003-8": "scenic",
    "002-4": "scenic",
}
MAP = {"日戳": "daily", "风景戳": "scenic", "无邮戳": "none", "看不清": "unclear"}


def load_key() -> str:
    c = Path.home() / "myCode/postmarkTrip/.env"
    if c.exists():
        for line in c.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("DEEPSEEK_API_KEY="):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> int:
    repo = Path(__file__).resolve().parents[4]
    cfg, why = resolve_config(provider="deepseek", api_key=load_key(), enabled=True, max_tokens=64)
    if cfg is None:
        print(f"VLM unavailable: {why}")
        return 1
    print(f"VLM: {cfg.describe()}")

    rows = []
    for rel in PAGES:
        p = repo / "docs" / "need" / "pic" / rel
        if not p.exists():
            continue
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        H, W = img.shape[:2]
        s = WORK / max(H, W)
        small = cv2.resize(img, (int(W * s), int(H * s)), interpolation=cv2.INTER_AREA)
        sp = split_page(small, dpi=600, detection_scale=s)
        for i, c in enumerate(sp.candidates):
            key = f"{Path(rel).stem}-{i}"
            card = c.ortho
            v = classify_postmark(card, dpi=600 * s)
            ans = ask(cfg, _pad_for_vlm(card), PROMPT)
            raw = (ans.text or "").strip().split()[0] if ans.text else "?"
            rows.append(
                {
                    "key": key,
                    "label": LABELS.get(key),
                    "local": v.kind,
                    "vlm": MAP.get(raw, raw),
                    "vlm_raw": raw,
                }
            )

    print()
    print(f"{'card':<9} {'label':<8} {'local':<8} {'vlm':<9} raw")
    print("-" * 46)
    for r in sorted(rows, key=lambda x: x["key"]):
        mark = ""
        if r["label"]:
            mark = "  OK" if r["vlm"] == r["label"] else "  X"
        print(
            f"{r['key']:<9} {r['label'] or '-'!s:<8} {r['local']:<8} "
            f"{r['vlm']:<9} {r['vlm_raw']}{mark}"
        )

    lab = [r for r in rows if r["label"]]
    print()
    print(f"有标签卡片 {len(lab)} 张：")
    for who in ("local", "vlm"):
        ok = sum(1 for r in lab if r[who] == r["label"])
        print(f"  {who:<6} {ok}/{len(lab)} = {ok / len(lab):.1%}")

    from collections import Counter

    print()
    print("VLM 在所有卡片上的分布：", dict(Counter(r["vlm"] for r in rows)))
    print("本地在所有卡片上的分布：", dict(Counter(r["local"] for r in rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
