"""云端 VLM 的**可插拔底座**（spec v1.0 §3.6 / V4 / D5）。

## 为什么单独一层（用户的明确要求）

用户要求这部分**模块化、可换底座、便于之后迁移**。所以这里把"调哪家云、
用哪个模型、怎么问"与"管线怎么用它"**分开**：

* 本模块只回答：**"把一张图交给云，拿回一段可核对的文字"**
* 管线（`pipeline.py`）回答：**"拿到这段文字以后算不算数、要不要人工确认"**

于是两种模式共享同一层：

| 模式 | VLM 的角色 | 谁调用 |
| --- | --- | --- |
| **A**（先做） | **助手**：本地管线跑完，疑难件再送云 | 服务器 |
| **B**（之后） | **唯一识别器**：APP 直连云，没有本地模型 | 客户端 |

**两者的差别只在"谁在什么时机调用它"，而不在调用本身** ——
所以只要这一层是干净的，迁到 B 不需要改它。

## Key 的来源与安全

* Key **不进日志、不进异常消息、不进返回值**。这一层只持有它、用它签请求。
* 支持"**provider 预设**"：选 ``deepseek`` 就自动填好 base_url 与默认模型，
  并从约定的环境变量取 Key。这样加一家新厂商 = **加一条预设**，
  而不是改调用方代码。
* 显式的 ``base_url`` / ``model`` / ``api_key`` **覆盖**预设 ——
  任何 OpenAI 兼容端点都能接，不必先被收进预设表。

## 实测过的能力（2026-10-08）

在 `清晰黑色日戳特写-1` 的戳区域上，``deepseek-flash`` 读出：

```
顶弧：福建南平          ← RapidOCR 给的是「福建南火」（错）
中部：2026.09.25.15     ← 对
底弧：建溪 -            ← 对
```

**注意**：vendor 公告说旧的 ``deepseek-v4-flash-vision-exp`` 已下线、
请求改由 ``deepseek-flash`` 承接 —— 但**实测它仍然接受图像输入且读得很准**。
所以模型名以厂商文档为准（``deepseek-flash``），而**能力以实测为准**。
这条正是 V4 要持续监控的东西。
"""

from __future__ import annotations

import base64
import json
import logging
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Final

import cv2
import numpy as np

logger = logging.getLogger("postmarktrip.recognition.vlm")

#: 写进 observation 的管线版本
VLM_PIPELINE_VERSION: Final = "p2-vlm-0.1.0"


@dataclass(frozen=True, slots=True)
class VlmProvider:
    """一家云厂商的接入信息。**加新厂商 = 加一条这个**。"""

    name: str
    base_url: str
    default_model: str
    #: 从这个环境变量取 Key（也可以显式传 api_key 覆盖）
    key_env: str
    #: 该厂商是否支持图像输入。**实测得出，不是照抄文档** ——
    #: 厂商下架视觉模型时文档与能力会不一致（本项目实测撞到过）。
    vision: bool = True
    note: str = ""


#: 预设表。OpenAI 兼容端点都可以按 ``custom`` 处理（显式给 base_url/model）。
PROVIDERS: Final[dict[str, VlmProvider]] = {
    "deepseek": VlmProvider(
        name="deepseek",
        base_url="https://api.deepseek.com",
        default_model="deepseek-flash",
        key_env="DEEPSEEK_API_KEY",
        note=(
            "实测（2026-10-08）deepseek-flash 支持图像输入，且在邮戳上读出了 "
            "RapidOCR 读错的「福建南平」。厂商文档说旧视觉模型已下线、"
            "由本模型承接 —— 但实测仍能看图，所以以实测为准。"
        ),
    ),
    "openai": VlmProvider(
        name="openai",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        key_env="OPENAI_API_KEY",
    ),
    "custom": VlmProvider(
        name="custom",
        base_url="",
        default_model="",
        key_env="VLM_API_KEY",
        note="任意 OpenAI 兼容端点。base_url 与 model 必须显式给。",
    ),
}


@dataclass(slots=True)
class VlmConfig:
    """一次调用所需的全部信息。**不含 Key 的明文副本以外的任何用途**。"""

    provider: str
    base_url: str
    model: str
    api_key: str
    timeout_s: float = 120.0
    max_tokens: int = 512
    #: **关掉推理（默认开）** —— 这不是性能调优，是正确性与成本的关键。
    #: 见 :func:`ask` 里那张实测对照表。
    disable_reasoning: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    def describe(self) -> str:
        """**可以安全打印**的自我描述（不含 Key）。"""
        return (
            f"provider={self.provider} model={self.model} "
            f"base_url={self.base_url} key=<len {len(self.api_key)}> "
            f"thinking={'off' if self.disable_reasoning else 'on'}"
        )


