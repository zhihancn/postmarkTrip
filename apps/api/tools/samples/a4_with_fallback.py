#!/usr/bin/env python3
"""A4, measured THROUGH the pipeline, with and without the cloud fallback.

Runs `recognize_card` (the real per-card entry point) on the 6 samples that
have a hand-transcribed date truth, once with `vlm_config=None` and once with
the DeepSeek config, so the delta is attributable to the fallback alone.

Truth is hand-transcribed (`eval_fields.TRUTH`), not OCR output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.pipeline import recognize_card
from postmarktrip.recognition.vlm import resolve_config

WORK = 2000
TRUTH = {
    "清晰黑色日戳特写-1.jpg": "2026-09-25",
    "清晰黑色日戳特写-2.jpg": "2026-09-25",
    "盖的不好-墨太多.jpg": "2026-10-06",
    "盖的不好-邮票歪，不清晰.jpg": "2026-09-25",
    "单张卡整张扫描图.jpg": "2026-10-01",
    "盖的不好-糊掉了.jpg": "2026-09-25",
}


def load_key() -> str:
    for c in (Path.home() / "myCode/postmarkTrip/.env", Path(".env")):
        if c.exists():
            for line in c.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("DEEPSEEK_API_KEY="):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> int:
    repo = Path(__file__).resolve().parents[4]
    cfg, why = resolve_config(provider="deepseek", api_key=load_key(), enabled=True, max_tokens=512)
    if cfg is None:
        print(f"VLM unavailable ({why}) -- measuring local only")
    else:
        print(f"VLM: {cfg.describe()}")

    print(f"{'sample':<30} {'no fallback':<14} {'with fallback':<16} truth")
    print("-" * 78)
    hits = {"off": 0, "on": 0}
    n = 0
    for name, truth in TRUTH.items():
        p = repo / "docs" / "need" / "pic" / name
        if not p.exists():
            continue
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        H, W = img.shape[:2]
        s = WORK / max(H, W)
        if s < 1.0:
            img = cv2.resize(img, (int(W * s), int(H * s)), interpolation=cv2.INTER_AREA)
        # 单卡特写：dpi=600 是实测值（见 ok/10 §七之十三），且图未被缩放时也成立
        dpi = 600.0 if s >= 1.0 else 600.0 * s
        n += 1

        off = recognize_card(img, dpi=dpi)
        off_date = off.parsed.date.value[:10] if off.parsed and off.parsed.date else ""
        ok_off = off_date == truth
        hits["off"] += ok_off

        if cfg is None:
            print(f"{name:<30} {off_date or '-':<14} {'(no vlm)':<16} {truth}")
            continue

        on = recognize_card(img, dpi=dpi, vlm_config=cfg)
        on_date = on.parsed.date.value[:10] if on.parsed and on.parsed.date else ""
        ok_on = on_date == truth
        hits["on"] += ok_on
        src = on.field_source
        print(
            f"{name:<30} {off_date or '-':<14} {on_date or '-':<16} {truth}"
            f"   {'OK' if ok_on else 'X'} src={src}"
        )

    print()
    print("=== A4（日期命中，分母 = 人工誊写真值）===")
    print(f"  无云端回退：{hits['off']}/{n} = {hits['off'] / n:.1%}")
    if cfg is not None:
        print(f"  有云端回退：{hits['on']}/{n} = {hits['on'] / n:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
