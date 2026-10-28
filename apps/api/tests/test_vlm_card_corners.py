"""整页角点解析的测试（`vlm.parse_card_corners`）。

## 为什么这组必须有

B 方案（云端几何兜底）**只在本地几何可疑时**才走这条路 —— 也就是说它平时
不跑。那么"它到底能不能用"就完全靠测试钉住：几何这类东西"看起来对"最不可靠
（一张镜像/扭转的卡图看起来仍然是一张卡）。

这里用的回复文本都按**实测形态**写：2026-10-13 在 `更多样本/001.jpg` 上问
`PROMPT_READ_CARD_CORNERS`，它回的是 9 条 `{"card": n, "corners": [[x,y]...]}`，
其中第三列的 x 到 **1.001**（越出画面）—— 那条越界值正是兜底要用的东西，
所以它**必须被保留**，不能在解析这层被夹掉。
"""

from __future__ import annotations

from postmarktrip.recognition.vlm import (
    CORNERS_UNIT,
    PROMPT_READ_CARD_CORNERS,
    corners_prompt,
    parse_card_corners,
)


def test_plain_prompt_shaped_reply() -> None:
    """prompt 要的形状：`[{"card": n, "corners": [[x,y] x4]}, ...]`。"""
    got = parse_card_corners(
        '[{"card": 1, "corners": [[0.002,0.002],[0.243,0.002],'
        "[0.243,0.302],[0.002,0.302]]},"
        ' {"card": 2, "corners": [[0.373,0.002],[0.622,0.002],'
        "[0.622,0.302],[0.373,0.302]]}]"
    )
    assert len(got) == 2
    assert got[0][0] == (0.002, 0.002)
    assert got[0][2] == (0.243, 0.302)
    assert got[1][0] == (0.373, 0.002)


def test_order_is_preserved_as_given() -> None:
    """解析**不重排** —— 重排是 `split.order_quad` 的职责（一处规则只写一遍）。

    这里给一份"从右上开始"的角点，断言解析**原样**返回。
    """
    got = parse_card_corners('[{"card": 1, "corners": [[0.9,0.1],[0.1,0.1],[0.1,0.9],[0.9,0.9]]}]')
    assert got[0][0] == (0.9, 0.1)


def test_out_of_frame_corners_survive_parsing() -> None:
    """⚠️ **越界角点必须活过解析** —— 这是 B 方案的立足点。

    实测 `001.jpg` 第三列回的 x=1.001（画面外）。若这里把它夹到 1.0，
    兜底就退化成"把同一个残缺形状又交回来一次"，而症状只是"没帮上忙"。
    """
    got = parse_card_corners(
        '[{"card": 9, "corners": [[0.759,0.710],[1.001,0.710],'
        "[1.001,0.999],[0.759,0.999]]},"
        ' {"card": 10, "corners": [[-0.02,0.10],[0.20,0.10],'
        "[0.20,0.50],[-0.02,0.50]]}]"
    )
    assert got[0][1] == (1.001, 0.710)
    assert got[1][0] == (-0.02, 0.10)
    assert got[1][3] == (-0.02, 0.50)


def test_markdown_fence_and_prose() -> None:
    """模型爱加围栏与解释 —— 不该因此丢掉整份结果。"""
    got = parse_card_corners(
        "好的，结果如下：\n```json\n"
        '[{"card": 1, "corners": [[0,0],[1,0],[1,1],[0,1]]}]\n'
        "```\n希望有帮助。"
    )
    assert len(got) == 1
    assert got[0][2] == (1, 1)


def test_single_quotes_and_trailing_comma() -> None:
    got = parse_card_corners(
        "[{'card': 1, 'corners': [[0,0],[1,0],[1,1],[0,1],]},"  # 内层尾逗号
        "]"  # 外层合法
    )
    assert len(got) == 1


def test_flat_eight_numbers() -> None:
    """模型有时摊平成一维 8 个数 —— 认它，因为形状是稳定的。"""
    got = parse_card_corners("[[0.1,0.2,0.3,0.2,0.3,0.9,0.1,0.9]]")
    assert got[0] == ((0.1, 0.2), (0.3, 0.2), (0.3, 0.9), (0.1, 0.9))


def test_alternate_key_names() -> None:
    """`corners` / `points` / `quad` 都是同一件事；key 名飘忽是常态。

    ⚠️ 这正是 `docs/ok/16` §11.3 那个教训的正解：那次是**按 key 白名单取数**
    而白名单漏了 key → 云端读到的值被静默丢弃。这里改成"先看形状、再认 key"。
    """
    for key in ("corners", "points", "quad"):
        got = parse_card_corners(f'[{{"card": 1, "{key}": [[0,0],[1,0],[1,1],[0,1]]}}]')
        assert got[0] == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)), key