def resolve_config(
    *,
    provider: str = "",
    base_url: str = "",
    model: str = "",
    api_key: str = "",
    enabled: bool = False,
    timeout_s: float = 120.0,
    max_tokens: int = 512,
    disable_reasoning: bool = True,
) -> tuple[VlmConfig | None, str]:
    """从"预设 + 显式覆盖"解析出配置。返回 ``(配置, 不可用原因)``。

    解析顺序（**显式 > 预设 > 环境变量**）：

    1. ``provider`` 选中一条预设（空则由 base_url 反推，再退回 ``custom``）
    2. ``base_url`` / ``model`` 非空则覆盖预设
    3. ``api_key`` 非空则用它；否则按预设的 ``key_env`` 从环境变量取

    **为什么不在不可用时报异常**：与 OCR 层同理 —— "没配云"是一个
    **正常状态**（D5：默认全本地），管线该继续跑本地那条路。
    """
    if not enabled:
        return None, "VLM_ENABLED=false（D5：默认全本地，疑难件必须手动送云端）"

    pname = (provider or "").strip().lower()
    if pname not in PROVIDERS:
        # 没给 provider 就按 base_url 猜；猜不到用 custom
        pname = "custom"

    preset = PROVIDERS[pname]
    resolved_url = (base_url or preset.base_url).strip().rstrip("/")
    resolved_model = (model or preset.default_model).strip()

    resolved_key = api_key.strip()
    if not resolved_key:
        import os

        resolved_key = (os.environ.get(preset.key_env) or "").strip()

    cfg = VlmConfig(
        provider=pname,
        base_url=resolved_url,
        model=resolved_model,
        api_key=resolved_key,
        timeout_s=timeout_s,
        max_tokens=max_tokens,
        disable_reasoning=disable_reasoning,
    )
    if not cfg.base_url:
        return None, f"provider={pname} 没有 base_url，且未显式提供"
    if not cfg.model:
        return None, f"provider={pname} 没有 model，且未显式提供"
    if not cfg.api_key:
        return None, (
            f"未找到 Key：预设 {pname} 约定的环境变量是 {preset.key_env}，也可显式传 VLM_API_KEY"
        )
    return cfg, ""


# --------------------------------------------------------------------------- #
#  提问（prompt 做成模块级常量，便于审阅与调优）
# --------------------------------------------------------------------------- #

#: **只读可见文字、不许推测** —— 这是 V4 的核心。
#:
#: 为什么把"看不清就说看不清"写进 prompt：V4 要量的正是**编造率**。
#: 如果 prompt 鼓励模型"尽力猜"，那测出来的高错误率分不清是模型的问题
#: 还是 prompt 的问题。**先给它一条不会被迫猜的路**，
#: 它仍然编造，才是模型的问题。
PROMPT_READ_POSTMARK: Final = (
    "这是一枚中国邮戳的局部图。请**只读出图上真实可见的文字**，"
    "按位置分三段回答：\n"
    "顶弧：\n中部：\n底弧：\n"
    "规则：\n"
    "1. **不要推测、不要补齐**。看不清的字写 `?`。\n"
    "2. 如果某一段根本没有文字，写「无」。\n"
    "3. 如果整张图根本没有邮戳，只回复「无邮戳」。\n"
    "4. 不要解释，不要输出多余内容。"
)

#: 用于"这上面有没有邮戳"的二值提问 —— 测幻觉用。
#: 拿"只有邮票、没有戳"的卡问它，诚实的回答是「无邮戳」。
PROMPT_HAS_POSTMARK: Final = (
    "这张图是一张邮戳卡（或卡片）。请只回答一个问题：**图上有没有盖邮戳？**\n"
    "只回复以下三者之一，不要解释：\n"
    "有邮戳\n"
    "无邮戳\n"
    "看不清"
)


#: 戳型判别的提问。**与 `PROMPT_READ_POSTMARK` 同样刻意保守**：
#:
#: * 明确给出判据（顶弧有无地名文字），而不是让模型凭印象
#: * **主动告诉它卡片抬头不是邮戳** —— 实测本地 OCR 会把
#:   「中国邮政 CHINA POST 邮戳卡」当成邮戳内容，所以这一条必须写
#: * 允许回答「无邮戳」，因为样本里确实有只贴邮票的卡（用户告知）
PROMPT_CLASSIFY_TYPE: Final = (
    "这是一张邮戳卡。请判断卡上那枚**邮戳**的戳型，只回复一个词：\n"
    "日戳 / 风景戳 / 无邮戳 / 看不清\n"
    "判据（务必按此，不要凭印象）：\n"
    "- 日戳：**顶弧（上半圆）有省市地名文字**，中部是日期数字，底弧是局名+戳号。\n"
    "- 风景戳：**顶弧是空的**（没有地名文字），中部或侧面是图案/线描/书法名称。\n"
    "注意：卡上印刷的「中国邮政 CHINA POST 邮戳卡」是卡片抬头，**不是邮戳**。\n"
    "只回复那一个词。"
)

