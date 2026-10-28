"""地图（M3d）的单元测试。

**不连数据库**：与 `test_gallery_timeline.py` 相同，用轻量替身喂夹具。
这里测的是四类"不会报错、只会给错数"的问题：

1. **多戳卡去重**：一张卡两个跨省戳，每个省各算一张；同省两个戳只算一张
   （`04` §五、M1）；
2. **市数不能用县数冒充**：戳落在县里要上溯到市；直辖市的区（level=3）
   没有市级祖先，`city_count` 必须是 0（M4）；
3. **同名省级节点合并**：历史重复种子会造出两个"福建省"，响应里不能两行；
4. **未填地点**：没有带地点戳的卡必须显式计数，且不能和"没坐标"混为一谈（M2）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from postmarktrip.api.routes_gallery import (
    _aggregate_provinces,
    _ancestor_at,
    _PlaceNode,
    gallery_map,
)
from postmarktrip.db.models import Card
from postmarktrip.domain.regions import province_code2

# --------------------------------------------------------------------------- #
#  夹具
# --------------------------------------------------------------------------- #


def node(
    name: str,
    level: int,
    *,
    parent: uuid.UUID | None = None,
    admin_code: str | None = None,
    place_id: uuid.UUID | None = None,
) -> _PlaceNode:
    return _PlaceNode(
        id=place_id or uuid.uuid4(),
        name=name,
        parent_id=parent,
        level=level,
        admin_code=admin_code,
    )


def chain_of(*nodes: _PlaceNode) -> dict[uuid.UUID, _PlaceNode]:
    return {item.id: item for item in nodes}


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def one(self) -> Any:
        assert len(self._rows) == 1
        return self._rows[0]


class _MapDb:
    """`gallery_map` 只做两件事：`scalar(count)` 与两次 `execute`。

    按语句里出现的表/列名分派，不按调用顺序 —— 调整查询顺序不会得到假失败。
    """

    def __init__(
        self,
        total_cards: int,
        postmark_rows: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]],
        place_rows: list[tuple[uuid.UUID, uuid.UUID | None, str, int, str | None]],
    ) -> None:
        self.total_cards = total_cards
        self.postmark_rows = postmark_rows
        self.place_rows = place_rows
        self.queries: list[str] = []

    def scalar(self, stmt: Any) -> int:
        self.queries.append(str(stmt.compile(dialect=postgresql.dialect())))
        return self.total_cards

    def execute(self, stmt: Any) -> _Result:
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        self.queries.append(sql)
        if "place_nodes" in sql:
            return _Result(self.place_rows)
        if "postmark.id" in sql:
            return _Result(self.postmark_rows)
        raise AssertionError(f"意料之外的查询（测试替身需要更新）：{sql[:200]}")


def call_map(db: _MapDb, **over: Any):
    params: dict[str, Any] = {
        "year": None,
        "visit_date_from": None,
        "visit_date_to": None,
        "place_id": None,
        "tag_id": None,
        "source": None,
        "provenance": "all",
        "level": "province",
    }
    params.update(over)
    return gallery_map(db, None, **params)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
#  省级编码
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("admin_code", "name", "expected"),
    [
        ("350000", "福建省", "35"),
        ("110000", "北京市", "11"),
        ("820000", "澳门特别行政区", "82"),
        ("350000.0", "福建省", "35"),  # 脏数据不把 "35.0" 当代码，但名字能兜住
        (None, "福建省", "35"),
        (None, "福建", "35"),
        (None, "内蒙古", "15"),
        (None, "火星省", None),
        ("", "", None),
        # 行政代码优先于名字：两个都对时不能反转
        ("510000", "福建省", "51"),
    ],
)
def test_province_code2(admin_code: str | None, name: str | None, expected: str | None) -> None:
    assert province_code2(admin_code, name) == expected


def test_all_34_provinces_have_a_code() -> None:
    """34 个省级（含港澳台）都必须能落到 SVG 的 data-code 上。"""
    names = [
        "北京市", "天津市", "河北省", "山西省", "内蒙古自治区", "辽宁省", "吉林省",
        "黑龙江省", "上海市", "江苏省", "浙江省", "安徽省", "福建省", "江西省",
        "山东省", "河南省", "湖北省", "湖南省", "广东省", "广西壮族自治区",
        "海南省", "重庆市", "四川省", "贵州省", "云南省", "西藏自治区", "陕西省",
        "甘肃省", "青海省", "宁夏回族自治区", "新疆维吾尔自治区", "台湾省",
        "香港特别行政区", "澳门特别行政区",
    ]  # fmt: skip
    codes = {province_code2(None, name) for name in names}
    assert None not in codes
    assert len(codes) == 34


# --------------------------------------------------------------------------- #
#  上溯
# --------------------------------------------------------------------------- #


def test_ancestor_at_finds_province_from_county() -> None:
    country = node("中华人民共和国", 0)
    province = node("福建省", 1, parent=country.id, admin_code="350000")
    city = node("南平市", 2, parent=province.id, admin_code="350700")
    county = node("延平区", 3, parent=city.id, admin_code="350702")
    nodes = chain_of(country, province, city, county)

    assert _ancestor_at(county.id, 1, nodes) is province
    assert _ancestor_at(county.id, 2, nodes) is city


def test_ancestor_at_returns_none_for_municipality_district() -> None:
    """直辖市的区父级就是省，没有市级祖先 —— 不要补假市级。"""
    country = node("中华人民共和国", 0)
    province = node("北京市", 1, parent=country.id, admin_code="110000")
    district = node("东城区", 3, parent=province.id, admin_code="110101")
    nodes = chain_of(country, province, district)

    assert _ancestor_at(district.id, 1, nodes) is province
    assert _ancestor_at(district.id, 2, nodes) is None


def test_ancestor_at_tolerates_cycles() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    nodes = {
        a: _PlaceNode(id=a, name="A", parent_id=b, level=3, admin_code=None),
        b: _PlaceNode(id=b, name="B", parent_id=a, level=2, admin_code=None),
    }
    assert _ancestor_at(a, 1, nodes) is None


# --------------------------------------------------------------------------- #
#  聚合
# --------------------------------------------------------------------------- #


def test_aggregate_dedupes_multi_postmark_cards() -> None:
    country = node("中华人民共和国", 0)
    province = node("福建省", 1, parent=country.id, admin_code="350000")
    nanping = node("南平市", 2, parent=province.id, admin_code="350700")
    yanping = node("延平区", 3, parent=nanping.id, admin_code="350702")
    nodes = chain_of(country, province, nanping, yanping)

    card = uuid.uuid4()
    rows = [
        (uuid.uuid4(), card, nanping.id),
        (uuid.uuid4(), card, yanping.id),
    ]
    result = _aggregate_provinces(rows, nodes)

    assert len(result.provinces) == 1
    item = result.provinces[0]
    assert item.name == "福建省"
    assert item.admin_code == "350000"
    assert item.code2 == "35"
    # 一张卡两个戳：卡只算一次，戳算两次
    assert item.card_count == 1
    assert item.postmark_count == 2
    # 市与区同属南平市：去重后 1 个市
    assert item.city_count == 1
    assert result.located_cards == {card}
    assert result.unresolved_postmarks == 0


def test_aggregate_splits_cross_province_cards_by_province() -> None:
    """一张卡跨省：每个省各算一张卡（所以 sum(card_count) 可能大于总卡数）。"""
    country = node("中华人民共和国", 0)
    fujian = node("福建省", 1, parent=country.id, admin_code="350000")
    jiangxi = node("江西省", 1, parent=country.id, admin_code="360000")
    nodes = chain_of(country, fujian, jiangxi)

    card = uuid.uuid4()
    rows = [
        (uuid.uuid4(), card, fujian.id),
        (uuid.uuid4(), card, jiangxi.id),
    ]
    result = _aggregate_provinces(rows, nodes)

    assert [item.name for item in result.provinces] == ["江西省", "福建省"]
    assert all(item.card_count == 1 for item in result.provinces)
    assert len(result.located_cards) == 1


def test_aggregate_municipality_has_no_city() -> None:
    country = node("中华人民共和国", 0)
    beijing = node("北京市", 1, parent=country.id, admin_code="110000")
    dongcheng = node("东城区", 3, parent=beijing.id, admin_code="110101")
    nodes = chain_of(country, beijing, dongcheng)

    card = uuid.uuid4()
    result = _aggregate_provinces([(uuid.uuid4(), card, dongcheng.id)], nodes)

    assert len(result.provinces) == 1
    assert result.provinces[0].name == "北京市"
    # M4：不出现"北京市 → 北京市"；区不是市
    assert result.provinces[0].city_count == 0


def test_aggregate_merges_duplicate_province_nodes_by_name() -> None:
    """历史上重复灌种子会有两个"福建省"；地图只能出一行。"""
    country = node("中华人民共和国", 0)
    first = node("福建省", 1, parent=country.id)  # 没有 admin_code 的旧节点
    second = node("福建省", 1, parent=country.id, admin_code="350000")
    nodes = chain_of(country, first, second)

    rows = [
        (uuid.uuid4(), uuid.uuid4(), first.id),
        (uuid.uuid4(), uuid.uuid4(), second.id),
    ]
    result = _aggregate_provinces(rows, nodes)

    assert len(result.provinces) == 1
    merged = result.provinces[0]
    assert merged.name == "福建省"
    # 只要有一个节点带代码，就用它上色
    assert merged.admin_code == "350000"
    assert merged.code2 == "35"
    assert merged.card_count == 2


def test_aggregate_sorts_by_card_count_then_name() -> None:
    country = node("中华人民共和国", 0)
    names = [("福建省", "350000"), ("江西省", "360000"), ("广东省", "440000")]
    provinces = [node(name, 1, parent=country.id, admin_code=code) for name, code in names]
    nodes = chain_of(country, *provinces)

    rows = []
    for index, province in enumerate(provinces):
        # 福建省 1 张、江西省 2 张、广东省 3 张 → 按卡数降序
        for _ in range(index + 1):
            rows.append((uuid.uuid4(), uuid.uuid4(), province.id))

    result = _aggregate_provinces(rows, nodes)
    assert [(p.name, p.card_count) for p in result.provinces] == [
        ("广东省", 3),
        ("江西省", 2),
        ("福建省", 1),
    ]


def test_aggregate_reports_unresolved_place_chain_without_dropping_cards() -> None:
    """地点链断了：戳不进任何省，但卡仍算"有地点"，只留日志不静默丢。"""
    orphan = node("延平区", 3, parent=uuid.uuid4(), admin_code="350702")
    nodes = chain_of(orphan)

    card = uuid.uuid4()
    result = _aggregate_provinces([(uuid.uuid4(), card, orphan.id)], nodes)

    assert result.provinces == []
    assert result.located_cards == {card}
    assert result.unresolved_postmarks == 1


def test_aggregate_empty() -> None:
    result = _aggregate_provinces([], {})
    assert result.provinces == []
    assert result.located_cards == set()
    assert result.unresolved_postmarks == 0


# --------------------------------------------------------------------------- #
#  端点装配
# --------------------------------------------------------------------------- #


def test_gallery_map_returns_null_points_and_without_place() -> None:
    country = node("中华人民共和国", 0)
    province = node("福建省", 1, parent=country.id, admin_code="350000")
    city = node("南平市", 2, parent=province.id, admin_code="350700")

    card = uuid.uuid4()
    rows = [(uuid.uuid4(), card, city.id)]
    db = _MapDb(
        total_cards=3,  # 2 张卡 + 1 张没地点
        postmark_rows=rows,
        place_rows=[
            (p.id, p.parent_id, p.name, p.level, p.admin_code) for p in (country, province, city)
        ],
    )

    result = call_map(db)

    assert result.total_cards == 3
    # 只有 1 张卡有带地点戳 → 另外 2 张未填地点
    assert result.without_place == 2
    assert len(result.provinces) == 1
    assert result.provinces[0].name == "福建省"
    assert result.provinces[0].card_count == 1
    # points 必须恒为 None —— [] 会被误读成"这里有坐标但没有点"
    assert result.points is None


def test_gallery_map_empty_database() -> None:
    db = _MapDb(total_cards=0, postmark_rows=[], place_rows=[])
    result = call_map(db)

    assert result.total_cards == 0
    assert result.without_place == 0
    assert result.provinces == []
    assert result.points is None


def test_gallery_map_only_queries_when_there_are_rows() -> None:
    """没有夹带地点的戳时，不该再发一次祖先查询。"""
    db = _MapDb(total_cards=2, postmark_rows=[], place_rows=[])
    call_map(db)

    assert not any("place_nodes" in sql for sql in db.queries)


# --------------------------------------------------------------------------- #
#  查询条件（与时间轴同源）
# --------------------------------------------------------------------------- #


def test_map_query_uses_the_card_scope_of_the_timeline() -> None:
    """地图不另写统计口径：卡级条件必须来自 `_timeline_conditions`。"""
    from postmarktrip.api.routes_gallery import _timeline_conditions

    conds = _timeline_conditions(
        _MapDb(total_cards=0, postmark_rows=[], place_rows=[]),  # type: ignore[arg-type]
        year=None,
        visit_date_from=None,
        visit_date_to=None,
        place_id=None,
        tag_id=None,
        source=None,
        provenance="real_scan",
    )
    sql = str(
        select(Card)
        .where(*conds)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "card.deleted_at IS NULL" in sql
    # 默认开关：没有任何戳 OR 存在真实戳（00 §3.3）
    assert "NOT (EXISTS" in sql
