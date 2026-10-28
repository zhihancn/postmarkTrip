"""网点列表导入的解析测试（`tools/import_post_offices.py`）。

## 为什么这组测试的形态与上一版完全不同

上一版读的是**国家邮政局平台**的 HTML，那里 15.3% 的名字带
`中国邮政集团有限公司…` 前缀，所以需要一整套"剥前缀 + 剥省市县"的规则 ——
而那套规则我**在真数据上错了两次**：

| 错法 | 症状 | 规模 |
| --- | --- | --- |
| 用短名做**子串前缀**匹配 | `芜湖路邮政支局` → `路邮政支局` | 750 条后缀残留 |
| 只在"有公司前缀时"才剥地名 | `枞阳县城关第三邮政所` 不剥 | 17252 条（31.6%）仍带地名 |

用户 2026-10-11 换成了**中国邮政官网那份**，名字本来就干净（`三清山邮政所`）。
于是**那整套规则连同它的坑一起不需要了** —— 这是本组测试比上一版短得多的
真正原因，不是"测试写少了"。

## 这一版要盯的东西变了两处

* **状态标记**（`（暂停）`/`（撤销）`）要进 `note` 而**不能进 `name`** ——
  它描述的是"这个所现在开不开"，不是它的名字
* **备注是目录原始信息的落点**，拼装规则要可测（否则将来没人知道
  `note` 里那串分号分隔的东西是怎么来的）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from import_post_offices import (  # noqa: E402
    STATUS_RE,
    OfficeRow,
    _clean,
    parse_csv,
)

HEADER = "省,市,县,服务网点名称,邮编,地址,是否办理金融业务,电话,营业时间"


def _write(tmp_path: Path, *lines: str) -> Path:
    p = tmp_path / "网点.csv"
    p.write_text("\n".join([HEADER, *lines]) + "\n", encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
#  名称：**不剥、不规范化**
# --------------------------------------------------------------------------- #


def test_name_is_kept_verbatim(tmp_path: Path) -> None:
    """名称原样保留 —— 这份数据的价值就在于"已经干净"。

    实测名字里绝大多数不带省市县前缀（`三清山邮政所`）。**不做任何剥离**：
    `芜湖路` 会被 `芜湖` 吃掉、`包河苑` 会被 `包河` 吃掉 —— 这两个坑我都踩过。
    """
    f = _write(
        tmp_path,
        "江西省,上饶市,玉山县,三清山邮政所,334717,江西省上饶市三清乡风景名胜区,否,0793-x,08:00-17:00",
        "安徽省,芜湖市,湾沚区,芜湖路邮政支局,241100,安徽省芜湖市湾沚区,是,0553-x,08:00-17:00",
    )
    rows = parse_csv(f).rows
    assert [r.name for r in rows] == ["三清山邮政所", "芜湖路邮政支局"]


def test_place_prefixed_names_are_also_left_alone(tmp_path: Path) -> None:
    """极少数名字确实带地名（`合肥市王大郢邮政所`）—— **也原样保留**。

    这类切不切是个判断题，而在这条管线上猜错的代价（切坏真名）比收益大。
    留给将来的模糊匹配处理，不在这里猜。
    """
    f = _write(
        tmp_path,
        "安徽省,合肥市,包河区,合肥市王大郢邮政所,230041,安徽省合肥市包河区,是,0551-x,08:00-17:00",
    )
    assert parse_csv(f).rows[0].name == "合肥市王大郢邮政所"


# --------------------------------------------------------------------------- #
#  状态标记
# --------------------------------------------------------------------------- #


def test_status_marker_goes_to_note_not_name(tmp_path: Path) -> None:
    """`（暂停）`/`（撤销）` 是**状态**，要进 `note`，不能进 `name`。

    实测这批数据里有 384 条 `（暂停）`、4 条 `（撤销）`。若留在 `name` 里，
    它就成了名字的一部分 —— 而"这个所暂停营业"与"这个所叫什么"是两件事，
    且状态会变（名字不会）。
    """
    f = _write(
        tmp_path,
        "上海市,徐汇区,徐汇区,康乐邮政所（暂停）,200030,上海市徐汇区,是,021-x,08:00-17:00",
        "上海市,浦东新区,浦东新区,某邮政所（撤销）,200120,上海市浦东新区,否,021-y,08:00-17:00",
    )
    rows = parse_csv(f).rows
    assert [r.name for r in rows] == ["康乐邮政所", "某邮政所"]
    assert [r.status for r in rows] == ["暂停", "撤销"]
    assert "目录状态：暂停" in rows[0].note


def test_halfwidth_parentheses_also_recognised(tmp_path: Path) -> None:
    """半角括号也要认（数据里两种都可能出现）。"""
    f = _write(
        tmp_path,
        "上海市,徐汇区,徐汇区,康乐邮政所(暂停),200030,上海市徐汇区,是,021-x,08:00-17:00",
    )
    assert parse_csv(f).rows[0].status == "暂停"


def test_ordinary_parentheses_are_not_treated_as_status(tmp_path: Path) -> None:
    """普通括号内容（不是状态词）不该被剥掉。

    这条挡的是"凡是括号都当状态"的过度匹配。
    """
    f = _write(
        tmp_path,
        "北京市,海淀区,海淀区,某大学（东区）邮政所,100084,北京市海淀区,否,010-x,08:00-17:00",
    )
    row = parse_csv(f).rows[0]
    assert row.name == "某大学（东区）邮政所"
    assert row.status == ""


def test_status_regex_lists_only_real_status_words() -> None:
    for word in ("暂停", "撤销", "停业", "装修", "筹建"):
        assert STATUS_RE.search(f"某某邮政所（{word}）"), word
    for not_status in ("东区", "老所", "1", "临时"):
        assert not STATUS_RE.search(f"某某邮政所（{not_status}）"), not_status


# --------------------------------------------------------------------------- #
#  清洗
# --------------------------------------------------------------------------- #


def test_fullwidth_and_ascii_spaces_are_trimmed(tmp_path: Path) -> None:
    """首尾空白要清，**含全角空格**。

    实测 CSV 里有 3 行名称末尾带 `\\u3000`（全角空格）——
    而 XLSX 里那 3 行没有，这是两份导出唯一的不一致。
    """
    assert _clean("　五原西路邮政支局　") == "五原西路邮政支局"
    assert _clean("  五原西路邮政支局  ") == "五原西路邮政支局"
    assert _clean(None) == ""
    f = _write(
        tmp_path,
        "河南省,三门峡市,湖滨区,五原西路邮政支局　,472000,河南省三门峡市,是,0398-x,08:00-17:00",
    )
    assert parse_csv(f).rows[0].name == "五原西路邮政支局"


def test_missing_required_column_is_reported_not_guessed(tmp_path: Path) -> None:
    """缺必需列就报错返回，**不猜列**（猜列会把整列数据错位）。"""
    p = tmp_path / "bad.csv"
    p.write_text("省,市,场所名称\n安徽省,安庆市,秀山邮政所\n", encoding="utf-8")
    rep = parse_csv(p)
    assert rep.rows == []
    assert rep.problems and "缺少必需列" in rep.problems[0]


def test_row_without_name_is_reported(tmp_path: Path) -> None:
    f = _write(tmp_path, "安徽省,安庆市,怀宁县,,246100,安徽省安庆市,是,0556-x,08:00-17:00")
    rep = parse_csv(f)
    assert rep.rows == []
    assert any("没有网点名称" in p for p in rep.problems)


# --------------------------------------------------------------------------- #
#  备注拼装
# --------------------------------------------------------------------------- #


def test_note_collects_the_directory_facts_in_a_stable_order() -> None:
    """备注是**目录原始信息**的落点，拼接顺序固定（否则无法测、无法比）。

    注意 `note` **不含任何判断** —— 只有"目录里写了什么"。
    """
    row = OfficeRow(
        province="江西省", city="上饶市", county="玉山县",
        name="三清山邮政所", postal_code="334717",
        address="江西省上饶市三清乡风景名胜区枫林镇枫林村枫林街",
        finance="否", phone="0793-x", hours="08:00-17:00",
        raw_name="三清山邮政所", status="",
    )
    note = row.note
    assert note.index("地址：") < note.index("邮编：")
    assert note.index("邮编：") < note.index("电话：")
    assert "334717" in note and "江西省上饶市三清乡风景名胜区" in note
    assert "目录状态" not in note, "没有状态时不该出现这一项"


def test_note_is_empty_when_the_directory_gives_nothing() -> None:
    row = OfficeRow(
        province="", city="", county="", name="某邮政所", postal_code="",
        address="", finance="", phone="", hours="", raw_name="某邮政所", status="",
    )
    assert row.note == ""
