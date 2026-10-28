"""标签关联的读写。

``TagLink`` 是多态关联（target_type + target_id，无外键约束），
所以关联的清理必须由这里统一负责 —— 这是多态关联的固有代价。
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from postmarktrip.db.models import Tag, TagLink

TARGET_CARD = "card"
TARGET_POSTMARK = "postmark"
TARGET_STAMP = "stamp"


def tags_for(db: Session, target_type: str, target_id: uuid.UUID) -> list[Tag]:
    stmt = (
        select(Tag)
        .join(TagLink, TagLink.tag_id == Tag.id)
        .where(TagLink.target_type == target_type, TagLink.target_id == target_id)
        .order_by(Tag.name)
    )
    return list(db.scalars(stmt))


def set_tags(db: Session, target_type: str, target_id: uuid.UUID, tag_ids: list[uuid.UUID]) -> None:
    """全量替换某个目标上的标签。"""
    db.execute(
        delete(TagLink).where(TagLink.target_type == target_type, TagLink.target_id == target_id)
    )
    seen: set[uuid.UUID] = set()
    for tid in tag_ids:
        if tid in seen:
            continue
        seen.add(tid)
        # 只挂真实存在的标签，避免脏 ID 造成悬空关联
        if db.get(Tag, tid) is None:
            continue
        db.add(TagLink(tag_id=tid, target_type=target_type, target_id=target_id))


def purge_for(db: Session, target_type: str, target_id: uuid.UUID) -> None:
    """删除目标时清理其标签关联（多态关联无法靠外键级联）。"""
    db.execute(
        delete(TagLink).where(TagLink.target_type == target_type, TagLink.target_id == target_id)
    )


def attach_many(
    db: Session, target_type: str, pairs: list[tuple[uuid.UUID, list[uuid.UUID]]]
) -> None:
    """批量设置（列表页用，避免 N+1）。"""
    for target_id, tag_ids in pairs:
        set_tags(db, target_type, target_id, tag_ids)
