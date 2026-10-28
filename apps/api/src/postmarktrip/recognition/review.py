"""置信度模型与校对队列（spec v1.0 §3.7）。

## 三层数据的分工（spec §3.7 的 L1/L2/L3）

| 层 | 含义 | 表 |
| --- | --- | --- |
| L1 | 原图（不可变，内容寻址） | `asset` |
| **L2** | **机器的每次"看法"**，同一张图可有 N 组，互不覆盖 | `observation` |
| **L3** | **人工确认过的值** | `correction`（"一等公民"，是标注资产） |

本模块负责：**把 `fields.ParsedFields` 变成 observation 草稿 + 决定哪些要进校对队列**。

## 为什么决策要**逐字段**做，不能整块一个分数

实测：同一枚戳上「日期」读到 0.96~0.99、「地名」读到 0.71~0.94。
如果取平均（0.85 左右）就分不出"日期可以直接采纳"与"地名必须人工看"。
而校对的人工成本是按字段计的 —— 让用户去确认一个已经 0.96 的日期是浪费。

## 阈值为什么**每个字段不同**

| 字段 | 阈值逻辑 |
| --- | --- |
| `date` | **低阈值即可自动采纳**（0.55）—— 它的格式由正则 + 合法性校验
（月 1~12、日 1~31）双重把关，错的可能性远小于自由文本 |
| `place` / `office` | **高阈值**（0.85 / 0.80）—— 它们是自由文本，
没有任何机械校验能发现「福建南火」是错的 |

## 一条重要的负面结论（决定了阈值不能定低）

实测地名/局名的 OCR 混淆**都是 1~2 个字**（`南火`/`南平`、`西玉山`/`江西玉山`、
`乔山`/`茶山`），而置信度**并不低**（0.71~0.94）。

所以:**置信度无法可靠地识别这类错误。** 真正能识别它们的是
**"这个地名在不在已知地名词表里"** —— 所以本模块把"未登记的地名"
一律送校对队列（``needs_review``），即使置信度很高。

**这与"高阈值"是两条独立的防线**，都要有：

1. 置信度低 → 送校对
2. 置信度**高但值未登记** → **也送校对**（防"自信的错值"）

第 2 条是这轮真正的收获：如果只看置信度，「福建南火」(0.90) 会被自动采纳。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal

from postmarktrip.recognition.fields import Field, ParsedFields
from postmarktrip.recognition.postmark_type import TYPE_DAILY, TYPE_SCENIC

logger = logging.getLogger("postmarktrip.recognition.review")

#: 写进每条 observation 的管线版本。**必须写** —— 否则模型升级后
#: 无法区分"哪些是旧管线跑出来的"，一键重跑就无从下手。
PIPELINE_VERSION: Final = "p2-0.1.0"

#: 决策档
Decision = Literal["auto_accept", "needs_review", "reject"]

#: **有机械校验的字段**：它们的值能被规则本身判真假，所以不需要词表兜底。
#:
#: `date` 的合法性由正则 + 月/日范围双重把关 —— 一个 `2026-13-45` 根本进不来。
#: 而 `place`/`office` 是自由文本，没有任何规则能发现「福建南火」是错的，
#: 所以只有它们需要词表。
#:
#: 这个集合存在的原因是防一个**隐患**：`decide()` 是通用函数，如果调用方
#: 误把 `known_values` 传给日期，日期会被当成"未登记的地名"而一律送校对 ——
#: 白白浪费用户的时间。显式列出来比靠调用方自觉可靠。
MECHANICALLY_VALIDATED: Final[frozenset[str]] = frozenset({"date", "office_no"})

#: **字段级**阈值（下限 / 上限）。
#: 低于 lower 直接 reject（当没读到），落在 [lower, upper) 送校对，
#: 达到 upper 才考虑自动采纳。
FIELD_THRESHOLDS: Final[dict[str, tuple[float, float]]] = {
    # 日期：格式被正则 + 合法性双重把关，所以低阈值即可
    "date": (0.40, 0.55),
    # 地名/局名：自由文本，没有机械校验能发现「福建南火」是错的
    "place": (0.35, 0.92),
    "office": (0.35, 0.85),
    "office_no": (0.30, 0.70),
    "stamp_region": (0.20, 0.60),
    "postmark_type": (0.30, 0.70),
    # 戳面原文（`docs/ok/16` §四）：自由文本，但**没有"已登记"这个概念**
    # —— 「建溪1」「三清山三龙出海」「春风得意马蹄疾」都是合法原文。
    # 所以它只吃阈值，绝不走词表（vocab/known 一律传 None）。
    "inscription": (0.35, 0.85),
    # 日期原文与时码：与 `date` 同源，随它一起判
    "date_text": (0.40, 0.55),
    "time_code": (0.40, 0.55),
}


@dataclass(slots=True)
class ObservationDraft:
    """一条待写入 ``observation`` 的记录（L2）。"""

    field_name: str
    raw_value: str
    normalized_value: str
    confidence: float
    #: 决策与理由，供校对 UI 直接显示
    decision: Decision = "needs_review"
    reason: str = ""
    bbox: dict[str, object] | None = None
    pipeline_version: str = PIPELINE_VERSION
    #: 命中区划时**从树里补出来的层级链**（从粗到细，如
    #: ``("中华人民共和国", "福建省", "南平市")``）。
    #:
    #: ⚠️ 它**不是识别输出** —— 只有最后一级是 OCR 读到的，上面几级是数据库补的。
    #: 空元组 = 没命中区划（或这个字段本来就不参与区划匹配，如 `date`）。
    place_path: tuple[str, ...] = ()


@dataclass(slots=True)
class ReviewItem:
    """校对队列里的一项。"""

    field_name: str
    value: str
    confidence: float
    #: 为什么需要人看（直接显示给用户）
    why: str
    source_line: str = ""


@dataclass(slots=True)
class ReviewBundle:
    """一次识别的完整产出：可自动采纳的 + 待校对的。"""

    observations: list[ObservationDraft] = field(default_factory=list)
    queue: list[ReviewItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def auto_accepts(self) -> list[ObservationDraft]:
        return [o for o in self.observations if o.decision == "auto_accept"]

    @property
    def rejects(self) -> list[ObservationDraft]:
        return [o for o in self.observations if o.decision == "reject"]


def decide(
    field_name: str,
    confidence: float,
    *,
    value: str = "",
    known_values: frozenset[str] | None = None,
    vocab: object | None = None,
) -> tuple[Decision, str]:
    """单个字段的决策。返回 ``(决策, 理由)``。

    ## 两条防线里的第二条（"防自信的错值"）

    它要回答的是：**"这个值像一个已登记的地名吗？"**
    有两种给法，二选一（``vocab`` 优先）：

    * ``known_values`` —— 字符串集合，判据是**精确相等**。简单，但只认
      "整串完全一致"，也回答不了"这个值属于哪个省"。
    * ``vocab`` —— 带 ``lookup(value)`` 的查询器（见
      `recognition.vocab.PlaceVocab`）。它按**区划树**判定，命中时还能
      **补出完整层级链** —— 用户要的"智能识别它属于哪个省"就是这个。

    用户 2026-10-08 定的规则：宽容度体现在**形态**上（允许「福建南平」这种
    连写、允许只写「南宁」），而**不是**放宽成子串包含 —— 后者会让
    「福建南火」命中「福建」而通过，而那正是这套机制存在的唯一理由。
    """
    lower, upper = FIELD_THRESHOLDS.get(field_name, (0.35, 0.85))

    if not value:
        return "reject", "值为空"
    if confidence < lower:
        return "reject", f"置信度 {confidence:.2f} 低于下限 {lower}（当没读到）"

    if field_name not in MECHANICALLY_VALIDATED:
        if vocab is not None:
            result = vocab.lookup(value)  # type: ignore[attr-defined]
            if not result.known:
                return (
                    "needs_review",
                    f"置信度 {confidence:.2f} 达标，但 {value!r} 不是已登记地名"
                    f"（也不是「上级+本级」的连写）—— 疑似 OCR 字形混淆"
                    f"（实测这类错误只有 1~2 个字且置信度不低）",
                )
            detail = result.hit.detail if result.hit else ""
            if confidence < upper:
                return "needs_review", (
                    f"置信度 {confidence:.2f} 落在 {lower}~{upper} 之间；{detail}；"
                    f"字段本身无机械校验能发现错值，交人工确认"
                )
            return "auto_accept", f"置信度 {confidence:.2f} ≥ {upper}；{detail}"
        if known_values is not None and value not in known_values:
            # **两条防线里更重要的那条**：防"自信的错值"。
            # 实测「福建南火」置信度 0.90，只看置信度会被自动采纳。
            return (
                "needs_review",
                f"置信度 {confidence:.2f} 达标，但 {value!r} 不在已登记的词表里 —— "
                f"疑似 OCR 字形混淆（实测这类错误只有 1~2 个字且置信度不低）",
            )

    if confidence < upper:
        return "needs_review", (
            f"置信度 {confidence:.2f} 落在 {lower}~{upper} 之间 —— "
            f"字段本身无机械校验能发现错值，交人工确认"
        )
    return "auto_accept", f"置信度 {confidence:.2f} ≥ {upper}"


def _known_in(vocab: object | None, known_values: frozenset[str] | None, value: str) -> bool:
    """值是否在词表里（新查询器 `lookup()` 与旧值集合两种都给法）。"""
    if not value:
        return False
    if vocab is not None:
        result = vocab.lookup(value)  # type: ignore[attr-defined]
        return bool(result.known)
    if known_values is not None:
        return value in known_values
    return False


def reclassify_for_postmark_type(
    parsed: ParsedFields,
    *,
    postmark_type: str,
    place_vocab: object | None,
    known_places: frozenset[str] | None,
    office_vocab: object | None,
    known_offices: frozenset[str] | None,
) -> str:
    """风景戳：把"顶弧文字"从 `place` 改判到 `office`。**原地修改 `parsed`**。

    返回一句说明（空串 = 没改），由调用方拼进 `office` 的原因里。

    ## 为什么需要（用户 2026-10-08 定的规则）

    戳型决定那串字算不算地名。项目自己的判据（`tools/samples/a3_vlm_whole.py`）：

    > * 日戳：**顶弧（上半圆）有省市地名文字**，中部是日期数字，底弧是局名+戳号。
    > * 风景戳：**顶弧是空的**（没有地名文字），中部或侧面是图案/线描/书法名称。

    用户据此的规则：

    > 「应该识别到如果是风景戳，然后再识别图中文字（比如是三清山。那就应该是
    > 邮局名称，而不应该放在地名。…如果戳面没有任何"地名信息"的话。
    > 就留空，或者根据邮局名录反查所在地」

    ## 三道保护（缺一个就会把对的值改错）

    1. **只对风景戳生效**。日戳的顶弧就是地名，绝不能碰。
    2. **值本身是已登记地名时不搬**。风景戳也可能带地名 —— 实测真值里
       `单张卡整张扫描图.jpg` 是风景戳，但 `place` = `江西玉山`、`office` = `三清乡`，
       **两个字段都对**。若不设这道保护，`江西玉山` 会被错误地搬进 office。
    3. **office 已有别的值时，只在新候选可信而原候选不可信时才换**；否则保留
       原 office，并把被挤掉的风景戳名写进说明。判据刻意**非对称** ——
       `office` 来自底弧（那里就是放局名的位置），而 `place` 在风景戳上来自
       中部名称行（那里的文字可能是图案名）。逐条理由见代码内注释。

    ## 为什么放在决策层而不是字段抽取层

    `parse_fields`（本地 OCR 路径）与 VLM 的云端字段都汇到 `build_bundle`，
    所以只有这里是**所有路径的单点**。而且**改这里不需要重跑识别**
    （原来 0.5s/卡，云端形态约 8s/卡）。
    """
    place = parsed.place
    if postmark_type != TYPE_SCENIC or place is None or not place.value:
        return ""

    #: 保护 ②：是已登记地名就保持不动 —— 风景戳也能有真地名
    if _known_in(place_vocab, known_places, place.value):
        return ""

    #: 保护 ④（2026-10-11 用户澄清，`docs/ok/17` §三 B0）：
    #: **只有在局名词表里命中，才把"顶弧/中部的文字"搬进局名。**
    #:
    #: 为什么加这一条：原来的判据是"office 为空就搬"，而它把
    #: **纯景点名/题字**也搬进局名（实测 `西海栈道`、`司春女神`）——
    #: 用户原话："西海栈道怎么能是'地名'呢？我宁可这个地名是空的"。
    #: 归因很具体：这条规则最初是照用户 2026-10-08 的澄清记的，而当时**举的例子
    #: 是 `三清山`** —— 那串字恰好**既是景点名、又是三清山邮政所的短名**
    #: （局名词表里真的命中）。于是"图案文字 → 局名"被写成了**无条件**的规则。
    #:
    #: 现在的判据与用户的定义对齐：**命中局名词表的才是局名**（`三清山`、`三清乡`）；
    #: 否则它既不是地名、也不是局名，**两个字段都不进**，只留在戳面原文里。
    if not _known_in(office_vocab, known_offices, place.value):
        dropped = place.value
        parsed.place = None
        return (
            f"风景戳：顶弧/中部文字 {dropped!r} 既不是已登记地名、也不在局名词表里 "
            f"→ **不进地名、也不进局名**（它是景点名/题字，只留在戳面原文里）；"
            f"该值仍是 OCR 的原始读数，未丢弃"
        )

    office = parsed.office
    if office is not None and office.value and office.value != place.value:
        #: 保护 ③：office 已有别的值时**只在"新候选可信而原候选不可信"时才换**。
        #
        # ## 为什么"两个都可信"也保留原 office（这条我反复过一次，记下来）
        #
        # 依据是**结构**，不是"哪个更可信"：
        #   * `office` 来自**底弧** —— 项目自己的判据说那里**就是**放局名的位置
        #   * `place` 在风景戳上来自**中部名称行** —— 那里的文字可以是**图案名**
        #     （实测样本 `风景戳特写(来源-求戳信)` 的名称是「企鹅献桃」，不是局名）
        #
        # 所以"新候选在词表里"不足以覆盖一个**位置正确**的局名，必须再加
        # "原候选不可信"。判据是**非对称**的 —— 因为两个位置的语义强度本来就
        # 不对称，硬凑成一句"取更可信"就把这层信息丢了。
        #
        # ⚠️ 我一度把它改成"只要求新候选可信"（理由写的是"新候选来自名称行、
        # 更像戳名"），被一条测试抓住 —— 那个理由是错的。恢复成非对称判据。
        #
        # ⚠️ 走到这里时保护④已经保证"新候选在局名词表里"（`place_known` 恒为真），
        # 所以条件退化成"原 office 也可信就保留"。
        office_known = _known_in(office_vocab, known_offices, office.value)
        if office_known:
            #: 不搬 —— 但"不是已登记地名"这个结论仍然成立，所以**也不该留在地名里**
            #: （否则它会以"地名"的身份挂着一张黄标出现，正是用户不要的那种）
            parsed.place = None
            return (
                f"风景戳：顶弧文字 {place.value!r} 未搬进局名（已有局名 "
                f"{office.value!r}，它在局名词表里）—— 该值仍是 OCR 的原始读数，未丢弃"
            )

    #: 搬。`parsed.place` 清空后，通用的字段循环会给它发出 reject ——
    #: 这正是"戳面没有任何地名信息就留空"的落点（不编一个省出来）；
    #: 那条 reject 的 reason 会被替换成上面返回的说明（见 `build_bundle`）。
    parsed.office = place
    parsed.place = None
    return (
        f"风景戳：顶弧/中部文字 {place.value!r} 在局名词表里命中 → 按戳型改判为**局名**"
        f"（风景戳的顶弧没有地名，见 a3_vlm_whole 的判据）"
    )


def build_bundle(
    parsed: ParsedFields,
    *,
    known_places: frozenset[str] | None = None,
    known_offices: frozenset[str] | None = None,
    place_vocab: object | None = None,
    office_vocab: object | None = None,
    postmark_type: str = "",
    postmark_type_confidence: float = 0.0,
    bbox: tuple[int, int, int, int] | None = None,
) -> ReviewBundle:
    """把一次字段抽取的结果转成 observation 草稿 + 校对队列。

    **每个抽到的字段都会产出一条 observation**，包括被 reject 的 ——
    因为 L2 层的意义就是"记录机器看到了什么"。被 reject 的也留着，
    它能让以后分析"哪些字段经常读不出来"。

    词表**两种给法**（见 `decide`）：

    * `place_vocab` / `office_vocab` —— 带 `lookup()` 的查询器，**优先**。
      这是生产路径（`routes_recognize` 传的是 `vocab.PlaceVocab`）。
    * `known_places` / `known_offices` —— 旧的值集合。保留是为了不破坏
      既有调用方与测试（它们只关心"某个值在不在名单里"）。
    """
    bundle = ReviewBundle()
    bbox_dict: dict[str, object] | None = None
    if bbox is not None:
        bbox_dict = {"x": bbox[0], "y": bbox[1], "w": bbox[2], "h": bbox[3]}

    # ---- 按戳型分流（必须在下面那个字段循环**之前**）----
    #
    # 风景戳的顶弧没有地名，中部/侧面才是图案或书法名称（见
    # `tools/samples/a3_vlm_whole.py` 的判据）。所以那串字要改判为**局名**。
    # 为什么放在这里而不是 `parse_fields`：本地 OCR 路径与 VLM 云端字段都汇到
    # `build_bundle`，这里是**所有路径的单点**；而且改这里不必重跑识别。
    office_reclassify_note = reclassify_for_postmark_type(
        parsed,
        postmark_type=postmark_type,
        place_vocab=place_vocab,
        known_places=known_places,
        office_vocab=office_vocab,
        known_offices=known_offices,
    )
    if office_reclassify_note:
        parsed.notes.append(office_reclassify_note)

    # (字段名, 值, 置信度, 词表查询器, 值集合)
    pairs: list[tuple[str, Field | None, object | None, frozenset[str] | None]] = [
        ("date", parsed.date, None, None),
        ("place", parsed.place, place_vocab, known_places),
        ("office", parsed.office, office_vocab, known_offices),
    ]
    # `office_no` 是**纯字符串**（没有置信度），所以它不在上面那组 (name, Field, known)
    # 里 —— 而这就是它此前从不出现的原因：`ParsedFields.office_no` 一直被正确
    # 拆出来，但 `build_bundle` 从不为它生成 observation，于是 API 响应与界面里
    # 都看不到它。
    #
    # 为什么必须补上：**局名+戳号是一个标识**。「中亭街」与「中亭街1」是不同邮局，
    # 少一个数字就是错的记录。实测云端能正确读出这个数字（原文行 `中亭街1`），
    # 所以此前丢的不是识别能力，是这一处接线。
    #
    # 置信度取 `office` 的（同一条 OCR 行拆出来的，没有独立置信度可言）；
    # `office` 缺失时给 0，让它走 reject。
    if parsed.office_no:
        base_conf = parsed.office.confidence if parsed.office else 0.0
        decision, reason = decide("office_no", base_conf, value=parsed.office_no)
        bundle.observations.append(
            ObservationDraft(
                field_name="office_no",
                raw_value=parsed.office_no,
                normalized_value=parsed.office_no,
                confidence=base_conf,
                decision=decision,
                reason=reason + "（戳号：与局名同一条 OCR 行拆出，随 `office` 的置信度）",
                bbox=bbox_dict,
            )
        )

    # ---- 戳面原文（`docs/ok/16` §四）：必填的**证据**，但不走词表 ----
    #
    # 为什么不放进上面的 `pairs`：
    #   1. **没有词表可判** —— 「建溪1」「三清山三龙出海」「春风得意马蹄疾」
    #      都是合法原文；"未登记"这个概念对它不成立。所以 vocab/known 都传 None。
    #   2. 它**空了会挡住提交**（前端与后端都拦，像 `visit_date`），
    #      所以空值那条 reason 必须说清"为什么必须填"，
    #      通用那句"未抽到（OCR 未返回匹配的行）"不足以让人知道这是必填项。
    #
    # 先补一条**日戳专用**的拼装规则（2026-10-11）：
    #
    # 用户给的定义就是"日戳的戳面原文 = 局名+戳号"，所以局名与戳号都读到时
    # 那串字**是确定的**（底弧那一行本来就写着它们）。这不是"编一个原文"，
    # 而是按戳型的定义把它拼出来 —— 而且 reason 里写明是**拼的**，人仍可改。
    #
    # ⚠️ **只对日戳拼**：风景戳/文化戳的原文在图画或中部文字里，拼不出
    # （那个情形就该让 `ok/16` §4.3 选的"拦提交"发生，由人填）。
    inscription_note = ""
    if (
        parsed.inscription is None
        and postmark_type == TYPE_DAILY
        and (parsed.office is not None or parsed.office_no)
    ):
        office_value = parsed.office.value if parsed.office else ""
        derived = f"{office_value}{parsed.office_no}"
        derived_conf = parsed.office.confidence if parsed.office else 0.0
        parsed.inscription = Field(value=derived, confidence=derived_conf, source_line=derived)
        #: ⚠️ 这句必须跟着**那条 observation**走（learned from `office` 的改判说明）：
        #: 只写进 `parsed.notes` 的话，界面上看到的仍是"置信度 0.90 ≥ 0.85"，
        #: 而"这串字是**拼的**、不是模型读到的原文"这条信息就丢了。
        inscription_note = (
            f"日戳的原文按「局名+戳号」**拼出** {derived!r}"
            f"（不是模型读到的原文；人可在界面上改）"
        )
        parsed.notes.append("戳面原文：" + inscription_note)

    if parsed.inscription is not None and parsed.inscription.value:
        decision, reason = decide(
            "inscription", parsed.inscription.confidence, value=parsed.inscription.value
        )
        if inscription_note:
            reason = f"{reason}；{inscription_note}"
        bundle.observations.append(
            ObservationDraft(
                field_name="inscription",
                raw_value=parsed.inscription.source_line or parsed.inscription.value,
                normalized_value=parsed.inscription.value,
                confidence=parsed.inscription.confidence,
                decision=decision,
                reason=reason,
                bbox=bbox_dict,
            )
        )
        if decision == "needs_review":
            bundle.queue.append(
                ReviewItem(
                    field_name="inscription",
                    value=parsed.inscription.value,
                    confidence=parsed.inscription.confidence,
                    why=reason,
                    source_line=parsed.inscription.source_line,
                )
            )
    else:
        bundle.observations.append(
            ObservationDraft(
                field_name="inscription",
                raw_value="",
                normalized_value="",
                confidence=0.0,
                decision="reject",
                reason=(
                    "戳面原文未读到 —— 它是**必填**的：`Postmark.inscription` "
                    "不能为空（戳面原文是证据本身），请人工填写后再入库"
                ),
                bbox=bbox_dict,
            )
        )

    # ---- 日期原文 / 时码：与 `office_no` 同一类（纯字符串，随主字段的置信度）----
    #
    # 两列（`postmark_date_text` / `time_code`）**早就在表里**，此前从没有任何
    # 代码填过它们 —— 用户 a1 里每次写的 `.09` / `.24` 就是这么丢的（`ok/15` §四）。
    # 空值**不发** observation（与 `office_no` 一致）：没读到就不该在档案里凭空多一行。
    for aux_name, aux_value, aux_hint in (
        ("date_text", parsed.date_text, "日期原文（保留戳面原格式，如 2026.10.01.09）"),
        ("time_code", parsed.time_code, "时码（日期末尾的时，如 09）"),
    ):
        if not aux_value:
            continue
        aux_conf = parsed.date.confidence if parsed.date else 0.0
        decision, reason = decide(aux_name, aux_conf, value=aux_value)
        bundle.observations.append(
            ObservationDraft(
                field_name=aux_name,
                raw_value=aux_value,
                normalized_value=aux_value,
                confidence=aux_conf,
                decision=decision,
                reason=f"{reason}（{aux_hint}；随 `date` 的置信度）",
                bbox=bbox_dict,
            )
        )

    for name, fld, field_vocab, known in pairs:
        if fld is None:
            #: 风景戳的改判可能**主动把 `place` 拿掉**（"那不是地名"）——
            #: 那时通用那句"未抽到（OCR 未返回匹配的行）"会误导人（OCR 明明读到了）。
            #: 用改判的说明替换它，人才知道那串字去哪了。
            empty_reason = "该字段未抽到（OCR 未返回匹配的行）"
            if name == "place" and office_reclassify_note:
                empty_reason = office_reclassify_note
            bundle.observations.append(
                ObservationDraft(
                    field_name=name,
                    raw_value="",
                    normalized_value="",
                    confidence=0.0,
                    decision="reject",
                    reason=empty_reason,
                    bbox=bbox_dict,
                )
            )
            continue
        decision, reason = decide(
            name, fld.confidence, value=fld.value, known_values=known, vocab=field_vocab
        )
        # 风景戳改判的说明**只在 office 那条**上加 —— 否则同一个解释会出现两次，
        # 而 `place` 那条的"未抽到"更该保持干净（它要说的是"这戳没有地名信息"）。
        if name == "office" and office_reclassify_note:
            reason = f"{reason}；{office_reclassify_note}"
        # 命中区划时把补出来的层级链留下 —— 用户要的"属于哪个省"就在这里。
        # ⚠️ 只放在内存里的 draft 上（L2 的 observation 表**没有**这一列，
        #    而且不该加：层级顺 `Postmark.place_id` 与 `place.parent_id`
        #    就能走上去，加列等于把可推导的东西冗余进历史数据）。
        place_path: tuple[str, ...] = ()
        if field_vocab is not None:
            resolution = field_vocab.lookup(fld.value)  # type: ignore[attr-defined]
            if resolution.known:
                place_path = resolution.place_path
        bundle.observations.append(
            ObservationDraft(
                field_name=name,
                raw_value=fld.source_line,
                normalized_value=fld.value,
                confidence=fld.confidence,
                decision=decision,
                reason=reason,
                bbox=bbox_dict,
                place_path=place_path,
            )
        )
        if decision == "needs_review":
            bundle.queue.append(
                ReviewItem(
                    field_name=name,
                    value=fld.value,
                    confidence=fld.confidence,
                    why=reason,
                    source_line=fld.source_line,
                )
            )

    if postmark_type:
        decision, reason = decide("postmark_type", postmark_type_confidence, value=postmark_type)
        bundle.observations.append(
            ObservationDraft(
                field_name="postmark_type",
                raw_value=postmark_type,
                normalized_value=postmark_type,
                confidence=postmark_type_confidence,
                decision=decision,
                reason=reason,
                bbox=bbox_dict,
            )
        )

    # 队列按"越不确定越靠前"排 —— 用户的时间该花在最可疑的地方
    bundle.queue.sort(key=lambda it: it.confidence)
    bundle.notes = list(parsed.notes)
    if not bundle.queue:
        bundle.notes.append("校对队列为空：所有抽到的字段都达到自动采纳阈值")
    return bundle


def observation_decimals(bundle: ReviewBundle) -> list[dict[str, object]]:
    """转成可以直接喂给 ``Observation(**row)`` 的字典列表。

    单独一层的理由：``Observation.confidence`` 是 ``Numeric(5,4)``，
    用 float 直接写会在某些驱动上被截成 0（整数）。所以统一转 ``Decimal`` ——
    这个坑不显眼，但会让所有置信度变成 0，而 0 看起来只是"识别质量差"。
    """
    rows: list[dict[str, object]] = []
    for o in bundle.observations:
        rows.append(
            {
                "pipeline_version": o.pipeline_version,
                "field_name": o.field_name,
                "raw_value": o.raw_value or None,
                "normalized_value": o.normalized_value or None,
                "confidence": Decimal(f"{o.confidence:.4f}"),
                "bbox": o.bbox,
            }
        )
    return rows
