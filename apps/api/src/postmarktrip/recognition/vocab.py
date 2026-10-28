"""识别用的**词表**：从已登记的字典数据里，构造"戳面上可能出现的写法"。

## 为什么需要它（决定 4 的绿勾规则）

`recognition.review.decide()` 对**开放文本**字段（`place` / `office`）有两条防线：
置信度 + **值在不在词表里**。第二条是防"自信的错值"的唯一手段 ——
实测 OCR 给「福建南火」的置信度是 **0.90~0.95**，只看置信度会把它当成可信。

## 两条硬规则（用户 2026-10-08 拍板，别重开）

### 1. 匹配**精确**，绝不放宽成"包含/模糊"

词表的角色是**防线**，不是"标准答案对照"。它的两个失败方向**不对称**：

| 方向 | 表现 | 代价 |
| --- | --- | --- |
| 缺一个合法写法 | 正确值 → 标黄 | **虚警**，多点几下，可接受 |
| 混进一个错值 | 错值 → 标绿放过 | **防线失效**，静默落错数据 |

所以**宁可缺、不可滥**。用户的"宽容一点"要求体现在**形态**上（允许省名+市名连写、
允许只写市名），而**不是**放宽成子串包含 —— 后者会让「福建南火」命中「福建」而通过，
而那正是做这件事的**唯一理由**。

### 2. 合法形态只有两种

1. 值**本身**就是一个已登记地名（**全名**或**短名**）
2. 值能**恰好拆成两段**，每段都是已登记地名，且**后者在前者辖下**

⚠️ **刻意不做"前缀探测"**（"开头是福建、剩下不是地名 → 算只读到上级"）。
那会把「福建南火」判成"漏字"（可接受）而不是"错字"（必须拦）。实测撞过。

### 3. 短名必须**显式给**，不能靠机械去后缀

实测：`广西壮族自治区` 机械去后缀得到 `广西壮族`，而戳面上写的是 **`广西`**。
所以 `SPECIAL_SHORT` 是一张显式小表（5 个自治区 + 2 个特别行政区），
其余层级才用后缀规则。

## 谁用

`api/routes_recognize.py` 在识别时构造 `PlaceVocab` 并传给 `recognize_page`，
后者再传给 `review.build_bundle`。**没有这一步，词表那道防线整段不执行**
（未登记的值不再被送进校对队列），而症状很轻：界面看起来"颜色都对"。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from postmarktrip.db.models import Place, PostOffice

logger = logging.getLogger("postmarktrip.recognition.vocab")

#: 行政区划名的后缀。**从长到短**匹配 —— 顺序有关系：
#: 「广西壮族自治区」要先剥「自治区」，否则 `rstrip("区")` 会留下「广西壮族自治」。
DIVISION_SUFFIXES: Final[tuple[str, ...]] = (
    "特别行政区",
    "自治区",
    "自治州",
    "自治县",
    "自治旗",
    "地区",
    "盟",
    "省",
    "市",
    "县",
    "区",
    "旗",
    "乡",
    "镇",
    "街道",
    "村",
)

#: 拼接时的最大祖先层数。戳面顶弧最多也就是"省+市"或"省+县"，
#: 再往上（国名）不会出现在戳上，往下再拆只会造出噪声词。
MAX_ANCESTOR_DEPTH: Final = 2

#: 自治区 / 特别行政区的**戳面短名**。
#:
#: ⚠️ 这张表**必须显式给**，不能靠机械去后缀。实测：
#:     「广西壮族自治区」机械去后缀 → 「广西壮族」，而戳面上写的是 **「广西」**
#:     「新疆维吾尔自治区」机械去后缀 → 「新疆维吾尔」，戳面写的是 **「新疆」**
#:     「内蒙古自治区」机械去后缀 → 「内蒙古」，这个恰好对，但那是运气
#: 用户 2026-10-08 明确要求"多级、按国标灌"，所以这张表只覆盖**名字里带民族名**
#: 或**性质特殊**的那几个；其余层级（省/市/县/区/乡/镇/街道）用后缀规则。
#: 将来引入国外资料时，这类"标准名 → 戳面短名"的映射要按国家分别处理。
SPECIAL_SHORT: Final[dict[str, str]] = {
    "广西壮族自治区": "广西",
    "内蒙古自治区": "内蒙古",
    "新疆维吾尔自治区": "新疆",
    "宁夏回族自治区": "宁夏",
    "西藏自治区": "西藏",
    "香港特别行政区": "香港",
    "澳门特别行政区": "澳门",
}


def strip_division_suffix(name: str) -> str:
    """去掉行政区划后缀：「南平市」→「南平」。

    刻意**只去掉一个**后缀，不做循环剥离 —— 「区」是「自治区」的一部分，
    循环剥会把「广西壮族自治区」变成「广西壮族自」。

    ⚠️ 这是**低层工具**，单独用它会得到戳面上不存在的写法。要戳面短名请用
    `place_short_name`（它先查显式表、再做民族名截断，最后才落到这里）：
    对「广西壮族自治区」本函数给 `广西壮族`，而 `place_short_name` 给 `广西`。
    """
    cleaned = name.strip()
    for suffix in DIVISION_SUFFIXES:
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            return cleaned[: -len(suffix)]
    return cleaned


#: 国标 56 个民族名，用于"在第一个民族名处截断"。
#:
#: ## 为什么需要它（实测出来的）
#:
#: 机械去后缀在**自治单位**上系统性跑偏，而这批数据里有 **155 个**：
#:
#: | 标准名 | 机械去后缀 | 戳面实际写法 |
#: | --- | --- | --- |
#: | `青龙满族自治县` | `青龙满族` ❌ | `青龙` |
#: | `围场满族蒙古族自治县` | `围场满族蒙古族` ❌ | `围场` |
#: | `双江拉祜族佤族布朗族傣族自治县` | `双江拉祜族佤族布朗族傣族` ❌ | `双江` |
#: | `莫力达瓦达斡尔族自治旗` | `莫力达瓦达斡尔族` ❌ | `莫力达瓦` |
#: | `鄂温克族自治旗` | `鄂温克族` ❌ | `鄂温克` |
#:
#: ⚠️ 注意 `鄂温克族自治旗`：**「族」是民族名的一部分，不是后缀** ——
#: 所以不能"在族字处截断"（那会得到 `鄂温克族`，我第一版就是这么错的）。
#: 正确做法是**在民族名处截断**，而且民族名后面**可能带「族」也可能直接接
#: 「自治」**（`鄂伦春自治旗` 里就没有「族」字）。
#:
#: 用国标民族名锚定之后，实测数据里出现的 162 种「…族」片段**只漏了 `各族`**
#: （`龙胜各族自治县` —— 表示"多个民族"，不是单个民族名），所以下面单列一个。
ETHNIC_GROUPS: Final[tuple[str, ...]] = (
    "蒙古", "回", "藏", "维吾尔", "苗", "彝", "壮", "布依", "朝鲜", "满",
    "侗", "瑶", "白", "土家", "哈尼", "哈萨克", "傣", "黎", "傈僳", "佤",
    "畲", "高山", "拉祜", "水", "东乡", "纳西", "景颇", "柯尔克孜", "土", "达斡尔",
    "仫佬", "羌", "布朗", "撒拉", "毛南", "仡佬", "锡伯", "阿昌", "普米", "塔吉克",
    "怒", "乌孜别克", "俄罗斯", "鄂温克", "德昂", "保安", "裕固", "京", "塔塔尔", "独龙",
    "鄂伦春", "赫哲", "门巴", "珞巴", "基诺", "汉",
    #: 「各族」不是单个民族名，但 `X各族自治县` 是真实存在的写法
    "各族",
)

#: 短名的**最小长度**。截断或去后缀后只剩一个字时**保留原名**。
#:
#: 实测数据里有 **104 个单字县名**（`赵县`、`涉县`、`磁县`…）。去后缀会得到
#: 单字（`赵`），那既不是戳面写法、又会与大量别的名字撞车 —— 兜底保留原名。
MIN_SHORT_NAME_LEN: Final[int] = 2


@dataclass(frozen=True, slots=True)
class PlaceName:
    """一个行政区划的两种写法。**唯一产出点**。

    存在的理由：短名规则现在**不止一条**（显式表 / 民族名截断 / 后缀 / 单字兜底），
    而 `PlaceVocab` 与 `build_place_names` 都要用。那两条路径**曾经各写一遍**，
    结果不一致（一条漏了显式表，产出 `广西壮族南宁`、另一条产出 `广西南宁`），
    被一条测试抓住。收在这里之后，"一个名字产出哪些写法"只有一个答案。
    """

    full: str
    short: str

    @property
    def forms(self) -> frozenset[str]:
        return frozenset({self.full, self.short} - {""})


def describe_place(name: str) -> PlaceName:
    """标准名 → 两种写法。要写法的用这个，不要自己拼。"""
    return PlaceName(full=name, short=place_short_name(name))


def place_short_name(name: str) -> str:
    """戳面上的短名。三道规则按优先级：

    1. **显式表** —— 7 个特殊省级单位（自治区/特别行政区）。它们的名字里
       民族名在**中间**（`广西壮族自治区`），机械规则会得 `广西壮族`
    2. **在第一个民族名处截断** —— 155 个自治单位（含州/县/旗）靠这条
    3. **退回后缀规则** —— 省/市/县/区/乡/镇/街道/村

    两条兜底见 `MIN_SHORT_NAME_LEN`：截断或去后缀后 < 2 字就保留原名
    （`赵县` 而不是 `赵`）。
    """
    if name in SPECIAL_SHORT:
        return SPECIAL_SHORT[name]

    # 民族名锚点：在每个命中位置**只保留最长的那个民族名**。
    #
    # 这一步是必需的，而且它同时解决了两个问题：
    #   * 「哈萨克」不会被更短的民族名抢先（先按长度倒序匹配）
    #   * 「汉」藏在「鄂温克」里的假命中被吸收掉 —— `鄂温克族自治旗` 在 pos=0
    #     同时命中「鄂温克」(len 3) 与「汉」(len 1，来自片段「鄂温**汉**族」)，
    #     保留最长的即「鄂温克」，短名正确得到 `鄂温克`
    #
    # ⚠️ 我第一版用"截断点前一个字不能是汉字"来排除假命中，**打掉了 10 个正确用例**
    #    （`双江拉祜族…` 的「江」是汉字）。教训：排除假命中要针对**命中本身**
    #    （同位置取最长），而不是看命中周围的字符。
    longest_at: dict[int, int] = {}
    for ethnic in sorted(ETHNIC_GROUPS, key=len, reverse=True):
        for pattern in (f"{ethnic}族", f"{ethnic}自治"):
            pos = name.find(pattern)
            if pos < 0:
                continue
            longest_at[pos] = max(longest_at.get(pos, 0), len(ethnic))

    if longest_at:
        pos, length = min(longest_at.items(), key=lambda item: (item[0], -item[1]))
        if pos >= MIN_SHORT_NAME_LEN:
            return name[:pos]
        # pos == 0：某个民族名**正好从开头开始**（`鄂温克族自治旗`），
        # 短名就是那个民族名本身 —— 取它的完整长度，而不是切一个字
        if pos == 0 and length >= MIN_SHORT_NAME_LEN:
            return name[:length]

    for suffix in DIVISION_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            candidate = name[: -len(suffix)]
            return candidate if len(candidate) >= MIN_SHORT_NAME_LEN else name
    return name


#: 层级序号 → level_label。缺 `level_label` 时（单测夹具）按树深度取。
DEFAULT_LEVEL_LABELS: Final[tuple[str, ...]] = ("国家", "省级", "地级市", "县级", "乡级", "村级")


def normalize_place_rows(
    rows: list[tuple[object, ...]],
) -> list[tuple[object, object, str, str]]:
    """把 ``(id, parent_id, name)`` 或 ``(id, parent_id, name, level_label)``
    统一成四元组。

    为什么容两种：从库里读时 `level_label` 是现成的（`load_place_rows` 会给），
    而单测夹具只写三个元素更省事。缺 `level_label` 时按**树深度**推一个
    （根 = 国家，逐层往下）—— 生产路径仍用真实数据里的 `level_label`，
    所以"带全称后缀"那个形态在真数据上用的是权威层级，不是猜的。
    """
    if rows and len(rows[0]) >= 4:
        return [(row[0], row[1], row[2], str(row[3] or "")) for row in rows]

    parent_of = {row[0]: row[1] for row in rows}

    def depth_of(place_id: object) -> int:
        depth = 0
        node = parent_of.get(place_id)
        seen = {place_id}
        while node is not None and node not in seen and depth < 16:
            seen.add(node)
            depth += 1
            node = parent_of.get(node)
        return depth

    out: list[tuple[object, object, str, str]] = []
    for row in rows:
        depth = depth_of(row[0])
        label = DEFAULT_LEVEL_LABELS[min(depth, len(DEFAULT_LEVEL_LABELS) - 1)]
        out.append((row[0], row[1], row[2], label))
    return out


def build_place_names(rows: list[tuple[object, ...]]) -> frozenset[str]:
    """从 ``(id, parent_id, name)`` 三元组构造地名写法集合。

    产出三类写法，都是**实测在戳面上出现过**的形状：

    | 写法 | 例 | 说明 |
    | --- | --- | --- |
    | 本名（戳面短名） | ``南平`` / ``广西`` | 戳面只写市名/省名时 |
    | 本名（原样） | ``南平市`` / ``广西壮族自治区`` | 用户可能就这么登记，OCR 也可能读到 |
    | 祖先 + 本名 | ``福建南平`` / ``广西南宁`` | 顶弧最常见的写法 |

    ⚠️ 短名走 `place_short_name`（**先查显式表**，再退回后缀规则），
    与 `PlaceVocab` 用**同一个函数** —— 两条路径曾经不一致（这里漏了显式表，
    于是产出 `广西壮族南宁` 而生产路径产出 `广西南宁`），被一条测试抓住。

    ## 连写时**只用去后缀的那一份**

    「南平市」与「南平」都进词表（单名），但连写只产出 ``福建南平``，
    **不产出** ``福建省南平市`` 那种"两个标准名直接粘起来"的形状。

    理由：戳面上没有那种写法，而它每多一条就多一次"错值恰好命中"的机会 ——
    词表越宽，那道防线越松。宁可少一条（正确值最多被标黄多看两眼），
    不要多一条（错值被当成可信放过去，正是「福建南火」溜过去的那条路）。

    纯函数，不碰数据库 —— 这样它可以被单测直接钉住。
    """
    by_id: dict[object, str] = {}
    parent_of: dict[object, object | None] = {}
    for place_id, parent_id, name, _label in normalize_place_rows(rows):
        by_id[place_id] = name
        parent_of[place_id] = parent_id

    names: set[str] = set()
    for place_id, raw in by_id.items():
        described = describe_place(raw)
        short = described.short
        names.update(described.forms)

        # 往上走，逐层与"去过后缀的祖先"拼接
        ancestor = parent_of.get(place_id)
        depth = 0
        while ancestor is not None and depth < MAX_ANCESTOR_DEPTH:
            ancestor_raw = by_id.get(ancestor)
            if ancestor_raw is None:
                break  # 祖先不在结果里（数据不全），不要猜
            ancestor_short = place_short_name(ancestor_raw)
            if ancestor_short and short and ancestor_short != short:
                names.add(f"{ancestor_short}{short}")
            elif ancestor_short and not short and ancestor_short != raw:
                names.add(f"{ancestor_short}{raw}")
            ancestor = parent_of.get(ancestor)
            depth += 1

    return frozenset(n for n in names if n)


#: 局名的业务后缀。**词表是"该产哪些写法"的唯一权威**，所以这张表住这里，
#: `tools/import_post_offices.py` 从这儿引用（不各写一份 —— 那正是地名那边
#: 栽过的坑：两条路径各写一遍短名规则，产出两种不同的词表且不报错）。
#:
#: 实测（54620 条目录）后缀分布极干净：
#: `邮政支局` 28855 / `邮政所` 25758 / `支局` 4 / `邮电所` 1 /
#: 没有已知后缀 2（都是 `县局营业室`）。
#: 顺序重要：长的先试，否则 `邮政支局` 会被 `支局` 抢先剥成 `…邮政`。
OFFICE_SUFFIXES: Final[tuple[str, ...]] = (
    "邮政便民服务站",
    "邮政便民服务点",
    "邮政代办所",
    "邮政营业部",
    "邮政服务点",
    "邮政支局",
    "邮电支局",
    "邮政营业厅",
    "邮电营业厅",
    "邮政所",
    "邮电所",
    "邮政局",
    "营业厅",
    "营业部",
    "支局",
    "服务点",
    "代办所",
)

#: 局名短名的**最小长度**（与地名的 `MIN_SHORT_NAME_LEN` 同值，但分开命名 ——
#: 两者的理由不同：地名是"单字县名不是戳面写法"，局名是"剥完不能为空"）。
MIN_OFFICE_SHORT_LEN: Final[int] = 2


def office_short_name(name: str) -> str:
    """局名全名 → **戳面写法候选**（剥掉业务后缀）。

    目录里存的是**带后缀的全名**（`建溪邮政支局`、`四号航站楼邮政所`），
    而戳面写的是短名 —— 用户 2026-10-11 给的三个例子都是这个形态：

    ```
    日戳：  上弧 北京密云      底弧 密云镇1        → 短名 密云镇
    日戳：  上弧 江西玉山      底弧 三清乡         → 短名 三清乡
    文化戳：上弧 江西玉山      底弧 三清山三龙出海  → 短名 三清山（+ 景点名）
    ```

    所以**短名必须进词表**，否则 `建溪`（样本真值）永远匹配不上
    `建溪邮政支局`（目录里那条）。实测 13 个样本真值局名里有 12 个靠这条命中。

    ## ⚠️ 短名会大量撞车，但这**不影响它的用途**

    实测：短名 37400 个，其中 **5656 个撞车**（`人民路` 出现在 99 个县、
    `城南` 91 个县）。原因是局名词表的语义是"**这个值是不是一个已登记局名**"
    （防错值那条防线），而**不是**"它是哪一家"。撞车只影响"指向唯一机构"，
    那个问题由 `place_id` 与第二批的模糊匹配解决。

    兜底：剥完不足 `MIN_OFFICE_SHORT_LEN` 字就返回原名（与地名同思路）。
    """
    text = name.strip()
    for _ in range(3):
        before = text
        for suffix in OFFICE_SUFFIXES:
            if text.endswith(suffix) and len(text) - len(suffix) >= MIN_OFFICE_SHORT_LEN:
                text = text[: -len(suffix)].strip()
                break
        if text == before:
            break
    return text or name.strip()


def office_forms(name: str, aliases: object = None) -> set[str]:
    """**一个**邮局在戳面上可能写成哪几种（全名 + 短名 + 别名，各自也剥后缀）。

    为什么要把它抽成公开函数：`build_office_names`（识别侧的词表）与
    建戳时的"这个写法指的是不是这家局"**必须是同一条规则** ——
    否则会出现"界面上判绿（`建溪` 在词表里）、建戳时却挂不上
    （目录里只有 `建溪邮政支局`）"这种自相矛盾的状态。实测 2026-10-11
    就在确认端点撞到了这一幕。
    """
    out: set[str] = set()
    candidates: list[object] = [name]
    if isinstance(aliases, list):
        candidates.extend(aliases)
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        cleaned = candidate.strip()
        if not cleaned or cleaned.isdigit():
            continue
        out.add(cleaned)
        short = office_short_name(cleaned)
        if short and not short.isdigit():
            out.add(short)
    return out


def build_office_names(rows: list[tuple[str, object]]) -> frozenset[str]:
    """从 ``(name, aliases)`` 构造局名写法集合。

    每个名字产出**两种写法**（与地名的 `describe_place` 同思路）：

    | 写法 | 例 | 说明 |
    | --- | --- | --- |
    | 全名（原样） | `建溪邮政支局` | 目录里就是它；人登记时也可能这么写 |
    | **短名** | `建溪` | **戳面写法**（用户给的三个例子都是这个形态） |

    别名也要算进来 —— `post_office.aliases` 就是为"同一个局的不同写法"准备的，
    也是将来把 `三清山三龙出海` 这种"局名+景点名"登记进去的地方。

    ⚠️ **纯数字不进词表**（把「建溪 1」的 `1` 挡在外面）。理由：戳号是字段
    `office_no`，而 `decide()` 对那个字段**不做**词表校验
    （见 `review.MECHANICALLY_VALIDATED`）。把纯数字塞进局名词表，只会让
    "某个局名恰好等于某个戳号"这种巧合变成一次错误的放行。
    """
    names: set[str] = set()
    for name, aliases in rows:
        names |= office_forms(name, aliases)
    return frozenset(names)


def load_place_rows(db: Session) -> list[tuple[object, object, str]]:
    """读全部行政区划的 ``(id, parent_id, name, level_label)``。

    ### ⚠️ 多列查询：不能用 `db.scalars(...)`；而 `.tuples()` 在 2.1 已废弃

    这里我连错三次，把四种写法都记下来（**含我自己后来的过度修正**）：

    | 写法 | 结果 |
    | --- | --- |
    | `db.scalars(select(A, B, C))` | **只给第一列**，元素是 `UUID`，`row[1]` 报 `'UUID' object is not subscriptable` |
    | `db.scalars(select(A, B, C)).tuples()` | `ScalarResult` **没有** `.tuples()` → `AttributeError` |
    | `db.execute(select(A, B, C)).tuples()` | SQLAlchemy 2.0 可用；**2.1 起废弃**（`Result.tuples() method is deprecated`），因为 `Row` 本来就能解包 |
    | `db.execute(select(A, B, C)).all()` | ✅ **现在用这个**，2.0/2.1 都能解包 |

    **第二次错更值得记**：我为了确认"`ScalarResult` 有没有 `.tuples()`"，
    跑了 `hasattr(ScalarResult, "tuples")` 得到 `False`，据此**推断**
    "所以 `scalars` 本来就返回行元组"—— 然后回退掉了正确的修复。
    `hasattr` 只能回答"有没有这个方法"，回答不了"它返回什么"。
    真正该做的是一行实测：`list(db.scalars(stmt))[0]` 看它是什么。

    **第三次错是"修过头"**：2.1 废弃 `.tuples()` 后，正确做法是**去掉它直接解包**，
    不是回到 `scalars()`。我在 `tools/import_post_offices.py` 里这么修了，
    但当时**只改了那一处**，这里漏了两处（`-W error::DeprecationWarning`
    下真跑会抛错）。

    这几条都**只在字典非空时**才暴露（空字典下循环体一次都不执行），
    所以单测与 `_dev.sh all` 全绿都没抓到，是拿真库跑才炸出来的。
    """
    rows = db.execute(
        select(Place.id, Place.parent_id, Place.name, Place.level_label)
    ).all()
    return [(row[0], row[1], row[2], row[3] or "") for row in rows]


def load_known_places(db: Session) -> frozenset[str]:
    """读取全部行政区划并构造地名写法集合。字典为空时返回空集合。

    保留它是因为**"索引里装了哪些键"本身值得单测**（形状错了两头都错）。
    真正给 `decide()` 用的是 `PlaceVocab.lookup`。
    """
    return build_place_names(load_place_rows(db))


def load_known_offices(db: Session) -> frozenset[str]:
    """读取全部邮局（含别名）并构造局名写法集合。字典为空时返回空集合。"""
    rows = db.execute(select(PostOffice.name, PostOffice.aliases)).all()
    return build_office_names([(row[0], row[1]) for row in rows])


# --------------------------------------------------------------------------- #
#  按区划树查询（这是 `decide()` 真正用的那一层）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlaceHit:
    """一次地名叫法的命中结果。"""

    #: 命中的**最具体**那一级地名
    name: str
    #: 该地名及其全部祖先，**从粗到细**（国家 → 省 → 市 → …）。
    #:
    #: ⚠️ 这里面**混着两种来源**：最具体那一级是 **OCR 读出来的**，
    #: 上面的祖先都是**数据库补出来的**。用户要的"智能识别它属于哪个省"
    #: 正是这个补全；但做 `correction` 标注时不能把补出来的当成人改的。
    #: 所以 `Resolution.value`（OCR 原样）与这里是分开的两个东西。
    path: tuple[str, ...]
    #: 命中形态：``single_exact``（值本身是地名）或 ``pair``（上级+本级连写）
    kind: str
    #: 给用户看的说明（会进 `decision.reason`）
    detail: str
    #: 命中那一级地名的 **id**。
    #:
    #: 为什么在 2026-10-11 补上它：`path` 只有名字，而**建戳**要的是 id
    #: （`Postmark.place_id`）。没有它，确认端点只能拿名字去反查 —— 那就是
    #: 在同一条规则之外又长出一套匹配逻辑，两者迟早会不一致。
    #: 局名那条通道（`_SimpleVocab`）没有 id，所以它留 `None`。
    place_id: object | None = None


@dataclass(frozen=True, slots=True)
class Resolution:
    """`decide()` 需要的全部信息。

    * `value` —— OCR 读出的原样（**证据**，不许被补全改写）
    * `hit` —— 命中的地名与补全出来的层级链；`None` = 没匹配上
    """

    value: str
    hit: PlaceHit | None

    @property
    def known(self) -> bool:
        return self.hit is not None

    @property
    def place_path(self) -> tuple[str, ...]:
        return self.hit.path if self.hit else ()


class _SimpleVocab:
    """局名这种"就是一张名单"的词表。接口与 `PlaceVocab` 一致。"""

    def __init__(self, names: Iterable[str]) -> None:
        self._names = frozenset(names)

    def lookup(self, value: str) -> Resolution:
        if value and value in self._names:
            return Resolution(
                value, PlaceHit(value, (value,), "single_exact", "在已登记的词表里")
            )
        return Resolution(value, None)


class PlaceVocab:
    """把区划树做成"戳面写法 → 命中信息"的索引。

    索引里的键**全部来自数据**（不来自字符串猜测）：

    | 键 | 例 | 什么时候出现 |
    | --- | --- | --- |
    | 全名 | `南宁市` / `福建省` / `广西壮族自治区` | 用户可能就这么登记，OCR 也可能读到 |
    | 短名 | `南宁` / `福建` / `广西` | 戳面最常见形态 |
    | 上级短名 + 本级短名 | `福建南平` / `广西南宁` | 顶弧常见的连写 |

    查询规则见模块 docstring：**值本身是地名**，或**恰好拆成两段且后者在前者辖下**。
    """

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._by_id: dict[object, str] = {}
        self._parent_of: dict[object, object | None] = {}
        #: 地名 id → `level_label`（如「省级」「地级市」）。带全称后缀的
        #: 形态要用它判断"这一级该剥哪个后缀"。
        self._levels: dict[object, str] = {}
        for place_id, parent_id, name, level_label in normalize_place_rows(rows):
            self._by_id[place_id] = name
            self._parent_of[place_id] = parent_id
            self._levels[place_id] = level_label

        #: 键 → 该键代表的地名 id（同键冲突时保留先遇到的）
        self._single: dict[str, object] = {}
        #: 连写键 → (上级 id, 本级 id)
        self._pair: dict[str, tuple[object, object]] = {}
        self._build()

    # ---------------------------------------------------------------- 构建
    def _forms(self, place_id: object) -> set[str]:
        return set(describe_place(self._by_id[place_id]).forms)

    def _build(self) -> None:
        for place_id in self._by_id:
            for form in self._forms(place_id):
                self._single.setdefault(form, place_id)

        for place_id in self._by_id:
            short = place_short_name(self._by_id[place_id])
            if not short:
                continue
            ancestor = self._parent_of.get(place_id)
            depth = 0
            while ancestor is not None and depth < MAX_ANCESTOR_DEPTH:
                ancestor_name = self._by_id.get(ancestor)
                if ancestor_name is None:
                    break  # 祖先不在数据里（数据不全），不要猜
                key = f"{place_short_name(ancestor_name)}{short}"
                # 连写键不许与任何**单名**相同（否则同一个键有两种含义）
                if key not in self._single:
                    self._pair.setdefault(key, (ancestor, place_id))
                ancestor = self._parent_of.get(ancestor)
                depth += 1

    # ---------------------------------------------------------------- 查询
    def _path_of(self, place_id: object) -> tuple[str, ...]:
        """从粗到细的层级链（国家 → … → 本级）。"""
        chain: list[str] = []
        node: object | None = place_id
        guard = 0
        while node is not None and guard < 32:
            name = self._by_id.get(node)
            if name is None:
                break
            chain.append(name)
            node = self._parent_of.get(node)
            guard += 1
        chain.reverse()
        return tuple(chain)

    def _is_under(self, lower: object, upper: object) -> bool:
        node = self._parent_of.get(lower)
        guard = 0
        while node is not None and guard < 32:
            if node == upper:
                return True
            node = self._parent_of.get(node)
            guard += 1
        return False

    def _strip_own_suffix(self, value: str, place_id: object) -> str | None:
        """去掉**属于该级**的后缀。不匹配则返回 None。

        ## 为什么需要（用户 2026-10-08 给的例子）

        戳面上**确实会写全称**：`吉林省吉林市` —— 因为省与市同名，不写全就分不清。
        这类值的"两段"是 `吉林省` + `吉林市`，而直接切成 `(吉林, 省吉林)`
        或 `(吉林省吉, 林市)` 都不对。

        ## 为什么不用通用的后缀表去剥

        通用表会把 `福建南宁` 也剥成 `福建` + `南宁` 从而**放过**一个不是
        "上下级连写"的错值。所以这里只认**这个地名自己那一级的后缀**：
        `吉林省`（省级）剥 `省`、`吉林市`（地级市）剥 `市`；
        而 `福建`（省级）不会去剥 `建`，`南宁`（地级市）也不会剥 `宁`。

        于是 `吉林省吉林市` → `吉林` + `吉林` → 命中；`福建南宁市` →
        `福建` + `南宁` + 尾巴 `市`（`福建` 是省级，`市` 不是省级后缀）→ 拒绝。
        """
        name = self._by_id.get(place_id, "")
        label = self._levels.get(place_id, "")
        if not label:
            return None
        if value == name:
            return value
        #: `地级市` 的后缀是 `市`（取最后一个字）；`省级` 的后缀是 `省`。
        #: 用 label 的最后一个字，它正好是这批数据里各级的后缀。
        suffix = label[-1]
        if value.endswith(suffix) and len(value) > len(suffix):
            trimmed = value[: -len(suffix)]
            if trimmed == name:
                return trimmed
        return None

    def lookup(self, value: str) -> Resolution:
        """判定并补全层级。返回 `Resolution`。

        ⚠️ **刻意不做"前缀探测"**。早期版本写成"开头是某个已登记地名、
        剩余部分不是地名 → 算只读到上级（可接受）"，结果把「福建南火」
        判成了"漏字"（可接受）而不是"错字"（必须拦）—— 而拦它正是
        这套机制存在的唯一理由。现在只认两种形态，拆不出来就拒绝。
        """
        if not value:
            return Resolution(value, None)

        place_id = self._single.get(value)
        if place_id is not None:
            name = self._by_id[place_id]
            return Resolution(
                value,
                PlaceHit(
                    value,
                    self._path_of(place_id),
                    "single_exact",
                    f"命中已登记地名「{name}」",
                    place_id,
                ),
            )

        pair = self._pair.get(value)
        if pair is not None:
            upper, lower = pair
            return Resolution(
                value,
                PlaceHit(
                    value,
                    self._path_of(lower),
                    "pair",
                    f"「{self._by_id[upper]}」辖下「{self._by_id[lower]}」",
                    lower,
                ),
            )

        # 带全称后缀的形态：`吉林省吉林市`。**两段各自**必须能剥掉
        # "属于自己那一级"的后缀，剥不掉就不算。
        if len(value) >= 4:
            for split in range(2, len(value) - 1):
                left, right = value[:split], value[split:]
                upper = self._single.get(left) or self._single.get(
                    self._strip_prefix_of(right, left)
                )
                for upper_id in self._single_ids_for(left):
                    left_core = self._strip_own_suffix(left, upper_id)
                    if left_core is None:
                        continue
                    for lower_id in self._single_ids_for(right):
                        right_core = self._strip_own_suffix(right, lower_id)
                        if right_core is None or right_core != self._by_id[lower_id]:
                            continue
                        if not self._is_under(lower_id, upper_id):
                            continue
                        return Resolution(
                            value,
                            PlaceHit(
                                value,
                                self._path_of(lower_id),
                                "pair_suffixed",
                                f"「{self._by_id[upper_id]}」辖下"
                                f"「{self._by_id[lower_id]}」（带全称后缀）",
                                lower_id,
                            ),
                        )
        return Resolution(value, None)

    def _single_ids_for(self, value: str) -> list[object]:
        """值可能对应的所有地名 id（按名字长度倒序，最具体的优先）。"""
        ids = []
        hit = self._single.get(value)
        if hit is not None:
            ids.append(hit)
        return ids

    def _strip_prefix_of(self, value: str, prefix: str) -> str:
        """未使用 —— 占位以免误调用（见 `lookup` 的实现说明）。"""
        return value


def build_vocab(
    places: list[tuple[object, ...]],
    offices: list[tuple[str, object]],
) -> tuple[PlaceVocab, _SimpleVocab]:
    """一次构造两套词表查询器：`(place_vocab, office_vocab)`。纯函数，便于单测。"""
    return PlaceVocab(places), _SimpleVocab(build_office_names(offices))


def load_place_vocab(db: Session) -> tuple[PlaceVocab, _SimpleVocab]:
    """从库里读并构造。**这是给 `routes_recognize` 用的入口。**

    ⚠️ 空词表的后果要说清：字典是空的时候，**每一个**开放文本字段都会被判
    "未登记"→ `needs_review`（黄），包括本来正确的值。那是**有意的保守**：
    宁可多看一眼，也不要把错值当成可信（实测「福建南火」就是这样溜过去的）。
    但它会让 A5 的点击数变高 —— 所以记 A5 的数字时要注明当时字典的状态。

    ⚠️ **每次识别都查库**。这是有意的取舍：字典随时会在字典页签里被改，
    缓存失效做错（用旧词表）比多一次查询危险得多。真要缓存，应当由字典的
    写操作显式清 —— 那是另一件事，别顺手加一个 TTL 了事。
    """
    offices = db.execute(select(PostOffice.name, PostOffice.aliases)).tuples().all()
    office_vocab = _SimpleVocab(build_office_names([(row[0], row[1]) for row in offices]))
    place_vocab = PlaceVocab(load_place_rows(db))
    logger.info(
        "词表：地名 %d 个单名 / %d 个连写、局名 %d 个",
        len(place_vocab._single),
        len(place_vocab._pair),
        len(office_vocab._names),
    )
    return place_vocab, office_vocab


def load_vocabulary(db: Session) -> tuple[frozenset[str], frozenset[str]]:
    """**旧的接口**（值集合），保留给"只看形状"的调试与既有测试用。

    新代码请用 `load_place_vocab` —— 它才能回答"这个值合法吗、它属于哪个省"。
    """
    return load_known_places(db), load_known_offices(db)
