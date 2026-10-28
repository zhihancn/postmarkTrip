"""卡片、邮戳、邮票路由。

分层：Card 是核心实体，Postmark / Stamp 是它的元素。
列表查询刻意用 **EXISTS 子查询**而不是 JOIN 来过滤邮戳相关条件 ——
JOIN 会因为"一张卡多个戳"而放大行数，导致分页计数错误。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from postmarktrip.api.deps import CurrentUser, DbDep
from postmarktrip.api.routes_stamp_catalog import _apply_catalog_to_stamp
from postmarktrip.db.models import Card, Place, Postmark, PostOffice, Stamp
from postmarktrip.domain import tagging
from postmarktrip.domain import vocab as V
from postmarktrip.domain.schemas import (
    CardCreate,
    CardListItem,
    CardQuery,
    CardRead,
    CardUpdate,
    OkResponse,
    Paged,
    PostmarkCreate,
    PostmarkRead,
    PostmarkUpdate,
    StampCreate,
    StampRead,
    StampUpdate,
    TagRead,
)

router = APIRouter(tags=["cards"])


# --------------------------------------------------------------------------- #
#  内部工具
# --------------------------------------------------------------------------- #


def _get_card(db: Session, card_id: uuid.UUID, *, include_deleted: bool = False) -> Card:
    stmt = (
        select(Card)
        .options(
            selectinload(Card.postmarks).selectinload(Postmark.place),
            selectinload(Card.postmarks).selectinload(Postmark.post_office),
            selectinload(Card.stamps),
        )
        .where(Card.id == card_id)
    )
    if not include_deleted:
        stmt = stmt.where(Card.deleted_at.is_(None))
    card = db.scalar(stmt)
    if card is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "卡片不存在")
    return card


def _tag_reads(db: Session, target_type: str, target_id: uuid.UUID) -> list[TagRead]:
    """把 ORM 的 ``list[Tag]`` 转成 schema 的 ``list[TagRead]``。

    为什么要这一层：``tagging.tags_for`` 返回 ORM 对象，而响应模型声明的是
    pydantic schema —— 直接赋值是**类型不匹配**（mypy 会报），
    而且运行时会依赖 pydantic 的宽容行为，一旦收紧校验就会在运行期炸。
    显式转换把这层差异放在一个地方，4 个调用点不必各写一遍推导式。
    """
    return [TagRead.model_validate(t) for t in tagging.tags_for(db, target_type, target_id)]


def _to_card_read(db: Session, card: Card) -> CardRead:
    out = CardRead.model_validate(card)
    out.tags = _tag_reads(db, tagging.TARGET_CARD, card.id)
    for pm in out.postmarks:
        pm.tags = _tag_reads(db, tagging.TARGET_POSTMARK, pm.id)
    return out


def _apply_filters(
    stmt: Select[tuple[Card]],
    f: CardQuery,
    db: Session,
    *,
    deleted: str = "exclude",
) -> Select[tuple[Card]]:
    """应用筛选条件。

    ``deleted`` 控制软删除记录的可见性：

    * ``exclude``（默认）—— 只看未删除的
    * ``only``           —— 只看回收站里的
    * ``include``        —— 全都要

    这个参数是**必需的**，不是可选装饰：回收站视图曾经因为
    「先 ``WHERE deleted_at IS NULL``、再 ``WHERE deleted_at IS NOT NULL``」
    两个互斥条件叠加而恒返回 0 行 —— 界面上表现为"回收站永远是空的"。
    """
    if deleted == "exclude":
        stmt = stmt.where(Card.deleted_at.is_(None))
    elif deleted == "only":
        stmt = stmt.where(Card.deleted_at.is_not(None))
    # deleted == "include" 时不加任何条件

    if f.visit_date_from:
        stmt = stmt.where(Card.visit_date >= f.visit_date_from)
    if f.visit_date_to:
        stmt = stmt.where(Card.visit_date <= f.visit_date_to)
    if f.source:
        stmt = stmt.where(Card.source == f.source.value)
    if f.status:
        stmt = stmt.where(Card.status == f.status.value)

    # ---- 邮戳相关条件：用 EXISTS，避免 JOIN 放大行数 ----
    pm_conds = []
    if f.postmark_type:
        pm_conds.append(Postmark.postmark_type == f.postmark_type.value)
    if f.is_color is not None:
        pm_conds.append(Postmark.is_color.is_(f.is_color))
    if f.provenance:
        pm_conds.append(Postmark.provenance == f.provenance.value)
    if f.post_office_id:
        pm_conds.append(Postmark.post_office_id == f.post_office_id)

    if f.place_id is not None:
        if f.place_descendants:
            parent = db.get(Place, f.place_id)
            if parent is not None and parent.path:
                # 物化路径前缀匹配 —— 这就是 path 存在的理由：
                # "按北京市筛选要带出所有区"
                sub = select(Place.id).where(Place.path.like(f"{parent.path}%"))
                pm_conds.append(Postmark.place_id.in_(sub))
            else:
                pm_conds.append(Postmark.place_id == f.place_id)
        else:
            pm_conds.append(Postmark.place_id == f.place_id)

    if pm_conds:
        stmt = stmt.where(
            exists(select(1).select_from(Postmark).where(Postmark.card_id == Card.id, *pm_conds))
        )

    # ---- 标签 ----
    if f.tag:
        from postmarktrip.db.models import Tag, TagLink

        tag_sub = (
            select(TagLink.target_id)
            .join(Tag, Tag.id == TagLink.tag_id)
            .where(TagLink.target_type == tagging.TARGET_CARD, Tag.name == f.tag)
        )
        stmt = stmt.where(Card.id.in_(tag_sub))

    # ---- 全文搜索 ----
    if f.q:
        like = f"%{f.q}%"
        pm_hit = exists(
            select(1)
            .select_from(Postmark)
            .where(
                Postmark.card_id == Card.id,
                or_(
                    Postmark.inscription.ilike(like),
                    Postmark.postmark_no.ilike(like),
                    Postmark.time_code.ilike(like),
                ),
            )
        )
        stamp_hit = exists(
            select(1)
            .select_from(Stamp)
            .where(
                Stamp.card_id == Card.id,
                or_(Stamp.name.ilike(like), Stamp.catalog_ref.ilike(like)),
            )
        )
        office_hit = exists(
            select(1)
            .select_from(Postmark)
            .join(PostOffice, PostOffice.id == Postmark.post_office_id)
            .where(Postmark.card_id == Card.id, PostOffice.name.ilike(like))
        )
        stmt = stmt.where(
            or_(Card.title.ilike(like), Card.note.ilike(like), pm_hit, stamp_hit, office_hit)
        )

    return stmt


@dataclass
class _CardSummary:
    """列表页需要的派生字段。

    刻意**不复用** ``CardListItem`` 当累加器：那要求为每张卡先构造一个完整的
    列表项（含必填的 ``visit_date`` / ``source`` / ``status``），于是不得不
    填一堆占位值。而正是"先建列表项、再往里填"这个思路让 ``_summaries``
    曾经静默返回空字典（见下）。
    """

    postmark_summary: str | None = None
    stamp_summary: str | None = None
    is_color: bool = False
    postmark_type: str | None = None
    place_names: list[str] = field(default_factory=list)


def _summaries(db: Session, card_ids: list[uuid.UUID]) -> dict[uuid.UUID, _CardSummary]:
    """一次批量取出列表页需要的派生字段，避免 N+1。

    ⚠️ **第一步必须为每张卡建占位条目。** 这里曾经写成 ``out = {}`` 之后
    直接 ``out.get(cid)`` —— 每次都是 ``None``、每个分支都 ``continue``，
    于是函数**静默返回空字典**。

    后果不是报错，而是卡片列表页上**戳面原文、票名·志号·面值、地点面包屑、
    彩色与戳种角标全部为空**。这类"不报错的空数据"只有真的看一眼界面才会
    发现，所以现在有一条带假 session 的单元测试兜着
    （``apps/api/tests/test_card_summaries.py``）。
    """
    if not card_ids:
        return {}

    # ★ 占位条目 —— 少了这一行，下面所有 out.get(cid) 都会是 None。
    out: dict[uuid.UUID, _CardSummary] = {cid: _CardSummary() for cid in card_ids}

    pm_rows = db.execute(
        select(Postmark.card_id, Postmark.inscription, Postmark.postmark_type, Postmark.is_color)
        .where(Postmark.card_id.in_(card_ids), Postmark.deleted_at.is_(None))
        .order_by(Postmark.created_at)
    ).all()
    for cid, inscription, ptype, is_color in pm_rows:
        item = out.get(cid)
        # 多戳卡片在列表上只反映**第一个**戳（按 created_at）——这是有意的：
        # 列表页要的是一行概览，不是全部内容（完整内容在详情页）。
        if item is None or item.postmark_summary is not None:
            continue
        item.postmark_summary = inscription
        item.postmark_type = ptype
        item.is_color = bool(is_color)

    st_rows = db.execute(
        select(Stamp.card_id, Stamp.name, Stamp.catalog_ref, Stamp.face_value, Stamp.currency)
        .where(Stamp.card_id.in_(card_ids), Stamp.deleted_at.is_(None))
        .order_by(Stamp.created_at)
    ).all()
    for cid, name, ref, face, cur in st_rows:
        item = out.get(cid)
        if item is None or item.stamp_summary is not None:
            continue
        parts = [p for p in (name, ref) if p]
        if face is not None:
            parts.append(f"{face}{cur or ''}")
        item.stamp_summary = " · ".join(parts) if parts else None

    # 地点按 level 升序，得到"国家 → 省 → 市 → 县"的面包屑顺序
    place_rows = db.execute(
        select(Postmark.card_id, Place.level, Place.name)
        .join(Place, Place.id == Postmark.place_id)
        .where(Postmark.card_id.in_(card_ids), Postmark.deleted_at.is_(None))
        .order_by(Postmark.card_id, Place.level)
    ).all()
    for cid, _level, name in place_rows:
        item = out.get(cid)
        if item is not None and name not in item.place_names:
            item.place_names.append(name)

    return out


# --------------------------------------------------------------------------- #
#  卡片
# --------------------------------------------------------------------------- #


@router.get("/cards", response_model=Paged[CardListItem])
def list_cards(
    db: DbDep,
    _: CurrentUser,
    q: Annotated[str | None, Query(max_length=200)] = None,
    visit_date_from: Annotated[date | None, Query()] = None,
    visit_date_to: Annotated[date | None, Query()] = None,
    place_id: Annotated[uuid.UUID | None, Query()] = None,
    place_descendants: bool = False,
    post_office_id: Annotated[uuid.UUID | None, Query()] = None,
    # 这四个用**枚举类型**声明，让 FastAPI 在解析请求时就校验。
    #
    # 之前声明成 str，再在函数体里构造 CardQuery —— 传入不认识的取值时，
    # Pydantic 会在处理函数**内部**抛 ValidationError，而 FastAPI 只把
    # 请求解析阶段的错误转成 422，处理函数里抛出的会变成 **500**。
    # 也就是说 `?status=foo` 会得到一个内部服务器错误，而不是"取值不合法"。
    #
    # 声明成枚举后：非法取值在进入函数之前就被挡下并返回带具体原因的 422，
    # 同时 OpenAPI 里会列出允许的取值（前端与文档都受益）。
    postmark_type: Annotated[V.PostmarkType | None, Query()] = None,
    source: Annotated[V.CardSource | None, Query()] = None,
    status_: Annotated[V.CardStatus | None, Query(alias="status")] = None,
    is_color: Annotated[bool | None, Query()] = None,
    provenance: Annotated[V.Provenance | None, Query()] = None,
    tag: Annotated[str | None, Query(max_length=64)] = None,
    include_deleted: bool = False,
    order_by: Annotated[str, Query(pattern="^(visit_date|created_at|title)$")] = "visit_date",
    order_desc: bool = True,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=200)] = 24,
) -> Paged[CardListItem]:
    f = CardQuery(
        q=q,
        visit_date_from=visit_date_from,
        visit_date_to=visit_date_to,
        place_id=place_id,
        place_descendants=place_descendants,
        post_office_id=post_office_id,
        postmark_type=postmark_type,
        source=source,
        status=status_,
        is_color=is_color,
        provenance=provenance,
        tag=tag,
        order_by=order_by,
        order_desc=order_desc,
        page=page,
        size=size,
    )

    base = _apply_filters(select(Card), f, db, deleted="only" if include_deleted else "exclude")

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    order_col = {"visit_date": Card.visit_date, "created_at": Card.created_at, "title": Card.title}[
        f.order_by
    ]
    order = order_col.desc().nullslast() if f.order_desc else order_col.asc().nullsfirst()

    cards = list(db.scalars(base.order_by(order, Card.id).offset((page - 1) * size).limit(size)))

    summaries = _summaries(db, [c.id for c in cards])
    items: list[CardListItem] = []
    for c in cards:
        item = CardListItem.model_validate(c)
        cached = summaries.get(c.id)
        if cached is not None:
            item.postmark_summary = cached.postmark_summary
            item.stamp_summary = cached.stamp_summary
            item.is_color = cached.is_color
            # `_summaries` 算出来的是 str（SQL 聚合的结果），
            # 而响应模型声明的是 PostmarkType 枚举。显式转换而不是
            # 靠 pydantic 的宽容行为 —— 后者一旦收紧校验就会在运行期炸。
            item.postmark_type = (
                V.PostmarkType(cached.postmark_type) if cached.postmark_type else None
            )
            item.place_names = cached.place_names
        items.append(item)

    return Paged[CardListItem](items=items, total=total, page=page, size=size)


@router.post("/cards", response_model=CardRead, status_code=status.HTTP_201_CREATED)
def create_card(payload: CardCreate, db: DbDep, user: CurrentUser) -> CardRead:
    card = Card(
        **payload.model_dump(exclude={"postmarks", "stamps", "tag_ids"}),
    )
    db.add(card)
    db.flush()

    for pm in payload.postmarks:
        row = Postmark(card_id=card.id, **pm.model_dump(exclude={"tag_ids"}))
        db.add(row)
        db.flush()  # 标签关联需要 id
        if pm.tag_ids:
            # 曾经漏掉这一句，导致「新建卡时随卡提交的邮戳标签」被静默丢弃。
            tagging.set_tags(db, tagging.TARGET_POSTMARK, row.id, pm.tag_ids)

    for st in payload.stamps:
        stamp_row = Stamp(card_id=card.id, **st.model_dump(exclude={"catalog_id"}))
        if st.catalog_id is not None:
            _apply_catalog_to_stamp(db, stamp_row, st.catalog_id)
        db.add(stamp_row)

    if payload.tag_ids:
        tagging.set_tags(db, tagging.TARGET_CARD, card.id, payload.tag_ids)

    db.commit()
    card = _get_card(db, card.id)
    return _to_card_read(db, card)


@router.get("/cards/{card_id}", response_model=CardRead)
def get_card(card_id: uuid.UUID, db: DbDep, _: CurrentUser) -> CardRead:
    return _to_card_read(db, _get_card(db, card_id))


@router.patch("/cards/{card_id}", response_model=CardRead)
def update_card(card_id: uuid.UUID, payload: CardUpdate, db: DbDep, _: CurrentUser) -> CardRead:
    card = _get_card(db, card_id)
    data = payload.model_dump(exclude_unset=True, exclude={"tag_ids"})
    for k, v in data.items():
        setattr(card, k, v)
    if payload.tag_ids is not None:
        tagging.set_tags(db, tagging.TARGET_CARD, card_id, payload.tag_ids)
    db.commit()
    return _to_card_read(db, _get_card(db, card_id))


@router.delete("/cards/{card_id}", response_model=OkResponse)
def delete_card(card_id: uuid.UUID, db: DbDep, _: CurrentUser) -> OkResponse:
    """软删除。30 天内可通过 /restore 恢复。"""
    card = _get_card(db, card_id)
    card.deleted_at = func.now()
    db.commit()
    return OkResponse(message="已移入回收站")


@router.post("/cards/{card_id}/restore", response_model=CardRead)
def restore_card(card_id: uuid.UUID, db: DbDep, _: CurrentUser) -> CardRead:
    card = _get_card(db, card_id, include_deleted=True)
    card.deleted_at = None
    db.commit()
    return _to_card_read(db, _get_card(db, card_id))


@router.delete("/cards/{card_id}/purge", response_model=OkResponse)
def purge_card(card_id: uuid.UUID, db: DbDep, _: CurrentUser) -> OkResponse:
    """彻底删除。**不删除任何资产** —— 原始件是 L0 证据，只进不出。"""
    card = _get_card(db, card_id, include_deleted=True)
    for pm in list(card.postmarks):
        tagging.purge_for(db, tagging.TARGET_POSTMARK, pm.id)
    tagging.purge_for(db, tagging.TARGET_CARD, card.id)
    db.delete(card)
    db.commit()
    return OkResponse(message="已彻底删除")


# --------------------------------------------------------------------------- #
#  邮戳
# --------------------------------------------------------------------------- #


@router.post(
    "/cards/{card_id}/postmarks", response_model=PostmarkRead, status_code=status.HTTP_201_CREATED
)
def add_postmark(
    card_id: uuid.UUID, payload: PostmarkCreate, db: DbDep, _: CurrentUser
) -> PostmarkRead:
    card = _get_card(db, card_id)
    pm = Postmark(card_id=card.id, **payload.model_dump(exclude={"tag_ids"}))
    db.add(pm)
    db.flush()
    if payload.tag_ids:
        tagging.set_tags(db, tagging.TARGET_POSTMARK, pm.id, payload.tag_ids)
    db.commit()
    db.refresh(pm)
    out = PostmarkRead.model_validate(pm)
    out.tags = _tag_reads(db, tagging.TARGET_POSTMARK, pm.id)
    return out


@router.patch("/postmarks/{postmark_id}", response_model=PostmarkRead)
def update_postmark(
    postmark_id: uuid.UUID, payload: PostmarkUpdate, db: DbDep, _: CurrentUser
) -> PostmarkRead:
    pm = db.get(Postmark, postmark_id)
    if pm is None or pm.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邮戳不存在")
    for k, v in payload.model_dump(exclude_unset=True, exclude={"tag_ids"}).items():
        setattr(pm, k, v)
    if payload.tag_ids is not None:
        tagging.set_tags(db, tagging.TARGET_POSTMARK, postmark_id, payload.tag_ids)
    db.commit()
    db.refresh(pm)
    out = PostmarkRead.model_validate(pm)
    out.tags = _tag_reads(db, tagging.TARGET_POSTMARK, pm.id)
    return out


@router.delete("/postmarks/{postmark_id}", response_model=OkResponse)
def delete_postmark(postmark_id: uuid.UUID, db: DbDep, _: CurrentUser) -> OkResponse:
    pm = db.get(Postmark, postmark_id)
    if pm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邮戳不存在")
    pm.deleted_at = func.now()
    db.commit()
    return OkResponse(message="已删除")


# --------------------------------------------------------------------------- #
#  邮票
# --------------------------------------------------------------------------- #


@router.get("/stamps/{stamp_id}", response_model=StampRead)
def get_stamp(stamp_id: uuid.UUID, db: DbDep, _: CurrentUser) -> Stamp:
    st = db.get(Stamp, stamp_id)
    if st is None or st.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邮票不存在")
    return st


@router.post(
    "/cards/{card_id}/stamps", response_model=StampRead, status_code=status.HTTP_201_CREATED
)
def add_stamp(card_id: uuid.UUID, payload: StampCreate, db: DbDep, _: CurrentUser) -> Stamp:
    card = _get_card(db, card_id)
    st = Stamp(card_id=card.id, **payload.model_dump(exclude={"catalog_id"}))
    if payload.catalog_id is not None:
        _apply_catalog_to_stamp(db, st, payload.catalog_id)
    db.add(st)
    db.commit()
    db.refresh(st)
    return st


@router.patch("/stamps/{stamp_id}", response_model=StampRead)
def update_stamp(stamp_id: uuid.UUID, payload: StampUpdate, db: DbDep, _: CurrentUser) -> Stamp:
    st = db.get(Stamp, stamp_id)
    if st is None or st.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邮票不存在")
    for k, v in payload.model_dump(exclude_unset=True, exclude={"catalog_id"}).items():
        setattr(st, k, v)
    if "catalog_id" in payload.model_fields_set:
        _apply_catalog_to_stamp(db, st, payload.catalog_id)
    elif st.catalog_id is not None:
        # 已关联的票：目录字段以标准票为准，避免旧草稿覆盖标准库的同步结果
        _apply_catalog_to_stamp(db, st, st.catalog_id)
    db.commit()
    db.refresh(st)
    return st


@router.delete("/stamps/{stamp_id}", response_model=OkResponse)
def delete_stamp(stamp_id: uuid.UUID, db: DbDep, _: CurrentUser) -> OkResponse:
    st = db.get(Stamp, stamp_id)
    if st is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邮票不存在")
    st.deleted_at = func.now()
    db.commit()
    return OkResponse(message="已删除")
