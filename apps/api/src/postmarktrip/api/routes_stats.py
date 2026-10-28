"""统计与导出路由。"""

from __future__ import annotations

import csv
import io
import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import distinct, func, select

from postmarktrip.api.deps import CurrentUser, DbDep
from postmarktrip.db.models import Card, Place, Postmark, PostOffice, Stamp, Tag, TagLink
from postmarktrip.domain import tagging
from postmarktrip.domain.schemas import (
    CountBucket,
    StatsByPlace,
    StatsByYear,
    StatsOverview,
)

router = APIRouter(tags=["stats"])


@router.get("/stats/overview", response_model=StatsOverview)
def overview(db: DbDep, _: CurrentUser) -> StatsOverview:
    live = Card.deleted_at.is_(None)

    cards = db.scalar(select(func.count()).select_from(Card).where(live)) or 0
    postmarks = (
        db.scalar(
            select(func.count())
            .select_from(Postmark)
            .join(Card, Card.id == Postmark.card_id)
            .where(live, Postmark.deleted_at.is_(None))
        )
        or 0
    )
    stamps = (
        db.scalar(
            select(func.count())
            .select_from(Stamp)
            .join(Card, Card.id == Stamp.card_id)
            .where(live, Stamp.deleted_at.is_(None))
        )
        or 0
    )
    offices = (
        db.scalar(
            select(func.count(distinct(Postmark.post_office_id)))
            .select_from(Postmark)
            .join(Card, Card.id == Postmark.card_id)
            .where(live, Postmark.deleted_at.is_(None), Postmark.post_office_id.is_not(None))
        )
        or 0
    )

    # 按层级统计"去过多少个不同的地点"。用 level_label 而不是"第 N 级" ——
    # 直辖市、省直管县都没有完整中间层（docs/need/03 §五）。
    rows = db.execute(
        select(Place.level_label, func.count(distinct(Place.id)))
        .select_from(Postmark)
        .join(Card, Card.id == Postmark.card_id)
        .join(Place, Place.id == Postmark.place_id)
        .where(live, Postmark.deleted_at.is_(None))
        .group_by(Place.level_label)
    ).all()
    places_by_level = {(label or "未标注"): int(n) for label, n in rows}

    first_last = db.execute(
        select(func.min(Card.visit_date), func.max(Card.visit_date)).where(live)
    ).one()
    first_visit, last_visit = first_last

    return StatsOverview(
        cards=cards,
        postmarks=postmarks,
        stamps=stamps,
        post_offices=offices,
        places_by_level=places_by_level,
        first_visit_date=first_visit,
        last_visit_date=last_visit,
    )


@router.get("/stats/by-year", response_model=StatsByYear)
def by_year(db: DbDep, _: CurrentUser) -> StatsByYear:
    year = func.extract("year", Card.visit_date).cast(__import__("sqlalchemy").Integer)
    rows = db.execute(
        select(year, func.count()).where(Card.deleted_at.is_(None)).group_by(year).order_by(year)
    ).all()
    return StatsByYear(
        buckets=[
            CountBucket(key=str(int(y)), label=f"{int(y)} 年", count=int(n))
            for y, n in rows
            if y is not None
        ]
    )


