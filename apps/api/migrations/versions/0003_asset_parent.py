"""asset.parent_asset_id：派生资产指回它的**原始件**

## 为什么加（`docs/ok/18` §3.5 / §四 D1-D2）

识别只产出**几何**（每张卡的四边形），而库里要的是**图**。A5 实测暴露：
卡片页/画廊显示的是**整页扫描件**（9 张卡挤在一张图里），因为确认时
`Card.image_asset_id` 只能指向整页资产 —— "切好的那张卡"从来没被存下来。

要把切好的卡（以及戳的抠图）落库，就绕不开一个事实：
`asset` 表**没有**指向来源的列，于是派生资产**找不到它的原始件**。
而"原始件不可变、处理只能派生新资产"正是 storage 的设计前提
（`Asset.is_immutable` 那段注释），所以这个链必须补上。

## 为什么是自引用可空 FK，而不是 `card.crop_asset_id`

* 自引用对**任何**派生物都成立（卡片裁切、戳抠图、邮票抠图、将来的缩略图缓存），
  不是"卡片"这一个特例的属性。
* `asset.kind` 里**早就**有 `postmark_cut` / `stamp_cut` 两个取值
  （`db/models.py:213`）—— 也就是数据模型上早有位置，只是从来没接上线。
* 可空：现有资产都是原始件（L0），没有来源；新派生物才有。

`ondelete="SET NULL"`：**原始件不该被删**（L0 不可变），真删了也不该连带
删掉派生记录（那会让"这张卡有图"变成消失）。SET NULL 让链接失效但数据留着。

Revision ID: 0003_asset_parent
Revises: 0002_post_office_note

⚠️ 这是本部署**第一次**加迁移。`apps/api/migrations/` 是 bind mount
（`docker-compose.a5.yml` 第 65 行），所以**不需要重建镜像**；api 容器
启动时 entrypoint 会跑 `alembic upgrade head`。迁移失败的表现是
api **无限重启**（entrypoint 视为致命），所以升级后必须立刻核对。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_asset_parent"
down_revision = "0002_post_office_note"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("asset", sa.Column("parent_asset_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_asset_parent_asset_id",
        "asset",
        "asset",
        ["parent_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_asset_parent_asset_id", "asset", ["parent_asset_id"])


def downgrade() -> None:
    op.drop_index("ix_asset_parent_asset_id", table_name="asset")
    op.drop_constraint("fk_asset_parent_asset_id", "asset", type_="foreignkey")
    op.drop_column("asset", "parent_asset_id")