#: **整卡一次读全部字段**（戳型 + 日期 + 地名 + 局名 + 戳号），要求 JSON。
#:
#: ## 为什么要这一条（2026-10-11 实测，见 ok/14 §十一）
#:
#: 原来云端形态是**每卡 4 次调用**：判型 1 次 + date/place/office 各 1 次。
#: 实测这一条整卡调用在 7 张真值样本上：
#:
#: | 项 | 结果 |
#: | --- | --- |
#: | 日期 | **7/7** |
#: | 地名 | **6/6** |
#: | 局名 | 4/5（唯一错：`八一一路` 读成 `八一路`） |
#: | 戳型 | 6/7（唯一错：把一张 5072² 判成 daily） |
#: | 延迟 | **约 1.0~1.3 秒/张** |
#:
#: 也就是说：**1 次调用比 4 次更准**，而且比本地那套（判型 4/9、
#: 地名 0/6）准得多。
#:
#: ## 三条刻意的设计
#:
#: 1. **明确要求"只读可见文字、不要推测"** —— 与 `PROMPT_READ_POSTMARK` 同一条
#:    原则。模型在盖得差的戳上倾向于"补齐"成合理的地名，而那是错误的主要来源。
#: 2. **主动说明卡片抬头不是邮戳** —— 实测本地 OCR 会把
#:    「中国邮政 CHINA POST 邮戳卡」当内容读进来。
#: 3. **允许空串** —— 读不到就填空，**不要编**。空值会进校对队列（正确行为），
#:    编出来的值会被当成真的（有害）。
#:
#: ⚠️ **不要这一条里问位置/坐标。** 实测整卡调用给出的边界框不可靠：
#: `w/短边` 应当≈0.46，实测有 0.031（框住了邮票）、0.204、0.214 的。
#: 7/7 只是"坐标落在图内"，不代表框对了。管线的 `dpi` 先验完全建立在
#: "精确圆"上，而它给不出 —— 所以经裁定**不救位置这条**（用户 2026-10-11：
#: "改。不救了"）。
PROMPT_READ_CARD_JSON: Final = (
    "这是一张邮戳卡。请读出**卡上那枚邮戳**的信息，只输出一个 JSON 对象，"
    "不要任何解释、不要 markdown 代码块：\n"
    "{\n"
    '  "type": "daily（或下面列表里的其他取值）",\n'
    '  "date": "YYYY-MM-DD",\n'
    '  "date_text": "戳面上日期的原文，如 2026.10.01.09（读不到就空串）",\n'
    '  "time_code": "日期末尾的时码数字，如 09（没有就空串）",\n'
    '  "place": "戳面上的地名（省/市/县名）",\n'
    '  "office": "戳面上的局名（不要末尾的戳号数字）",\n'
    '  "office_no": "局名后面的戳号数字，没有就空串",\n'
    '  "inscription": "戳面原文，见下面的取值规则（只抄可见文字，不要推测）"\n'
    "}\n"
    "戳种取值（只选一个）：\n"
    "- daily（日戳）：**顶弧（上半圆）有省市地名文字**，中部是日期数字，底弧是局名+戳号。\n"
    "- scenic（风景戳）：**顶弧可能是空的**，中部或侧面是图案/线描/书法名称。\n"
    "- cultural（文化日戳）：形如日戳（有日期与局名），但底部或中部另有**一串景点/"
    "文物/文化名称**（如「三清山三龙出海」「文成塔」）—— 这串字**不是**局名。\n"
    "- commemorative（纪念戳）/ propaganda（宣传戳）/ postal_sub（邮政副戳）/"
    "meter（邮资机戳）/ business（营业戳）/ agency（代办所戳）/"
    "temporary（临时邮政局戳）/ other（其他）。\n"
    "⚠️ **先判断 daily 与 scenic 这两种最常见的**；只有当这枚戳明显不符合"
    "上面两条判据时，才用后面的取值。拿不准就用 daily 或 scenic。\n"
    "`inscription`（戳面原文）按戳型取：\n"
    "- daily（日戳）：**局名 + 戳号**，如「建溪1」「中亭街1」「杨真堂1」。\n"
    "- cultural（文化日戳）：**下方弧的那串戳名**，如「三清山三龙出海」「文成塔」。\n"
    "- scenic（风景戳）：**图案里的文字**，如「春风得意马蹄疾」「巨蟒出山东方女神」。\n"
    "- 只要**戳面上真实可见的文字**：不要把日期、地名、卡片抬头写进去；\n"
    "  一串原文里带局名与戳号就照抄（如「四号航站楼2」）。\n"
    "- 卡上印刷的「中国邮政 CHINA POST 邮戳卡」是卡片抬头，**不是邮戳内容**。\n"
    "- **只读真实可见的文字，不要推测、不要补齐**。看不清就用空串。\n"
    "- 读不到的项填空串，**不要编**。\n"
    "\n"
    "⚠️ 两条实测踩过的坑，请特别注意：\n"
    "1. **线描/图案式的邮戳也是邮戳。** 有些邮戳的戳面是一幅风景线描图"
    "（山水、建筑等），它**仍然是盖上去的邮戳**，不是卡片上的印刷内容。"
    "不要因为「看起来像印刷的图案」就说卡上没有邮戳 —— 实测有过这种误判，"
    "把整枚戳的信息全丢成了空串。\n"
    "   * 判据：**卡片抬头**（中国邮政/CHINA POST/邮戳卡 这类字样加上绿色标志）"
    "是印刷的；而**圆形的、有线描或文字的印记**是邮戳。\n"
    "   * 邮票（齿孔边缘、有面值如「10分」、有「保护森林」这类主题字）**不是邮戳**。\n"
    "2. **`office` 要的是局名，不是戳面上的景物题字。** 风景戳常写"
    "「局名 + 景点名」（如「三清山三龙出海」）；其中**局名**是"
    "与邮政机构名对应的那部分（如「三清山」「三清乡」），而景点名"
    "（如「三龙出海」「一线天」）**不要**放进 `office`。\n"
    "   * 若戳面有一行很长的题字（如某纪念碑/景区的全名），它通常**不是**局名。\n"
    "   * 拿不准时 `office` 填空串 —— **不要用题字替代**。"
)

