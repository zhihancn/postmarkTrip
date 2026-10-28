"""字段抽取：从 OCR 文本行里取出结构化字段（spec v1.0 §3.6）。

## 分工

OCR 只负责"这张图上有什么字"，本模块负责"**哪个字是哪个字段**"。
分开的理由：字段规则（日期格式、局名后缀）是**可测可改**的，
而 OCR 换引擎时它们不该跟着变。

## 字段与判据

| 字段 | 判据 | 依据 |
| --- | --- | --- |
| ``date`` | ``\\d{4}[.\\-/]\\d{1,2}[.\\-/]\\d{1,2}([.\\-/]\\d{1,2})?`` | 实测所有日戳样本都是 `YYYY.MM.DD[.HH]` |
| ``place`` | 顶弧那一行（在戳的上半部） | 日戳顶弧是省市名 |
| ``office`` | 底弧那一行（在戳的下半部） | 日戳底弧是局名 + 戳号 |
| ``office_no`` | ``office`` 末尾的 1~2 位数字 | 实测「建溪 1」「茶山 1」「四号航站楼 2」 |

## 两个刻意的设计

**① 置信度分开记。** 每个字段带自己的 ``confidence``，
来自**产出该字段的那一行 OCR 置信度**——不是整块的平均。
原因：整块平均会被"日期行很准、地名行很差"稀释掉，
而用户校对时是**逐字段**确认的（spec §3.7）。

**② 不做"猜测式补齐"。** 抽不到就留空 + 记原因，
不用字典去猜省份。原因：邮戳地名有大量生僻字与历史地名，
字典猜测会产出**看起来对的错值**，而错值比空值更有害 ——
空值会进校对队列，错值可能被直接采纳。

## 已知的 OCR 混淆（实测，必须在清洗里处理）

| OCR 输出 | 真值 | 说明 |
| --- | --- | --- |
| 南火 / 南天 / 老南平 | 南平 | 「平」被误认 |
| 社丹江 / 牡丹江 | 牡丹江 | 「牡」被误认 |
| 四号航站耀 | 四号航站楼 | 「楼」被误认 |
| 乔山 / 齐山 | 茶山 | 字形相近 |

**本模块不做这些替换** —— 它们是**样本特异的混淆**，
硬编码进去等于把 6 张样本的答案写进代码（过拟合）。
正确处理是：让校对队列把它们作为"低置信候选"展示给用户确认。
这份表记在这里是为了**说明为什么准确率数字不等于"OCR 不行"**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 日期正则。允许 `.` `-` `/` 三种分隔符（不同戳的格式不同），
#: 也允许末尾多一段时间（`.HH`）—— 实测「2026.09.25.15」的 15 是时。
DATE_RE = re.compile(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{1,2}))?")

#: OCR 常把空格插进数字之间（实测「20 26 09.25.16」）。
#: 所以匹配前要把"数字之间的空格"去掉 —— 但**不能去掉所有空格**，
#: 否则「四号航站楼 2」会粘成「四号航站楼2」，戳号就分不出来了。
_DIGIT_SPACE_RE = re.compile(r"(?<=\d)\s+(?=\d)")

#: 局名末尾的戳号。只取 1~2 位，且允许有空格分隔。
_OFFICE_NO_RE = re.compile(r"\s*(\d{1,2})\s*$")

#: 明显不是地名的行（明信片上印刷的抬头/面值）。整块 OCR 会把它们读进来。
_NON_PLACE_TOKENS = (
    "中国邮政",
    "CHINA",
    "POST",
    "美丽中国",
    "邮戳卡",
    "保护森林",
    "牡丹江雪乡",
)
_NON_PLACE_RE = re.compile("|".join(re.escape(t) for t in _NON_PLACE_TOKENS), re.IGNORECASE)


@dataclass(slots=True)
class Field:
    """一个抽取到的字段值。"""

    value: str
    #: 产出该字段的 OCR 行置信度（**不是整块平均**）
    confidence: float
    #: 该字段来自哪一行（原文），便于校对时对照
    source_line: str = ""
    #: 归一化后的结构（日期拆成 年月日时）
    parts: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedFields:
    """一次抽取的全部结果。缺的字段**不出现在字典里**，而不是给空串。"""

    date: Field | None = None
    place: Field | None = None
    office: Field | None = None
    office_no: str = ""
    #: **戳面原文**（`docs/ok/16` §四）。按戳型的取值规则：
    #: 日戳 = 局名+戳号；文化戳 = 下方弧的戳名；风景戳 = 图画里的文字。
    #: 它是**证据**，也是唯一必填的文本字段（`Postmark.inscription` NOT NULL）。
    inscription: Field | None = None
    #: 日期**原文**（如 `2026.10.01.09`）→ `Postmark.postmark_date_text`。
    #: 空串时下游退回 `date.source_line`。
    date_text: str = ""
    #: 时码（如 `09`）→ `Postmark.time_code`。空串时下游从 `date.parts` 推。
    time_code: str = ""
    #: 判据说明，逐条可读。校对 UI 与排障都用它。
    notes: list[str] = field(default_factory=list)

    @property
    def found(self) -> list[str]:
        """抽到的字段名（用于算 A4 的分子）。"""
        out = []
        if self.date:
            out.append("date")
        if self.place:
            out.append("place")
        if self.office:
            out.append("office")
        return out


def time_code_of(date_field: Field | None) -> str:
    """从日期字段的归一化结构里取时码（`.09` 里的 `09`）。

    为什么从 `parts` 取而不是再正则一遍原文：`_make_field` 已经把"时"解出来了，
    再解一次就是第二个真相来源（两处对不上时不知道该信谁）。
    """
    if date_field is None:
        return ""
    hour = date_field.parts.get("hour")
    if isinstance(hour, int):
        return f"{hour:02d}"
    return ""


def normalise_digits(text: str) -> str:
    """去掉**数字之间**的空格。见 ``_DIGIT_SPACE_RE`` 的说明。"""
    return _DIGIT_SPACE_RE.sub("", text)


def find_date(lines: list[tuple[str, float]]) -> Field | None:
    """在若干 (文本, 置信度) 行里找日期。

    取**置信度最高**的那个匹配，而不是第一个 —— 实测同一张图上
    印刷的"202..."与戳上的日期可能同时命中（整块 OCR 会读到边上的印刷内容）。

    ## 两条策略

    1. **带分隔符的正则** —— `2026.09.25.15` 这类规整格式
    2. **纯数字兜底** —— 实测 OCR 会给出 `20 26 09.25.16`（把 2026 拆成
       `20 26`、并丢掉一个分隔符）。去掉分隔符与空格后是 `2026092516`
       = 10 位 = `YYYYMMDDHH`。**规律是位数**：8 位 `YYYYMMDD`、
       10 位 `YYYYMMDDHH`、12 位 `YYYYMMDDHHMM`、14 位带秒。

    只靠策略 1 会漏掉实测存在的格式，所以两条都要。
    """
    best: Field | None = None
    for text, conf in lines:
        cand = parse_date_text(text, confidence=conf)
        if cand is None:
            continue
        if best is None or cand.confidence > best.confidence:
            best = cand
    return best


def parse_date_text(text: str, *, confidence: float) -> Field | None:
    """把**一段日期原文**解析成 `Field`（两条策略都试）。

    抽成公开函数的原因：整卡云端调用那条路径拿到的也是"一段日期原文"
    （`docs/ok/16` §四 让它回 `date_text`），它需要**与本地完全同一套**
    解析规则 —— 否则同一天在两条路径上会被解析成不同的东西。
    """
    if not text.strip():
        return None
    cleaned = normalise_digits(text)
    return _parse_by_pattern(cleaned, text, confidence) or _parse_by_digits(text, confidence)


def _plausible(year: int, month: int, day: int, hour: int | None) -> bool:
    if not (1900 <= year <= 2200):
        return False
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return False
    return hour is None or 0 <= hour <= 23


def _parse_by_pattern(text: str, source: str, conf: float) -> Field | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour_raw = m.group(4)
    hour = int(hour_raw) if hour_raw not in (None, "") else None
    if not _plausible(year, month, day, hour):
        return None
    return _make_field(year, month, day, hour, source, conf)


def _parse_by_digits(source: str, conf: float) -> Field | None:
    """纯数字兜底。见 :func:`find_date` 的说明。"""
    digits = "".join(ch for ch in source if ch.isdigit())
    # 优先长位数（含时分），但年份必须是 4 位
    for size, has_hour in ((14, True), (12, True), (10, True), (8, False)):
        if len(digits) != size:
            continue
        year = int(digits[0:4])
        month = int(digits[4:6])
        day = int(digits[6:8])
        hour = int(digits[8:10]) if has_hour else None
        if not _plausible(year, month, day, hour):
            return None
        return _make_field(year, month, day, hour, source, conf)
    return None


def _make_field(
    year: int, month: int, day: int, hour: int | None, source: str, conf: float
) -> Field:
    parts: dict[str, object] = {"year": year, "month": month, "day": day}
    value = f"{year}-{month:02d}-{day:02d}"
    if hour is not None:
        parts["hour"] = hour
        value += f" {hour:02d}:00"
    return Field(value=value, confidence=conf, source_line=source, parts=parts)


def _is_place_like(text: str) -> bool:
    """像地名的行：有 2 个以上汉字、且不是印刷抬头。"""
    han = re.findall(r"[\u4e00-\u9fff]", text)
    if len(han) < 2:
        return False
    return not _NON_PLACE_RE.search(text)


def split_office_and_no(text: str) -> tuple[str, str]:
    """把「建溪 1」拆成 ``("建溪", "1")``；没有戳号时返回 ``(text, "")``。

    **只从末尾取 1~2 位数字**：局名里也可能含数字（如「四号航站楼」），
    贪婪匹配会把「四号航站楼 2」拆错。
    """
    cleaned = text.strip()
    m = _OFFICE_NO_RE.search(cleaned)
    if not m:
        return cleaned, ""
    return cleaned[: m.start()].strip(), m.group(1)


def parse_fields(
    lines: list[tuple[str, float]],
    *,
    region_height: int,
    line_y_positions: list[float] | None = None,
) -> ParsedFields:
    """从 OCR 行里抽字段。

    ## 上下半部怎么分：**以日期行为基准**，不用几何中线

    这是实测撞出来的，而且是本模块最关键的一步。

    第一版用"区域几何中线"分上下。实测失败：`清晰黑色日戳特写-1` 上
    「福建南火」（顶弧地名，y=389）被分到了**下半部** —— 因为
    **检测到的圆心比真圆心高约 240px**：

    | 行 | y | 说明 |
    | --- | --- | --- |
    | 福建南火（顶弧） | 389 | 真圆心应在此行下方 |
    | 2026.09.25.15 | **583** | **日期在戳的正中心** |
    | 建溪（底弧） | 775 | |
    | 检测到的圆心 | **339** | 偏高 243px |

    两条弧文字的中点 (389+775)/2 = 582，与日期行 y=583 **几乎重合** ——
    这不是巧合：**日期就印在戳的几何中心**。

    所以正确的基准是**日期行**：它上方的行是顶弧（地名）、下方的行是底弧（局名）。
    这样即使在圆心定位偏差很大的情况下，上下归属仍然正确 ——
    比"修正圆心"更省事也更稳。

    没有日期行时才回落到几何中线，并把这个不确定性记进 ``notes``。
    """
    out = ParsedFields()
    if not lines:
        out.notes.append("OCR 没有返回任何文本行")
        return out

    date = find_date(lines)
    if date:
        out.date = date
        out.notes.append(f"日期：命中 {date.source_line!r}（置信度 {date.confidence:.2f}）")
    else:
        out.notes.append("日期：所有行都没有匹配 YYYY.MM.DD[.HH]")

    if line_y_positions is None or region_height <= 0:
        out.notes.append("地名/局名：缺少行位置信息，跳过（不猜）")
        return out

    # ---- 分割基准：优先日期行的 y，其次几何中线 ----
    date_line = date.source_line if date else None
    split_y: float | None = None
    split_basis = ""
    if date is not None:
        for (text, _conf), y in zip(lines, line_y_positions, strict=False):
            if text == date_line:
                split_y = y
                split_basis = "日期行（日期印在戳的中心，是最可靠的基准）"
                break
    if split_y is None:
        split_y = region_height / 2.0
        split_basis = "几何中线（**没有日期行，圆心偏差未被校正**）"
    out.notes.append(f"上下分割：以 {split_basis}，基准 y={split_y:.0f}")

    upper: list[tuple[str, float]] = []
    lower: list[tuple[str, float]] = []
    for (text, conf), y in zip(lines, line_y_positions, strict=False):
        if not _is_place_like(text):
            continue
        # 日期行本身不能当地名 —— 否则「2026.09.25.15」会被塞进 place
        if date_line is not None and text == date_line:
            continue
        (upper if y < split_y else lower).append((text, conf))

    if upper:
        text, conf = max(upper, key=lambda t: t[1])
        out.place = Field(value=text, confidence=conf, source_line=text)
        out.notes.append(f"地名：取基准上方最高置信行 {text!r}（{conf:.2f}）")
    else:
        out.notes.append("地名：基准上方没有像地名的行")

    if lower:
        text, conf = max(lower, key=lambda t: t[1])
        office, no = split_office_and_no(text)
        out.office = Field(value=office, confidence=conf, source_line=text)
        out.office_no = no
        out.notes.append(
            f"局名：取基准下方最高置信行 {text!r}（{conf:.2f}）→ 局名 {office!r} 戳号 {no!r}"
        )
    else:
        out.notes.append("局名：基准下方没有像地名的行")

    # ---- 日期原文与时码（两列早已存在，此前从没有人填 —— `ok/15` §四 a1 疏漏 #1/#2）----
    if date is not None:
        #: 原文 = 产出日期的那一行（不是重新格式化出来的值）
        out.date_text = date.source_line.strip()
        out.time_code = time_code_of(date)
        if out.time_code:
            out.notes.append(f"时码：从日期原文推出 {out.time_code!r}")

    # ⚠️ 戳面原文**不在这里拼**：它的规则要按**戳型**分流（日戳 = 局名+戳号；
    # 风景戳/文化戳的原文在图画或中部文字里，拼不出来）。`parse_fields` 只看得到
    # 文本行，看不到戳型，而 `review.build_bundle` 两条路径都会经过、且拿得到
    # 戳型 —— 所以那条规则放在那里（见 `build_bundle` 里"日戳专用的拼装规则"）。

    return out
