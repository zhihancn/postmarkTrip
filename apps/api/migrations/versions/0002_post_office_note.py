"""post_office.note：机构备注（自由文本）

## 为什么加

用户 2026-10-11 提的：目录数据有**对不上的时候**，需要一个"想填什么填什么"的
备注列。实测三种情况：

* 同一家所两个来源的邮编不同（`三清山邮政所` 334717 vs 334703）
* 目录里**查不到**某些戳上写的机构（`三清山主题邮局` —— 只有 `三清山邮政所`）
* 名称在目录里带状态（数据源里有 384 条 `（暂停）`、4 条 `（撤销）`）

`postmark` 早已有 `note`（Text）；机构侧缺一个对称的列。

## ⚠️ 语义约束（写在这里，因为它是设计决定而不是实现细节）

**OCR 永不自动写这一列。** 与 `Postmark.note` 同一原则：它是"解释与存疑"的
落点，不是数据字段 —— 机器填进去只会污染人写的东西。

## 为什么是可空

不加 `server_default`：老数据为 NULL 正是"人还没写"的准确表达。
用 `nullable=True` 而不是 `server_default=''`，避免把"没写"与"写了空串"
混成一种状态。

Revision ID: 0002_post_office_note
Revises: 0001_baseline
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_post_office_note"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("post_office", sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("post_office", "note")