@router.get("/stats/by-place", response_model=StatsByPlace)
def by_place(
    db: DbDep,
    _: CurrentUser,
    level_label: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> StatsByPlace:
    """按某层级统计跑戳次数。

    UI 上不显示"第 N 级"，只显示层级名（省/地级市/县级…）。
    """
    stmt = (
        select(Place.level_label, Place.name, func.count(Postmark.id))
        .select_from(Postmark)
        .join(Card, Card.id == Postmark.card_id)
        .join(Place, Place.id == Postmark.place_id)
        .where(Card.deleted_at.is_(None), Postmark.deleted_at.is_(None))
        .group_by(Place.level_label, Place.name)
        .order_by(func.count(Postmark.id).desc())
        .limit(limit)
    )
    if level_label:
        stmt = stmt.where(Place.level_label == level_label)

    rows = db.execute(stmt).all()
    label = level_label or (rows[0][0] if rows else None)
    return StatsByPlace(
        level_label=label,
        buckets=[CountBucket(key=name, label=name, count=int(n)) for _lvl, name, n in rows],
    )


# --------------------------------------------------------------------------- #
#  CSV 导出
# --------------------------------------------------------------------------- #

#: 列顺序与 docs/spec/v0.1 §6 的约定一致 —— 为 v1.0 的自动识别预留同构格式。
CSV_COLUMNS = [
    "card_uid",
    "visit_date",
    "received_date",
    "source",
    "status",
    "postmark_type",
    "inscription",
    "postmark_date",
    "postmark_date_text",
    "time_code",
    "postmark_no",
    "country",
    "admin_path",
    "post_office",
    "postal_code",
    "is_color",
    "color_layers",
    "provenance",
    "catalog_ref",
    "stamp_name",
    "face_value",
    "currency",
    "card_tags",
    "postmark_tags",
    "title",
    "note",
    "image_filename",
]


def _tag_names_bulk(
    db: DbDep, target_type: str, target_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """一次取出多个目标的标签名。

    导出路径上不能用 ``tagging.tags_for`` 逐个查 —— 那是每行一次往返。
    """
    if not target_ids:
        return {}
    rows = db.execute(
        select(TagLink.target_id, Tag.name)
        .join(Tag, Tag.id == TagLink.tag_id)
        .where(TagLink.target_type == target_type, TagLink.target_id.in_(target_ids))
        .order_by(TagLink.target_id, Tag.name)
    ).all()
    out: dict[uuid.UUID, list[str]] = {}
    for tid, name in rows:
        out.setdefault(tid, []).append(name)
    return out


@router.get("/export/cards.csv")
def export_cards_csv(db: DbDep, _: CurrentUser) -> StreamingResponse:
    """一行 = 一个邮戳（同卡多戳产生多行，``card_uid`` 相同）。

    邮票在卡片内以 ``|`` 聚合 —— 导出的目的是**可读的备份**，
    不是无损往返格式（那件事交给将来的 JSON 导出）。
    """
    cards = list(
        db.scalars(select(Card).where(Card.deleted_at.is_(None)).order_by(Card.visit_date.desc()))
    )
    card_ids = [c.id for c in cards]

    postmarks: dict[uuid.UUID, list[Postmark]] = {}
    stamps: dict[uuid.UUID, list[Stamp]] = {}
    place_by_id: dict[uuid.UUID, Place] = {}
    office_by_id: dict[uuid.UUID, PostOffice] = {}
    card_tags: dict[uuid.UUID, list[str]] = {}
    pm_tags: dict[uuid.UUID, list[str]] = {}

    if card_ids:
        for pm in db.scalars(
            select(Postmark)
            .where(Postmark.card_id.in_(card_ids), Postmark.deleted_at.is_(None))
            .order_by(Postmark.card_id, Postmark.created_at)
        ):
            postmarks.setdefault(pm.card_id, []).append(pm)
        for st in db.scalars(
            select(Stamp)
            .where(Stamp.card_id.in_(card_ids), Stamp.deleted_at.is_(None))
            .order_by(Stamp.card_id, Stamp.created_at)
        ):
            stamps.setdefault(st.card_id, []).append(st)

        all_pm = [pm for lst in postmarks.values() for pm in lst]

        # 地点与邮局一次批量取出。原先是在循环里逐个 db.get() ——
        # 每行两次查询，1000 张卡就是几千次数据库往返。
        place_ids = {pm.place_id for pm in all_pm if pm.place_id}
        office_ids = {pm.post_office_id for pm in all_pm if pm.post_office_id}
        if place_ids:
            place_by_id = {
                p.id: p for p in db.scalars(select(Place).where(Place.id.in_(place_ids)))
            }
        if office_ids:
            office_by_id = {
                o.id: o for o in db.scalars(select(PostOffice).where(PostOffice.id.in_(office_ids)))
            }

        # 标签同理：两种 target_type 各一次查询。
        card_tags = _tag_names_bulk(db, tagging.TARGET_CARD, card_ids)
        if all_pm:
            pm_tags = _tag_names_bulk(db, tagging.TARGET_POSTMARK, [pm.id for pm in all_pm])

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)

    for card in cards:
        st_list = stamps.get(card.id, [])
        st_ref = "|".join(s.catalog_ref or "" for s in st_list if s.catalog_ref)
        st_name = "|".join(s.name or "" for s in st_list if s.name)
        st_face = "|".join(str(s.face_value) for s in st_list if s.face_value is not None)
        st_cur = "|".join(s.currency or "" for s in st_list if s.currency)

        # 没有邮戳的卡也要占一行，否则卡本身会从导出里消失
        for pm in postmarks.get(card.id) or [None]:
            place = place_by_id.get(pm.place_id) if pm and pm.place_id else None
            office = office_by_id.get(pm.post_office_id) if pm and pm.post_office_id else None

            # ★ 按**列名**成行，而不是按位置。位置对应一旦错位，
            #   csv.writer 不会报错，只会安静地导出一份列错位的文件 ——
            #   而导出文件往往是唯一的备份。
            values: dict[str, object] = {
                "card_uid": str(card.id),
                "visit_date": card.visit_date.isoformat() if card.visit_date else "",
                "received_date": card.received_date.isoformat() if card.received_date else "",
                "source": card.source,
                "status": card.status,
                "postmark_type": pm.postmark_type if pm else "",
                "inscription": pm.inscription if pm else "",
                "postmark_date": pm.postmark_date.isoformat() if pm and pm.postmark_date else "",
                "postmark_date_text": pm.postmark_date_text if pm else "",
                "time_code": pm.time_code if pm else "",
                "postmark_no": pm.postmark_no if pm else "",
                "country": place.country_code if place else "",
                "admin_path": place.path if place else "",
                "post_office": office.name if office else "",
                "postal_code": office.postal_code if office else "",
                "is_color": "" if pm is None else ("true" if pm.is_color else "false"),
                "color_layers": pm.color_layers if pm and pm.color_layers else "",
                "provenance": pm.provenance if pm else "",
                "catalog_ref": st_ref,
                "stamp_name": st_name,
                "face_value": st_face,
                "currency": st_cur,
                "card_tags": "|".join(card_tags.get(card.id, [])),
                "postmark_tags": "|".join(pm_tags.get(pm.id, [])) if pm else "",
                "title": card.title or "",
                "note": (card.note or "").replace("\r\n", " ").replace("\n", " "),
                "image_filename": "",
            }
            missing = set(CSV_COLUMNS) - set(values)
            if missing:
                raise RuntimeError(f"导出缺列 {sorted(missing)} —— CSV_COLUMNS 与行构造不同步")
            writer.writerow([values[c] for c in CSV_COLUMNS])

    # ★ UTF-8 BOM：没有它 Excel 打开中文会乱码（验收标准 A9）
    payload = "\ufeff" + buf.getvalue()
    return StreamingResponse(
        iter([payload.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="postmarktrip-cards.csv"',
            "Cache-Control": "no-store",
        },
    )
