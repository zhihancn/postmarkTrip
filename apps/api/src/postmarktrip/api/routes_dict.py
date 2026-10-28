"""字典路由：行政区划树、邮局、标签、词表。

行政区划用**自引用树 + 物化路径**，而不是固定层级列。理由见
docs/need/03 §五：中国 4–5 级、日本 3 级、美国 3 级都能装进同一张表；
直辖市如实存为「省级 → 县级」，中间层留空，不做「北京市-北京市-东城区」的取巧。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from postmarktrip.api.deps import CurrentUser, DbDep
from postmarktrip.api.http import UNPROCESSABLE
from postmarktrip.db.models import Place, PostOffice, Tag, TagLink
from postmarktrip.domain import vocab as V
from postmarktrip.domain.schemas import (
    LABELS,
    OkResponse,
    Paged,
    PlaceCreate,
    PlaceNode,
    PlaceRead,
    PlaceUpdate,
    PostOfficeCreate,
    PostOfficeRead,
    PostOfficeUpdate,
    TagCreate,
    TagRead,
    VocabOption,
    VocabResponse,
)

router = APIRouter(tags=["dict"])


# --------------------------------------------------------------------------- #
#  物化路径维护
# --------------------------------------------------------------------------- #


def _compute_path(db: DbDep | object, node: Place, parent: Place | None) -> str:
    """``path`` 规则：国家节点用国家码做可读前缀，其余用自身 UUID。

    形如 ``/CN/<uuid>/<uuid>/`` —— 前缀可读，其余无歧义，且不受 admin_code 变更影响。
    """
    if parent is None:
        return f"/{node.country_code or node.id}/"
    base = parent.path or f"/{parent.id}/"
    return f"{base}{node.id}/"


def _rebuild_subtree_paths(db: object, node: Place) -> None:
    """节点移动后重算整棵子树的 path。"""
    from sqlalchemy.orm import Session

    assert isinstance(db, Session)
    stack = [node]
    while stack:
        cur = stack.pop()
        cur.path = _compute_path(db, cur, cur.parent)
        db.flush()
        stack.extend(cur.children)


# --------------------------------------------------------------------------- #
#  行政区划
# --------------------------------------------------------------------------- #


@router.get("/places", response_model=list[PlaceRead])
def list_places(
    db: DbDep,
    parent_id: Annotated[uuid.UUID | None, Query()] = None,
    country_code: Annotated[str | None, Query(max_length=2)] = None,
    level: Annotated[int | None, Query(ge=0, le=10)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[Place]:
    stmt = select(Place)
    if parent_id is not None:
        stmt = stmt.where(Place.parent_id == parent_id)
    if country_code:
        stmt = stmt.where(Place.country_code == country_code.upper())
    if level is not None:
        stmt = stmt.where(Place.level == level)
    if q:
        stmt = stmt.where(or_(Place.name.ilike(f"%{q}%"), Place.name_en.ilike(f"%{q}%")))
    stmt = stmt.order_by(Place.level, Place.name).limit(limit)
    return list(db.scalars(stmt))


@router.get("/places/tree", response_model=list[PlaceNode])
def place_tree(
    db: DbDep,
    root_id: Annotated[uuid.UUID | None, Query()] = None,
    depth: Annotated[int, Query(ge=1, le=6)] = 2,
    country_code: Annotated[str | None, Query(max_length=2)] = None,
) -> list[PlaceNode]:
    """按需展开的树。**不一次拉全树** —— 中国五级区划有几十万节点。

    实现：先取根层，再按层批量取子节点（每层一次查询，共 depth 次）。
    """
    nodes: dict[uuid.UUID, PlaceNode] = {}

    def to_node(p: Place) -> PlaceNode:
        n = PlaceNode.model_validate(p)
        n.children = []
        nodes[p.id] = n
        return n

    if root_id is not None:
        root = db.get(Place, root_id)
        if root is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "地点不存在")
        current = [to_node(root)]
    else:
        stmt = select(Place).where(Place.parent_id.is_(None))
        if country_code:
            stmt = stmt.where(Place.country_code == country_code.upper())
        current = [to_node(p) for p in db.scalars(stmt.order_by(Place.name))]

    for _ in range(depth - 1):
        if not current:
            break
        ids = [n.id for n in current]
        children = list(
            db.scalars(select(Place).where(Place.parent_id.in_(ids)).order_by(Place.name))
        )
        buckets: dict[uuid.UUID, list[PlaceNode]] = {}
        for c in children:
            buckets.setdefault(c.parent_id, []).append(to_node(c))  # type: ignore[arg-type]
        for n in current:
            n.children = buckets.get(n.id, [])
        current = [c for lst in buckets.values() for c in lst]

    # 邮局数：一次查询覆盖本层全部节点，避免 N+1。
    # 此前从未赋值，所以字典页的"邮局数"一列恒显示 0。
    if nodes:
        counts = dict(
            db.execute(
                select(PostOffice.place_id, func.count())
                .where(PostOffice.place_id.in_(list(nodes)))
                .group_by(PostOffice.place_id)
            ).all()
        )
        for pid, node in nodes.items():
            node.post_office_count = int(counts.get(pid, 0))

    roots = [n for n in nodes.values() if n.parent_id is None or n.parent_id not in nodes]
    return roots if root_id is None else [nodes[root_id]]


@router.post("/places", response_model=PlaceRead, status_code=status.HTTP_201_CREATED)
def create_place(payload: PlaceCreate, db: DbDep, _: CurrentUser) -> Place:
    parent = None
    if payload.parent_id is not None:
        parent = db.get(Place, payload.parent_id)
        if parent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "上级地点不存在")
        if payload.level <= parent.level:
            raise HTTPException(
                UNPROCESSABLE,
                f"层级必须深于上级（上级 level={parent.level}）",
            )

    node = Place(**payload.model_dump())
    if node.country_code:
        node.country_code = node.country_code.upper()
    if node.country_code is None and parent is not None:
        node.country_code = parent.country_code  # 继承国家码
    db.add(node)
    db.flush()
    node.path = _compute_path(db, node, parent)
    db.commit()
    db.refresh(node)
    return node


@router.patch("/places/{place_id}", response_model=PlaceRead)
def update_place(place_id: uuid.UUID, payload: PlaceUpdate, db: DbDep, _: CurrentUser) -> Place:
    node = db.get(Place, place_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "地点不存在")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(node, k, v)
    if node.country_code:
        node.country_code = node.country_code.upper()
    db.commit()
    db.refresh(node)
    return node


@router.delete("/places/{place_id}", response_model=OkResponse)
def delete_place(place_id: uuid.UUID, db: DbDep, _: CurrentUser) -> OkResponse:
    node = db.get(Place, place_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "地点不存在")
    has_children = db.scalar(
        select(func.count()).select_from(Place).where(Place.parent_id == place_id)
    )
    if has_children:
        raise HTTPException(status.HTTP_409_CONFLICT, "该地点下还有下级，无法删除")
    # 被邮戳引用时不允许删除（Postmark.place_id 是 SET NULL，静默置空会更糟）
    from postmarktrip.db.models import Postmark

    refs = db.scalar(
        select(func.count()).select_from(Postmark).where(Postmark.place_id == place_id)
    )
    if refs:
        raise HTTPException(status.HTTP_409_CONFLICT, f"已被 {refs} 条邮戳引用，无法删除")
    db.delete(node)
    db.commit()
    return OkResponse(message="已删除")


# --------------------------------------------------------------------------- #
#  邮局
# --------------------------------------------------------------------------- #


@router.get("/post-offices", response_model=Paged[PostOfficeRead])
def list_post_offices(
    db: DbDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    place_id: Annotated[uuid.UUID | None, Query()] = None,
    place_descendants: Annotated[bool, Query()] = False,
    is_theme: Annotated[bool | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Paged[PostOfficeRead]:
    stmt = select(PostOffice)
    if place_id is not None:
        if place_descendants:
            parent = db.get(Place, place_id)
            if parent is not None and parent.path:
                sub = select(Place.id).where(Place.path.like(f"{parent.path}%"))
                stmt = stmt.where(PostOffice.place_id.in_(sub))
            else:
                stmt = stmt.where(PostOffice.place_id == place_id)
        else:
            stmt = stmt.where(PostOffice.place_id == place_id)
    if is_theme is not None:
        stmt = stmt.where(PostOffice.is_theme.is_(is_theme))
    if q:
        stmt = stmt.where(or_(PostOffice.name.ilike(f"%{q}%"), PostOffice.postal_code == q))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(PostOffice.name).offset((page - 1) * size).limit(size)).all()
    return Paged[PostOfficeRead](
        items=[PostOfficeRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        size=size,
    )


@router.get("/post-offices/{office_id}", response_model=PostOfficeRead)
def get_post_office(office_id: uuid.UUID, db: DbDep) -> PostOffice:
    """按 id 反查邮局。

    前端的 PostOfficePicker 需要它：只拿到 ``post_office_id`` 时
    （例如从列表或旧链接进入）要能补出名称，否则只能显示
    "已选邮局（名称未加载）"。
    """
    row = db.get(PostOffice, office_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邮局不存在")
    return row


@router.post("/post-offices", response_model=PostOfficeRead, status_code=status.HTTP_201_CREATED)
def create_post_office(payload: PostOfficeCreate, db: DbDep, _: CurrentUser) -> PostOffice:
    if payload.place_id is not None and db.get(Place, payload.place_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "所选地点不存在")
    row = PostOffice(**payload.model_dump())
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:  # pragma: no cover
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "邮局已存在或数据冲突") from exc
    db.refresh(row)
    return row


@router.patch("/post-offices/{office_id}", response_model=PostOfficeRead)
def update_post_office(
    office_id: uuid.UUID, payload: PostOfficeUpdate, db: DbDep, _: CurrentUser
) -> PostOffice:
    row = db.get(PostOffice, office_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邮局不存在")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/post-offices/{office_id}", response_model=OkResponse)
def delete_post_office(office_id: uuid.UUID, db: DbDep, _: CurrentUser) -> OkResponse:
    row = db.get(PostOffice, office_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邮局不存在")
    from postmarktrip.db.models import Postmark

    refs = db.scalar(
        select(func.count()).select_from(Postmark).where(Postmark.post_office_id == office_id)
    )
    if refs:
        raise HTTPException(status.HTTP_409_CONFLICT, f"已被 {refs} 条邮戳引用，无法删除")
    db.delete(row)
    db.commit()
    return OkResponse(message="已删除")


# --------------------------------------------------------------------------- #
#  标签
# --------------------------------------------------------------------------- #


@router.get("/tags", response_model=list[TagRead])
def list_tags(db: DbDep, category: Annotated[V.TagCategory | None, Query()] = None) -> list[Tag]:
    stmt = select(Tag)
    if category is not None:
        stmt = stmt.where(Tag.category == category.value)
    return list(db.scalars(stmt.order_by(Tag.category, Tag.name)))


@router.post("/tags", response_model=TagRead, status_code=status.HTTP_201_CREATED)
def create_tag(payload: TagCreate, db: DbDep, _: CurrentUser) -> Tag:
    row = Tag(
        name=payload.name.strip(),
        category=payload.category.value if payload.category else None,
        color=payload.color,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "标签已存在") from exc
    db.refresh(row)
    return row


@router.delete("/tags/{tag_id}", response_model=OkResponse)
def delete_tag(tag_id: uuid.UUID, db: DbDep, _: CurrentUser) -> OkResponse:
    row = db.get(Tag, tag_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "标签不存在")
    db.execute(delete(TagLink).where(TagLink.tag_id == tag_id))
    db.delete(row)
    db.commit()
    return OkResponse(message="已删除")


# --------------------------------------------------------------------------- #
#  词表
# --------------------------------------------------------------------------- #


def _opts(key: str, values: list[str]) -> list[VocabOption]:
    return [VocabOption(value=v, label=LABELS[key].get(v, v)) for v in values]


@router.get("/vocab", response_model=VocabResponse)
def get_vocab() -> VocabResponse:
    """前端下拉框的数据源。**前端不硬编码任何枚举** —— 词表只有一个归属地。"""
    return VocabResponse(
        postmark_types=_opts("postmark_type", [e.value for e in V.PostmarkType]),
        primary_postmark_types=[e.value for e in V.PRIMARY_POSTMARK_TYPES],
        card_sources=_opts("card_source", [e.value for e in V.CardSource]),
        card_statuses=_opts("card_status", [e.value for e in V.CardStatus]),
        catalog_systems=_opts("catalog_system", [e.value for e in V.CatalogSystem]),
        provenances=_opts("provenance", [e.value for e in V.Provenance]),
        tag_categories=_opts("tag_category", [e.value for e in V.TagCategory]),
        level_labels=[e.value for e in V.LevelLabel],
        face_value_suggestions=[
            {"value": v, "currency": c}
            for v, c in V.PREFERRED_FACE_VALUES + V.FACE_VALUE_SUGGESTIONS
        ],
    )
