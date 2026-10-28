"""`domain/postmark_assembly.py` 的单测（`docs/ok/16` §五）。

这一层是"确认时建戳"的全部规则所在，而它的错**不会崩**：只会把 `place_id`
挂到隔壁县、或把原文写错。所以逐条钉：

1. **字段映射完备性** —— 新增一个戳面字段却忘了落列 = 静默丢数据
2. **日期/时码** —— 原文里带时（`.09`）时不能把它当日期
3. **局名只在 place 子树内匹配**（用户 2026-10-11 明确的要求）——
   实测「人民路」出现在 104 个县，全局匹配必然挂错
4. **主题邮局判据** —— `inscription` 与 `office` 两处都判
"""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

from postmarktrip.db.models import Place, PostOffice
from postmarktrip.domain import postmark_assembly as pa

# --------------------------------------------------------------------------- #
#  字段映射的完备性
# --------------------------------------------------------------------------- #

def test_field_mapping_covers_every_postmark_field_from_the_stamp() -> None:
    """哪些戳面字段进哪个列，必须**恰好**是这张表。

    两边都断言（多一个、少一个都要人来改这条测试），因为"少一个"意味着
    那个人确认过的值悄悄没落库 —— 而这种故障没有任何报错。
    """
    assert set(pa.FIELD_TO_POSTMARK_COLUMN) == {
        "postmark_type",
        "inscription",
        "date",
        "date_text",
        "time_code",
        "office_no",
    }
    assert set(pa.FIELD_TO_POSTMARK_COLUMN.values()) == {
        "postmark_type",
        "inscription",
        "postmark_date",
        "postmark_date_text",
        "time_code",
        "postmark_no",
    }


def test_values_come_from_the_confirmed_map() -> None:
    values = pa.postmark_values_from(
        {
            "postmark_type": "scenic",
            "inscription": "三清山三龙出海",
            "date": "2026-10-01 09:00",
            "date_text": "2026.10.01.09",
            "time_code": "09",
            "office_no": "1",
        }
    )
    assert values["postmark_type"] == "scenic"
    assert values["inscription"] == "三清山三龙出海"
    assert values["postmark_date"] == date(2026, 10, 1)
    assert values["postmark_date_text"] == "2026.10.01.09"
    assert values["time_code"] == "09"
    assert values["postmark_no"] == "1"
    assert values["provenance"] == "real_scan"


def test_empty_optional_values_become_none_not_empty_string() -> None:
    """空串 → `None`。列可空，而"空串"在库里与"没这个信息"分不开。"""
    values = pa.postmark_values_from({"inscription": "建溪1"})
    assert values["postmark_date"] is None
    assert values["postmark_date_text"] is None
    assert values["time_code"] is None
    assert values["postmark_no"] is None


def test_bad_or_missing_type_falls_back_to_other() -> None:
    assert pa.postmark_values_from({})["postmark_type"] == "other"
    assert pa.postmark_values_from({"postmark_type": "日戳"})["postmark_type"] == "other"


def test_unparsable_date_is_none_not_a_guess() -> None:
    values = pa.postmark_values_from({"date": "看不清"})
    assert values["postmark_date"] is None


# --------------------------------------------------------------------------- #
#  主题邮局
# --------------------------------------------------------------------------- #


def test_theme_marker_in_inscription_or_office() -> None:
    assert pa.looks_like_theme_office("晋祠天龙山主题邮局1", "")
    # 实测模型有时把它放进 office 而不是原文 —— 两处都判（用户同意）
    assert pa.looks_like_theme_office("", "三清山主题邮局")
    assert not pa.looks_like_theme_office("建溪1", "建溪")
    assert not pa.looks_like_theme_office("", None)


# --------------------------------------------------------------------------- #
#  局名解析：**只在 place 子树内**（这条是用户明确要求的）
# --------------------------------------------------------------------------- #