#: 从整卡 JSON 回复里取出的字段。**解析与容错都在 `parse_card_json` 里**。
#:
#: ⚠️ 这份名单**必须与 prompt 里那份 JSON 模板逐项对应**。2026-10-11 加
#: `date_text` / `time_code` / `inscription` 时就漏了这一处：模型明明回了
#: `"inscription": "建溪1"`（`云端回复原文` 里看得到），却被这里**静默丢掉**，
#: 于是戳面原文是空的、`date_text` 退回了规范值 —— 端到端跑一次才发现。
#: 加字段时**两处一起改**（这里 + `PROMPT_READ_CARD_JSON`）。
_CARD_JSON_FIELDS: Final[tuple[str, ...]] = (
    "type",
    "date",
    "date_text",
    "time_code",
    "place",
    "office",
    "office_no",
    "inscription",
)


def parse_card_json(text: str) -> dict[str, str]:
    """把 `PROMPT_READ_CARD_JSON` 的回复解析成 ``{字段: 字符串}``。

    容错要宽 —— 模型会以各种方式包装 JSON（markdown 代码块、前后加解释、
    单引号、末尾多余逗号）。**但只在真的解析不出来时才放弃**，不要因为
    格式瑕疵就丢掉整份结果（那份结果值一次 API 调用）。

    返回的字典**只包含实际给出的键**；调用方据此判断"它到底答了什么"。
    缺失的键不出现在结果里 —— 与 `ParsedFields` 的约定一致
    （"缺的字段不出现，而不是给空串"）。
    """
    import re as _re

    if not text:
        return {}
    raw = text.strip()
    #: 去掉 markdown 代码块围栏
    fence = _re.search(r"```(?:json)?\s*(.*?)```", raw, _re.S)
    if fence:
        raw = fence.group(1).strip()
    #: 取最外层的一对花括号
    m = _re.search(r"\{.*\}", raw, _re.S)
    if not m:
        return {}
    body = m.group(0)

    data: object
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        #: 常见瑕疵：单引号、尾随逗号、True/False 大小写
        fixed = body.replace("'", '"')
        fixed = _re.sub(r",\s*([}\]])", r"\1", fixed)
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError:
            return {}
    if not isinstance(data, dict):
        return {}

    out: dict[str, str] = {}
    for key in _CARD_JSON_FIELDS:
        val = data.get(key)
        if val is None:
            continue
        text_val = str(val).strip()
        #: 模型有时把"没有"写成这些词，一律当空
        if text_val in ("null", "None", "无", "N/A", "n/a", "-", "?"):
            text_val = ""
        out[key] = text_val
    return out


