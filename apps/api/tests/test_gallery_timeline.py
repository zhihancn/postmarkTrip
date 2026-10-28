"""画廊时间轴的单元测试（不连数据库）。

测的是三类**不会报错、只会给错数**的问题：

1. 筛选条件漏了/串了 —— 例如"只看真实跑戳"没有落到 `EXISTS postmark` 上，
   界面上表现为"开关拨了没反应"。
2. 计数口径与列表口径不一致 —— 例如总数 12、但第 2 页只有 9 条。
3. 卡片级聚合丢字段 —— `_summaries` 曾经静默返回空字典（`test_card_summaries.py`），
   同样的错在时间轴上会让每一行都没有地点/戳面原文。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from postmarktrip.api.routes_gallery import (
    _place_path,
    _timeline_conditions,
    _timeline_counts,
    _timeline_items,
)
from postmarktrip.db.models import Card

# --------------------------------------------------------------------------- #
#  测试替身
# --------------------------------------------------------------------------- #


class _PlaceWithPath:
    path = "/CN/35/"


class _StubDb:
    """`_timeline_conditions` 只需要 `db.get(Place, id)`；`_timeline_items`
    只需要 `db.execute(stmt).all()`。按语句里出现的列名分派，不按调用顺序 ——
    否则别人调整查询顺序会得到误导性的失败。
    """

    def __init__(self, place_path: str | None = "/CN/35/") -> None:
        self._place_path = place_path
        self.queries: list[str] = []
        self.pm_rows: list[tuple[Any, ...]] = []
        self.tag_rows: list[tuple[Any, ...]] = []
        self.place_rows: list[tuple[Any, ...]] = []

    def get(self, model: Any, pk: Any) -> Any:
        if self._place_path is None:
            return None

        class _Place:
            path = self._place_path

        return _Place()

    def execute(self, stmt: Any) -> Any:
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        self.queries.append(sql)
        if "postmark.inscription" in sql:
            return _Result(self.pm_rows)
        if "tag.name" in sql:
            return _Result(self.tag_rows)
        if "place_chain" in sql:
            return _Result(self.place_rows)
        raise AssertionError(f"意料之外的查询（测试替身需要更新）：{sql[:200]}")


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows

    def one(self) -> Any:
        assert len(self._rows) == 1
        return self._rows[0]


class _CountRow:
    def __init__(self, total: int, with_cut: int, without_postmark: int) -> None:
        self.total = total
        self.with_cut = with_cut
        self.without_postmark = without_postmark


class _CountDb:
    def __init__(self, row: _CountRow) -> None:
        self._row = row
        self.sql = ""

    def execute(self, stmt: Any) -> Any:
        self.sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        return _Result([self._row])


def _conditions(**over: Any) -> list[Any]:
    params: dict[str, Any] = {
        "year": None,
        "visit_date_from": None,
        "visit_date_to": None,
        "place_id": None,
        "tag_id": None,
        "source": None,
        "provenance": "all",
    }
    params.update(over)
    return _timeline_conditions(_StubDb(), **params)


def _sql(conds: list[Any]) -> str:
    stmt = select(Card).where(*conds)
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------- #
#  筛选条件
# --------------------------------------------------------------------------- #


def test_default_only_excludes_soft_deleted_cards() -> None:
    sql = _sql(_conditions())
    assert "card.deleted_at IS NULL" in sql
    # provenance='all' 时不该出现 provenance 条件
    assert "postmark.provenance" not in sql


def test_year_becomes_a_half_open_range() -> None:
    sql = _sql(_conditions(year=2026))
    assert "card.visit_date >= '2026-01-01'" in sql
    assert "card.visit_date <= '2026-12-31'" in sql


def test_explicit_date_range_is_kept() -> None:
    sql = _sql(
        _conditions(visit_date_from=date(2025, 1, 1), visit_date_to=date(2025, 12, 31))
    )
    assert "card.visit_date >= '2025-01-01'" in sql
    assert "card.visit_date <= '2025-12-31'" in sql


def test_provenance_filter_is_about_any_real_postmark_on_the_card() -> None:
    """00 §3.3：卡本身没有 provenance 列 —— 必须落到"存在任一真实戳"。"""
    sql = _sql(_conditions(provenance="real_scan"))
    assert "EXISTS" in sql
    assert "postmark.provenance = 'real_scan'" in sql
    # 不能退化成"卡表上有这一列"的写法
    assert "card.provenance" not in sql


def test_provenance_all_removes_the_exists_condition() -> None:
    sql = _sql(_conditions(provenance="all"))
    assert "postmark.provenance" not in sql


def test_real_only_keeps_cards_that_have_no_postmark_at_all() -> None:
    """★ 没有戳的卡必须保留（`01` §七：它是卡，行内显示"未建戳"）。

    如果这里只写 `EXISTS (real_scan)`，默认开关下"未建戳"的卡会被静默隐藏：
    时间轴少一行、`without_postmark` 永远是 0，而两者都在验收里（G4/G7）。
    """
    sql = _sql(_conditions(provenance="real_scan"))
    assert "NOT (EXISTS" in sql
    assert "postmark.provenance = 'real_scan'" in sql


def test_non_real_provenance_does_not_include_cards_without_postmarks() -> None:
    """按 generated/third_party 筛时，没有戳的卡不该混进来。"""
    sql = _sql(_conditions(provenance="generated"))
    assert "postmark.provenance = 'generated'" in sql
    assert "NOT (EXISTS" not in sql


def test_place_filter_uses_materialized_path_prefix() -> None:
    """选省级要带出所有下级（00 §3.2）—— 靠 place.path 前缀，不是只看 level。"""
    sql = _sql(_conditions(place_id=uuid.uuid4()))
    # literal_binds 会把 LIKE 里的 % 转义成 %%（DBAPI 参数风格），所以只断言前缀
    assert "place.path LIKE '/CN/35/" in sql
    assert "postmark.place_id IN (SELECT place.id" in sql


def test_place_without_path_falls_back_to_exact_match() -> None:
    conds = _timeline_conditions(
        _StubDb(place_path=None),
        year=None,
        visit_date_from=None,
        visit_date_to=None,
        place_id=uuid.uuid4(),
        tag_id=None,
        source=None,
        provenance="all",
    )
    sql = _sql(conds)
    assert "postmark.place_id = " in sql
    assert "place.path LIKE" not in sql


def test_tag_filter_targets_card_tags() -> None:
    tag_id = uuid.uuid4()
    sql = _sql(_conditions(tag_id=tag_id))
    assert "tag_link.target_type = 'card'" in sql
    assert f"tag_link.tag_id = '{tag_id}'" in sql


def test_source_filter_uses_the_card_column() -> None:
    from postmarktrip.domain.vocab import CardSource

    sql = _sql(_conditions(source=CardSource.MAIL_REQUEST))
    assert "card.source = 'mail_request'" in sql


# --------------------------------------------------------------------------- #
#  计数
# --------------------------------------------------------------------------- #


def test_counts_sql_uses_the_same_card_scope_and_counts_imageless() -> None:
    db = _CountDb(_CountRow(total=12, with_cut=9, without_postmark=2))
    assert _timeline_counts(db, _conditions()) == (12, 9, 2)
    # count(列) 天然跳过 NULL —— 不需要再写一遍"is not null"
    assert "count(card.image_asset_id)" in db.sql
    # 未建戳的卡用 FILTER + NOT EXISTS 数，不能靠前端猜
    assert "FILTER (WHERE NOT (EXISTS" in db.sql


# --------------------------------------------------------------------------- #
#  地点链
# --------------------------------------------------------------------------- #


def test_place_path_is_ordered_root_to_leaf() -> None:
    country, province, city = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    chain = {
        city: ("南平市", province),
        province: ("福建省", country),
        country: ("中华人民共和国", None),
    }
    assert _place_path(city, chain) == ["中华人民共和国", "福建省", "南平市"]


def test_place_path_tolerates_missing_ancestors_and_cycles() -> None:
    lone = uuid.uuid4()
    assert _place_path(lone, {}) == []
    a, b = uuid.uuid4(), uuid.uuid4()
    # a -> b -> a 的数据错误不该把接口挂死
    chain = {a: ("A", b), b: ("B", a)}
    assert _place_path(a, chain) == ["B", "A"]


def test_place_path_skips_unknown_parent() -> None:
    leaf = uuid.uuid4()
    chain = {leaf: ("建溪", uuid.uuid4())}
    assert _place_path(leaf, chain) == ["建溪"]


# --------------------------------------------------------------------------- #
#  卡片级聚合
# --------------------------------------------------------------------------- #


def _card(**over: Any) -> Card:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "visit_date": date(2026, 9, 25),
        "source": "field_trip",
        "status": "collected",
        "title": None,
        "image_asset_id": None,
        "scene": None,
    }
    values.update(over)
    return Card(**values)


def test_timeline_items_aggregate_postmarks_places_offices_and_tags() -> None:
    country, province, city = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    office = "建溪邮政支局"
    card = _card(image_asset_id=uuid.uuid4(), scene={"schema_version": 1, "slots": []})

    db = _StubDb()
    db.pm_rows = [
        (card.id, "建溪 1", "daily", city, office),
        (card.id, "三清山", "scenic", city, office),
    ]
    db.tag_rows = [(card.id, "主题邮局")]
    db.place_rows = [
        (city, "南平市", province),
        (province, "福建省", country),
        (country, "中华人民共和国", None),
    ]

    items = _timeline_items(db, [card])  # type: ignore[arg-type]

    assert len(items) == 1
    item = items[0]
    assert item.card_id == card.id
    assert item.thumb_asset_id == card.image_asset_id
    assert item.has_scene is True
    assert item.postmark_count == 2
    assert item.inscriptions == ["建溪 1", "三清山"]
    assert item.postmark_types == ["daily", "scenic"]
    # 同一个局出现两次也只算一个
    assert item.office_names == [office]
    assert item.place_path == ["中华人民共和国", "福建省", "南平市"]
    assert item.tag_names == ["主题邮局"]


def test_timeline_items_keep_cards_without_postmarks() -> None:
    """没有戳的卡**必须**留下一行 —— 它是一张卡，不是噪声（01 §七）。"""
    card = _card()
    db = _StubDb()
    db.pm_rows = []
    db.tag_rows = []
    db.place_rows = []

    items = _timeline_items(db, [card])  # type: ignore[arg-type]

    assert len(items) == 1
    assert items[0].postmark_count == 0
    assert items[0].place_path == []
    assert items[0].office_names == []
    assert items[0].has_scene is False


def test_timeline_items_empty_input_issues_no_queries() -> None:
    db = _StubDb()
    assert _timeline_items(db, []) == []  # type: ignore[arg-type]
    assert db.queries == []


@pytest.mark.parametrize("source", ["field_trip", "mail_request"])
def test_timeline_items_preserve_source(source: str) -> None:
    card = _card(source=source)
    db = _StubDb()
    db.pm_rows = []
    db.tag_rows = []
    db.place_rows = []
    items = _timeline_items(db, [card])  # type: ignore[arg-type]
    assert items[0].source == source
