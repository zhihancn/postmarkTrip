"""票墙的单元测试（不连数据库）。

重点：
* S1：志号 → 系列（普/纪/特/编年/其他/未编目）与分组顺序；
* S2：面值显示不出现浮点尾巴；
* 票墙的口径：provenance 跟随卡；**只有票没有戳**的卡在默认开关下仍显示；
* `cards_without_stamp` 是"票未录"，与"没有贴片图"分开。
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from postmarktrip.api.routes_gallery import (
    _cards_without_stamp,
    _face_label,
    _group_stamp_items,
    _series_of,
    _stamp_conditions,
    _stamp_counts,
    _stamp_items,
)
from postmarktrip.db.models import Card, Stamp
from postmarktrip.domain.schemas import GalleryStampItem

# --------------------------------------------------------------------------- #
#  测试替身
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows

    def one(self) -> Any:
        assert len(self._rows) == 1
        return self._rows[0]


class _PlaceDb:
    def get(self, model: Any, pk: Any) -> Any:
        class _Place:
            path = "/CN/35/"

        return _Place()


class _TagRowsDb:
    def __init__(self, tags: list[tuple[uuid.UUID, str]]) -> None:
        self._tags = tags

    def execute(self, stmt: Any) -> Any:
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        if "tag.name" in sql:
            return _Result(self._tags)
        raise AssertionError(f"意料之外的查询：{sql[:200]}")


class _ScalarDb:
    """`_cards_without_stamp` 走两次 `db.scalar`：count 与 first id。"""

    def __init__(self, count: int, first: uuid.UUID | None) -> None:
        self._values = [count, first]
        self.seen: list[str] = []

    def scalar(self, stmt: Any) -> Any:
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        self.seen.append(sql)
        return self._values[len(self.seen) - 1]


def _conditions(**over: Any) -> list[Any]:
    params: dict[str, Any] = {
        "year": None,
        "visit_date_from": None,
        "visit_date_to": None,
        "place_id": None,
        "tag_id": None,
        "series": None,
        "catalog_system": None,
        "face_min": None,
        "face_max": None,
        "provenance": "real_scan",
    }
    params.update(over)
    return _stamp_conditions(_PlaceDb(), **params)  # type: ignore[arg-type]


def _sql(conds: list[Any]) -> str:
    stmt = select(Stamp).join(Card, Card.id == Stamp.card_id).where(*conds)
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------- #
#  S1：系列
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("ref", "key", "label"),
    [
        ("普30", "pu", "普"),
        ("纪94", "ji", "纪"),
        ("特56", "te", "特"),
        ("2023-1", "yearly", "编年"),
        ("J1", "other", "其他"),
        (None, "uncatalogued", "未编目"),
        ("  ", "uncatalogued", "未编目"),
    ],
)
def test_series_of(ref: str | None, key: str, label: str) -> None:
    assert _series_of(ref) == (key, label)


# --------------------------------------------------------------------------- #
#  S2：面值
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "currency", "expected"),
    [
        (Decimal("0.40"), "CNY", "40分"),
        (Decimal("0.08"), "CNY", "8分"),
        (Decimal("1.20"), "CNY", "1.20元"),
        (Decimal("0.10"), None, "10分"),
        (Decimal("4.50"), "USD", "4.50 USD"),
        (None, "CNY", "面值未知"),
    ],
)
def test_face_label_never_leaks_float(
    value: Decimal | None, currency: str | None, expected: str
) -> None:
    assert _face_label(value, currency) == expected
    # 防回归：0.1 + 0.2 那类尾巴不该出现在面值里
    assert "000000" not in _face_label(value, currency)


# --------------------------------------------------------------------------- #
#  筛选
# --------------------------------------------------------------------------- #


def test_default_conditions_exclude_deleted_stamps_and_cards() -> None:
    sql = _sql(_conditions())
    assert "stamp.deleted_at IS NULL" in sql
    assert "card.deleted_at IS NULL" in sql


def test_provenance_follows_the_card_and_keeps_stamp_only_cards() -> None:
    """★ S7：只有票、没有戳的卡是合法的，默认开关下不能被隐藏。"""
    sql = _sql(_conditions(provenance="real_scan"))
    assert "NOT (EXISTS" in sql
    assert "postmark.provenance = 'real_scan'" in sql
    assert "card.provenance" not in sql


def test_provenance_all_removes_the_condition() -> None:
    sql = _sql(_conditions(provenance="all"))
    assert "postmark.provenance =" not in sql


def test_series_filters_use_catalog_ref_prefixes() -> None:
    assert "stamp.catalog_ref LIKE '普%" in _sql(_conditions(series="pu"))
    assert "stamp.catalog_ref LIKE '纪%" in _sql(_conditions(series="ji"))
    assert "stamp.catalog_ref LIKE '特%" in _sql(_conditions(series="te"))
    yearly = _sql(_conditions(series="yearly"))
    assert "stamp.catalog_ref ~" in yearly
    uncatalogued = _sql(_conditions(series="uncatalogued"))
    assert "stamp.catalog_ref IS NULL" in uncatalogued
    other = _sql(_conditions(series="other"))
    assert "NOT LIKE" in other


def test_face_range_and_catalog_system_filters() -> None:
    from postmarktrip.domain.vocab import CatalogSystem

    sql = _sql(
        _conditions(
            face_min=Decimal("0.10"),
            face_max=Decimal("1.20"),
            catalog_system=CatalogSystem.CHINA,
        )
    )
    assert "stamp.face_value >= 0.10" in sql
    assert "stamp.face_value <= 1.20" in sql
    assert "stamp.catalog_system = 'china'" in sql


def test_place_filter_looks_at_the_cards_postmarks() -> None:
    sql = _sql(_conditions(place_id=uuid.uuid4()))
    assert "place.path LIKE '/CN/35/" in sql
    assert "postmark.place_id IN (SELECT place.id" in sql
    assert "postmark.card_id = card.id" in sql


def test_tag_filter_targets_stamp_tags() -> None:
    tag_id = uuid.uuid4()
    sql = _sql(_conditions(tag_id=tag_id))
    assert "tag_link.target_type = 'stamp'" in sql
    assert f"tag_link.tag_id = '{tag_id}'" in sql


def test_counts_use_image_asset_id() -> None:
    class _CountDb:
        def execute(self, stmt: Any) -> Any:
            self.sql = str(
                stmt.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
            return _Result([type("Row", (), {"total": 2, "with_cut": 1})()])

    db = _CountDb()
    assert _stamp_counts(db, _conditions()) == (2, 1)  # type: ignore[arg-type]
    assert "count(stamp.image_asset_id)" in db.sql
    assert "JOIN CARD" in db.sql.upper()


# --------------------------------------------------------------------------- #
#  票未录
# --------------------------------------------------------------------------- #


def test_cards_without_stamp_counts_postmark_cards_without_stamps() -> None:
    first = uuid.uuid4()
    db = _ScalarDb(count=3, first=first)
    assert _cards_without_stamp(db) == (3, first)  # type: ignore[arg-type]
    assert len(db.seen) == 2
    # 有戳、没有票 —— 不能写成"缺贴片"
    assert "EXISTS" in db.seen[0]
    assert "NOT (EXISTS" in db.seen[0]
    assert "ORDER BY card.visit_date DESC" in db.seen[1]


# --------------------------------------------------------------------------- #
#  瓦片与分组
# --------------------------------------------------------------------------- #


def _item(
    *,
    catalog_ref: str | None = "普30",
    name: str | None = "保护森林",
    face: Decimal | None = Decimal("0.40"),
    currency: str | None = "CNY",
    issue: date | None = date(2002, 2, 1),
    tags: list[str] | None = None,
    asset: uuid.UUID | None = None,
) -> GalleryStampItem:
    return GalleryStampItem(
        stamp_id=uuid.uuid4(),
        card_id=uuid.uuid4(),
        asset_id=asset,
        catalog_ref=catalog_ref,
        catalog_system="china",
        name=name,
        face_value=face,
        currency=currency,
        face_label=_face_label(face, currency),
        issue_date=issue,
        visit_date=date(2026, 9, 25),
        tag_names=tags or [],
        asset_width=986,
        asset_height=692,
    )


def test_group_by_series_orders_pu_ji_te_yearly_other_uncatalogued() -> None:
    items = [
        _item(catalog_ref="J1"),
        _item(catalog_ref=None),
        _item(catalog_ref="2023-1"),
        _item(catalog_ref="特56"),
        _item(catalog_ref="纪94"),
        _item(catalog_ref="普30"),
    ]
    groups = _group_stamp_items(items, "series")
    assert [g.key for g in groups] == ["pu", "ji", "te", "yearly", "other", "uncatalogued"]


def test_group_by_face_sorts_numerically_and_unknown_last() -> None:
    items = [
        _item(face=Decimal("1.20")),
        _item(face=None, currency=None),
        _item(face=Decimal("0.08")),
        _item(face=Decimal("0.40")),
    ]
    groups = _group_stamp_items(items, "face")
    assert [g.label for g in groups] == ["8分", "40分", "1.20元", "面值未知"]


def test_group_by_year_descending_and_unknown_last() -> None:
    items = [_item(issue=date(2002, 2, 1)), _item(issue=None), _item(issue=date(2023, 1, 5))]
    groups = _group_stamp_items(items, "year")
    assert [g.label for g in groups] == ["2023", "2002", "未知"]


def test_group_by_tag_untagged_last() -> None:
    items = [_item(tags=[]), _item(tags=["建溪"]), _item(tags=["主题邮局"])]
    groups = _group_stamp_items(items, "tag")
    assert [g.label for g in groups] == ["主题邮局", "建溪", "无标签"]


def test_group_by_none() -> None:
    groups = _group_stamp_items([_item(), _item()], "none")
    assert len(groups) == 1
    assert groups[0].label == "全部"
    assert groups[0].count == 2


def test_stamp_items_keep_tags_and_dimensions() -> None:
    stamp_id = uuid.uuid4()
    rows = [
        (
            stamp_id,
            uuid.uuid4(),
            uuid.uuid4(),
            "普30",
            "china",
            "保护森林",
            Decimal("0.40"),
            "CNY",
            date(2002, 2, 1),
            date(2026, 9, 25),
            986,
            692,
        )
    ]
    db = _TagRowsDb(tags=[(stamp_id, "建溪")])
    items = _stamp_items(db, rows)  # type: ignore[arg-type]
    assert len(items) == 1
    item = items[0]
    assert item.face_label == "40分"
    assert item.tag_names == ["建溪"]
    assert (item.asset_width, item.asset_height) == (986, 692)


@pytest.mark.parametrize("group_by", ["series", "face", "year", "tag", "none"])
def test_grouping_never_drops_stamps(group_by: str) -> None:
    items = [_item(catalog_ref="普30"), _item(catalog_ref=None), _item(face=None)]
    groups = _group_stamp_items(items, group_by)  # type: ignore[arg-type]
    assert sum(g.count for g in groups) == 3