#: **整页一次问"每张卡的四个角"**（B 方案：本地几何可疑时的云端兜底）。
#:
#: ## 为什么坐标用**比例**而不是像素
#:
#: 同一份回答要能被"送云端的那张缩略图"与"原图"两种尺度共用。写像素就必须
#: 先约定尺度，而模型对"这是多少像素"没有稳定概念 —— 实测它更擅长估比例。
#: 所以这里把约定写成 **0~1 的比例**，`vlm.CORNERS_UNIT` 记录这个约定，
#: 调用方（`split.cloud_corners_to_quads`）按它换算。
#:
#: ## 三条刻意的设计（与 `PROMPT_READ_CARD_JSON` 同一套原则）
#:
#: 1. **只给看得见的，不要猜** —— V4 的核心。实测 001.jpg 上它老老实实回了
#:    9 张卡的九个矩形（3×3），而不是编一堆。
#: 2. **越出画面的角点照样给**：扫描时卡被画面边缘切掉是常态（实测
#:   `更多样本/001.jpg` 三行卡的左右两侧都被切），而**本地漏切的正是这一行**
#:   （底行三张被 Otsu 粘成一个 2.05×卡的大连通域）。让模型按"它本该是矩形"
#:   把画面外的角点推出来，才能把这行修回来；只给看得见的部分等于把
#:   同一个残缺形状又交回来一次。
#:    ⚠️ 这一条是**明确允许推断**的例外，所以必须在 prompt 里写清"哪部分可以推断"：
#:   只有"被画面边缘切掉的边"可以外推，看不见的卡不许凭空造。
#: 3. **顺序固定**（左上→右上→右下→左下）—— 透视变换对角的顺序极其敏感，
#:   顺序错了会得到一张镜像/扭转的图，而它"看起来"仍是一张卡，不会立刻暴露。
#:   即便如此，调用方仍会用 `split.order_quad` 再排一次（**不信任云端顺序**）。
PROMPT_READ_CARD_CORNERS: Final = (
    "这是一张扫描件，上面平放着若干张**矩形的邮戳卡**（浅色卡片，置于深色垫纸上）。\n"
    "请找出图上**每一张卡**，给出每张卡的四个角点。\n"
    "\n"
    "坐标规则：以**你看到的这张图**为准，按**比例**给出：左上角是 (0,0)，"
    "右下角是 (1,1)；x 向右增大，y 向下增大。\n"
    "\n"
    "规则：\n"
    "1. **只给图上真实存在的卡，不要凭空造**。看不清就不给。\n"
    "2. 卡片被画面边缘切掉时**照样要给**：按「它本该是个矩形」把落在画面外的"
    "那个角点推出来，比例值可以是负数或大于 1。\n"
    "3. 每张卡的四个角点顺序固定：左上 → 右上 → 右下 → 左下。\n"
    "4. 不要解释、不要 markdown 代码块，只输出一个 JSON 数组。\n"
    "5. 一张卡都没有时输出 []。\n"
    "\n"
    "输出格式：\n"
    '[{"card": 1, "corners": [[x,y],[x,y],[x,y],[x,y]]}, ...]'
)

#: `PROMPT_READ_CARD_CORNERS` 的坐标单位：**比例**（0~1，相对送进去的那张图）。
#: 与提示词里的约定必须一致 —— 不一致的表现是"角点整体缩了一档"，
#: 看起来像"模型给歪了"，而不是像单位错误。
CORNERS_UNIT: Final = "fraction"


