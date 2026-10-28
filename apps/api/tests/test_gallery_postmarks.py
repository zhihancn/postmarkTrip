"""戳墙的单元测试（不连数据库）。

重点盯三件事：

1. **筛选 SQL 的口径**：provenance 看戳自己；卡被删时戳也必须消失；
2. **分组与排序**（W1）以及"无抠图"仍出现在墙上（W2）；
3. **没有把票墙的概念混进来**（W7）：戳墙不判断"这张卡有没有票"。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from postmarktrip.api.routes_gallery import (
    _group_wall_entries,
    _place_names,
    _postmark_conditions,
    _postmark_counts,
    _wall_entries,
    _WallEntry,
)
from postmarktrip.db.models import Card, Postmark
from postmarktrip.domain.schemas import GalleryPostmarkItem

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


class _TagRowsDb:
    """`_wall_entries` 只用 `db.execute(stmt).all()`；按语句里的列名分派。"""

    def __init__(self, tags: list[tuple[uuid.UUID, str]], chain: list[tuple[Any, ...]]) -> None:
        self._tags = tags
        self._chain = chain
        self.queries: list[str] = []

    def execute(self, stmt: Any) -> Any:
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        self.queries.append(sql)
        if "tag.name" in sql:
            return _Result(self._tags)
        if "place_chain" in sql:
            return _Result(self._chain)
        raise AssertionError(f"意料之外的查询：{sql[:200]}")


class _CountRow:
    def __init__(self, total: int, with_cut: int) -> None:
        self.total = total
        self.with_cut = with_cut


class _CountDb:
    def __init__(self, total: int, with_cut: int) -> None:
        self._row = _CountRow(total, with_cut)
        self.sql = ""

    def execute(self, stmt: Any) -> Any:
        self.sql = str(
            stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        return _Result([self._row])


def _conditions(**over: Any) -> list[Any]:
    params: dict[str, Any] = {
        "year": None,
        "visit_date_from": None,
        "visit_date_to": None,
        "place_id": None,
        "tag_id": None,
        "postmark_type": None,
        "is_color": None,
        "provenance": "real_scan",
    }
    params.update(over)
    return _postmark_conditions(_PlaceDb(), **params)


class _PlaceDb:
    def get(self, model: Any, pk: Any) -> Any:
        class _Place:
            path = "/CN/35/"

        return _Place()


def _sql(conds: list[Any]) -> str:
    stmt = select(Postmark).join(Card, Card.id == Postmark.card_id).where(*conds)
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------- #
#  筛选
# --------------------------------------------------------------------------- #


def test_default_conditions_exclude_deleted_postmarks_and_deleted_cards() -> None:
    sql = _sql(_conditions())
    assert "postmark.deleted_at IS NULL" in sql
    assert "card.deleted_at IS NULL" in sql


def test_provenance_is_about_the_postmark_itself() -> None:
    """00 §3.3：戳墙看**戳自己**的 provenance，不是卡。"""
    sql = _sql(_conditions(provenance="real_scan"))
    assert "postmark.provenance = 'real_scan'" in sql
    assert "card.provenance" not in sql


def test_provenance_all_removes_the_condition() -> None:
    sql = _sql(_conditions(provenance="all"))
    # select(Postmark) 会带出 provenance 列，所以只看 WHERE 里的比较
    assert "postmark.provenance =" not in sql


def test_year_filter_uses_card_visit_date() -> None:
    sql = _sql(_conditions(year=2026))
    assert "card.visit_date >= '2026-01-01'" in sql
    assert "card.visit_date <= '2026-12-31'" in sql


def test_place_filter_uses_the_place_subtree() -> None:
    sql = _sql(_conditions(place_id=uuid.uuid4()))
    assert "place.path LIKE '/CN/35/" in sql
    assert "postmark.place_id IN (SELECT place.id" in sql


def test_tag_filter_targets_postmark_tags() -> None:
    tag_id = uuid.uuid4()
    sql = _sql(_conditions(tag_id=tag_id))
    assert "tag_link.target_type = 'postmark'" in sql
    assert f"tag_link.tag_id = '{tag_id}'" in sql


def test_type_and_color_filters() -> None:
    from postmarktrip.domain.vocab import PostmarkType

    sql = _sql(_conditions(postmark_type=PostmarkType.SCENIC, is_color=True))
    assert "postmark.postmark_type = 'scenic'" in sql
    assert "postmark.is_color IS true" in sql


def test_counts_are_global_and_use_image_asset_id() -> None:
    db = _CountDb(total=42, with_cut=39)
    assert _postmark_counts(db, _conditions()) == (42, 39)
    assert "count(postmark.image_asset_id)" in db.sql
    assert "JOIN CARD" in db.sql.upper()


# --------------------------------------------------------------------------- #
#  瓦片
# --------------------------------------------------------------------------- #


def _item(
    *,
    postmark_id: uuid.UUID | None = None,
    inscription: str = "建溪",
    postmark_type: str = "daily",
    is_color: bool = False,
    tags: list[str] | None = None,
    asset_id: uuid.UUID | None = None,
) -> GalleryPostmarkItem:
    return GalleryPostmarkItem(
        postmark_id=postmark_id or uuid.uuid4(),
        card_id=uuid.uuid4(),
        asset_id=asset_id,
        inscription=inscription,
        visit_date=date(2026, 9, 25),
        place_name=None,
        office_name=None,
        is_color=is_color,
        color_layers=None,
        tag_names=tags or [],
        postmark_type=postmark_type,
    )


def _entry(item: GalleryPostmarkItem, path: list[str] | None = None) -> _WallEntry:
    return _WallEntry(item=item, place_path=path or [])


def test_place_names_drops_the_country_level() -> None:
    assert _place_names([]) is None
    assert _place_names(["中华人民共和国"]) == "中华人民共和国"
    assert (
        _place_names(["中华人民共和国", "福建省", "南平市", "延平区"])
        == "福建省 / 南平市 / 延平区"
    )


# --------------------------------------------------------------------------- #
#  分组
# --------------------------------------------------------------------------- #


def test_group_by_type_orders_by_vocab_and_keeps_without_cut_tiles() -> None:
    without_cut = _item(postmark_type="scenic", inscription="没有抠图")  # asset_id=None
    daily = _item(postmark_type="daily", inscription="日戳")
    other = _item(postmark_type="other", inscription="其他")

    groups = _group_wall_entries(
        [_entry(without_cut), _entry(daily), _entry(other)], "type"
    )

    assert [g.key for g in groups] == ["daily", "scenic", "other"]
    assert [g.label for g in groups] == ["日戳", "风景戳", "其他"]
    # W2：没有抠图的戳仍然在墙上（虚线瓦片由前端画），不能静默丢
    assert groups[1].items[0].asset_id is None
    assert groups[1].count == 1


def test_group_by_color() -> None:
    groups = _group_wall_entries(
        [_entry(_item(is_color=True)), _entry(_item(is_color=False))], "color"
    )
    assert [(g.key, g.label) for g in groups] == [("mono", "单色"), ("color", "套色")]


def test_group_by_tag_uses_first_tag_and_puts_untagged_last() -> None:
    a = _item(tags=["主题邮局", "彩色戳"])
    b = _item(tags=["建溪"])
    c = _item(tags=[])

    groups = _group_wall_entries([_entry(a), _entry(b), _entry(c)], "tag")

    assert [g.label for g in groups] == ["主题邮局", "建溪", "无标签"]
    # 多标签只归第一组，避免同一块瓦片在多个组里重复出现
    assert all(not (g.key == "主题邮局" and len(g.items) > 1) for g in groups)


def test_group_by_province_puts_unspecified_last() -> None:
    fujian = _entry(_item(), ["中华人民共和国", "福建省", "南平市"])
    zhejiang = _entry(_item(), ["中华人民共和国", "浙江省"])
    nowhere = _entry(_item(), [])

    groups = _group_wall_entries([nowhere, fujian, zhejiang], "province")

    assert [g.label for g in groups] == ["浙江省", "福建省", "未分省"]


def test_group_by_none_gives_one_group() -> None:
    groups = _group_wall_entries([_entry(_item()), _entry(_item())], "none")
    assert len(groups) == 1
    assert groups[0].label == "全部"
    assert groups[0].count == 2


# --------------------------------------------------------------------------- #
#  行 → 瓦片
# --------------------------------------------------------------------------- #


def test_wall_entries_build_place_path_office_and_tags() -> None:
    postmark_id = uuid.uuid4()
    country, province, city = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = [
        (
            postmark_id,
            uuid.uuid4(),
            uuid.uuid4(),
            "建溪",
            "daily",
            False,
            None,
            city,
            "建溪邮政支局",
            date(2026, 9, 25),
        )
    ]
    db = _TagRowsDb(
        tags=[(postmark_id, "建溪")],
        chain=[
            (city, "南平市", province),
            (province, "福建省", country),
            (country, "中华人民共和国", None),
        ],
    )

    entries = _wall_entries(db, rows)  # type: ignore[arg-type]

    assert len(entries) == 1
    item = entries[0].item
    assert item.postmark_id == postmark_id
    assert item.place_name == "福建省 / 南平市"
    assert item.office_name == "建溪邮政支局"
    assert item.tag_names == ["建溪"]
    assert entries[0].place_path == ["中华人民共和国", "福建省", "南平市"]


def test_wall_entries_without_place_are_still_returned() -> None:
    rows = [
        (
            uuid.uuid4(),
            uuid.uuid4(),
            None,
            "无图戳",
            "daily",
            False,
            None,
            None,
            None,
            date(2026, 9, 25),
        )
    ]
    db = _TagRowsDb(tags=[], chain=[])
    entries = _wall_entries(db, rows)  # type: ignore[arg-type]

    assert entries[0].item.asset_id is None
    assert entries[0].item.place_name is None
    assert entries[0].item.tag_names == []


@pytest.mark.parametrize("group_by", ["type", "color", "tag", "province", "none"])
def test_grouping_never_drops_items(group_by: str) -> None:
    items = [_item(tags=["建溪"], postmark_type="daily") for _ in range(3)]
    entries = [_entry(item, ["中华人民共和国", "福建省"]) for item in items]
    groups = _group_wall_entries(entries, group_by)  # type: ignore[arg-type]
    assert sum(g.count for g in groups) == 3