class _Row:
    """`db.execute(select(PostOffice.id, PostOffice.name, PostOffice.aliases))` 的返回行。

    真 SQLAlchemy 会给 `Row`，它**支持按列名取属性**（`row.name`）——
    所以这里也用属性而不是元组，免得替身比生产更宽松。
    """

    def __init__(self, id: uuid.UUID, name: str, aliases: list[str] | None = None) -> None:
        self.id = id
        self.name = name
        self.aliases = aliases or []


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _OfficeSession:
    """只回答两件事：`get(Place, id)` 与那句子树查询。"""

    def __init__(self, *, place: Any, rows: list[Any]) -> None:
        self.place = place
        self.rows = rows
        self.statements: list[Any] = []

    def get(self, entity: Any, ident: Any) -> Any:
        if entity is Place and self.place is not None and ident == self.place.id:
            return self.place
        return None

    def execute(self, stmt: Any) -> _Rows:
        self.statements.append(stmt)
        return _Rows(self.rows)


def _fake_place() -> Any:
    return type("P", (), {"id": uuid.uuid4(), "name": "玉山县", "path": "/CN/prov/city/county/"})()


def test_office_without_place_is_never_matched_globally() -> None:
    """地点没解析出来 → **不做全局匹配**，连查询都不发。

    这是用户 2026-10-11 的要求（【建议1】）。理由是可实测的：
    「人民路」出现在 **104** 个县（`ok/14-B` §8.1 记的是 99，更多）。
    """
    session = _OfficeSession(place=None, rows=[])
    office_id, note = pa.resolve_office_id(session, "建溪", None)  # type: ignore[arg-type]
    assert office_id is None
    assert "不做全局匹配" in note
    assert session.statements == [], "不该发任何查询 —— 全局匹配正是要避免的那件事"


def test_subtree_query_is_scoped_by_place_path() -> None:
    """查询必须**带着子树条件**（`place.path LIKE <前缀>%`）。

    只断言"返回对了"不够：全局查询在单行数据上也会返回对的那一行 ——
    错的是**在真实数据上**会挂到隔壁县。所以这里直接看 SQL 有没有子树约束。
    """
    place = _fake_place()
    session = _OfficeSession(place=place, rows=[_Row(uuid.uuid4(), "三清山邮政所")])
    pa.resolve_office_id(session, "三清山", place.id)  # type: ignore[arg-type]
    sql = str(session.statements[0]).lower()
    assert "place.path like" in sql, f"缺少子树条件：{sql}"
    assert "post_office.place_id in" in sql, f"缺少子树过滤：{sql}"


def test_unique_hit_inside_subtree_is_attached() -> None:
    place = _fake_place()
    office_id = uuid.uuid4()
    session = _OfficeSession(place=place, rows=[_Row(office_id, "三清山邮政所")])
    got, note = pa.resolve_office_id(session, "三清山", place.id)  # type: ignore[arg-type]
    assert got == office_id
    assert "唯一" in note


def test_stamp_short_name_matches_the_directory_full_name() -> None:
    """戳面写**短名**（`建溪`），目录里存**全名**（`建溪邮政支局`）—— 必须挂上。

    这是 2026-10-11 端到端跑出来的真 bug：识别侧词表把 `建溪` 认成"已登记局名"
    （界面判绿），而建戳时按**精确名**匹配挂不上 —— 同一个值两套规则，
    于是"界面说认得出、库里挂不上"。修法是两边共用 `vocab.office_forms`。
    """
    place = _fake_place()
    office_id = uuid.uuid4()
    session = _OfficeSession(place=place, rows=[_Row(office_id, "建溪邮政支局")])
    got, note = pa.resolve_office_id(session, "建溪", place.id)  # type: ignore[arg-type]
    assert got == office_id, f"短名没匹配上全名：{note}"
    assert "建溪邮政支局" in note


def test_alias_also_matches() -> None:
    """别名同理（`post_office.aliases` 就是为"同一局的不同写法"准备的）。"""
    place = _fake_place()
    office_id = uuid.uuid4()
    session = _OfficeSession(
        place=place, rows=[_Row(office_id, "三清山邮政所", ["三清山三龙出海"])]
    )
    got, _ = pa.resolve_office_id(session, "三清山三龙出海", place.id)  # type: ignore[arg-type]
    assert got == office_id


def test_no_hit_leaves_it_empty_and_says_why() -> None:
    place = _fake_place()
    session = _OfficeSession(place=place, rows=[])
    got, note = pa.resolve_office_id(session, "三清山主题邮局", place.id)  # type: ignore[arg-type]
    assert got is None
    assert "留空" in note and "正确" in note, "名录里没有的机构名，留空是正确结果"


