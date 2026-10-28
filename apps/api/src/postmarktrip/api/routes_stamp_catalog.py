"""标准邮票库（M3C）路由：手工维护 3 列 + 未关联清单。

设计边界（`docs/spec/m3/M3C-标准邮票库维护.md`）：
* 这里只做"标准票的身份/目录字段"，不做 Colnect 搜图（那是 colnetApi）；
* 官方页抓取/解析（M3C-3）不在本文件；
* 用户票通过 `stamp.catalog_id` 关联；关联时把目录字段**快照**到票上，
  保证票墙立即按标准信息分组；标准票更新时同步已关联票（标准库是权威）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from postmarktrip.api.deps import CurrentUser, DbDep
from postmarktrip.db.models import Card, Stamp, StampCatalog
from postmarktrip.domain.catalog_service import (
    ApplyParseError,
    apply_last_parse,
    run_fetch_job,
)
from postmarktrip.domain.schemas import (
    Paged,
    StampCatalogApplyParse,
    StampCatalogCreate,
    StampCatalogRead,
    StampCatalogUpdate,
    UnlinkedStampRead,
)
from postmarktrip.domain.stamp_catalog import (
    catalog_snapshot_values,
    generate_standard_code,
    normalize_issue_url,
)

router = APIRouter(tags=["stamp-catalog"])

SourceState = Literal["draft", "partial", "verified", "stale"]
FETCH_STALE_AFTER = timedelta(minutes=2)


def _get_catalog(db: Session, catalog_id: uuid.UUID) -> StampCatalog:
    row = db.get(StampCatalog, catalog_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "标准票不存在")
    return row


def _check_duplicates(
    db: Session,
    *,
    standard_code: str | None,
    colnect_c_id: int | None,
    issue_key: str | None,
    design_no: str | None,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """人工可读的查重：先硬键（编号/c_id），再软键（发行页+图序）。"""
    if standard_code:
        stmt = select(StampCatalog).where(StampCatalog.standard_code == standard_code)
        if exclude_id is not None:
            stmt = stmt.where(StampCatalog.id != exclude_id)
        hit = db.scalar(stmt)
        if hit is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"标准编号已存在：{hit.standard_code}"
            )

    if colnect_c_id is not None:
        stmt = select(StampCatalog).where(StampCatalog.colnect_c_id == colnect_c_id)
        if exclude_id is not None:
            stmt = stmt.where(StampCatalog.id != exclude_id)
        hit = db.scalar(stmt)
        if hit is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Colnect c_id 已关联到 {hit.standard_code}",
            )

    if issue_key and design_no:
        stmt = select(StampCatalog).where(
            StampCatalog.issue_key == issue_key,
            StampCatalog.design_no == design_no,
        )
        if exclude_id is not None:
            stmt = stmt.where(StampCatalog.id != exclude_id)
        hit = db.scalar(stmt)
        if hit is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"同一发行页 + 图序已存在：{hit.standard_code}",
            )


def _sync_linked_stamps(db: Session, catalog: StampCatalog) -> None:
    """标准票是权威：更新后把快照同步到已关联的用户票。"""
    values = catalog_snapshot_values(catalog)
    if not values:
        return
    db.execute(update(Stamp).where(Stamp.catalog_id == catalog.id).values(**values))


def _apply_catalog_to_stamp(db: Session, stamp: Stamp, catalog_id: uuid.UUID | None) -> None:
    """把一枚用户票关联到标准票，并复制可快照字段。"""
    if catalog_id is None:
        stamp.catalog_id = None
        return
    catalog = _get_catalog(db, catalog_id)
    stamp.catalog_id = catalog.id
    for key, value in catalog_snapshot_values(catalog).items():
        setattr(stamp, key, value)


# --------------------------------------------------------------------------- #
#  标准票 CRUD
# --------------------------------------------------------------------------- #


@router.get("/stamp-catalog", response_model=Paged[StampCatalogRead])
def list_stamp_catalog(
    db: DbDep,
    _: CurrentUser,
    q: Annotated[str | None, Query(max_length=200)] = None,
    source_state: Annotated[SourceState | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Paged[StampCatalogRead]:
    stmt = select(StampCatalog)
    count_stmt = select(func.count()).select_from(StampCatalog)

    if q:
        pattern = f"%{q.strip()}%"
        cond = or_(
            StampCatalog.standard_code.ilike(pattern),
            StampCatalog.catalog_ref.ilike(pattern),
            StampCatalog.design_name.ilike(pattern),
            StampCatalog.issue_name.ilike(pattern),
            StampCatalog.design_no.ilike(pattern),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    if source_state:
        stmt = stmt.where(StampCatalog.source_state == source_state)
        count_stmt = count_stmt.where(StampCatalog.source_state == source_state)

    total = db.scalar(count_stmt) or 0
    rows = list(
        db.scalars(
            stmt.order_by(StampCatalog.updated_at.desc(), StampCatalog.id)
            .offset((page - 1) * size)
            .limit(size)
        )
    )
    return Paged[StampCatalogRead](
        items=[StampCatalogRead.model_validate(row) for row in rows],
        total=total,
        page=page,
        size=size,
    )


@router.post(
    "/stamp-catalog",
    response_model=StampCatalogRead,
    status_code=status.HTTP_201_CREATED,
)
def create_stamp_catalog(
    payload: StampCatalogCreate, db: DbDep, _: CurrentUser
) -> StampCatalogRead:
    standard_code = (payload.standard_code or "").strip() or generate_standard_code()
    issue_key = normalize_issue_url(payload.issue_url)
    _check_duplicates(
        db,
        standard_code=standard_code,
        colnect_c_id=payload.colnect_c_id,
        issue_key=issue_key,
        design_no=payload.design_no,
    )

    data = payload.model_dump(exclude={"standard_code"})
    row = StampCatalog(
        **data,
        standard_code=standard_code,
        issue_key=issue_key,
        source_state="draft",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return StampCatalogRead.model_validate(row)


@router.get("/stamp-catalog/{catalog_id:uuid}", response_model=StampCatalogRead)
def get_stamp_catalog(catalog_id: uuid.UUID, db: DbDep, _: CurrentUser) -> StampCatalogRead:
    return StampCatalogRead.model_validate(_get_catalog(db, catalog_id))


@router.patch("/stamp-catalog/{catalog_id:uuid}", response_model=StampCatalogRead)
def update_stamp_catalog(
    catalog_id: uuid.UUID, payload: StampCatalogUpdate, db: DbDep, _: CurrentUser
) -> StampCatalogRead:
    row = _get_catalog(db, catalog_id)
    data = payload.model_dump(exclude_unset=True)

    if "issue_url" in data:
        row.issue_key = normalize_issue_url(data["issue_url"])

    _check_duplicates(
        db,
        standard_code=None,
        colnect_c_id=data.get("colnect_c_id"),
        issue_key=row.issue_key,
        design_no=data.get("design_no", row.design_no),
        exclude_id=row.id,
    )

    if "source_state" in data:
        new_state = data["source_state"]
        row.source_state = new_state
        if new_state == "verified":
            row.last_verified_at = datetime.now(UTC)
    for key, value in data.items():
        if key == "source_state":
            continue
        if key == "details" and value is None:
            value = {}
        setattr(row, key, value)

    _sync_linked_stamps(db, row)
    db.commit()
    db.refresh(row)
    return StampCatalogRead.model_validate(row)


@router.post(
    "/stamp-catalog/{catalog_id:uuid}/refresh",
    response_model=StampCatalogRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def refresh_stamp_catalog(
    catalog_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: DbDep,
    _: CurrentUser,
) -> StampCatalogRead:
    """抓取官方发行页（后台）+ 解析；解析结果先不写目录字段。"""
    row = _get_catalog(db, catalog_id)
    if not row.issue_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有原始发行网址")
    if row.fetch_status in ("queued", "running"):
        requested = row.fetch_requested_at
        if requested is not None and datetime.now(UTC) - requested < FETCH_STALE_AFTER:
            return StampCatalogRead.model_validate(row)
        # 超过 2 分钟还没结束，视为卡住；允许重新入队。

    row.fetch_status = "queued"
    row.fetch_error = None
    row.fetch_requested_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    background_tasks.add_task(run_fetch_job, str(row.id))
    return StampCatalogRead.model_validate(row)


@router.post(
    "/stamp-catalog/{catalog_id:uuid}/apply-parse",
    response_model=StampCatalogRead,
)
def apply_parse(
    catalog_id: uuid.UUID,
    payload: StampCatalogApplyParse,
    db: DbDep,
    _: CurrentUser,
) -> StampCatalogRead:
    """人工复核后应用解析结果；一页多枚时必须传 design_no。"""
    row = _get_catalog(db, catalog_id)
    try:
        row = apply_last_parse(db, row, payload.design_no)
    except ApplyParseError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    _sync_linked_stamps(db, row)
    db.commit()
    db.refresh(row)
    return StampCatalogRead.model_validate(row)


# --------------------------------------------------------------------------- #
#  未关联的用户票（票墙「未编目」入口）
# --------------------------------------------------------------------------- #


@router.get("/stamp-catalog/unlinked-stamps", response_model=Paged[UnlinkedStampRead])
def list_unlinked_stamps(
    db: DbDep,
    _: CurrentUser,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Paged[UnlinkedStampRead]:
    """有票记录、但还没关联标准票（且没有志号）的用户票。

    这正是票墙「未编目」的补录队列：点进去 → 建/选标准票 → 关联。
    """
    conds = [
        Stamp.deleted_at.is_(None),
        Card.deleted_at.is_(None),
        Stamp.catalog_id.is_(None),
        or_(Stamp.catalog_ref.is_(None), func.trim(Stamp.catalog_ref) == ""),
    ]
    base = select(Stamp).join(Card, Card.id == Stamp.card_id).where(*conds)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(
        select(
            Stamp.id,
            Stamp.card_id,
            Card.visit_date,
            Card.title,
            Stamp.catalog_ref,
            Stamp.name,
            Stamp.face_value,
            Stamp.currency,
            Stamp.issue_date,
        )
        .select_from(Stamp)
        .join(Card, Card.id == Stamp.card_id)
        .where(*conds)
        .order_by(Card.visit_date.desc(), Stamp.created_at.desc(), Stamp.id)
        .offset((page - 1) * size)
        .limit(size)
    ).all()

    items = [
        UnlinkedStampRead(
            stamp_id=stamp_id,
            card_id=card_id,
            card_visit_date=visit_date,
            card_title=title,
            catalog_ref=catalog_ref,
            name=name,
            face_value=face_value,
            currency=currency,
            issue_date=issue_date,
        )
        for (
            stamp_id,
            card_id,
            visit_date,
            title,
            catalog_ref,
            name,
            face_value,
            currency,
            issue_date,
        ) in rows
    ]
    return Paged[UnlinkedStampRead](items=items, total=total, page=page, size=size)


# 供 routes_cards 复用（避免把关联逻辑复制两份）
__all__ = ["_apply_catalog_to_stamp", "router"]
