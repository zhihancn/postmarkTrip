#!/usr/bin/env python3
"""P3 experiment: "pure cloud" (local geometry + cloud recognition) vs the
current local-first hybrid.

Measures, on the 6 samples that have hand-transcribed truth:

  * ``local``  -- `recognize_card` with NO cloud: local OCR + local type
  * ``hybrid`` -- `recognize_card` WITH cloud: local first, cloud only on failure
  * ``cloud``  -- one whole-card cloud call reads date/place/office directly

and reports per-field hit rate, latency per card, and tokens per card, so the
architecture choice becomes arithmetic rather than opinion.

Why whole-card for the cloud form: the local pipeline's dominant failure was
NOT the recogniser but the REGION (the circle detector landing on the postcard's
printed header). Giving the model the whole card lets it find the postmark
itself -- the same fix that took A4 from 83.3% to 100%.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from postmarktrip.recognition.fields import parse_fields
from postmarktrip.recognition.pipeline import (
    _pad_for_vlm,
    _vlm_lines_to_rows,
    recognize_card,
)
from postmarktrip.recognition.vlm import ask, resolve_config

WORK = 2000

#: Hand-transcribed truth (same source as tools/samples/eval_fields.py).
TRUTH: dict[str, dict[str, str]] = {
    "清晰黑色日戳特写-1.jpg": {"date": "2026-09-25", "place": "福建南平", "office": "建溪"},
    "清晰黑色日戳特写-2.jpg": {"date": "2026-09-25", "place": "福建南平", "office": "八一一路"},
    "盖的不好-墨太多.jpg": {"date": "2026-10-06", "place": "福建厦门", "office": "四号航站楼"},
    "盖的不好-邮票歪，不清晰.jpg": {"date": "2026-09-25", "place": "福建南平", "office": "茶山"},
    "单张卡整张扫描图.jpg": {"date": "2026-10-01", "place": "江西玉山", "office": "三清乡"},
    "盖的不好-糊掉了.jpg": {"date": "2026-09-25", "place": "福建南平", "office": ""},
}

#: Whole-CARD field prompt. Differences from PROMPT_READ_POSTMARK matter:
#:  * says it is the WHOLE CARD, so the model must locate the postmark itself
#:  * warns that the printed header is not the postmark -- the local OCR's
#:    failure mode, so the cloud must not be led into it
PROMPT_CARD_FIELDS = (
    "这是一整张邮戳卡的照片。请**只读出卡上那枚邮戳里真实可见的文字**，"
    "按位置分三段回答：\n"
    "顶弧：\n中部：\n底弧：\n"
    "规则：\n"
    "1. 卡上印刷的「中国邮政 CHINA POST 邮戳卡」是**卡片抬头，不是邮戳**，不要读它。\n"
    "2. **不要推测、不要补齐**。看不清的字写 `?`。\n"
    "3. 如果某一段没有文字，写「无」。\n"
    "4. 如果卡上根本没有邮戳，只回复「无邮戳」。\n"
    "5. 不要解释。"
)


def norm(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum())


def same(field: str, got: str, want: str) -> bool:
    """Field-aware comparison.

    The parser returns date+time ("2026-09-25 15:00") while the truth only
    records the day, so a raw string compare marked EVERY date wrong -- the
    first run of this tool reported date 0/6 for all three forms, which was a
    measurement bug, not a system problem. Compare the date part only.
    """
    g, w = norm(got), norm(want)
    if not w:
        return False
    if field == "date":
        return g.startswith(w)
    return g == w


def load_key() -> str:
    c = Path.home() / "myCode/postmarkTrip/.env"
    if c.exists():
        for line in c.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("DEEPSEEK_API_KEY="):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def fields_of(parsed: object) -> dict[str, str]:
    out = {}
    for f in ("date", "place", "office"):
        v = getattr(parsed, f, None)
        out[f] = v.value if v is not None else ""
    return out


def main() -> int:
    repo = Path(__file__).resolve().parents[4]
    cfg, why = resolve_config(provider="deepseek", api_key=load_key(), enabled=True, max_tokens=512)
    if cfg is None:
        print(f"VLM unavailable: {why}")
        return 1
    print(f"VLM: {cfg.describe()}\n")

    forms = ("local", "hybrid", "cloud")
    hits = {f: {"date": 0, "place": 0, "office": 0} for f in forms}
    totals = {f: {"latency": 0.0, "ptok": 0, "ctok": 0, "calls": 0} for f in forms}
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
        dpi = 600.0 * min(s, 1.0)
        n += 1
        got: dict[str, dict[str, str]] = {}

        # ---- local only ----
        t0 = time.perf_counter()
        r = recognize_card(img, dpi=dpi)
        totals["local"]["latency"] += time.perf_counter() - t0
        got["local"] = fields_of(r.parsed)

        # ---- hybrid (local, cloud only on failure) ----
        t0 = time.perf_counter()
        r = recognize_card(img, dpi=dpi, vlm_config=cfg)
        totals["hybrid"]["latency"] += time.perf_counter() - t0
        got["hybrid"] = fields_of(r.parsed)
        if r.field_source.startswith("vlm"):
            totals["hybrid"]["calls"] += 1

        # ---- cloud reads the whole card ----
        whole = _pad_for_vlm(img)
        t0 = time.perf_counter()
        ans = ask(cfg, whole, PROMPT_CARD_FIELDS)
        lat = time.perf_counter() - t0
        totals["cloud"]["latency"] += lat
        totals["cloud"]["calls"] += 1
        totals["cloud"]["ptok"] += ans.prompt_tokens
        totals["cloud"]["ctok"] += ans.completion_tokens
        rows = _vlm_lines_to_rows(ans.text)
        parsed = parse_fields(
            [(t, 0.75) for t, _y in rows],
            region_height=whole.shape[0],
            line_y_positions=[y for _t, y in rows],
        )
        got["cloud"] = fields_of(parsed)

        print(
            f"--- {name}  (cloud {lat:.2f}s, {ans.prompt_tokens}+{ans.completion_tokens} tok) ---"
        )
        for f in forms:
            marks = []
            for fld in ("date", "place", "office"):
                want = truth[fld]
                if not want:
                    marks.append(f"{fld}:n/a")
                    continue
                ok = same(fld, got[f][fld], want)
                hits[f][fld] += ok
                marks.append(f"{fld}:{'OK' if ok else 'X'}")
            print(
                f"    {f:<7} {got[f]['date'] or '-':<11} "
                f"{got[f]['place'] or '-':<7} {got[f]['office'] or '-':<9} "
                f"{'  '.join(marks)}"
            )

    print()
    print("=== 每字段命中率（分母 = 有真值的张数）===")
    denom = {"date": n, "place": n, "office": 5}  # 一张的 office 真值为空
    for f in forms:
        row = "  ".join(
            f"{k} {hits[f][k]}/{denom[k]} = {hits[f][k] / denom[k]:.0%}"
            for k in ("date", "place", "office")
        )
        print(f"  {f:<7} {row}")

    print()
    print("=== 每卡成本 ===")
    print("  （注意：hybrid 的云端调用只统计了字段兜底；")
    print("    它还会给每张卡发**戳型**调用，那部分算在它的延迟里）")
    for f in forms:
        t = totals[f]
        avg = t["latency"] / n
        tok = (t["ptok"] + t["ctok"]) / n
        print(
            f"  {f:<7} 平均 {avg:.2f}s/卡   云端调用 {t['calls']}/{n} 卡   平均 token {tok:.0f}/卡"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

    # ---- 合并调用：②可以一次问完「戳型 + 三字段」，所以是 1 次/卡 ----
    tot_tok = (totals["cloud"]["ptok"] + totals["cloud"]["ctok"]) / n
    print()
    print("=== 若选②（本地几何 + 云识别），并且把戳型与字段合并成一次调用 ===")
    print("  云端调用 1 次/卡（现状 hybrid 是 戳型 1 次 + 字段失败时再加 1 次）")
    print(f"  平均 token {tot_tok:.0f}/卡")
    print(f"  每 1000 张卡约 {tot_tok * 1000 / 1_000_000:.2f}M token")
    print(
        f"  平均延迟 {totals['cloud']['latency'] / n:.2f}s/卡 "
        f"（hybrid {totals['hybrid']['latency'] / n:.2f}s/卡，"
        f"因为 hybrid 还要跑本地 OCR）"
    )