def test_incomplete_or_broken_entries_are_dropped_not_patched() -> None:
    """**四角缺一就丢这张卡** —— 绝不替它补一个角。

    补出来的角会产出一张"看起来是卡、实际是错的"的透视变换，比漏一张有害。
    """
    got = parse_card_corners(
        '[{"card": 1, "corners": [[0,0],[1,0],[1,1]]},'  # 只有三个角
        ' {"card": 2, "corners": "整张卡"},'  # 根本不是坐标
        ' {"card": 3, "corners": [[0,0],[1,0],[1,1],[0,1]]}]'  # 这条好的
    )
    assert len(got) == 1
    assert got[0] == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


def test_non_finite_values_are_dropped() -> None:
    """NaN/inf 必须在**解析这一层**挡掉：它们会让裁剪与 IoU 产生静默的乱结果。"""
    got = parse_card_corners(
        '[{"card": 1, "corners": [[0,0],[NaN,0],[1,1],[0,1]]},'
        ' {"card": 2, "corners": [[0,0],[Infinity,0],[1,1],[0,1]]},'
        ' {"card": 3, "corners": [[0,0],[1,0],[1,1],[0,1]]}]'
    )
    assert len(got) == 1


def test_string_numbers_are_accepted() -> None:
    """模型会把数字写成字符串（`"0.24"`）—— 不该因此丢掉这张卡。"""
    got = parse_card_corners(
        '[{"card": 1, "corners": [["0.24","0.3"],["0.6","0.3"],["0.6","0.7"],["0.24","0.7"]]}]'
    )
    assert got[0][0] == (0.24, 0.3)


def test_empty_and_unparseable_return_empty_list() -> None:
    """真的解析不出来才放弃（返回空表 → 调用方沿用本地几何）。"""
    assert parse_card_corners("") == []
    assert parse_card_corners("我看不出这里有卡") == []
    assert parse_card_corners("[不是合法的 json") == []
    assert parse_card_corners('{"card": 1}') == []  # 没有方括号数组
    assert parse_card_corners("[]") == []


def test_the_prompt_declares_the_unit_the_parser_assumes() -> None:
    """**提示词与解析器的单位约定必须写在两处且互相印证。**

    `CORNERS_UNIT` 是给调用方换算用的常量。若哪天有人把 prompt 改成"给像素"
    而没改这个常量，换算就会整体错一档 —— 而症状是"模型给歪了"，
    不是"单位错了"。所以这里用一条断言把两者钉在一起。
    """
    assert CORNERS_UNIT == "fraction"
    assert "比例" in PROMPT_READ_CARD_CORNERS
    assert "(0,0)" in PROMPT_READ_CARD_CORNERS
    assert "(1,1)" in PROMPT_READ_CARD_CORNERS


def test_the_prompt_allows_out_of_frame_corners() -> None:
    """越界角点必须被**明确允许**：否则模型只回看得见的部分，
    而"被画面边缘切掉的卡"正是本地漏切最多的那一类（实测 001 底行三张）。"""
    assert "画面外" in PROMPT_READ_CARD_CORNERS
    assert "负数" in PROMPT_READ_CARD_CORNERS or "大于 1" in PROMPT_READ_CARD_CORNERS


def test_the_prompt_forbids_inventing_cards() -> None:
    """同时必须**禁止编卡** —— "允许外推边"与"允许造卡"是两件事。"""
    assert "不要凭空造" in PROMPT_READ_CARD_CORNERS


# --------------------------------------------------------------------------- #
#  corners_prompt：答少了才加的那句「还差 N 张」
# --------------------------------------------------------------------------- #


def test_no_hint_by_default() -> None:
    """没缺就不加提示 —— 提示是纠正"答少了"的手段，不是常态。"""
    assert corners_prompt() == PROMPT_READ_CARD_CORNERS
    assert corners_prompt(expected=9, missed=0) == PROMPT_READ_CARD_CORNERS
    assert corners_prompt(expected=0, missed=3) == PROMPT_READ_CARD_CORNERS


def test_hint_states_the_gap_and_keeps_the_no_invention_rule() -> None:
    """提示里必须同时有**缺口数字**与**不许编**这两件事。

    只有前者会诱导它凑数；只有后者不给它任何额外线索（实测 `001.jpg`
    同提示重问仍会漏同一张）。

    ⚠️ "不许编"这一句是提示**自己**的措辞（"不要编"），与原始 prompt 的
    "不要凭空造"不同 —— 两者都要能看到，因为它们各自出现在不同的分支里。
    """
    got = corners_prompt(expected=9, missed=1)
    assert got.startswith(PROMPT_READ_CARD_CORNERS)
    assert "共有 9 张" in got
    assert "还差 1 张" in got
    assert "不要编" in got
    assert "只给真实存在的卡" in got
