"""识别管线的**端到端入口**（spec v1.0 §3.2–§3.8）。

## 这个模块存在的理由

在它之前，每个阶段各自可测，但**没有一条调用链把它们串起来** ——
于是"管线能跑"这件事只能靠人按顺序手动调各模块来验证，
而那种验证无法回归（改了下游不知道上游还连通不连）。

## 一条链

```
卡片图（整页或单卡）
  └─ split.split_page             → 卡片候选（正射图 / bbox / size_suspect / DPI）
      └─ postmark_type.classify   → 日戳 / 风景戳 / unknown + 戳圈
          └─ text_regions          → 整块戳区域（主路径）
              └─ ocr.recognize     → 文本行 + 逐行置信度
                  └─ fields.parse  → 日期 / 地名 / 局名+戳号
                      └─ review    → observation 草稿 + 校对队列
                          └─ cutout → 戳贴片 / 票贴片
```

## 两条刻意的设计

**① 不抛异常，降级要在结果里看得见。**
每个阶段失败都被记进 ``stage_notes`` 并继续往下走。理由：一枚戳读不出来
不该让整页失败 —— 那一页里其他 8 枚还是好的。而"失败"必须以
**结构化的形式**出现（``stages`` 字典 + notes），不能被静默吞掉。

**② 不写数据库。**
本模块只产出**纯数据**（可 JSON 序列化）。写库是调用方的事 ——
这样管线的正确性能脱离数据库单测，也让"重跑历史"不必担心副作用。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np

from postmarktrip.domain.vocab import PostmarkType
from postmarktrip.recognition.cutout import Cutout, cut_postmark, cut_stamp
from postmarktrip.recognition.fields import (
    Field,
    ParsedFields,
    parse_date_text,
    parse_fields,
    time_code_of,
)
from postmarktrip.recognition.ocr import recognize
from postmarktrip.recognition.postmark_type import (
    TYPE_UNKNOWN,
    Circle,
    TypeVerdict,
    classify_postmark,
    detect_stamp_region,
)
from postmarktrip.recognition.review import (
    PIPELINE_VERSION,
    ReviewBundle,
    build_bundle,
    observation_decimals,
)
from postmarktrip.recognition.split import (
    DEFAULT_EXPECTED_CARDS,
    GEOMETRY_MODE,
    CloudCorners,
    GeometryMode,
    SplitResult,
    merge_cloud_corners,
    quad_iou,
    split_page,
    suspect_geometry,
    warp_card,
)
from postmarktrip.recognition.text_regions import (
    extract_postmark_region,
    postmark_diameter_hint_px,
)
from postmarktrip.recognition.vlm import CORNERS_SHAPE_HINT, VlmConfig

logger = logging.getLogger("postmarktrip.recognition.pipeline")

#: 处理长边上限。整页 A4@600dpi 是 35 MP，全分辨率跑 Hough 要几十秒，
#: 而各阶段需要的细节远低于此（戳径在 2000px 下仍有 200+px）。
DEFAULT_WORK_LONG_SIDE = 2000

#: 兜底的名义 DPI。切分器能从卡片尺寸反推出真实值时会覆盖它。
#: 取 600 是因为实测样本的整页扫描就是 600（见 ok/10 §三）。
FALLBACK_DPI = 600.0

#: 云端的"戳型"判定所带的最小置信度。
#:
#: 为什么需要它：覆盖戳型时如果原样继承**本地**的置信度，本地判 `unknown`
#: （confidence=0.0）就会让"云端判对的 scenic"变成 `reject`（未采信）。
#: 取值与云端字段的口径一致（那些字段一律记 0.9），且高于 `postmark_type`
#: 的 auto_accept 阈值 0.7 —— 云端判型是这条形态的意义所在。
#: 见 `docs/ok/15` §3.3。
CLOUD_TYPE_CONFIDENCE = 0.9


@dataclass(slots=True)
class CardResult:
    """一张卡片的完整识别结果。"""

    index: int
    bbox: tuple[int, int, int, int]
    #: 卡片正射图的尺寸（像素）
    card_size: tuple[int, int]
    size_suspect: bool
    #: **检测到的四边形**（原图坐标，4x2）。
    #:
    #: 为什么不只给 bbox：卡在扫描时可能是**倾斜的**（实测 `更多样本/005.jpg`
    #: 底行就有明显倾斜），用轴对齐的 bbox 画出来会与实际边界差很多，
    #: 而"切得准不准"正是用户在界面上首先要判断的事。
    quad: tuple[tuple[float, float], ...] = ()
    #: 戳型判别（含全部中间量，便于排障）
    verdict: TypeVerdict | None = None
    #: 字段抽取结果
    parsed: ParsedFields | None = None
    #: OCR 行（原文 + 置信度），供校对 UI 对照
    ocr_lines: list[tuple[str, float]] = field(default_factory=list)
    #: 贴片（原图分辨率）
    cutouts: list[Cutout] = field(default_factory=list)
    #: **戳圆在页面坐标下的 (cx, cy, r)**，由 :func:`recognize_page` 填好。
    #:
    #: ⚠️ 为什么要有这个字段：``verdict.circle`` 是**卡局部**坐标
    #: （classify_postmark 跑在正射图上）。界面要把它画在整页上，
    #: 所以需要页面坐标 —— 而这个变换**必须在生产数据的地方做**。
    #:
    #: 第一版把这个变换放在 API 路由里，结果另一个消费者
    #: （`docs/frontDemo/generate.py`，它直接读 `verdict.circle`）
    #: **静默地保留了那个 bug**，重新生成的叠加图与修复前**逐字节相同**。
    #: 需要每个消费者记得做的变换，一定会被漏掉。
    circle_page: tuple[float, float, float] | None = None
    #: 决策与校对队列
    bundle: ReviewBundle | None = None
    #: 字段来自哪条通道：``local``（本地 OCR）或 ``vlm``（云端回退）。
    #: **必须记** —— 云端结果不可复现，校对与统计都要能区分它们。
    field_source: str = "local"
    #: **这一张卡的几何**来自哪条通道：``local``（本地切分）或 ``vlm``
    #: （B 方案的云端角点兜底）。
    #:
    #: ⚠️ 与 `field_source` 是**两件事**，刻意分开：一张卡完全可能是
    #: "几何本地给的、字段云端读的"（常见），或反过来。
    #: 合成一个字段会让"这张要不要多看一眼"这个问题答不出来 ——
    #: 而界面标这个标记的唯一目的就是回答它。
    geometry_source: str = "local"
    #: 这一张卡上发生过的降级/异常（**不静默**）
    notes: list[str] = field(default_factory=list)

    @property
    def postmark_type(self) -> str:
        return self.verdict.kind if self.verdict else TYPE_UNKNOWN


@dataclass(slots=True)
class PageResult:
    """一页（或一张单卡图）的完整结果。"""

    #: 处理尺度（相对原图）。所有像素判据都在这尺度上算，所以必须记。
    work_scale: float
    work_size: tuple[int, int]
    split: SplitResult | None = None
    cards: list[CardResult] = field(default_factory=list)
    #: 整页级别的说明（含各级降级）
    stage_notes: list[str] = field(default_factory=list)
    pipeline_version: str = PIPELINE_VERSION

    @property
    def card_count(self) -> int:
        return len(self.cards)

    def to_dict(self) -> dict[str, Any]:
        """可 JSON 序列化的摘要（不含图像像素）。

        图像不进去是有意的：观察记录只需要**元数据 + 位置**，
        而把 base64 像素塞进 observation 会让表迅速膨胀。
        """
        return {
            "pipeline_version": self.pipeline_version,
            "work_scale": round(self.work_scale, 4),
            "work_size": list(self.work_size),
            "split_level": self.split.level if self.split else None,
            "geometry_source": self.split.geometry_source if self.split else "local",
            "geometry_mode": GEOMETRY_MODE.value,
            "card_count": self.card_count,
            "stage_notes": list(self.stage_notes),
            "cards": [
                {
                    "index": c.index,
                    "bbox": list(c.bbox),
                    "card_size": list(c.card_size),
                    "size_suspect": c.size_suspect,
                    "geometry_source": c.geometry_source,
                    "postmark_type": c.postmark_type,
                    "type_confidence": c.verdict.confidence if c.verdict else 0.0,
                    "date": c.parsed.date.value if c.parsed and c.parsed.date else None,
                    "place": c.parsed.place.value if c.parsed and c.parsed.place else None,
                    "office": c.parsed.office.value if c.parsed and c.parsed.office else None,
                    "office_no": c.parsed.office_no if c.parsed else "",
                    "review_queue": [
                        {"field": it.field_name, "value": it.value, "why": it.why}
                        for it in (c.bundle.queue if c.bundle else [])
                    ],
                    "cutouts": [
                        {
                            "kind": k.kind,
                            "size": list(k.image.shape[:2]),
                            "size_mm": list(k.size_mm) if k.size_mm else None,
                            "dpi": k.dpi,
                            "source_bbox": list(k.source_bbox),
                        }
                        for k in c.cutouts
                    ],
                    "notes": list(c.notes),
                }
                for c in self.cards
            ],
        }


def _resize_for_work(image: np.ndarray, long_side: int) -> tuple[np.ndarray, float]:
    """按需缩小，返回 ``(图, 缩放比)``。缩放比必须一路传下去 ——
    所有以像素表达的判据都要乘它，否则量纲不一致（这个错犯过两次）。"""
    import cv2

    h, w = image.shape[:2]
    if max(h, w) <= long_side:
        return image, 1.0
    scale = long_side / max(h, w)
    return (
        cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA),
        scale,
    )


def recognize_card(
    card_image: np.ndarray,
    *,
    index: int = 0,
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0),
    quad: tuple[tuple[float, float], ...] = (),
    dpi: float | None = None,
    size_suspect: bool = False,
    known_places: frozenset[str] | None = None,
    known_offices: frozenset[str] | None = None,
    #: **这一张卡在原图分辨率下的正射图**。云端形态②要送它给 VLM。
    #:
    #: ⚠️ 语义在 2026-10-11 晚被收紧过一次（原来写的是"原图 = 未缩放的整页"）：
    #: 送整页会让模型在多卡页里**随便挑一枚戳**（`docs/ok/15` §3.2 的对照实验：
    #: 同一张卡，送整页得"泉州/东街"、送该卡裁切得"福建厦门/机场"）。
    #: 也不能送 work 尺度的 `card_image`（329x644 那种，模型看不见戳）。
    #: 正确粒度 = **用同一个 quad 从原图再裁一次**（见 `recognize_page` 的循环）。
    #: None 时退回 `card_image`（单卡入口的情形：那时 `card_image` 就是那张卡）。
    original_image: object | None = None,
    #: 按区划树查询的词表（`vocab.PlaceVocab`）。给了它就用**它**判"是不是地名"
    #: —— 比 `known_places` 那个扁平集合多两件事：能判"上级+本级"的连写是否
    #: **真的**是父子关系，还能补出完整层级链。见 `review.decide`。
    place_vocab: object | None = None,
    office_vocab: object | None = None,
    vlm_config: VlmConfig | None = None,
    vlm_primary: bool = False,
) -> CardResult:
    """识别**一张卡**（已切好的正射图）。单卡图可以直接用它，跳过切分。

    ``vlm_config`` 给定时启用**云端回退**：本地这一遍**没抽到日期**时，
    把同一块区域再交给云端 VLM 读一次并重新抽字段。

    为什么要这个回退（实测依据）：`盖的不好-墨太多` 上，本地 OCR 在三个
    margin 下**只读到明信片印刷抬头**（「邮政」「美丽中国」），从未读到邮戳 ——
    圆定位落在了抬头而不是邮戳上。而同一张图 VLM 读出了正确的
    「福建厦门」。所以这是一次**真实的疑难件**，正是 D5 说的那种情形。

    **只在本地失败时才调用**，所以它不是"每次都送云"：
    正常样本零额外延迟与费用。
    """
    result = CardResult(
        index=index,
        bbox=bbox,
        card_size=(card_image.shape[1], card_image.shape[0]),
        size_suspect=size_suspect,
        # ⚠️ 必须**转发**进来。这个参数曾经加了签名却没往下传，
        # 于是 API 层的 quad 永远是空 —— 而"空"看起来像"切分器没给"，
        # 排查会往切分器方向走错路（实测绕过一次）。
        quad=quad,
    )

    # ---- 戳型判别（含戳圈定位）----
    #
    # 尺寸先验用 **dpi**，不用"卡片几何比例"。这个选择是被实测推翻又推翻定下的：
    #
    # 第 11 轮我曾改为 `postmark_diameter_hint_px`（用"卡短边 × 25/54"消掉 DPI），
    # 理由是"正射图尺度未知、传绝对 DPI 必然错"，并声称特写的有效 DPI 约 360。
    #
    # **那个诊断是错的**（见 ok/10 §七之十三 的 DPI 审计）：
    #   * 特写是**从 600dpi 多卡图裁剪**出来的、**未缩放** → 真值就是 600dpi
    #   * 用检测半径 ÷ 600dpi 的物理期望半径，9 张的中位数是 **0.99**（几乎吻合）；
    #     ÷ 360dpi 的期望则是 1.65（差 65%）
    #
    # 而几何先验在**裁剪图**上是有害的：裁剪后的长宽比仍可能落在"像整卡"的
    # 容差内，于是它按整卡推算戳径，结果圆心能偏 **250px**（实测 `有污渍`：
    # 几何先验给 (759,1378)，dpi=600 给 (551,853)）。
    #
    # 所以：**优先用 dpi**（它现在有实测支撑）；只有拿不到 dpi 时才退回几何先验。
    try:
        if dpi:
            verdict = classify_postmark(card_image, dpi=float(dpi))
        else:
            hint = postmark_diameter_hint_px(card_image)
            verdict = classify_postmark(card_image, expected_diameter_px=hint)
            if hint:
                result.notes.append(
                    f"无 dpi，退回几何先验 {hint:.0f}px —— "
                    f"该先验在裁剪图上可能偏（实测圆心可偏 250px），仅作兜底"
                )
    except Exception as exc:
        result.notes.append(f"戳型判别失败：{type(exc).__name__}: {exc}")
        return result
    result.verdict = verdict
    if dpi:
        result.notes.append(f"戳径先验按 dpi={float(dpi):.0f} 折算（实测 8/9 张吻合）")

    # ---- 戳型：给了云端配置就用 VLM 决定（实测依据见下）----
    #
    # 实测（`tools/samples/a3_vlm_whole.py`）：在我逐张判读的 9 张上，
    # **本地特征 4/9 = 44.4%，整卡 VLM 9/9 = 100%**。
    #
    # 本地特征失败**不是阈值问题**（ok/09 E-15）：风景戳的线描图正好落在
    # 本地特征找"文字"的位置上，结构比文字还丰富。
    # 换阈值、换"块的尺寸分布"都试过，都不行。
    #
    # 所以：**给了云端配置就让 VLM 决定戳型**，本地判定作为对照记录在 notes 里
    # （便于继续观察本地特征在真实数据上的表现，也为离线场景保留它）。
    #
    # ⚠️ **`vlm_primary` 时跳过这一步**（2026-10-11）：那种形态下**同一次**
    # 整卡调用已经把戳型一起给出来了（见下面那段）。若仍跑这里，就是每卡
    # **2 次**调用，而实测"整卡一次"本来就够 —— 省下的那一次正是改这个的原因。
    if vlm_config is not None and not vlm_primary:
        from postmarktrip.recognition.vlm import (
            PROMPT_CLASSIFY_TYPE,
            ask,
        )

        type_ans = ask(vlm_config, _pad_for_vlm(card_image), PROMPT_CLASSIFY_TYPE)
        vlm_kind = _vlm_kind(type_ans.text) if type_ans.ok else ""
        if vlm_kind:
            result.notes.append(
                f"戳型由云端判定为 {vlm_kind}（本地曾判 {verdict.kind}）—— "
                f"本地特征在 9 张人工判读样本上只有 44.4%，见 ok/09 E-15"
            )
            verdict = TypeVerdict(
                kind=vlm_kind,
                confidence=0.8,
                top_arc_text=verdict.top_arc_text,
                center_image=verdict.center_image,
                circle=verdict.circle,
                reason=f"云端 VLM 判定（本地为 {verdict.kind}）",
            )
            result.verdict = verdict
        else:
            result.notes.append(
                f"云端戳型不可用（{type_ans.reason[:60]}），沿用本地判定 {verdict.kind}"
            )
    if verdict.circle is None:
        result.notes.append(f"未定位到戳圈：{verdict.reason}")
        result.bundle = build_bundle(ParsedFields(), postmark_type=verdict.kind)
        return result

    # ---- 文字区域（整块，主路径）----
    region = extract_postmark_region(card_image, verdict.circle)
    if region is None:
        result.notes.append("戳区域为空（戳圈过小或落在画面外）")
        result.bundle = build_bundle(
            ParsedFields(),
            postmark_type=verdict.kind,
            postmark_type_confidence=verdict.confidence,
        )
        return result
    canvas, region_bbox = region

    # ---- OCR ----
    ocr = recognize(canvas, orientation="horizontal")
    if not ocr.available:
        result.notes.append(f"OCR 不可用：{ocr.reason}")
        result.bundle = build_bundle(
            ParsedFields(),
            postmark_type=verdict.kind,
            postmark_type_confidence=verdict.confidence,
        )
        return result
    lines = [(ln.text, ln.confidence) for ln in ocr.lines]
    result.ocr_lines = lines
    ys = [float(np.mean([p[1] for p in ln.box])) for ln in ocr.lines]
    if not lines:
        result.notes.append("OCR 没有返回任何文本行")

    # ---- 字段抽取 ----
    parsed = parse_fields(lines, region_height=canvas.shape[0], line_y_positions=ys)
    result.parsed = parsed
    result.notes.extend(parsed.notes)

    # ---- 云识别优先：**整卡一次读完戳型与全部字段**（形态②，2026-10-11 改）----
    #
    # ## 为什么改成"整卡 + 一次调用"
    #
    # 原来这里是：拿**裁剪区域**（`extract_postmark_region`）喂 VLM，按"顶弧/中部/
    # 底弧"三段读文本，再交给 `parse_fields` 解析。而外层还有一次
    # `PROMPT_CLASSIFY_TYPE` 判型 —— 于是**每卡 4 次云端调用**。
    #
    # 实测（7 张真值样本，见 ok/14 §十一）：**整卡一次调用反而更准**：
    #
    # | 项 | 整卡 1 次调用 | 本地那套 |
    # | --- | --- | --- |
    # | 日期 | **7/7** | 差（`盖的不好-邮票歪` 连地名都抽不到）|
    # | 地名 | **6/6** | 本地 0/6（ok/11 记录）|
    # | 局名 | 4/5 | |
    # | 戳型 | 6/7 | 本地 4/9（ok/09 E-15）|
    # | 延迟 | **约 1.0~1.3s/卡** | |
    #
    # 而且它**绕开了本地那条坏链**：本地判型依赖"文字环"，而圆检测在极小扰动下
    # 会落到不同局部解（同一张卡差 1x1 就给 r=352 与 r=226），于是返回 `unknown`、
    # 字段全空。云端判型**不经过文字环**，所以那个症状不会出现。
    #
    # ## 三件事一起变了（都写在这里，免得下一个人以为是三处独立改动）
    #
    # 1. 输入从"裁剪区域"改成**整卡** —— 本地找圆不可靠时，区域本身就是错的
    #    （实测教训：`盖的不好-墨太多` 上圆落在明信片印刷抬起而不是邮戳上）
    # 2. 输出从"三段文本"改成**JSON** —— 字段直接给出，不再靠 `parse_fields` 猜
    # 3. 戳型也由这**同一次**调用给出 —— 省掉 `PROMPT_CLASSIFY_TYPE` 那一次
    #
    # ⚠️ **不做位置/坐标**（用户 2026-10-11 裁定"不救了"）：实测整卡调用给的
    # 边界框不可靠（`w/短边` 该≈0.46，实测有 0.031 —— 框住了邮票）。而管线的
    # `dpi` 先验、环字高度阈值、上下分割全建立在"精确圆"上，它给不出。
    #
    # ⚠️ 已知简化（**如实写明，别当它不存在**）：这里仍是"先跑完本地再覆盖"，
    # 所以白跑了约 0.3s/卡的本地 OCR。正式版应当做真正的分支——直接跳过本地。
    if vlm_config is not None and vlm_primary:
        from postmarktrip.recognition.vlm import PROMPT_READ_CARD_JSON, ask, parse_card_json

        #: ⚠️ **送"这张卡的原图分辨率裁切"，不送整页、也不送 work 尺度卡图**。
        #:
        #: 这是 2026-10-11 两次实测叠出来的结论：
        #:
        #: | 送给 VLM 的图 | 结果 |
        #: | --- | --- |
        #: | 工作尺度卡图 329x644 | `type=scenic`，其余全空 |
        #: | 同卡放大 3.65x / 4.86x / 6.08x | **全空（更差）** |
        #: | 单卡样本的**原图 5072x7014** | `date=2026-10-01 place=江西玉山 office=三清山` ✅ |
        #: | **多卡页的整页原图**（曾经的做法）| 模型面对 9 枚戳随便挑一枚 → 张冠李戴 ❌ |
        #:
        #: 我原以为"卡图太小、模型看不见戳"，于是去放大 —— **错了两次**：放大既
        #: 没帮助（插值造不回细节），又丢掉了上下文。第二轮才发现"送原图"这条
        #: 结论是在**单卡**样本上得出的 —— 那时"原图"就等于"那张卡"，推广到多卡页
        #: 就变成了"给错对象"。**粒度是"一张卡"，不是"一次上传"。**
        #: 详见 `docs/ok/15` §3.2。
        #:
        #: **不额外花钱**：`vlm._data_url` 的注释写明厂商按"一张图 ≤384 tokens"
        #: 计费 —— 所以送原图与送小图同价，而缩了会丢小字。
        whole_card = _pad_for_vlm(original_image if original_image is not None else card_image)
        ans = ask(vlm_config, whole_card, PROMPT_READ_CARD_JSON)
        got = parse_card_json(ans.text) if ans.ok else {}
        if got:
            #: 戳型：**云端说 priority**（这条形态的意义就在于此）。认不出就沿用本地。
            vlm_kind = _vlm_kind(got.get("type", ""))
            if vlm_kind:
                #: ⚠️ 置信度**不能原样继承本地**（2026-10-11 晚修，是接线修好后
                #: 才暴露出来的第二层问题）：本地判 `unknown` 时 confidence=0.0，
                #: 于是"云端判对了 scenic"却带着 0.0 → `decide()` 判 `reject`
                #: （"置信度 0.00 低于下限 0.3"）→ 界面上显示**未采信**，
                #: 一枚判对的风景戳反而像是"没读到"。实测 `002.jpg` 8 张里
                #: 有 3 张是这个形态。
                #: 取 `max(本地, 云端下限)`：本地高时保留它的置信度（那是有效信息），
                #: 本地低（尤其 unknown=0）时给一个**云端的下限** —— 与云端字段
                #: 一律记 0.9 的口径一致。
                verdict = TypeVerdict(
                    kind=vlm_kind,
                    confidence=max(verdict.confidence, CLOUD_TYPE_CONFIDENCE),
                    top_arc_text=verdict.top_arc_text,
                    center_image=verdict.center_image,
                    circle=verdict.circle,
                    reason=(
                        f"云端整卡判定（本地曾判 {verdict.kind}，置信度 {verdict.confidence:.2f}）"
                    ),
                )
                result.verdict = verdict

            cloud = _parsed_from_card_json(got)
            #: ✅ 只要这次调用**给出了任何可用的东西**就采信它 —— 包括"全都空"。
            #: 因为"空"是有信息的：它表示**这枚戳上没有那些字段**，
            #: 而沿用本地会把一个已被证伪的读数（如`福建南火`）当成真的。
            result.parsed = cloud
            result.ocr_lines = [(f"{k}={v}", 0.9) for k, v in got.items() if v]
            #: 来源写细一点（`docs/ok/17` §二 A3）：这条是"整卡一次 JSON 调用"，
            #: 与下面那条"本地/JSON 都没日期时的回退"是**两条不同的通道**，
            #: 而它们以前都只写 `"vlm"` —— 诊断时只能靠置信度 0.90/0.75 去猜。
            result.field_source = "vlm_json"
            result.notes.append(
                f"戳型与字段由**整卡一次**云端调用读取"
                f"（{vlm_config.provider}/{vlm_config.model}，{ans.latency_s:.1f}s）"
                f" —— 1 次调用替代原来的 4 次；本地只做几何"
            )
            result.notes.append("云端原始 JSON：" + json.dumps(got, ensure_ascii=False))
            #: ⚠️ **原文一定要留在 notes 里，包括"解析成功但字段全空"的情况。**
            #: 实测踩过：两张风景戳样本云端返回空字段，而同一张图手工调用却能读出
            #: 内容。当时我只有"解析后的 JSON"（全是空串），看不到模型**实际**
            #: 回了什么，于是没法判断是"它没读到"还是"我们的解析丢了东西"。
            #: 留原文是一行成本，省下的是一轮瞎猜。
            if ans.text:
                result.notes.append(f"云端回复原文：{ans.text[:300]}")
        else:
            result.notes.append(
                f"云端整卡读取不可用（ok={ans.ok} {ans.reason[:60]}），沿用本地结果"
            )
            if ans.text:
                result.notes.append(f"云端原文（解析失败）：{ans.text[:300]}")

    # ---- 云端回退：**最终生效的那份**还没拿到日期时，把整张卡交给 VLM ----
    #
    # 判据是"**没抽到日期**"而不是"置信度低"：日期是结构化字段，
    # 抽不到它就是一次明确的失败；而置信度低可能只是盖得淡。
    #
    # ⚠️ **判据看的是 `result.parsed`（最终生效的那份），不是本地那份**
    # （2026-10-11 修，见 `docs/ok/17` §二 A1）：形态②（整卡 JSON）已经读到
    # 日期时，再跑一次回退既白花一次调用，又会用另一条通道的结果**覆盖**它 ——
    # 实测 003.jpg 卡 #1 的 `inscription` 就是这么被冲掉的。
    #
    # ⚠️ **必须升级粒度（给整卡），不是重读同一块裁剪。**
    #
    # 实测教训：`盖的不好-墨太多` 上本地 OCR 只读到明信片印刷抬头，
    # 原因是**圆定位落在了抬头而不是邮戳上** —— 区域本身就是错的。
    # 第一版回退把**同一块错区域**再交给 VLM，结果它也只好读到抬头，
    # note 里留下"云端回退也没抽到日期"，A4 一点没变。
    #
    # 正确做法是让 VLM 在**整张卡**上自己找邮戳 —— 它有空间理解能力，
    # 能处理"我们的圆检测找不到"的情形，而这正是"疑难件"该有的含义。
    if vlm_config is not None and result.parsed.date is None:
        from postmarktrip.recognition.vlm import (
            PROMPT_READ_POSTMARK,
            ask,
        )

        whole = _pad_for_vlm(card_image)
        ans = ask(vlm_config, whole, PROMPT_READ_POSTMARK)
        if ans.ok and ans.text:
            vlm_lines = _vlm_lines_to_rows(ans.text)
            if vlm_lines:
                vlm_parsed = parse_fields(
                    [(txt, 0.75) for txt, _y in vlm_lines],
                    region_height=whole.shape[0],
                    line_y_positions=[y for _t, y in vlm_lines],
                )
                if vlm_parsed.date is not None:
                    #: ⚠️ **只补缺，不整体替换**（`docs/ok/17` §二 A2）：
                    #: 回退通道是"三段文本 + `parse_fields`"，它**产不出风景戳原文**；
                    #: 整体替换会把形态②已经读到的 `inscription` 静默丢掉。
                    merged, filled = _merge_fallback(result.parsed, vlm_parsed)
                    result.parsed = merged
                    result.ocr_lines = [(txt, 0.75) for txt, _y in vlm_lines]
                    result.field_source = "vlm_fallback"
                    result.notes.append(
                        f"整卡 JSON 与本地都没拿到日期 → 云端回退成功"
                        f"（provider={vlm_config.provider} model={vlm_config.model}，"
                        f"{ans.latency_s:.1f}s）；本次由回退补齐的字段："
                        f"{'、'.join(filled) if filled else '（无）'}"
                    )
                else:
                    result.notes.append("云端回退也没抽到日期")
        else:
            result.notes.append(f"云端回退不可用：{ans.reason[:80]}")

    # ---- 决策与校对队列 ----
    # ⚠️ 必须传 `result.parsed`，**不是局部变量 `parsed`**。
    #
    # `result.parsed` 是"最终生效的那一份" —— 本地解析，或者被云端覆盖后的
    # （形态②：`vlm_primary`）。曾经这里传的是局部 `parsed`，于是云端页
    # **显示本地字段却标着 `field_source="vlm"`** —— 一个会误导人的组合：
    # 界面看起来"云端读得跟本地一样"，其实 bundle 根本没用到云端结果。
    # 戳号的丢失就是这样被掩盖的（云端读到了「中亭街1」，bundle 里是本地
    # 读的「中亭街」）。
    result.bundle = build_bundle(
        result.parsed,
        known_places=known_places,
        known_offices=known_offices,
        place_vocab=place_vocab,
        office_vocab=office_vocab,
        postmark_type=verdict.kind,
        postmark_type_confidence=verdict.confidence,
        bbox=region_bbox,
    )

    # ---- 抠图 ----
    pm = cut_postmark(card_image, verdict.circle, dpi=int(dpi) if dpi else None)
    if pm:
        result.cutouts.append(pm)
    stamp_box = detect_stamp_region(card_image)
    if stamp_box:
        st = cut_stamp(card_image, stamp_box, dpi=int(dpi) if dpi else None)
        if st:
            result.cutouts.append(st)
    else:
        result.notes.append("未定位到邮票区域（按设计返回 None，交人工框选）")

    return result


def _quad_bbox(quad: np.ndarray) -> tuple[int, int, int, int]:
    """四角点 → 外接框 ``(x, y, w, h)``，**全部是 Python int**。

    存在的唯一理由是打掉 numpy 类型：``numpy.float32`` 不能 ``json.dumps``，
    而 observation 的 ``bbox`` 列是 JSONB。详见调用处的说明。
    """
    if quad is None or getattr(quad, "size", 0) == 0:
        return (0, 0, 0, 0)
    xs = quad[:, 0]
    ys = quad[:, 1]
    x0, y0 = int(xs.min()), int(ys.min())
    x1, y1 = int(np.ceil(xs.max())), int(np.ceil(ys.max()))
    return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))


#: 回退通道**能补**的字段名（与 `ParsedFields` 的字段一一对应）。
#: 漏一个就等于"那条通道读到了但没并进来"，而这种事没有报错。
_FALLBACK_FIELDS: Final[tuple[str, ...]] = (
    "date",
    "place",
    "office",
    "office_no",
    "inscription",
    "date_text",
    "time_code",
)


def _merge_fallback(base: ParsedFields, fallback: ParsedFields) -> tuple[ParsedFields, list[str]]:
    """ "只补缺"地合并回退通道的结果。返回 `(合并后的, 由回退补齐的字段名)`。

    ## 为什么必须是"补缺"而不是"替换"（`docs/ok/17` §二 A2）

    形态②（整卡一次 JSON 调用）已经能读出**戳面原文**，而回退通道是
    "三段文本 + `parse_fields`" —— 它**产不出风景戳原文**。原来的写法是
    `result.parsed = vlm_parsed`（整体替换），于是实测 003.jpg 卡 #1 上
    云端**明明回了** `inscription=西海栈道`，却被这条通道静默冲掉，
    界面上表现为"缺戳面原文"（要人手敲一行）。

    合并规则很简单：**已有值的一律留着，只填空的**。
    字符串字段以"非空"为有值，`Field | None` 字段以"非 None"为有值。
    """
    merged = ParsedFields(
        date=base.date,
        place=base.place,
        office=base.office,
        office_no=base.office_no,
        inscription=base.inscription,
        date_text=base.date_text,
        time_code=base.time_code,
        notes=list(base.notes),
    )
    filled: list[str] = []
    for name in _FALLBACK_FIELDS:
        current = getattr(merged, name)
        value = getattr(fallback, name)
        if isinstance(current, str):
            if current or not value:
                continue
        elif current is not None:
            continue
        if value:
            setattr(merged, name, value)
            filled.append(name)
    merged.notes.extend(fallback.notes)
    return merged, filled


def _parsed_from_card_json(got: dict[str, str]) -> ParsedFields:
    """整卡 JSON 回复 → `ParsedFields`。

    ## 为什么要有这一层（不是"多此一举的转换"）

    `parse_fields` 是**从行文本猜字段**的器（它按 y 位置分顶弧/底弧）。而整卡
    调用**已经把字段给出**了，再让 `parse_fields` 去猜等于把信息又丢一次
    —— 实测形态②原来就是那么做的（把 JSON 前的三段文本喂给 `parse_fields`），
    字段归属要靠猜。

    所以这里**直接用**：

    * `place` / `office` / `office_no` 直接落到对应字段
    * `date` / `date_text` / `time_code` —— **优先按日期原文解析**（原文里同时带时，
      如 `2026.10.01.09`）：走与本地路径**同一个** `parse_date_text()`，这样
      同一天在两条路径上不会被解析成不同的东西。原文没有时才退回规范值
    * `inscription`（戳面原文，`docs/ok/16` §四）直接落到对应字段
    * 置信度给 `0.9`（云端没有逐字段置信度，这是给决策层的输入；
      `decide` 的阈值会据此判绿/黄，而词表那条防线仍然生效）

    ⚠️ **缺的字段不出现**，而不是给空串 —— 与 `ParsedFields` 的既有约定一致。
    这样下游能区分"云端说这个字段是空的"与"云端没给这个字段"。
    """
    out = ParsedFields()
    #: 云端没有逐字段置信度，给一个统一值。**不要给 1.0** ——
    #: 那会让"词表未命中"这条防线看起来像误报；0.9 如实表达"云端说的，待确认"。
    conf = 0.9

    place = got.get("place", "").strip()
    if place:
        out.place = Field(value=place, confidence=conf, source_line=place)

    office = got.get("office", "").strip()
    office_no = got.get("office_no", "").strip()
    if office or office_no:
        #: 局名的 `source_line` 用"局名+戳号"的原始样子 —— 与本地路径一致，
        #: 这样校对界面显示的仍是戳面上那一行。
        line = f"{office}{office_no}" if office and office_no else (office or office_no)
        if office:
            out.office = Field(value=office, confidence=conf, source_line=line)
        #: 戳号是纯字符串字段（没有置信度），与 `ParsedFields` 的约定一致
        out.office_no = office_no

    normalized_date = got.get("date", "").strip()
    raw_date = got.get("date_text", "").strip() or normalized_date
    if raw_date:
        parsed_date = parse_date_text(raw_date, confidence=conf)
        if parsed_date is not None:
            out.date = parsed_date
            out.date_text = raw_date
            out.time_code = time_code_of(parsed_date)
            out.notes.append(f"日期：云端给出 {raw_date!r} → {parsed_date.value}")
        else:
            out.notes.append(f"日期：云端给出 {raw_date!r}，但解析不出合法日期（丢弃）")
    #: 云端单独给了时码、而原文里没有时（如原文只到日）→ 用它补。
    #: **只在它长得像个时码时**（1~2 位数字），否则宁可留空。
    raw_time = got.get("time_code", "").strip()
    if raw_time and not out.time_code and len(raw_time) <= 2 and raw_time.isdigit():
        out.time_code = f"{int(raw_time):02d}"
        out.notes.append(f"时码：云端单独给出 {raw_time!r} → {out.time_code!r}")
    #: 原文兜底：云端没回 `date_text` 时，至少把规范值当原文留着，别丢
    if out.date is not None and not out.date_text:
        out.date_text = normalized_date or out.date.source_line

    inscription = got.get("inscription", "").strip()
    if inscription:
        out.inscription = Field(value=inscription, confidence=conf, source_line=inscription)

    out.notes.append(f"字段来自整卡云端调用（{sorted(got)}）")
    return out


def recognize_page(
    image: np.ndarray,
    *,
    long_side: int = DEFAULT_WORK_LONG_SIDE,
    dpi: float | None = None,
    known_places: frozenset[str] | None = None,
    known_offices: frozenset[str] | None = None,
    place_vocab: object | None = None,
    office_vocab: object | None = None,
    vlm_config: VlmConfig | None = None,
    vlm_primary: bool = False,
    expected_cards: int = DEFAULT_EXPECTED_CARDS,
) -> PageResult:
    """识别**一整页**：切分 → 对每张卡走 :func:`recognize_card`。

    ``dpi`` 为 None 时按"切分器从卡片尺寸反推的值"用，反推不到则用
    ``FALLBACK_DPI``。**这个选择会被记进 notes** —— 因为它直接决定
    戳圈搜索范围与所有 mm↔px 换算。

    ## 几何来自哪条通道：`split.GEOMETRY_MODE`（B/A 共用的开关）

    ``vlm_config`` 给定（= 请求里 ``use_vlm=true`` 且服务端开了）时：

    * ``CLOUD_FIRST``（**A，当前生效**）—— **每页都问**一次云端"每张卡的
      四个角"，几何以云端为准，本地只做交叉校验（`_crosscheck_note`）；
    * ``LOCAL_FIRST``（B）—— 先跑 `split.suspect_geometry`，**只在可疑时**问云端。

    ``expected_cards`` 是"这一页应该有几张卡"的先验，默认
    :data:`split.DEFAULT_EXPECTED_CARDS`（9，实测样本都是 3×3 的整页扫描）。
    **单卡入口必须传 0**（= "不知道有几张"）—— 否则每张单卡图都会被
    "少于 9 张"判成可疑，于是每传一张卡就多花一次云端调用，
    而单卡图恰恰最不需要云端兜底（它本来就只有一张卡）。
    """
    work, scale = _resize_for_work(image, long_side)
    page = PageResult(work_scale=scale, work_size=(work.shape[1], work.shape[0]))

    # ---- 切分 ----
    try:
        # split_page 的 dpi 是 int（它内部按整数算阈值）；显式取整而不是
        # 靠隐式转换 —— 加 type: ignore 会把"这里确实发生了取整"藏起来。
        split = split_page(work, dpi=int(dpi) if dpi else None, detection_scale=scale)
    except Exception as exc:
        page.stage_notes.append(f"切分失败：{type(exc).__name__}: {exc}")
        return page
    page.split = split
    page.stage_notes.extend(split.notes)

    # ---- B 方案：本地几何**可疑**时，问一次云端"每张卡的四个角" ----
    #
    # ⚠️ 触发条件是 `vlm_config is not None` —— 也就是**请求显式带
    # `use_vlm=true`**。这条线是刻意的（与 `recognize_card` 里云端那两条
    # 通道同一个开关）：
    #
    #   * 端点默认 `use_vlm=false` → **零云端调用**，`_diag_split.py` 那种
    #     "只看几何"的诊断可以反复跑而完全不花钱、也不受云端影响；
    #   * 云端结果**不可复现**，而几何错的表现（一张歪的卡图）不会报错 ——
    #     默认开着它等于把"可复现"这条性质送掉。
    #
    # 实测（2026-10-13）：`use_vlm=false` 时 001/005 仍是 8/7 张（与改动前
    # 逐字相同），证明这条分支没被打开；`use_vlm=true` 才走下面这段。
    if vlm_config is not None:
        #: **每次调用都重新读一次模块级常量**（而不是把它固化成一个默认参数）——
        #: 这样它才是真的"一个常量切回"（`ok/20` §七的回滚点），
        #: 而且测试能 monkeypatch 它来分别验证 B 与 A 两条路。
        geometry_mode = GEOMETRY_MODE
        #: **B 与 A 的分叉点**（`split.GEOMETRY_MODE`）。
        #:
        #: * `LOCAL_FIRST`（B）：先看本地几何可不可疑，可疑才问云端；
        #: * `CLOUD_FIRST`（A）：**每页都问**，本地几何只留作交叉校验。
        #:
        #: ⚠️ 注意 `expected_cards <= 0` 时两条路都**不问**云端：那是调用方在说
        #: "这一页本来就该有几张卡我不知道"（单卡入口就是这样）。对它做
        #: "少于期望张数"的判断只会把**每一张单卡图**都判成可疑，
        #: 于是每传一张卡就多花一次云端调用 —— 而单卡图恰恰是最不需要云端兜底的
        #: （它本来就只有一张卡，切分出错的空间极小）。
        cloud_first = geometry_mode is GeometryMode.CLOUD_FIRST
        verdict = suspect_geometry(
            split.candidates, (work.shape[1], work.shape[0]), expected_cards=expected_cards
        )
        page.stage_notes.append(
            f"几何模式 = {geometry_mode.value}；"
            + (
                "云端优先（每页都问）"
                if cloud_first and expected_cards > 0
                else f"几何可疑性检查：{'**可疑** → ' + verdict.reason_text if verdict.needed else '正常（不调云端）'}"
            )
        )
        if expected_cards > 0 and (cloud_first or verdict.needed):
            local_n = len(split.candidates)
            #: **本地那一份要留住**：追问出来的第二份答案必须从**同一份本地结果**
            #: 重新合并，不能在"第一次合并的结果"上再合并一次。
            #:
            #: ⚠️ 这是实测踩出来的（2026-10-13，`005.jpg` 那趟追问）：
            #: 第一次云端答案把底行三张框歪了（350x209、宽高比 1.68），而本地
            #: 7 张里只有 3 张与它重叠 —— 剩下 4 张本地卡**留在结果里**；
            #: 此时再叠上第二份云端答案，两份云端各说各的、彼此不重叠，
            #: 最后 **13 张**（正确 9 张）。那是"越修越坏"，从数字上还看不出来。
            #: 所以规则写死：**每份云端答案都独立地与本地合并，两份之间只比优劣、不相加。**
            baseline = split

            def _with_cloud(hint: str) -> SplitResult:
                corners = ask_card_corners(vlm_config, work, hint=hint)
                page.stage_notes.append(
                    f"云端角点（hint={'有' if hint else '无'}）："
                    f"ok={corners.ok} 给出 {len(corners.quads)} 个四边形，"
                    f"耗时 {corners.latency_s:.1f}s"
                    + (f"；{corners.reason[:60]}" if corners.reason else "")
                )
                if not corners.quads:
                    return baseline
                merged = merge_cloud_corners(
                    work, baseline, corners, dpi=baseline.dpi, detection_scale=scale
                )
                #: 只把**这一份云端答案带来的** notes 报出来（`merge_cloud_corners`
                #: 的 notes = 本地那份 + 云端那几条，本地那几条上面已经报过了，
                #: 再报一遍会让同一段在页面上一串重复）。
                page.stage_notes.extend(merged.notes)
                return merged

            split = _with_cloud("")
            page.split = split

            #: **答得不对就带线索再问一次（最多 2 次调用）**。
            #:
            #: 两种"不对"都有实测依据（2026-10-13，同一台机器同一个模型）：
            #:   * **答少了**：`001.jpg` 同图同提示两次分别回了 9 条与 8 条；
            #:   * **答歪了**：另一次运行里 `005.jpg` 的底行三张被框成 391×547
            #:     （同页另两行 604/656 高）→ 宽高比 0.71 → 形状不对。
            #:
            #: 所以第二次给的是**缺口/判据**（`vlm.corners_prompt`），
            #: 而不是把同一句重发；两次结果谁好谁留下（好 = 形状可疑的云端卡更少，
            #: 其次卡更多）。上限写死在代码里：这是纠正已知非确定性的补丁，
            #: 不是可调策略。
            suspect_cloud = _cloud_shape_suspects(split)
            short = len(split.candidates) < expected_cards
            if suspect_cloud or short:
                why = (
                    f"云端给的 {suspect_cloud} 张形状不像卡"
                    if suspect_cloud
                    else f"合计只有 {len(split.candidates)} 张 < 期望 {expected_cards} 张"
                )
                page.stage_notes.append(f"→ 追问一次（{why}）")
                first = split
                first_bad = len(suspect_cloud)
                second = _with_cloud(CORNERS_SHAPE_HINT)
                second_bad = len(_cloud_shape_suspects(second))
                better = second_bad < first_bad or (
                    second_bad == first_bad and len(second.candidates) > len(first.candidates)
                )
                if better:
                    split = second
                dropped = first if better else second
                page.split = split
                page.stage_notes.append(
                    f"两次云端答案取优：第 1 次 {len(first.candidates)} 张/形状可疑 {first_bad} 张，"
                    f"第 2 次 {len(second.candidates)} 张/形状可疑 {second_bad} 张 "
                    f"→ 采用第 {'2' if better else '1'} 次"
                )
                #: ⚠️ 被**放弃**那份的 notes 也要留：否则排障时看不到"另一份
                #: 为什么没被采用"，而那正是要复盘的东西。**采用**那份的 notes
                #: 由下面统一加（只加一次，避免同一段重复出现两遍）。
                if dropped is not split:
                    page.stage_notes.append(
                        "未采用的云端答案（仅供排障）：" + "；".join(dropped.notes)
                    )

            if split is not baseline:
                page.stage_notes.extend(split.notes)
            #: **A 模式下的"本地只做交叉校验"**（`ok/20` §七 第二句）：
            #: 本地某个框与云端给的**最像**的那张，IoU 低到不可能是同一张卡时，
            #: 两条通道在这一处**根本不同意** —— 那正是人该看一眼的地方。
            #: B 模式下不提这条：它已经把结论写成"替换了多少张"了。
            if cloud_first:
                page.stage_notes.append(_crosscheck_note(baseline, split))
            page.stage_notes.append(
                f"云端几何兜底：本地 {local_n} 张 → 合计 {len(split.candidates)} 张"
                f"（触发原因：{verdict.reason_text}）"
            )
        else:
            page.stage_notes.append("云端几何没有介入（本地几何正常，或这一页没有张数先验）")

    # ---- DPI 决策（记下来，因为它影响后面所有物理量）----
    #
    # 🔴 **单位陷阱（我在这里犯过一次错，症状很隐蔽）**
    #
    # `split.dpi` 永远是**原图尺度**的 DPI；而 `recognize_card` 收到的是
    # 切分器产出的**正射图**，它是**工作尺度**（= 原图 x scale）的。
    #
    # 所以无论 dpi 是"反推来的"还是"调用方给的"，给下阶段的都必须是
    # **work 尺度的 dpi**：`dpi_work = dpi_original * scale`。
    #
    # 曾经写错成"调用方给的值就直接用" → 600dpi 下戳径期望 590px，
    # 而工作尺度 0.285 的卡裁剪图里真戳只有约 168px，**落在搜索区间之外**。
    # 于是 `find_postmark_circle` 去抓了个**卡尺度的圆**（实测 r=302 而卡宽 361），
    # 后续"顶弧"取到的是**明信片印刷抬头**或**邮票图案**。
    #
    # 后果不是崩溃，而是**特征全是噪声**：`top_arc_text` 在日戳/风景戳上
    # 完全重叠（实测 28 张上一致率仅 42.9%），看起来像"判据不成立"。
    if split.dpi_source.startswith("cards"):
        page.stage_notes.append(f"DPI 由卡片尺寸反推为 {split.dpi}（原图尺度）")
    elif dpi:
        page.stage_notes.append(f"DPI 采用调用方给的值 {dpi}（原图尺度）")
    else:
        page.stage_notes.append(
            f"DPI 无法反推，用兜底值 {FALLBACK_DPI}（原图尺度）—— "
            f"**毫米数字不可信**（见 ok/09 E-04 与 ok/10 §七之二）"
        )
    # **统一折算到 work 尺度**（这一步是必需的，不是优化）
    work_dpi = float(split.dpi or FALLBACK_DPI) * scale
    page.stage_notes.append(
        f"→ work 尺度 dpi = {work_dpi:.1f}（= {split.dpi} x {scale:.4f}）；"
        f"正射图就在这个尺度上，所以后续先验必须用它"
    )

    # ---- 逐卡识别 ----
    for i, cand in enumerate(split.candidates):
        #: 云端形态②要送"**这一张卡**"的原图。
        #:
        #: ⚠️ 这里修掉一个很隐蔽的坑（2026-10-11，见 `docs/ok/15` §3.2）：
        #: 此前传的是 `image` —— **整页**。于是多卡页（一页 9 枚戳）里，模型
        #: 面对 9 枚戳被问"卡上那枚邮戳"，只能随便挑一枚：实测 `002.jpg`
        #: 有 **5 张卡拿到同一组值**，用户上传的"厦门机场"那枚被填成
        #: "泉州/东街"。
        #:
        #: 也不能退回 `cand.ortho`：它是 **work 尺度**（329x644 那种），
        #: 实测模型看不清（`docs/ok/14-B` §11.4 的四组对照）。
        #: 所以正确粒度是"**用同一个 quad，从原图再裁一次**"：几何与 ortho
        #: 完全一致，只是分辨率回到原图。单卡入口两者等价（那边本来就退
        #: 回 `card_image`）。
        card_original = (
            warp_card(image, np.asarray(cand.quad, dtype=np.float32) / np.float32(scale))
            if scale
            else None
        )
        # ⚠️ bbox 必须转成 Python int。`cand.quad` 是 numpy 数组，
        # `.min()` 返回 `numpy.float32` —— 它能算、能比较、能打印，
        # 但**不能 json.dumps**。而 observation 要写进 JSONB 列，
        # 所以这个类型问题会在写库那一刻才炸，离出错的地方很远。
        # 集成测试抓到过一次，所以这里显式取整。
        card = recognize_card(
            cand.ortho,
            index=i,
            bbox=_quad_bbox(cand.quad),
            dpi=work_dpi,
            size_suspect=cand.size_suspect,
            quad=tuple((float(x), float(y)) for x, y in cand.quad),
            original_image=card_original,
            known_places=known_places,
            known_offices=known_offices,
            place_vocab=place_vocab,
            office_vocab=office_vocab,
            vlm_config=vlm_config,
            vlm_primary=vlm_primary,
        )
        # 页面坐标的戳圆：在这里填，消费者**不需要**自己变换
        if card.verdict is not None and card.verdict.circle is not None:
            card.circle_page = circle_to_page(card.verdict.circle, card.quad, card.card_size)
        #: 几何来源**逐卡**记录（界面按卡标"几何来自云端"）。
        #: 从候选上取，而不是从 `split.geometry_source` 取：一页里可能
        #: **云端补了 3 张、另外 6 张仍是本地的** —— 页级那个值只说"这页用过云端"。
        card.geometry_source = cand.geometry_source
        card.notes.insert(0, f"切分来源={cand.origin} 面积比={cand.area_ratio:.2f}")
        page.cards.append(card)

    if not page.cards:
        page.stage_notes.append("切分没有产出任何卡片候选")
    return page


def observations_for_card(
    result: CardResult,
) -> list[dict[str, Any]]:
    """把一张卡的结果摊平成可直接写库的 observation 行。

    这一层单独存在的理由：管线的其余部分**不依赖数据库**，
    所以可以在没有 PG 的环境里单测；而写库形状只有一个地方要维护。
    """
    if result.bundle is None:
        return []
    return observation_decimals(result.bundle)


def _vlm_lines_to_rows(text: str) -> list[tuple[str, float]]:
    """把 VLM 的自由文本转成 ``(文本, y)`` 行，供 :func:`parse_fields` 使用。

    VLM 不返回行坐标，所以 y 用**行序号**代替 —— 而 prompt 要求它按
    「顶弧/中部/底弧」三段回答，所以序号顺序恰好就是从上到下。
    这样"以日期行为基准分上下"那条判据仍然可用。

    置信度统一给 0.75（落在"需人工确认"区间）：**VLM 不返回字级置信度**，
    而我们也不该为它编一个高置信度 —— 那会让 confidence 那条防线失效。
    """
    import re

    rows: list[tuple[str, float]] = []
    for i, raw in enumerate(text.splitlines()):
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^(顶弧|中部|底弧|上弧|下弧)\s*[:：]\s*", "", s)
        if not s or s in ("无", "无邮戳", "看不清"):
            continue
        rows.append((s, float(i)))
    return rows


#: 送云端问"每张卡的四个角"时，图的**长边上限**。
#:
#: 与 `DEFAULT_WORK_LONG_SIDE` 分开是一个刻意的选择：那个数服务于**本地判据**
#: （毫米阈值、Hough 圆），调它是为了本地精度；这个数只服务于"模型能不能看清
#: 卡的边界"。实测（2026-10-13）在 2000 长的图上，`001.jpg` 的 9 张卡
#: 全被正确框出（第三列甚至给出画面外的 x=1.001），所以照用 2000。
#: 独立成一个常量是因为**将来要调它时不该连带改动本地那套**。
VLM_CORNERS_LONG_SIDE: Final = 2000

#: 问"每张卡的四个角"时给的补全预算。
#:
#: 实测（2026-10-13，`001.jpg`）这一问花了 **460 补全 tokens**（9 张卡 × 8 个数），
#: 而 `settings.vlm_max_tokens` 默认是 **512** —— 只差 50。而截断会返回
#: `finish_reason=length` 且 **content 为空**，于是被记成"调用失败"，
#: 表现为"云端兜底好像没用"，看不出是预算问题。所以这里显式给宽一点。
VLM_CORNERS_MAX_TOKENS: Final = 1600


#: 交叉校验的门槛：本地某个框与它**最像**的那个云端框，IoU 低于它就报出来。
#:
#: 取 0.2 而不是 `SplitParams.cloud_iou`（0.5）是有意的：0.5 是"判为同一张卡"，
#: 而这里问的是"两条通道**明显**不同意"。实测（2026-10-13，`001.jpg` #6）：
#: 本地那个斜正方形与云端同一张卡的 IoU 只有约 **0.35** —— 它低于 0.5
#: （所以是"不同意"），但高于 0.2（所以不会被当成"完全不同的两张卡"而误报）。
CROSSCHECK_MIN_IOU: Final = 0.2


def _crosscheck_note(local: SplitResult, merged: SplitResult) -> str:
    """A 模式（`cloud_first`）下的**交叉校验**：本地与云端在哪里明显不同意。

    本地检测在 A 里**不再决定几何**，但它仍然是一条**独立的证据**：
    本地是"按像素找亮区/边线"，云端是"按空间理解找矩形"。两者在某个位置
    给出的框 IoU 很低时，最可能的解释是**这一处真的有问题**
    （切歪了、两张粘连、或者那块根本不是卡）—— 而不是"其中一条总是对的"。

    所以这里只**报告**，不改任何几何：几何已经听云端的了。
    """
    if not local.candidates:
        return "交叉校验：本地一张都没切出，无从对照"
    cloud_quads = [
        np.asarray(c.quad, dtype=np.float32)
        for c in merged.candidates
        if c.geometry_source == "vlm"
    ]
    if not cloud_quads:
        return "交叉校验：这次没有采纳任何云端几何，无法对照"
    worst: list[str] = []
    for i, cand in enumerate(local.candidates):
        best = max(quad_iou(np.asarray(cand.quad, dtype=np.float32), q) for q in cloud_quads)
        if best < CROSSCHECK_MIN_IOU:
            worst.append(f"本地#{i}（最好 IoU {best:.2f}）")
    if not worst:
        return (
            f"交叉校验：本地 {len(local.candidates)} 张与云端几何的最差 IoU 仍 >= "
            f"{CROSSCHECK_MIN_IOU} —— 两条通道没有明显分歧"
        )
    return (
        f"⚠️ 交叉校验：{len(worst)} 个本地框与云端几何**明显不同意**"
        f"（{'、'.join(worst)}）；几何以云端为准，但这几处值得人看一眼"
    )


def _cloud_shape_suspects(split: SplitResult) -> list[int]:
    """**云端给的**那几张里，形状不像卡的是哪几张（返回下标）。

    ⚠️ 只用 ``CardCandidate.size_suspect`` **这一个**判据，不在这里另判长宽比。
    理由：`size_suspect` 就是切分器的"不像卡"口径（`card_shape_suspect` 这个
    **唯一实现**给出来的），它已经包含"长/宽比出界"那一层。再加一条更松的
    判据会让本来正常的云端答案被反复追问 —— 而"正常的卡"在这里恰恰是
    **允许有一点点不规整**的（真卡在扫描里就是会歪、会被裁）。

    只看 ``geometry_source == "vlm"`` 的候选：本地留下的那些本来就可能是
    被边缘裁掉的异形卡（N8：标注但**不丢弃**），拿它们去质疑云端是错的。
    """
    return [
        i for i, c in enumerate(split.candidates) if c.geometry_source == "vlm" and c.size_suspect
    ]


def _resize_for_vlm_corners(image: np.ndarray, long_side: int) -> tuple[np.ndarray, float]:
    """把整页缩到合理边长再送云端。返回 ``(图, 比例)``。

    ⚠️ **比例必须能被还原**：云端按比例给坐标，而比例是乘在**送出去的图**上的。
    这里返回的比例用于把"送图尺度"换算回"当前图尺度"，见 `ask_card_corners`。
    """
    import cv2

    h, w = image.shape[:2]
    if max(h, w) <= long_side:
        return image, 1.0
    ratio = long_side / max(h, w)
    return (
        cv2.resize(image, (int(w * ratio), int(h * ratio)), interpolation=cv2.INTER_AREA),
        ratio,
    )


def ask_card_corners(cfg: VlmConfig, page: np.ndarray, *, hint: str = "") -> CloudCorners:
    """问云端"这一页上每张卡的四个角"。**永不抛异常**（失败 = 空 `quads`）。

    ## 为什么送缩小图，而 `PROMPT_READ_CARD_JSON` 那条送原图

    两条通道的**任务不同**，所以最优输入尺寸也不同，这不是不一致：

    * 读字段（`PROMPT_READ_CARD_JSON`）：在小字上做**感知**，缩了就丢笔画，
      所以送原图、且实测厂商按"一张图 ≤384 tokens"计费**缩了也不省钱**。
    * 找卡的角点（本函数）：要的是**整页的空间布局**。实测送 **2000 长**
      的整页（`001.jpg` 是 5072x7014）它给出 9 个正确矩形，
      连画面外的角点都推得出来 —— 这是"看全局"的任务，不是"看小字"的。

    ## 比例只有一处换算

    送出去的图比例 `ratio` 在这里**立刻**折算回 `page` 尺度：
    ``CloudCorners.unit`` 保持 `fraction`（`vlm.CORNERS_UNIT` 的约定），
    而 `sent_size` 记的是送图尺寸 —— `split.cloud_corners_to_quads` 按
    **目标图**的宽高换算即可。也就是说 `ratio` 只用于**说明这是哪张图**，
    不参与数值换算（数值全在比例里）。

    ## 一次调用就是一次调用 —— **重试在调用方**

    这个函数只负责"问一次并解析"，不做重试：判断"这次答得够不够好"需要
    **合并后的结果**（要看形状、要看总数），而那在 `recognize_page` 手里。
    所以重试逻辑只有一处（`recognize_page` 里那段 `_retry`），
    这里保持成一个薄封装 —— 两处各写一半的重试一定会漂开。
    """
    from postmarktrip.recognition.vlm import (
        CORNERS_UNIT,
        ask,
        corners_prompt,
        parse_card_corners,
    )

    sent, ratio = _resize_for_vlm_corners(page, VLM_CORNERS_LONG_SIDE)
    sent_size = (sent.shape[1], sent.shape[0])
    ans = ask(cfg, sent, corners_prompt(hint=hint), max_tokens=VLM_CORNERS_MAX_TOKENS)
    if not ans.ok:
        return CloudCorners(
            ok=False, reason=ans.reason, latency_s=ans.latency_s, sent_size=sent_size
        )
    quads = parse_card_corners(ans.text)
    return CloudCorners(
        quads=quads,
        unit=CORNERS_UNIT,
        sent_size=sent_size,
        #: ⚠️ **`ok` 说的是"调用成功了"，不是"拿到卡了"。**
        #: 一个真的没有卡的空页会回 `[]` —— 那是成功且正确的答案，
        #: 记成失败会让"云端说这页没卡"与"云端没答上来"混在一起。
        ok=True,
        reason="" if quads else f"调用成功但没有可用的角点（补全比例 {ratio:.4f}）",
        latency_s=ans.latency_s,
    )


def _pad_for_vlm(image: np.ndarray, ratio: float = 0.08) -> np.ndarray:
    """给整卡加白边再送云端。

    与 ``ocr._prepare`` 同理（实测：区域被内容填满时模型会把笔画当成巨大块）：
    留白能显著改善识别。这里比例取小一些，因为整卡本身已有边距。
    """
    img = image
    if img.ndim == 2:
        img = np.stack([img] * 3, axis=-1)
    elif img.shape[2] == 4:
        img = img[:, :, :3]
    h, w = img.shape[:2]
    py, px = max(8, int(h * ratio)), max(8, int(w * ratio))
    canvas = np.full((h + 2 * py, w + 2 * px, 3), 255, np.uint8)
    canvas[py : py + h, px : px + w] = img
    return canvas


#: 「VLM 的回答」→「内部戳型值」。
#:
#: ⚠️ **两条通道的回答语言不同，这里必须都认**（2026-10-11 修，是踩出来的）：
#:
#:   * `PROMPT_CLASSIFY_TYPE`（判型通道）要**中文**：日戳 / 风景戳 / 无邮戳 / 看不清
#:   * `PROMPT_READ_CARD_JSON`（整卡 JSON 通道）要**英文枚举**：daily / scenic / …
#:
#: 而此前这里**只有中文分支** —— 于是形态②（整卡一次调用）里云端判的戳型
#: **从未生效**：`_vlm_kind("scenic")` 返回 `""`，`recognize_card` 里那段
#: "云端说 priority"的覆盖分支永不执行，最终戳型一直是**本地**判的。
#:
#: 症状极隐蔽：字段确实来自云端（`field_source="vlm"`）、看起来"云端在工作"，
#: 只有戳型没接上。**证据**：同一页跑"云端 vs 纯本地"两次，戳型 9/9
#: **连同置信度逐一相同**（详见 `docs/ok/14-B` §12.4#11、`docs/ok/15` §3.3）。
_KIND_BY_ANSWER: Final[dict[str, str]] = {
    #: 英文枚举（整卡 JSON 通道）—— 直接取自枚举，**不手写第二份**，免得漂移
    **{e.value: e.value for e in PostmarkType},
    #: 中文（判型通道；也用于模型整句回答时的关键词兜底）
    "日戳": PostmarkType.DAILY.value,
    "风景戳": PostmarkType.SCENIC.value,
    "风景": PostmarkType.SCENIC.value,
    "纪念戳": PostmarkType.COMMEMORATIVE.value,
    "宣传戳": PostmarkType.PROPAGANDA.value,
    "邮政副戳": PostmarkType.POSTAL_SUB.value,
    "邮资机戳": PostmarkType.METER.value,
    "营业戳": PostmarkType.BUSINESS.value,
    "代办所戳": PostmarkType.AGENCY.value,
    "临时邮政局戳": PostmarkType.TEMPORARY.value,
    "文化日戳": PostmarkType.CULTURAL.value,
    "文化戳": PostmarkType.CULTURAL.value,
}


def _vlm_kind(text: str) -> str:
    """把 VLM 的回复映射成内部的戳型常量。

    **只认它能确定映射的词/枚举值**；「无邮戳」「看不清」返回空串，
    表示"不该由这条通道给出戳型" —— 而不是硬塞成 daily。

    ⚠️ 兜底匹配按**长度降序**：「文化日戳」必须先于「日戳」命中 ——
    否则文化日戳会被静默判成日戳（顺序问题，不是阈值问题）。
    """
    s = (text or "").strip().lower()
    if not s:
        return ""
    #: 1) 整串就是一个答案 —— 两条通道的正常回答都落在这里
    if s in _KIND_BY_ANSWER:
        return _KIND_BY_ANSWER[s]
    #: 2) 整句回答（"这是一枚风景戳" / "the type is daily"）→ 关键词兜底
    for token in sorted(_KIND_BY_ANSWER, key=len, reverse=True):
        if token in s:
            return _KIND_BY_ANSWER[token]
    return ""


def circle_to_page(
    circle: Circle,
    quad: tuple[tuple[float, float], ...],
    card_size: tuple[int, int],
) -> tuple[float, float, float]:
    """把戳圆从**卡局部坐标**映射回**页面坐标**。

    ## 为什么需要这个函数

    ``classify_postmark`` 是在**正射图**（卡裁剪）上跑的，所以
    ``verdict.circle`` 的 cx/cy/r 都是**卡局部**的。而界面要把它画在
    **整页**上（这样"这个圆到底圈住了什么"才一眼可见 —— 它是最难从字段值
    看出来的失效模式）。

    直接把卡局部坐标画到页面上，结果是**所有卡的圆都堆在页面左上角** ——
    实测 7 张卡的圆全部重叠在卡 3 的位置。这个错**不报错**，只是看起来
    "圆检测很乱"，很容易被误判成检测器的问题。

    ## 做法

    正射用的是 ``getPerspectiveTransform(src=quad, dst=正交矩形)``，
    所以反向映射就是同一个变换的逆。半径用**同一个变换作用在
    (cx+r, cy) 上得到的距离**，而不是直接缩放 —— 透视下半径不是常数。
    """
    if len(quad) != 4:
        return circle.cx, circle.cy, circle.r
    # cv2 按需导入：识别是可选 extra，本模块刻意不做顶层 cv2 导入
    import cv2

    w, h = card_size
    src = np.asarray(quad, dtype=np.float32)
    dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
    inv = cv2.getPerspectiveTransform(dst, src)
    pt = np.array([[[circle.cx, circle.cy]]], dtype=np.float32)
    out = cv2.perspectiveTransform(pt, inv)[0][0]
    edge = np.array([[[circle.cx + circle.r, circle.cy]]], dtype=np.float32)
    out_edge = cv2.perspectiveTransform(edge, inv)[0][0]
    r = float(np.hypot(out_edge[0] - out[0], out_edge[1] - out[1]))
    return float(out[0]), float(out[1]), r