def test_duplicate_names_are_not_guessed() -> None:
    """命中多家同名 → **不挂**（宁可留空，也不硬塞一个错的）。"""
    place = _fake_place()
    rows = [_Row(uuid.uuid4(), "人民路邮政所"), _Row(uuid.uuid4(), "人民路邮政支局")]
    session = _OfficeSession(place=place, rows=rows)
    got, note = pa.resolve_office_id(session, "人民路", place.id)  # type: ignore[arg-type]
    assert got is None
    assert "命中多家同名" in note
    assert "人民路邮政所" in note, "要把撞名的是哪几家说出来，人才知道怎么办"


def test_post_office_model_really_has_the_columns_we_filter_on() -> None:
    """我们按 `name` / `aliases` 精确匹配 —— 这两个列必须真的存在。

    这条是防"表改了、解析还按老列查"（那会静默查不到，然后留空）。
    """
    assert hasattr(PostOffice, "name")
    assert hasattr(PostOffice, "aliases")
    assert hasattr(PostOffice, "place_id")


# --------------------------------------------------------------------------- #
#  `resolve_card_links`：识别页显示 + 确认落库的**同一条规则**（ok/18 §3.3）
# --------------------------------------------------------------------------- #


class _PlaceVocabStub:
    """只回答 `lookup(text)` —— 与 `PlaceVocab` 对外的形状一致。"""

    def __init__(self, hits: dict[str, tuple[uuid.UUID, tuple[str, ...]]]) -> None:
        self._hits = hits

    def lookup(self, text: str) -> Any:
        got = self._hits.get(text)
        if got is None:
            return SimpleNamespace(known=False, hit=None)
        pid, path = got
        return SimpleNamespace(known=True, hit=SimpleNamespace(place_id=pid, path=path))


class _LinksSession(_OfficeSession):
    """加一个能力：`get(PostOffice, id)`（补显示用的局名）。"""

    def __init__(self, *, place: Any, rows: list[Any], offices: dict[Any, Any]) -> None:
        super().__init__(place=place, rows=rows)
        self._offices = offices

    def get(self, entity: Any, ident: Any) -> Any:
        if entity is PostOffice:
            return self._offices.get(ident)
        return super().get(entity, ident)


def test_resolve_card_links_returns_ids_names_and_a_note() -> None:
    """一次调用给出：要挂的 id、给人看的名字、以及一句人话说明。"""
    place = _fake_place()
    office_id = uuid.uuid4()
    office_place = SimpleNamespace(id=place.id, name="玉山县")
    office = SimpleNamespace(id=office_id, name="三清山邮政所", place_id=place.id)
    session = _LinksSession(place=place, rows=[_Row(office_id, "三清山邮政所")], offices={office_id: office})
    vocab = _PlaceVocabStub({"江西玉山": (place.id, ("中华人民共和国", "江西省", "玉山县"))})

    links = pa.resolve_card_links(
        session, vocab, place_text="江西玉山", office_text="三清山"  # type: ignore[arg-type]
    )
    assert links.place_id == place.id and links.place_name == "玉山县"
    assert links.office_id == office_id and links.office_name == "三清山邮政所"
    assert links.office_place_name == "玉山县"
    assert "地点：" in links.note and "局名：" in links.note
    assert links.resolved_any


def test_resolve_card_links_says_why_when_nothing_matches() -> None:
    """解析不出来时**必须给理由**（界面要显示"为什么没挂上"）。"""
    session = _OfficeSession(place=None, rows=[])
    vocab = _PlaceVocabStub({})
    links = pa.resolve_card_links(
        session, vocab, place_text="西海栈道", office_text=""  # type: ignore[arg-type]
    )
    assert links.place_id is None and links.office_id is None
    assert not links.resolved_any
    assert "不是已登记地名" in links.note
    assert session.statements == [], "地点没解析出来时不该发局名查询"


def test_resolve_card_links_skips_everything_when_there_is_no_text() -> None:
    """两个文本都空 → 一条说明都不该编（空就是空）。"""
    session = _OfficeSession(place=None, rows=[])
    links = pa.resolve_card_links(
        session, _PlaceVocabStub({}), place_text="", office_text=""  # type: ignore[arg-type]
    )
    assert links.note == ""
    assert not links.resolved_any
