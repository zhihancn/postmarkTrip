"""画廊需要的部分索引（v1.1 M3）

## 为什么加（`docs/spec/m3/00` §3.7）

戳墙 / 票墙的第一道筛选是"有贴片"（`image_asset_id IS NOT NULL`），
而绝大多数历史记录的贴片列是空的。部分索引只收非空行：

* 索引更小（空行不进索引）
* 查询计划能直接只扫"有图"的那部分

时间轴本身不用这两个索引（它按 `card.visit_date` 排），但 M3 的四个视图
共用一套数据约定，索引在 M3-0 一次补齐，免得 M3b/M3c 各写一次迁移。

## 风险

纯新增索引，不改列、不回填。建索引期间会短暂持有写锁；
本库当前记录量很小（card=1 / postmark=3 / stamp=2），可忽略。

Revision ID: 0005_gallery_indexes
Revises: 0004_template_and_scene
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_gallery_indexes"
down_revision = "0004_template_and_scene"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_postmark_image_asset",
        "postmark",
        ["image_asset_id"],
        postgresql_where=sa.text("image_asset_id IS NOT NULL"),
    )
    op.create_index(
        "ix_stamp_image_asset",
        "stamp",
        ["image_asset_id"],
        postgresql_where=sa.text("image_asset_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_stamp_image_asset", table_name="stamp")
    op.drop_index("ix_postmark_image_asset", table_name="postmark")
