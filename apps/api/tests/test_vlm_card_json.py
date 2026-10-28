"""整卡一次云端调用的解析测试（`vlm.parse_card_json`）。

## 为什么这组必须有

整卡调用是**每次要花钱**的路径，而它的输出是**模型的自由文本**。
解析一失败就丢掉整份结果 —— 那份结果值一次 API 调用。所以容错要宽，
但"宽"到什么程度必须**钉住**，否则下一次有人收紧它会静默丢数据。

实测会出现的包装形式（都来自真调用或常见模型行为）：
  * markdown 代码块围栏
  * JSON 前后带解释
  * 单引号、尾随逗号
  * 把"没有"写成 `无` / `null` / `?`
"""

from __future__ import annotations

from postmarktrip.recognition.vlm import parse_card_json


def test_plain_json() -> None:
    got = parse_card_json(
        '{"type": "daily", "date": "2026-09-25", "place": "福建南平", '
        '"office": "建溪", "office_no": "1"}'
    )
    assert got == {
        "type": "daily",
        "date": "2026-09-25",
        "place": "福建南平",
        "office": "建溪",
        "office_no": "1",
    }


def test_markdown_fenced_json() -> None:
    """模型很爱加 ```json 围栏。"""
    got = parse_card_json(
        '好的，结果如下：\n```json\n{"type": "scenic", "place": "江西玉山"}\n```\n'
    )
    assert got["type"] == "scenic"
    assert got["place"] == "江西玉山"


def test_json_surrounded_by_prose() -> None:
    """前后带解释文字 —— 取最外层花括号即可。"""
    got = parse_card_json('分析：这是一枚日戳。\n{"office": "四号航站楼"}\n希望有帮助。')
    assert got["office"] == "四号航站楼"


def test_single_quotes_and_trailing_comma() -> None:
    """单引号 + 尾随逗号是常见瑕疵，不该因此丢掉整份结果。"""
    got = parse_card_json("{'type': 'daily', 'office': '建溪',}")
    assert got["type"] == "daily"
    assert got["office"] == "建溪"


def test_empty_markers_become_empty_strings_not_missing_keys() -> None:
    """模型把"没有"写成这些词时，一律当**空串**（而不是当成一个值）。

    这条很重要：`office: "无"` 若原样进决策层，会变成"局名是『无』"这个荒谬结论。
    """
    for marker in ("无", "null", "None", "N/A", "-", "?"):
        got = parse_card_json(f'{{"office": "{marker}"}}')
        assert got["office"] == "", marker


def test_missing_keys_are_absent_from_the_result() -> None:
    """**缺的键不出现**，而不是给空串 —— 与 `ParsedFields` 的约定一致。

    调用方据此区分"云端说这个字段是空的"与"云端没提这个字段"。
    """
    got = parse_card_json('{"type": "daily", "date": "2026-09-25"}')
    assert set(got) == {"type", "date"}
    assert "place" not in got
    assert "office" not in got


def test_extra_keys_are_dropped() -> None:
    """只取我们认识的键（模型常自作主张加 `confidence`/`box` 之类）。"""
    got = parse_card_json(
        '{"type": "daily", "confidence": 0.9, "box": {"x": 1}, "place": "福建厦门"}'
    )
    assert set(got) == {"type", "place"}


def test_unparseable_returns_empty_dict() -> None:
    """真的解析不出来才放弃 —— 这时返回空字典，调用方沿用本地结果。"""
    assert parse_card_json("") == {}
    assert parse_card_json("我看不出这是一枚邮戳") == {}
    assert parse_card_json("{不是合法的 json") == {}
    assert parse_card_json("[1, 2, 3]") == {}  # 顶层不是对象


def test_non_string_values_are_stringified() -> None:
    """模型可能把戳号写成数字（`"office_no": 1`），不该因此丢掉它。"""
    got = parse_card_json('{"office_no": 1, "place": 2}')
    assert got["office_no"] == "1"
    assert got["place"] == "2"


def test_scenic_card_with_empty_place() -> None:
    """实测过的真实形态：风景戳那张 `place`/`office` 都读不到。

    **不能把它当成"解析失败"** —— 它是有效结果（"这枚戳上没有那些字段"）。
    """
    got = parse_card_json(
        '{"type": "scenic", "date": "2026-10-01", "place": "", "office": "", "office_no": ""}'
    )
    assert got["type"] == "scenic"
    assert got["place"] == ""
    assert got["date"] == "2026-10-01"