def corners_prompt(*, expected: int = 0, missed: int = 0, hint: str = "") -> str:
    """`PROMPT_READ_CARD_CORNERS` + 可选的一段**追问**。

    追问只在"上一次答得不对"时加（调用方决定），因为它是**唯一被允许的引导**。
    两种被实测证明需要的追问：

    * **答少了**（`expected` / `missed`）：实测（2026-10-13）`001.jpg` 同图同
      提示两次分别回了 **9 条与 8 条** —— 模型自己会漏一张。
      说的是"我们数着还差 N 张"，而不是"补一张出来"。
    * **答歪了**（`hint`，见 :data:`CORNERS_SHAPE_HINT`）：同一天另一次运行里
      `005.jpg` 的**底行三张**被框成 391×547（同页另两行是 604/656 高），
      宽高比 0.71 → 形状不对。那次张数是对的、只是框歪，所以要另一种追问。

    两种追问都**不放松"只给真实存在的卡"** —— 那是 V4 的核心，
    不许为了凑数把它让掉。
    """
    parts: list[str] = []
    if expected > 0 and missed > 0:
        parts.append(
            f"这张图上**共有 {expected} 张卡**，而你上面只给出了 {expected - missed} 张，"
            f"还差 {missed} 张。请仔细再看一遍（尤其是画面边缘、被切掉一半的、"
            "以及与垫纸颜色接近的），把**漏掉的那几张**补上。"
        )
    if hint:
        parts.append(hint)
    if not parts:
        return PROMPT_READ_CARD_CORNERS
    return (
        PROMPT_READ_CARD_CORNERS
        + "\n\n补充："
        + "".join(parts)
        + "仍然只给真实存在的卡，不要编。输出格式与要求同上，只输出那个 JSON 数组。"
    )


#: **形状追问**：张数对、但框出来的形状不像卡时用。
#:
#: 它把判据说出来（"同一页的卡尺寸应当彼此接近、都是竖版矩形"），因为实测那次
#: 错的正是**一行比另两行矮了约 10%** —— 那是"行内不一致"，
#: 而不是"某种卡的正常差异"。
CORNERS_SHAPE_HINT: Final = (
    "上一次给出的四边形里，有几张的形状**不像一张竖版矩形卡**"
    "（同一页的卡尺寸应当彼此接近，都接近 54×90mm 的竖版比例）。"
    "请重新仔细量一遍**每张卡**的四个角，尤其是被画面边缘切到的那几张，"
    "按它**本来的矩形**把画面外的角点推出来。"
)


def _numbers_in(node: object) -> list[float] | None:
    """``[x, y]`` → ``[float, float]``；形状不对返回 ``None``。"""
    if not isinstance(node, list) or len(node) != 2:
        return None
    out: list[float] = []
    for v in node:
        if isinstance(v, bool):
            return None
        if isinstance(v, int | float):
            out.append(float(v))
        elif isinstance(v, str):
            try:
                out.append(float(v.strip()))
            except ValueError:
                return None
        else:
            return None
    return out


def _corners_of(entry: object) -> list[float] | None:
    """从一条记录里取出**四个角点**，摊平成 8 个数。

    认多种形状（模型写法飘忽，而这些都不是"解析失败"）：
      * ``{"corners": [[x,y] x4]}``   ← prompt 要的形状
      * ``{"points": ...}`` / ``{"quad": ...}`` / ``{"box": ...}``
      * 直接就是 ``[[x,y] x4]``
      * 摊平的一维 ``[x1,y1,x2,y2,x3,y3,x4,y4]``
    """
    if isinstance(entry, dict):
        for key in ("corners", "points", "quad", "box", "bbox"):
            if key in entry:
                return _corners_of(entry[key])
        return None
    if not isinstance(entry, list):
        return None
    if len(entry) == 4 and all(isinstance(p, list) for p in entry):
        flat: list[float] = []
        for p in entry:
            xy = _numbers_in(p)
            if xy is None:
                return None
            flat.extend(xy)
        return flat
    if len(entry) == 8 and all(isinstance(v, int | float) for v in entry):
        return [float(v) for v in entry]
    return None


