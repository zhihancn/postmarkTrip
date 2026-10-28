"""展览集（画廊）路由。

时间轴（M3a）、戳墙（M3b）、票墙（M3c）、地图（M3d）都在这一个模块：
四个视图共用同一套筛选条件（`_timeline_conditions` / `_card_provenance_condition`），
拆文件会让"同一查询"变成四份各自演化的口径。

## 三条刻意的约定

1. **只读、要登录**：画廊端点不写库。筛选与聚合都在查询层做，
   不缓存、不物化 —— 数据量到万级再谈。
2. **返回的是"卡片级聚合"**：时间轴一行 = 一张卡。前端拿到就能渲染，
   不需要为 30 行再各发一次请求（`_summaries` 那个 N+1 教训）。
3. **缺图 / 未建戳必须计数**（`docs/spec/m3/00` §3.8、验收 G4/G7）：
   这些记录仍然出现在 `items` 里，只是数量单独给出来。
   静默少一行会让人以为自己的收藏丢了。

`provenance` 的口径见 `00` §3.3：**卡看"有没有任一真实戳"**，
所以这里用 `EXISTS postmark WHERE provenance=…`，不是卡的列（卡没有这一列）。
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from sqlalchemy import ColumnElement, and_, exists, func, literal, or_, select
from sqlalchemy.orm import Session

from postmarktrip.api.deps import CurrentUser, DbDep
from postmarktrip.db.models import Asset, Card, Place, Postmark, PostOffice, Stamp, Tag, TagLink
from postmarktrip.domain import tagging, vocab
from postmarktrip.domain.regions import province_code2
from postmarktrip.domain.schemas import (
    LABELS,
    GalleryMap,
    GalleryMapProvince,
    GalleryPostmarkGroup,
    GalleryPostmarkItem,
    GalleryPostmarkWall,
    GalleryStampGroup,
    GalleryStampItem,
    GalleryStampWall,
    GalleryTimeline,
    GalleryTimelineItem,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["gallery"])

#: `all` 是 UI 的"关闭只看真实跑戳"—— 它不是一个 provenance 取值，
#: 而是"不加 provenance 条件"。放进字面量类型里，让非法取值在请求解析阶段
#: 就得到 422，而不是在函数体里抛 500（`routes_cards` 踩过这个坑）。
ProvenanceFilter = Literal["real_scan", "generated", "third_party", "all"]


# --------------------------------------------------------------------------- #
#  查询条件（拆出来是为了能被单元测试直接编译成 SQL 检查）
# --------------------------------------------------------------------------- #


def _card_provenance_condition(
    provenance: ProvenanceFilter,
) -> ColumnElement[bool] | None:
    """"卡上有没有真实戳"（时间轴 / 票墙共用，`00` §3.3）。

    `all` 返回 None = 不加条件。

    `real_scan` 刻意是 `没有任何戳 OR 存在真实戳`：
    * 没有戳的卡仍然保留（时间轴要显示"未建戳"；票墙要显示"只有票没戳"的合法卡）；
    * 如果只写 `EXISTS real_scan`，`without_postmark` / 合法的"只有票"会被静默隐藏。
    """
    if provenance == "all":
        return None
    any_postmark = exists(
        select(1)
        .select_from(Postmark)
        .where(Postmark.card_id == Card.id, Postmark.deleted_at.is_(None))
    )
    matched = exists(
        select(1)
        .select_from(Postmark)
        .where(
            Postmark.card_id == Card.id,
            Postmark.deleted_at.is_(None),
            Postmark.provenance == provenance,
        )
    )
    return or_(~any_postmark, matched) if provenance == "real_scan" else matched


def _timeline_conditions(
    db: Session,
    *,
    year: int | None,
    visit_date_from: date | None,
    visit_date_to: date | None,
    place_id: uuid.UUID | None,
    tag_id: uuid.UUID | None,
    source: vocab.CardSource | None,
    provenance: ProvenanceFilter,
) -> list[ColumnElement[bool]]:
    """时间轴的全部筛选条件。

    刻意返回条件列表而不是拼好的语句：计数查询与分页查询必须**用同一组条件**，
    否则会出现"总数 12、翻页只有 9 条"这类只在第二页才看得见的错。
    """
    conds: list[ColumnElement[bool]] = [Card.deleted_at.is_(None)]

    if year is not None:
        conds.append(Card.visit_date >= date(year, 1, 1))
        conds.append(Card.visit_date <= date(year, 12, 31))
    if visit_date_from is not None:
        conds.append(Card.visit_date >= visit_date_from)
    if visit_date_to is not None:
        conds.append(Card.visit_date <= visit_date_to)
    if source is not None:
        conds.append(Card.source == source.value)

    # ---- 邮戳相关：一律用 EXISTS，避免 JOIN 因"一卡多戳"放大行数 ----
    provenance_cond = _card_provenance_condition(provenance)
    if provenance_cond is not None:
        conds.append(provenance_cond)

    if place_id is not None:
        parent = db.get(Place, place_id)
        if parent is not None and parent.path:
            # 物化路径前缀匹配："选福建省要带出所有下级"
            place_cond: ColumnElement[bool] = Postmark.place_id.in_(
                select(Place.id).where(Place.path.like(f"{parent.path}%"))
            )
        else:
            place_cond = Postmark.place_id == place_id
        conds.append(
            exists(
                select(1)
                .select_from(Postmark)
                .where(
                    Postmark.card_id == Card.id,
                    Postmark.deleted_at.is_(None),
                    place_cond,
                )
            )
        )

    if tag_id is not None:
        # 画廊按"卡上的标签"筛（不是戳上的）—— 与时间轴一行 = 一张卡一致。
        conds.append(
            Card.id.in_(
                select(TagLink.target_id).where(
                    TagLink.target_type == tagging.TARGET_CARD,
                    TagLink.tag_id == tag_id,
                )
            )
        )

    return conds


def _timeline_counts(db: Session, conds: list[ColumnElement[bool]]) -> tuple[int, int, int]:
    """(total, with_cut, without_postmark)。

    `with_cut` 用 `count(card.image_asset_id)` —— SQL 的 count(列) 天然跳过 NULL，
    比 `count(*) filter (...)` 少一处重复表达。
    """
    any_postmark = exists(
        select(1)
        .select_from(Postmark)
        .where(Postmark.card_id == Card.id, Postmark.deleted_at.is_(None))
    )
    row = db.execute(
        select(
            func.count().label("total"),
            func.count(Card.image_asset_id).label("with_cut"),
            func.count().filter(~any_postmark).label("without_postmark"),
        )
        .select_from(Card)
        .where(*conds)
    ).one()
    return int(row.total), int(row.with_cut), int(row.without_postmark)


# --------------------------------------------------------------------------- #
#  卡片级聚合（一次批量取，不随页大小增长查询次数）
# --------------------------------------------------------------------------- #


@dataclass
class _CardFacts:
    """一页里每张卡的派生字段。**先给每张卡建占位条目** —— `_summaries` 的教训。"""

    inscriptions: list[str] = field(default_factory=list)
    postmark_types: list[str] = field(default_factory=list)
    office_names: list[str] = field(default_factory=list)
    place_id: uuid.UUID | None = None
    tag_names: list[str] = field(default_factory=list)


#: place.id -> (name, parent_id)
PlaceChain = dict[uuid.UUID, tuple[str, uuid.UUID | None]]


def _load_place_chain(db: Session, place_ids: set[uuid.UUID]) -> PlaceChain:
    """一次递归 CTE 取回这些地点的**全部祖先**。

    为什么不逐级查：每级一次往返，深一点就是 5–6 次；为什么不按 `path` 反查：
    `path` 存的是行政代码，不能直接得到名字链。
    """
    if not place_ids:
        return {}

    anchor = (
        select(Place.id, Place.parent_id, Place.name)
        .where(Place.id.in_(place_ids))
        .cte("place_chain", recursive=True)
    )
    step = select(Place.id, Place.parent_id, Place.name).join(
        anchor, Place.id == anchor.c.parent_id
    )
    chain = anchor.union_all(step)
    rows = db.execute(select(chain.c.id, chain.c.name, chain.c.parent_id)).all()
    return {pid: (name, parent_id) for pid, name, parent_id in rows}


@dataclass(frozen=True)
class _PlaceNode:
    """地图上溯需要的一行地点：名字链不够，还要 level 与 admin_code。"""

    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    level: int
    admin_code: str | None


def _load_place_nodes(db: Session, place_ids: set[uuid.UUID]) -> dict[uuid.UUID, _PlaceNode]:
    """递归取回这些地点的全部祖先（含 level / admin_code）。

    与 `_load_place_chain` 是同一棵树的两种投影：那个给时间轴/戳墙做"名字链"，
    这个给地图找 **level=1 的省** 与 `admin_code`。

    递归带 `depth` 上限（64 层）：数据里若真有环，没有上限的 `UNION ALL`
    会一直跑下去，接口表现为挂死而不是报错；有上限时最多多取几行、
    由 `_ancestor_at` 的 visited 集合收尾。
    """
    if not place_ids:
        return {}

    columns = (Place.id, Place.parent_id, Place.name, Place.level, Place.admin_code)
    anchor = (
        select(*columns, literal(0).label("depth"))
        .where(Place.id.in_(place_ids))
        .cte("place_nodes", recursive=True)
    )
    step = (
        select(*columns, (anchor.c.depth + 1).label("depth"))
        .join(anchor, Place.id == anchor.c.parent_id)
        .where(anchor.c.depth < 64)
    )
    nodes = anchor.union_all(step)
    rows = db.execute(
        select(nodes.c.id, nodes.c.parent_id, nodes.c.name, nodes.c.level, nodes.c.admin_code)
    ).all()
    return {
        row[0]: _PlaceNode(
            id=row[0],
            parent_id=row[1],
            name=row[2],
            level=row[3],
            admin_code=row[4],
        )
        for row in rows
    }


def _place_path(place_id: uuid.UUID | None, chain: PlaceChain) -> list[str]:
    """从粗到细的地点名。找不到的祖先直接跳过（不编造）。"""
    names: list[str] = []
    current = place_id
    seen: set[uuid.UUID] = set()
    while current is not None and current in chain and current not in seen:
        seen.add(current)
        name, parent_id = chain[current]
        names.append(name)
        current = parent_id
    names.reverse()
    return names


def _timeline_items(db: Session, cards: list[Card]) -> list[GalleryTimelineItem]:
    """把 ORM 卡片 + 批量派生数据拼成列表项。"""
    if not cards:
        return []

    card_ids = [card.id for card in cards]
    facts: dict[uuid.UUID, _CardFacts] = {cid: _CardFacts() for cid in card_ids}

    # 戳 + 局名一次取回（outer join：没有局名也要有戳）
    pm_rows = db.execute(
        select(
            Postmark.card_id,
            Postmark.inscription,
            Postmark.postmark_type,
            Postmark.place_id,
            PostOffice.name,
        )
        .outerjoin(PostOffice, PostOffice.id == Postmark.post_office_id)
        .where(Postmark.card_id.in_(card_ids), Postmark.deleted_at.is_(None))
        .order_by(Postmark.card_id, Postmark.created_at)
    ).all()

    place_ids: set[uuid.UUID] = set()
    for card_id, inscription, postmark_type, place_id, office_name in pm_rows:
        item = facts.get(card_id)
        if item is None:
            continue
        item.inscriptions.append(inscription)
        if postmark_type not in item.postmark_types:
            item.postmark_types.append(postmark_type)
        if office_name and office_name not in item.office_names:
            item.office_names.append(office_name)
        if place_id is not None:
            place_ids.add(place_id)
            if item.place_id is None:
                item.place_id = place_id

    # 卡上的标签一次取回
    tag_rows = db.execute(
        select(TagLink.target_id, Tag.name)
        .join(Tag, Tag.id == TagLink.tag_id)
        .where(
            TagLink.target_type == tagging.TARGET_CARD,
            TagLink.target_id.in_(card_ids),
        )
        .order_by(TagLink.target_id, Tag.name)
    ).all()
    for card_id, name in tag_rows:
        item = facts.get(card_id)
        if item is not None:
            item.tag_names.append(name)

    chain = _load_place_chain(db, place_ids)

    out: list[GalleryTimelineItem] = []
    for card in cards:
        item = facts[card.id]
        out.append(
            GalleryTimelineItem(
                card_id=card.id,
                visit_date=card.visit_date,
                title=card.title,
                thumb_asset_id=card.image_asset_id,
                place_path=_place_path(item.place_id, chain),
                office_names=item.office_names,
                postmark_count=len(item.inscriptions),
                inscriptions=item.inscriptions,
                postmark_types=item.postmark_types,
                tag_names=item.tag_names,
                has_scene=card.scene is not None,
                source=vocab.CardSource(card.source),
            )
        )
    return out


# --------------------------------------------------------------------------- #
#  端点
# --------------------------------------------------------------------------- #


@router.get("/gallery/timeline", response_model=GalleryTimeline)
def gallery_timeline(
    db: DbDep,
    _: CurrentUser,
    year: Annotated[int | None, Query(ge=1800, le=2200)] = None,
    visit_date_from: Annotated[date | None, Query()] = None,
    visit_date_to: Annotated[date | None, Query()] = None,
    place_id: Annotated[uuid.UUID | None, Query()] = None,
    tag_id: Annotated[uuid.UUID | None, Query()] = None,
    source: Annotated[vocab.CardSource | None, Query()] = None,
    provenance: Annotated[ProvenanceFilter, Query()] = "real_scan",
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 30,
) -> GalleryTimeline:
    """时间轴：按 `visit_date` 倒序的卡片流，每页默认 30。

    排序是 `visit_date DESC, created_at DESC` —— **不按入库时间**：
    用户的心智顺序是"我哪天跑的戳"，不是"我哪天录的"。
    """
    conds = _timeline_conditions(
        db,
        year=year,
        visit_date_from=visit_date_from,
        visit_date_to=visit_date_to,
        place_id=place_id,
        tag_id=tag_id,
        source=source,
        provenance=provenance,
    )
    total, with_cut, without_postmark = _timeline_counts(db, conds)

    stmt = (
        select(Card)
        .where(*conds)
        .order_by(Card.visit_date.desc(), Card.created_at.desc(), Card.id)
        .offset((page - 1) * size)
        .limit(size)
    )
    cards = list(db.scalars(stmt))

    return GalleryTimeline(
        total=total,
        with_cut=with_cut,
        without_cut=total - with_cut,
        without_postmark=without_postmark,
        items=_timeline_items(db, cards),
        page=page,
        size=size,
    )


# --------------------------------------------------------------------------- #
#  戳墙（M3b）
# --------------------------------------------------------------------------- #

PostmarkGroupBy = Literal["type", "color", "tag", "province", "none"]
PostmarkSort = Literal["visit_desc", "visit_asc"]


def _postmark_conditions(
    db: Session,
    *,
    year: int | None,
    visit_date_from: date | None,
    visit_date_to: date | None,
    place_id: uuid.UUID | None,
    tag_id: uuid.UUID | None,
    postmark_type: vocab.PostmarkType | None,
    is_color: bool | None,
    provenance: ProvenanceFilter,
) -> list[ColumnElement[bool]]:
    """戳墙的筛选条件。

    与时间轴的两点不同：
    * **必须 JOIN card** —— 卡被删了，卡上的戳也不该出现在墙上；
    * provenance 看**戳自己**（`00` §3.3），不是"卡上有任一真实戳"。
    """
    conds: list[ColumnElement[bool]] = [
        Postmark.deleted_at.is_(None),
        Card.deleted_at.is_(None),
    ]

    if year is not None:
        conds.append(Card.visit_date >= date(year, 1, 1))
        conds.append(Card.visit_date <= date(year, 12, 31))
    if visit_date_from is not None:
        conds.append(Card.visit_date >= visit_date_from)
    if visit_date_to is not None:
        conds.append(Card.visit_date <= visit_date_to)
    if postmark_type is not None:
        conds.append(Postmark.postmark_type == postmark_type.value)
    if is_color is not None:
        conds.append(Postmark.is_color.is_(is_color))
    if provenance != "all":
        conds.append(Postmark.provenance == provenance)

    if place_id is not None:
        parent = db.get(Place, place_id)
        if parent is not None and parent.path:
            conds.append(
                Postmark.place_id.in_(
                    select(Place.id).where(Place.path.like(f"{parent.path}%"))
                )
            )
        else:
            conds.append(Postmark.place_id == place_id)

    if tag_id is not None:
        # 戳墙按**戳上的标签**筛（不是卡上的）—— 与"一枚戳一块瓦片"一致。
        conds.append(
            Postmark.id.in_(
                select(TagLink.target_id).where(
                    TagLink.target_type == tagging.TARGET_POSTMARK,
                    TagLink.tag_id == tag_id,
                )
            )
        )

    return conds


def _postmark_counts(
    db: Session, conds: list[ColumnElement[bool]]
) -> tuple[int, int]:
    """(total, with_cut)。`without_cut` = total - with_cut。"""
    row = db.execute(
        select(
            func.count().label("total"),
            func.count(Postmark.image_asset_id).label("with_cut"),
        )
        .select_from(Postmark)
        .join(Card, Card.id == Postmark.card_id)
        .where(*conds)
    ).one()
    return int(row.total), int(row.with_cut)


@dataclass
class _WallEntry:
    item: GalleryPostmarkItem
    #: 从粗到细的地点链（用于按省份分组）
    place_path: list[str]


def _place_names(path: list[str]) -> str | None:
    """去掉国家那一级，得到 "福建省 / 南平市 / 延平区"。"""
    if not path:
        return None
    return " / ".join(path[1:] if len(path) > 1 else path)


def _wall_entries(
    db: Session,
    rows: Sequence[Any],
) -> list[_WallEntry]:
    """把一页邮戳行 + 批量取回的标签/地点链拼成瓦片。"""
    if not rows:
        return []

    postmark_ids: list[uuid.UUID] = [row[0] for row in rows]
    place_ids: set[uuid.UUID] = {row[7] for row in rows if row[7] is not None}
    chain = _load_place_chain(db, place_ids)

    tag_rows = db.execute(
        select(TagLink.target_id, Tag.name)
        .join(Tag, Tag.id == TagLink.tag_id)
        .where(
            TagLink.target_type == tagging.TARGET_POSTMARK,
            TagLink.target_id.in_(postmark_ids),
        )
        .order_by(TagLink.target_id, Tag.name)
    ).all()
    tags_by_postmark: dict[uuid.UUID, list[str]] = {}
    for postmark_id, name in tag_rows:
        tags_by_postmark.setdefault(postmark_id, []).append(name)

    entries: list[_WallEntry] = []
    for (
        postmark_id,
        card_id,
        asset_id,
        inscription,
        postmark_type,
        is_color,
        color_layers,
        place_id,
        office_name,
        visit_date,
    ) in rows:
        path = _place_path(place_id, chain)
        item = GalleryPostmarkItem(
            postmark_id=postmark_id,
            card_id=card_id,
            asset_id=asset_id,
            inscription=inscription,
            visit_date=visit_date,
            place_name=_place_names(path),
            office_name=office_name,
            is_color=bool(is_color),
            color_layers=color_layers,
            tag_names=tags_by_postmark.get(postmark_id, []),
            postmark_type=postmark_type,
        )
        entries.append(_WallEntry(item=item, place_path=path))
    return entries


def _bucket_of(entry: _WallEntry, group_by: PostmarkGroupBy) -> tuple[str, str]:
    """(key, label)。空分组也保留（"未分省"/"无标签"），不静默丢瓦片。"""
    item = entry.item
    if group_by == "type":
        return item.postmark_type, LABELS["postmark_type"].get(
            item.postmark_type, item.postmark_type
        )
    if group_by == "color":
        return ("color", "套色") if item.is_color else ("mono", "单色")
    if group_by == "tag":
        name = item.tag_names[0] if item.tag_names else ""
        return (name or "untagged", name or "无标签")
    if group_by == "province":
        # place_path[0] 是国家；省级是 [1]。没有地点的归"未分省"并置底。
        province = entry.place_path[1] if len(entry.place_path) > 1 else ""
        return (province or "unspecified", province or "未分省")
    return "all", "全部"


def _group_wall_entries(
    entries: list[_WallEntry], group_by: PostmarkGroupBy
) -> list[GalleryPostmarkGroup]:
    """按组聚合（组内保持查询顺序：visit_date DESC）。

    ⚠️ 一枚戳有多个标签时**只按第一个标签归组**（按名字排序后的第一个）。
    否则同一块瓦片会在多个组里重复出现、计数也跟着虚高。
    """
    buckets: dict[str, tuple[str, list[GalleryPostmarkItem]]] = {}
    for entry in entries:
        key, label = _bucket_of(entry, group_by)
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = (label, [entry.item])
        else:
            bucket[1].append(entry.item)

    type_order = {value.value: index for index, value in enumerate(vocab.PostmarkType)}
    color_order = {"mono": 0, "color": 1}

    if group_by == "type":
        ordered = sorted(buckets.items(), key=lambda kv: type_order.get(kv[0], 999))
    elif group_by == "color":
        ordered = sorted(buckets.items(), key=lambda kv: color_order.get(kv[0], 999))
    elif group_by in ("tag", "province"):
        # 有名字的按名字排；"无标签"/"未分省"置底
        ordered = sorted(
            buckets.items(),
            key=lambda kv: (kv[0] in ("untagged", "unspecified"), kv[1][0]),
        )
    else:
        ordered = list(buckets.items())

    return [
        GalleryPostmarkGroup(key=key, label=label, count=len(items), items=items)
        for key, (label, items) in ordered
    ]


@router.get("/gallery/postmarks", response_model=GalleryPostmarkWall)
def gallery_postmarks(
    db: DbDep,
    _: CurrentUser,
    year: Annotated[int | None, Query(ge=1800, le=2200)] = None,
    visit_date_from: Annotated[date | None, Query()] = None,
    visit_date_to: Annotated[date | None, Query()] = None,
    place_id: Annotated[uuid.UUID | None, Query()] = None,
    tag_id: Annotated[uuid.UUID | None, Query()] = None,
    type_: Annotated[vocab.PostmarkType | None, Query(alias="type")] = None,
    is_color: Annotated[bool | None, Query()] = None,
    provenance: Annotated[ProvenanceFilter, Query()] = "real_scan",
    group_by: Annotated[PostmarkGroupBy, Query()] = "type",
    sort: Annotated[PostmarkSort, Query()] = "visit_desc",
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 60,
) -> GalleryPostmarkWall:
    """戳墙：一页 60 枚戳，按 `group_by` 分组。

    **不 JOIN stamp、不判断"这张卡有没有票"** —— 那是票墙的职责
    （`00` §3.8）。这里只回答"我盖过哪些戳"。
    """
    conds = _postmark_conditions(
        db,
        year=year,
        visit_date_from=visit_date_from,
        visit_date_to=visit_date_to,
        place_id=place_id,
        tag_id=tag_id,
        postmark_type=type_,
        is_color=is_color,
        provenance=provenance,
    )
    total, with_cut = _postmark_counts(db, conds)

    if sort == "visit_asc":
        order_by = (Card.visit_date.asc(), Postmark.created_at.asc(), Postmark.id)
    else:
        order_by = (Card.visit_date.desc(), Postmark.created_at.desc(), Postmark.id)

    rows = db.execute(
        select(
            Postmark.id,
            Postmark.card_id,
            Postmark.image_asset_id,
            Postmark.inscription,
            Postmark.postmark_type,
            Postmark.is_color,
            Postmark.color_layers,
            Postmark.place_id,
            PostOffice.name,
            Card.visit_date,
        )
        .select_from(Postmark)
        .join(Card, Card.id == Postmark.card_id)
        .outerjoin(PostOffice, PostOffice.id == Postmark.post_office_id)
        .where(*conds)
        .order_by(*order_by)
        .offset((page - 1) * size)
        .limit(size)
    ).all()

    entries = _wall_entries(db, rows)
    return GalleryPostmarkWall(
        total=total,
        with_cut=with_cut,
        without_cut=total - with_cut,
        groups=_group_wall_entries(entries, group_by),
        page=page,
        size=size,
    )


# --------------------------------------------------------------------------- #
#  票墙（M3c）
# --------------------------------------------------------------------------- #

StampGroupBy = Literal["series", "face", "year", "tag", "none"]
StampSeries = Literal["pu", "ji", "te", "yearly", "other", "uncatalogued"]

#: 系列名与排序（"未编目"置底，便于回填志号）
_SERIES_LABELS = {
    "pu": "普",
    "ji": "纪",
    "te": "特",
    "yearly": "编年",
    "other": "其他",
    "uncatalogued": "未编目",
}
_SERIES_ORDER = {key: index for index, key in enumerate(_SERIES_LABELS)}

_YEARLY_RE = re.compile(r"^\d{4}-\d+")


def _series_of(catalog_ref: str | None) -> tuple[str, str]:
    """从志号前缀推断系列（`03` §四）。"""
    ref = (catalog_ref or "").strip()
    if ref == "":
        return "uncatalogued", _SERIES_LABELS["uncatalogued"]
    if ref.startswith("普"):
        return "pu", _SERIES_LABELS["pu"]
    if ref.startswith("纪"):
        return "ji", _SERIES_LABELS["ji"]
    if ref.startswith("特"):
        return "te", _SERIES_LABELS["te"]
    if _YEARLY_RE.match(ref):
        return "yearly", _SERIES_LABELS["yearly"]
    return "other", _SERIES_LABELS["other"]


def _trim_decimal(value: Decimal) -> str:
    """Decimal → 最短人话（去尾零，不用 float，`03` S2）。"""
    return format(value.normalize(), "f")


def _money_text(value: Decimal) -> str:
    """金额保留两位（`1.20` 不会变成 `1.2`，也不会出现浮点尾巴）。"""
    return format(value.quantize(Decimal("0.01")), "f")


def _face_label(value: Decimal | None, currency: str | None) -> str:
    """面值人话：CNY < 1 元显示"分"，否则"元"；其它币种带代码。"""
    if value is None:
        return "面值未知"
    cur = (currency or "CNY").upper()
    if cur in ("CNY", "RMB"):
        if value < 1:
            return f"{_trim_decimal(value * 100)}分"
        return f"{_money_text(value)}元"
    return f"{_money_text(value)} {cur}"


def _stamp_conditions(
    db: Session,
    *,
    year: int | None,
    visit_date_from: date | None,
    visit_date_to: date | None,
    place_id: uuid.UUID | None,
    tag_id: uuid.UUID | None,
    series: StampSeries | None,
    catalog_system: vocab.CatalogSystem | None,
    face_min: Decimal | None,
    face_max: Decimal | None,
    provenance: ProvenanceFilter,
) -> list[ColumnElement[bool]]:
    """票墙筛选。

    provenance **跟随卡**（`00` §3.3）；默认 `real_scan` 下"只有票、没有戳"的卡
    仍然保留（`03` §二：那是合法状态）。
    """
    conds: list[ColumnElement[bool]] = [
        Stamp.deleted_at.is_(None),
        Card.deleted_at.is_(None),
    ]

    if year is not None:
        conds.append(Card.visit_date >= date(year, 1, 1))
        conds.append(Card.visit_date <= date(year, 12, 31))
    if visit_date_from is not None:
        conds.append(Card.visit_date >= visit_date_from)
    if visit_date_to is not None:
        conds.append(Card.visit_date <= visit_date_to)
    if catalog_system is not None:
        conds.append(Stamp.catalog_system == catalog_system.value)
    if face_min is not None:
        conds.append(Stamp.face_value >= face_min)
    if face_max is not None:
        conds.append(Stamp.face_value <= face_max)

    ref = Stamp.catalog_ref
    trimmed = func.trim(ref)
    if series == "pu":
        conds.append(ref.like("普%"))
    elif series == "ji":
        conds.append(ref.like("纪%"))
    elif series == "te":
        conds.append(ref.like("特%"))
    elif series == "yearly":
        conds.append(ref.regexp_match(r"^\d{4}-\d+"))
    elif series == "other":
        conds.append(and_(ref.is_not(None), trimmed != ""))
        conds.append(~ref.like("普%"))
        conds.append(~ref.like("纪%"))
        conds.append(~ref.like("特%"))
        conds.append(~ref.regexp_match(r"^\d{4}-\d+"))
    elif series == "uncatalogued":
        conds.append(or_(ref.is_(None), trimmed == ""))

    provenance_cond = _card_provenance_condition(provenance)
    if provenance_cond is not None:
        conds.append(provenance_cond)

    if place_id is not None:
        parent = db.get(Place, place_id)
        if parent is not None and parent.path:
            place_sub = select(Place.id).where(Place.path.like(f"{parent.path}%"))
            conds.append(
                exists(
                    select(1)
                    .select_from(Postmark)
                    .where(
                        Postmark.card_id == Card.id,
                        Postmark.deleted_at.is_(None),
                        Postmark.place_id.in_(place_sub),
                    )
                )
            )
        else:
            conds.append(
                exists(
                    select(1)
                    .select_from(Postmark)
                    .where(
                        Postmark.card_id == Card.id,
                        Postmark.deleted_at.is_(None),
                        Postmark.place_id == place_id,
                    )
                )
            )

    if tag_id is not None:
        # 票墙按**票上的标签**筛。
        conds.append(
            Stamp.id.in_(
                select(TagLink.target_id).where(
                    TagLink.target_type == tagging.TARGET_STAMP,
                    TagLink.tag_id == tag_id,
                )
            )
        )

    return conds


def _stamp_counts(db: Session, conds: list[ColumnElement[bool]]) -> tuple[int, int]:
    row = db.execute(
        select(
            func.count().label("total"),
            func.count(Stamp.image_asset_id).label("with_cut"),
        )
        .select_from(Stamp)
        .join(Card, Card.id == Stamp.card_id)
        .where(*conds)
    ).one()
    return int(row.total), int(row.with_cut)


def _cards_without_stamp(db: Session) -> tuple[int, uuid.UUID | None]:
    """(数量, 最近一张卡的 id)：**票未录**的主要缺口（`03` §二）。

    这是全库口径，不受票墙当前筛选影响 —— 空态要回答"还有多少张卡没补票"。
    """
    has_postmark = exists(
        select(1)
        .select_from(Postmark)
        .where(Postmark.card_id == Card.id, Postmark.deleted_at.is_(None))
    )
    has_stamp = exists(
        select(1)
        .select_from(Stamp)
        .where(Stamp.card_id == Card.id, Stamp.deleted_at.is_(None))
    )
    where = (Card.deleted_at.is_(None), has_postmark, ~has_stamp)
    count = db.scalar(select(func.count()).select_from(Card).where(*where)) or 0
    first = db.scalar(
        select(Card.id)
        .where(*where)
        .order_by(Card.visit_date.desc(), Card.created_at.desc())
        .limit(1)
    )
    return int(count), first


def _stamp_items(db: Session, rows: Sequence[Any]) -> list[GalleryStampItem]:
    if not rows:
        return []

    stamp_ids: list[uuid.UUID] = [row[0] for row in rows]
    tag_rows = db.execute(
        select(TagLink.target_id, Tag.name)
        .join(Tag, Tag.id == TagLink.tag_id)
        .where(
            TagLink.target_type == tagging.TARGET_STAMP,
            TagLink.target_id.in_(stamp_ids),
        )
        .order_by(TagLink.target_id, Tag.name)
    ).all()
    tags_by_stamp: dict[uuid.UUID, list[str]] = {}
    for stamp_id, name in tag_rows:
        tags_by_stamp.setdefault(stamp_id, []).append(name)

    items: list[GalleryStampItem] = []
    for (
        stamp_id,
        card_id,
        asset_id,
        catalog_ref,
        catalog_system,
        name,
        face_value,
        currency,
        issue_date,
        visit_date,
        asset_width,
        asset_height,
    ) in rows:
        items.append(
            GalleryStampItem(
                stamp_id=stamp_id,
                card_id=card_id,
                asset_id=asset_id,
                catalog_ref=catalog_ref,
                catalog_system=catalog_system,
                name=name,
                face_value=face_value,
                currency=currency,
                face_label=_face_label(face_value, currency),
                issue_date=issue_date,
                visit_date=visit_date,
                tag_names=tags_by_stamp.get(stamp_id, []),
                asset_width=asset_width,
                asset_height=asset_height,
            )
        )
    return items


def _stamp_bucket(item: GalleryStampItem, group_by: StampGroupBy) -> tuple[str, str]:
    if group_by == "series":
        return _series_of(item.catalog_ref)
    if group_by == "face":
        if item.face_value is None:
            return "unknown", _face_label(None, None)
        currency = (item.currency or "CNY").upper()
        return f"{currency}:{_trim_decimal(item.face_value)}", item.face_label
    if group_by == "year":
        if item.issue_date is None:
            return "unknown", "未知"
        return str(item.issue_date.year), str(item.issue_date.year)
    if group_by == "tag":
        name = item.tag_names[0] if item.tag_names else ""
        return (name or "untagged", name or "无标签")
    return "all", "全部"


def _group_stamp_items(
    items: list[GalleryStampItem], group_by: StampGroupBy
) -> list[GalleryStampGroup]:
    buckets: dict[str, tuple[str, list[GalleryStampItem]]] = {}
    face_values: dict[str, Decimal | None] = {}
    for item in items:
        key, label = _stamp_bucket(item, group_by)
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = (label, [item])
        else:
            bucket[1].append(item)
        if group_by == "face":
            face_values[key] = item.face_value

    if group_by == "series":
        ordered = sorted(buckets.items(), key=lambda kv: _SERIES_ORDER.get(kv[0], 999))
    elif group_by == "face":
        ordered = sorted(
            buckets.items(),
            key=lambda kv: (
                face_values.get(kv[0]) is None,
                face_values.get(kv[0]) or Decimal(0),
            ),
        )
    elif group_by == "year":
        ordered = sorted(
            buckets.items(),
            key=lambda kv: (kv[0] == "unknown", -int(kv[0]) if kv[0] != "unknown" else 0),
        )
    elif group_by == "tag":
        ordered = sorted(
            buckets.items(),
            key=lambda kv: (kv[0] == "untagged", kv[1][0]),
        )
    else:
        ordered = list(buckets.items())

    return [
        GalleryStampGroup(key=key, label=label, count=len(group_items), items=group_items)
        for key, (label, group_items) in ordered
    ]


@router.get("/gallery/stamps", response_model=GalleryStampWall)
def gallery_stamps(
    db: DbDep,
    _: CurrentUser,
    year: Annotated[int | None, Query(ge=1800, le=2200)] = None,
    visit_date_from: Annotated[date | None, Query()] = None,
    visit_date_to: Annotated[date | None, Query()] = None,
    place_id: Annotated[uuid.UUID | None, Query()] = None,
    tag_id: Annotated[uuid.UUID | None, Query()] = None,
    series: Annotated[StampSeries | None, Query()] = None,
    catalog_system: Annotated[vocab.CatalogSystem | None, Query()] = None,
    face_min: Annotated[Decimal | None, Query(ge=0)] = None,
    face_max: Annotated[Decimal | None, Query(ge=0)] = None,
    provenance: Annotated[ProvenanceFilter, Query()] = "real_scan",
    group_by: Annotated[StampGroupBy, Query()] = "series",
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 60,
) -> GalleryStampWall:
    """票墙：一页 60 枚票，按 `group_by` 分组。

    **空墙不是 bug**：确认建卡时不建 `stamp` 行，只有"框票"才会建。
    `cards_without_stamp` 明确指出这个数据缺口，并给"去框票"的入口。
    """
    conds = _stamp_conditions(
        db,
        year=year,
        visit_date_from=visit_date_from,
        visit_date_to=visit_date_to,
        place_id=place_id,
        tag_id=tag_id,
        series=series,
        catalog_system=catalog_system,
        face_min=face_min,
        face_max=face_max,
        provenance=provenance,
    )
    total, with_cut = _stamp_counts(db, conds)
    missing_count, missing_first = _cards_without_stamp(db)

    rows = db.execute(
        select(
            Stamp.id,
            Stamp.card_id,
            Stamp.image_asset_id,
            Stamp.catalog_ref,
            Stamp.catalog_system,
            Stamp.name,
            Stamp.face_value,
            Stamp.currency,
            Stamp.issue_date,
            Card.visit_date,
            Asset.width,
            Asset.height,
        )
        .select_from(Stamp)
        .join(Card, Card.id == Stamp.card_id)
        .outerjoin(Asset, Asset.id == Stamp.image_asset_id)
        .where(*conds)
        .order_by(Card.visit_date.desc(), Stamp.created_at.desc(), Stamp.id)
        .offset((page - 1) * size)
        .limit(size)
    ).all()

    items = _stamp_items(db, rows)
    return GalleryStampWall(
        total=total,
        with_cut=with_cut,
        stamps_without_cut=total - with_cut,
        cards_without_stamp=missing_count,
        first_card_without_stamp=missing_first,
        groups=_group_stamp_items(items, group_by),
        page=page,
        size=size,
    )


# --------------------------------------------------------------------------- #
#  地图（M3d，形态 B：省级清单 + 静态省份图）
# --------------------------------------------------------------------------- #

MapLevel = Literal["province"]


def _ancestor_at(
    db_place_id: uuid.UUID,
    level: int,
    nodes: dict[uuid.UUID, _PlaceNode],
) -> _PlaceNode | None:
    """从某地点上溯，找**第一个** `level` 层的祖先（含自己）。

    直辖市在树里是「省级 → 县级」：区是 level=3，父级直接是 level=1。
    上溯到 level=2 会先遇到 level=1（`current.level < level`）并返回 None ——
    这正是要的"不要用县数冒充市数"（`04` §五）。
    """
    current = nodes.get(db_place_id)
    seen: set[uuid.UUID] = set()
    while current is not None and current.id not in seen:
        if current.level == level:
            return current
        if current.level < level:
            return None
        seen.add(current.id)
        current = nodes.get(current.parent_id) if current.parent_id is not None else None
    return None


@dataclass
class _ProvinceBucket:
    place_id: uuid.UUID
    name: str
    admin_code: str | None
    code2: str | None
    cards: set[uuid.UUID] = field(default_factory=set)
    postmarks: int = 0
    cities: set[uuid.UUID] = field(default_factory=set)


@dataclass
class _MapAggregate:
    provinces: list[GalleryMapProvince]
    #: 至少有一枚"带地点戳"的卡（不一定能归到省 —— 见 `unresolved_postmarks`）
    located_cards: set[uuid.UUID]
    #: 有 place_id、但祖先链里找不到 level=1 的戳数（数据异常，正常为 0）
    unresolved_postmarks: int


def _aggregate_provinces(
    rows: Sequence[tuple[uuid.UUID, uuid.UUID, uuid.UUID]],
    nodes: dict[uuid.UUID, _PlaceNode],
) -> _MapAggregate:
    """把 (戳, 卡, 地点) 行按**省级祖先**聚合成地图数据。

    纯函数：不碰数据库，测试可以直接喂夹具（`test_gallery_map.py`）。

    * `card_count` 按卡去重 —— 一张卡两个跨省戳会在两个省各 +1；
    * `postmark_count` 按戳计数，是次要数字；
    * `city_count` 数 level=2 的**祖先**：戳落在县里（level=3）也算到它所属的市；
    * 同名省级节点合并（历史上可能因重复灌种子出现两个"福建省"），
      否则地图列表会出两行、SVG 却只有一块。
    """
    buckets: dict[str, _ProvinceBucket] = {}
    located: set[uuid.UUID] = set()
    unresolved = 0

    for _postmark_id, card_id, place_id in rows:
        located.add(card_id)

        province = _ancestor_at(place_id, 1, nodes)
        if province is None:
            unresolved += 1
            continue

        bucket = buckets.get(province.name)
        if bucket is None:
            bucket = _ProvinceBucket(
                place_id=province.id,
                name=province.name,
                admin_code=province.admin_code,
                code2=province_code2(province.admin_code, province.name),
            )
            buckets[province.name] = bucket
        elif bucket.admin_code is None and province.admin_code:
            # 两个同名节点里只要有一个带代码，就用它给 SVG 上色。
            bucket.admin_code = province.admin_code
            bucket.code2 = province_code2(province.admin_code, bucket.name)

        bucket.cards.add(card_id)
        bucket.postmarks += 1

        city = _ancestor_at(place_id, 2, nodes)
        if city is not None:
            bucket.cities.add(city.id)

    provinces = [
        GalleryMapProvince(
            place_id=bucket.place_id,
            name=bucket.name,
            admin_code=bucket.admin_code,
            code2=bucket.code2,
            card_count=len(bucket.cards),
            city_count=len(bucket.cities),
            postmark_count=bucket.postmarks,
        )
        for bucket in sorted(buckets.values(), key=lambda item: (-len(item.cards), item.name))
    ]
    return _MapAggregate(
        provinces=provinces,
        located_cards=located,
        unresolved_postmarks=unresolved,
    )


@router.get("/gallery/map", response_model=GalleryMap)
def gallery_map(
    db: DbDep,
    _: CurrentUser,
    year: Annotated[int | None, Query(ge=1800, le=2200)] = None,
    visit_date_from: Annotated[date | None, Query()] = None,
    visit_date_to: Annotated[date | None, Query()] = None,
    place_id: Annotated[uuid.UUID | None, Query()] = None,
    tag_id: Annotated[uuid.UUID | None, Query()] = None,
    source: Annotated[vocab.CardSource | None, Query()] = None,
    provenance: Annotated[ProvenanceFilter, Query()] = "real_scan",
    level: Annotated[MapLevel, Query()] = "province",
) -> GalleryMap:
    """地图：按省聚合卡数 / 戳数 / 市数（形态 B，不需要经纬度）。

    **筛选条件与时间轴完全同一组**（`_timeline_conditions`）：
    地图是列表的可视化，不是独立事实源（`04` §四）。多个筛选项同时出现时
    与时间轴给出同一批卡 —— 否则用户会看到"图上 5 张、列表 3 张"。

    `points` 恒为 null：现在 `place.lat/lng` 覆盖率是 0（`04` §一），
    返回 [] 或假坐标都会让前端误以为"这里没有戳"。

    `level` 现在只接受 `province`；留着参数是为了以后城市级下钻是加法，
    而不是改端点签名。
    """
    conds = _timeline_conditions(
        db,
        year=year,
        visit_date_from=visit_date_from,
        visit_date_to=visit_date_to,
        place_id=place_id,
        tag_id=tag_id,
        source=source,
        provenance=provenance,
    )
    total_cards = int(db.scalar(select(func.count()).select_from(Card).where(*conds)) or 0)

    # ⚠️ 不要把 Postmark JOIN Card 写在外层再从 `conds` 里带 EXISTS：
    # `_timeline_conditions` 的每条 EXISTS 都以 `Postmark` 为子查询的 FROM，
    # 外层一旦也有 `Postmark`，SQLAlchemy 的自动关联会把子查询的 FROM 关联掉，
    # 报 "returned no FROM clauses due to auto-correlation"。
    # 先把"这批卡"收成一个子查询，外层只查 postmark，语义也更容易读。
    card_scope = select(Card.id).where(*conds).subquery()
    rows = db.execute(
        select(Postmark.id, Postmark.card_id, Postmark.place_id)
        .where(
            Postmark.card_id.in_(select(card_scope.c.id)),
            Postmark.deleted_at.is_(None),
            Postmark.place_id.is_not(None),
        )
        .order_by(Postmark.card_id, Postmark.id)
    ).all()

    # `place_id.is_not(None)` 在 SQL 层已经过滤过；这里再收一次是给类型看，
    # 也顺手把 Row 变成普通元组（`_aggregate_provinces` 是纯函数）。
    located_rows = [(row[0], row[1], row[2]) for row in rows if row[2] is not None]
    nodes = _load_place_nodes(db, {row[2] for row in located_rows})
    aggregate = _aggregate_provinces(located_rows, nodes)

    if aggregate.unresolved_postmarks:
        # 不静默丢：这些戳有 place_id，但树上没有 level=1 的祖先（数据坏了）。
        # 界面上表现为"总数对不上"，所以至少留一条日志可查。
        logger.warning(
            "gallery map: %s postmark(s) have a place_id but no level=1 ancestor",
            aggregate.unresolved_postmarks,
        )

    return GalleryMap(
        total_cards=total_cards,
        # 没有"带地点戳"的卡 = 未填地点。不是"没有坐标"（04 §五）。
        without_place=total_cards - len(aggregate.located_cards),
        provinces=aggregate.provinces,
        points=None,
    )
