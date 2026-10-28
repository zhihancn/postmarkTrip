"""标准邮票库（M3C-1）：stamp_catalog + stamp.catalog_id

纯新增：一张表 + 一个可空外键。用户票不强制关联，老数据不受影响。

Revision ID: 0006_stamp_catalog
Revises: 0005_gallery_indexes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_stamp_catalog"
down_revision = "0005_gallery_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stamp_catalog",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("standard_code", sa.String(length=64), nullable=False),
        sa.Column("colnect_c_id", sa.BigInteger(), nullable=True),
        sa.Column("colnect_url", sa.Text(), nullable=True),
        sa.Column("issue_url", sa.Text(), nullable=True),
        sa.Column("issue_key", sa.String(length=500), nullable=True),
        sa.Column("catalog_system", sa.String(length=24), nullable=True),
        sa.Column("catalog_ref", sa.String(length=64), nullable=True),
        sa.Column("design_no", sa.String(length=32), nullable=True),
        sa.Column("design_name", sa.String(length=200), nullable=True),
        sa.Column("issue_name", sa.String(length=200), nullable=True),
        sa.Column("face_value", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("source_state", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_snapshot_path", sa.String(length=500), nullable=True),
        sa.Column("raw_sha256", sa.String(length=64), nullable=True),
        sa.Column("parse_version", sa.String(length=32), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("standard_code"),
        sa.UniqueConstraint("colnect_c_id"),
    )
    op.create_index("ix_stamp_catalog_issue_key", "stamp_catalog", ["issue_key"])
    op.create_index("ix_stamp_catalog_issue_url", "stamp_catalog", ["issue_url"])
    op.create_index("ix_stamp_catalog_catalog_ref", "stamp_catalog", ["catalog_ref"])
    op.create_index("ix_stamp_catalog_design_name", "stamp_catalog", ["design_name"])

    op.add_column("stamp", sa.Column("catalog_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_stamp_catalog_id_stamp_catalog",
        "stamp",
        "stamp_catalog",
        ["catalog_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_stamp_catalog_id", "stamp", ["catalog_id"])


def downgrade() -> None:
    op.drop_index("ix_stamp_catalog_id", table_name="stamp")
    op.drop_constraint("fk_stamp_catalog_id_stamp_catalog", "stamp", type_="foreignkey")
    op.drop_column("stamp", "catalog_id")
    op.drop_index("ix_stamp_catalog_design_name", table_name="stamp_catalog")
    op.drop_index("ix_stamp_catalog_catalog_ref", table_name="stamp_catalog")
    op.drop_index("ix_stamp_catalog_issue_url", table_name="stamp_catalog")
    op.drop_index("ix_stamp_catalog_issue_key", table_name="stamp_catalog")
    op.drop_table("stamp_catalog")