def parse_card_corners(text: str) -> list[tuple[tuple[float, float], ...]]:
    """把 `PROMPT_READ_CARD_CORNERS` 的回复解析成**每个卡一份四角点**。

    返回 ``[((x,y) x4), ...]``，**顺序与云端给的顺序一致**（是否可靠由调用方
    用 ``split.order_quad`` 兜一次）。坐标**未做任何换算** —— 单位见
    :data:`CORNERS_UNIT`，换算是调用方的事（这样本函数是纯的、可单测的）。

    ## 为什么容错要宽、但**不替它编**

    与 `parse_card_json` 同一个取舍：一份回答值一次 API 调用，格式瑕疵不该
    让整份作废（容错）。但**一个角点都不能瞎补**：四角缺一，这张卡就丢掉 ——
    宁可少一张卡，也不要一张"三个真角 + 一个补出来的角"的透视变换
    （它产出的图看起来是张卡，实际是错的，比漏掉有害）。

    ## ⚠️ 白名单的教训（`docs/ok/16` §11.3）

    `parse_card_json` 曾经因为白名单漏了 `inscription` 而**静默丢弃**云端
    读到的值。所以这里刻意**不按 key 白名单取数**，而是"**先看形状、再认
    key**"（见 `_corners_of`）：key 名字飘忽是常态，而"四个二元组"这个形状
    是稳定的。形状不对就当没有，**且不抛异常**。
    """
    import re as _re

    if not text:
        return []
    raw = text.strip()

    #: markdown 围栏
    fence = _re.search(r"```(?:json)?\s*(.*?)```", raw, _re.S)
    if fence:
        raw = fence.group(1).strip()
    #: 取最外层的一对方括号
    m = _re.search(r"\[.*\]", raw, _re.S)
    if not m:
        return []
    body = m.group(0)

    data: object
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        fixed = body.replace("'", '"')
        fixed = _re.sub(r",\s*([}\]])", r"\1", fixed)
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []

    out: list[tuple[tuple[float, float], ...]] = []
    for entry in data:
        flat = _corners_of(entry)
        if flat is None:
            continue
        #: 非有限值（NaN/inf）会让后面的 IoU 与裁剪产生**静默的乱结果**，
        #: 所以在这里就挡掉 —— 它不是"解析失败"，是"这个值不能用"。
        if not all(math.isfinite(v) for v in flat):
            continue
        out.append(
            (
                (flat[0], flat[1]),
                (flat[2], flat[3]),
                (flat[4], flat[5]),
                (flat[6], flat[7]),
            )
        )
    return out


@dataclass(slots=True)
class VlmAnswer:
    """一次云调用的结果。"""

    text: str
    model: str
    provider: str
    #: 耗时（秒）
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 是否成功。False 时 ``text`` 为空而 ``reason`` 说明原因。
    ok: bool = True
    reason: str = ""
    #: 原始返回（排障用，**不含 Key**）
    raw: dict[str, object] = field(default_factory=dict)


