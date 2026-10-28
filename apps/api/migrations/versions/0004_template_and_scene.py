"""template 表 + card.scene / card.template_id：电子卡（v1.1 M1）

## 为什么加（`docs/spec/v1.1` §2.2）

M1 要能把一张卡渲染成电子卡，就需要两样东西：

1. **模板**：一份具名的场景图。内置模板走 `public-data/templates/*.json`
   （随仓库、只读、加模板不需要 DDL），这张表只放**用户建的**模板（M2 的编辑器写入）。
2. **卡片自己的场景**：`card.scene` 存**快照**，`card.template_id` 只记来源。
   只存模板 id 的话，改一次模板会让所有旧卡片的电子卡跟着变 —— 与
   `docs/ok/04` §4.2「引用 + 快照」同一条理由。

## 为什么 scene 是 JSONB 而不是槽位表

场景图是**契约**（`contract/scene.schema.json`，v1）。拆成 `slot` 表会让
"导出一份场景 / 把它发给别人"变成多表 JOIN，也让 schema 版本迁移更难；
而它的查询需求只有"按 card 取一条"。

## 风险

纯新增（一张表 + 两个可空列），对现有行没有回填、没有 NOT NULL 约束。
`card.template_id` 用 `SET NULL`：删模板不该删卡片。

Revision ID: 0004_template_and_scene
Revises: 0003_asset_parent
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_template_and_scene"
down_revision = "0003_asset_parent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "template",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("scene", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_template_scope", "template", ["scope"])

    op.add_column(
        "card", sa.Column("scene", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column("card", sa.Column("template_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_card_template_id",
        "card",
        "template",
        ["template_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_card_template_id", "card", ["template_id"])


def downgrade() -> None:
    op.drop_index("ix_card_template_id", table_name="card")
    op.drop_constraint("fk_card_template_id", "card", type_="foreignkey")
    op.drop_column("card", "template_id")
    op.drop_column("card", "scene")
    op.drop_index("ix_template_scope", table_name="template")
    op.drop_table("template")
