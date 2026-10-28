"""M3C-5：邮票识别候选（最近一次）。

只加状态/候选列，不自动改 `catalog_id`；候选由 colnetApi 返回，人工选。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_stamp_identify"
down_revision = "0007_catalog_fetch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stamp",
        sa.Column("identify_status", sa.String(length=16), server_default="idle", nullable=False),
    )
    op.add_column("stamp", sa.Column("identify_error", sa.Text(), nullable=True))
    op.add_column("stamp", sa.Column("identify_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "stamp",
        sa.Column(
            "identify_candidates",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("stamp", "identify_candidates")
    op.drop_column("stamp", "identify_at")
    op.drop_column("stamp", "identify_error")
    op.drop_column("stamp", "identify_status")
