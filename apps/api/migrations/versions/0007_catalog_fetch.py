"""M3C-3：标准票抓取状态列。

只加状态/错误/请求时间，不改现有数据。抓取内容放 `details.last_parse` 与
`raw_snapshot_path`，避免再加一张"每次抓取一行"的表（V1 不需要历史表）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_catalog_fetch"
down_revision = "0006_stamp_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stamp_catalog",
        sa.Column("fetch_status", sa.String(length=16), server_default="idle", nullable=False),
    )
    op.add_column("stamp_catalog", sa.Column("fetch_error", sa.Text(), nullable=True))
    op.add_column(
        "stamp_catalog",
        sa.Column("fetch_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stamp_catalog", "fetch_requested_at")
    op.drop_column("stamp_catalog", "fetch_error")
    op.drop_column("stamp_catalog", "fetch_status")