def _data_url(image: np.ndarray, *, quality: int = 92) -> str:
    """把图编码成 ``data:image/jpeg;base64,...``。

    **不缩放**：实测厂商按"一张图 ≤384 tokens"计费，所以缩放不省钱，
    而缩了会丢掉我们要认的小字。所以这里只编码，不改尺寸。
    """
    img = image
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        img = img[:, :, :3]
    ok, buf = cv2.imencode(".jpg", np.ascontiguousarray(img), [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:  # pragma: no cover - 编码失败极罕见，但不静默
        raise RuntimeError("JPEG 编码失败")
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def ask(
    cfg: VlmConfig,
    image: np.ndarray,
    prompt: str = PROMPT_READ_POSTMARK,
    *,
    max_tokens: int | None = None,
) -> VlmAnswer:
    """把一张图与一段提问发给云，取回文字。**永不抛异常。**

    与 OCR 层同一个取舍：网络/额度/认证失败都是**预期内的运行状态**，
    管线该记为"这一次没拿到"并继续，而不是整条任务失败。

    ``max_tokens`` 覆盖 ``cfg.max_tokens``（None = 用配置值）。**只在这一层加**，
    而不是给每个调用方各配一份 `VlmConfig` —— 调用链上只有一次 HTTP 构造，
    多一份配置就多一处会漂移的地方。

    ⚠️ 为什么需要它：``cfg.max_tokens`` 默认 512，对"整卡读几个字段"够用，
    但 **B 方案的整页角点（一页 9 张卡 × 8 个数）会被截断** → `finish=length`
    → 按下面的规则算**调用失败**，于是兜底永远不生效，而症状只是"云端没帮上忙"。
    实测 001.jpg 的角点回答是 **460 补全 tokens**，离 512 只差 50。
    """
    if not cfg.configured:
        return VlmAnswer(
            text="",
            model=cfg.model,
            provider=cfg.provider,
            ok=False,
            reason="配置不完整：" + cfg.describe(),
        )

    #: 本次调用实际使用的预算（`max_tokens` 参数 > 配置值）。
    #: 下面"被截断"那条错误消息里必须报**这个**数 —— 报配置值会在
    #: "本次传了更大的预算"时给出一个与事实不符的数字，而那正是排障时要看的。
    effective_max_tokens = max_tokens if max_tokens is not None else cfg.max_tokens

    payload = {
        "model": cfg.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _data_url(image)}},
                ],
            }
        ],
        "max_tokens": effective_max_tokens,
    }
    # ⚠️ **关掉推理**。实测（2026-10-08，deepseek-flash）同一张图：
    #
    # | 设置 | finish | 耗时 | 补全 tokens | 推理字数 | content |
    # | --- | --- | --- | --- | --- | --- |
    # | 不加控制 | length | 5.27s | 900 | 1313 | **空** |
    # | ``thinking: disabled`` | stop | **0.68s** | **24** | 0 | 正确 |
    #
    # 不关推理时它**把预算烧在思考上、最后交不出答案**
    # （``finish_reason='length'`` + 空 content）。而读字是**感知任务**，
    # 推理不带来精度收益，只带来延迟、费用与这个失败模式。
    #
    # 关掉后：**快 8 倍、补全 tokens 省 37 倍、失败消失**。
    # 所以这个参数与 OCR 层的 ``enable_mkldnn=False`` 同类 ——
    # **看起来像调优，实际是必需的**。
    if cfg.disable_reasoning:
        payload["thinking"] = {"type": "disabled"}
    req = urllib.request.Request(
        cfg.base_url + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            # Key 只在这里出现一次，且不进日志
            "Authorization": f"Bearer {cfg.api_key}",
        },
    )

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_s) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = "(读不到响应体)"
        logger.warning("VLM HTTP %s（provider=%s）", exc.code, cfg.provider)
        return VlmAnswer(
            text="",
            model=cfg.model,
            provider=cfg.provider,
            ok=False,
            reason=f"HTTP {exc.code}: {body}",
            latency_s=time.perf_counter() - t0,
        )
    except Exception as exc:
        logger.warning("VLM 调用失败（provider=%s）：%s", cfg.provider, type(exc).__name__)
        return VlmAnswer(
            text="",
            model=cfg.model,
            provider=cfg.provider,
            ok=False,
            reason=f"{type(exc).__name__}: {exc}",
            latency_s=time.perf_counter() - t0,
        )

    latency = time.perf_counter() - t0
    try:
        choice = data["choices"][0]
        text = (choice["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        return VlmAnswer(
            text="",
            model=cfg.model,
            provider=cfg.provider,
            ok=False,
            reason=f"返回结构异常：{type(exc).__name__}",
            latency_s=latency,
            raw=data if isinstance(data, dict) else {},
        )

    usage = data.get("usage") or {}
    finish = str(choice.get("finish_reason") or "")
    model_name = str(data.get("model") or cfg.model)
    ptok = int(usage.get("prompt_tokens") or 0)
    ctok = int(usage.get("completion_tokens") or 0)
    raw = data if isinstance(data, dict) else {}

    # ⚠️ **被预算截断必须算失败**，不能当成"图上没有文字"。
    #
    # 实测（2026-10-08）：`max_tokens` 偏小时返回 `finish_reason='length'`
    # 且 **content 为空**。若这里返回 ok=True/text=""，调用方会得出
    # 「这枚戳上没有文字」这个**静默的错误结论** —— 而真相是
    # 「我们根本没拿到答案」。两者在统计上必须分开：
    # 一个是识别质量，一个是调用失败。
    if finish == "length":
        return VlmAnswer(
            text="",
            model=model_name,
            provider=cfg.provider,
            ok=False,
            reason=(
                f"被 max_tokens={effective_max_tokens} 截断（finish_reason=length，"
                f"补全 {ctok} tokens）—— 这不是「没有文字」，是本次调用失败"
            ),
            latency_s=latency,
            prompt_tokens=ptok,
            completion_tokens=ctok,
            raw=raw,
        )

    # 空 content 对我们的 prompt **永远不是合法答案**：
    # prompt 明确要求"某段无文字写「无」"、"整图无邮戳写「无邮戳」"，
    # 所以任何情况下都应有非空文本。空 = 出问题了。
    if not text:
        return VlmAnswer(
            text="",
            model=model_name,
            provider=cfg.provider,
            ok=False,
            reason=f"返回了空 content（finish_reason={finish!r}）—— 视为调用失败",
            latency_s=latency,
            prompt_tokens=ptok,
            completion_tokens=ctok,
            raw=raw,
        )

    return VlmAnswer(
        text=text,
        model=model_name,
        provider=cfg.provider,
        latency_s=latency,
        prompt_tokens=ptok,
        completion_tokens=ctok,
        raw=raw,
    )


__all__ = [
    "CORNERS_SHAPE_HINT",
    "CORNERS_UNIT",
    "PROMPT_CLASSIFY_TYPE",
    "PROMPT_HAS_POSTMARK",
    "PROMPT_READ_CARD_CORNERS",
    "PROMPT_READ_CARD_JSON",
    "PROMPT_READ_POSTMARK",
    "PROVIDERS",
    "VLM_PIPELINE_VERSION",
    "VlmAnswer",
    "VlmConfig",
    "VlmProvider",
    "ask",
    "corners_prompt",
    "parse_card_corners",
    "parse_card_json",
    "resolve_config",
]
