"""Alembic 运行环境。

两条硬性约束（对应交付要求）：

1. **数据库地址只来自应用配置** —— ``postmarktrip.config.settings.database_url``。
   本文件里不出现任何 URL 字面量，也不写入 ``alembic.ini``。

2. **compare_type / compare_server_default 必须为 True。**
   否则 autogenerate 会漏掉"列类型变更"与"server_default 变更"。本项目的
   ``db/base.py`` 里大量时间戳列用了 ``server_default=func.now()``，
   漏检会让迁移悄悄不生效 —— 也就是"跑了 upgrade 但库里没变"。

一个刻意的实现选择：**不用 ``config.set_main_option("sqlalchemy.url", ...)``**。
Alembic 的 ``Config`` 基于 ``ConfigParser``，会对 ``%`` 做插值；口令里若含
``%``（base64 随机口令很常见）就会炸在插值上。直接把 URL 交给
``create_engine`` / ``context.configure`` 可以完全绕开这个坑。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import postmarktrip.db.models  # noqa: F401  —— 必须导入：表的注册发生在模块导入时
from postmarktrip.config import settings
from postmarktrip.db.base import Base

# --------------------------------------------------------------------------- #
#  基础
# --------------------------------------------------------------------------- #
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: autogenerate 的比对基准。必须直接引用项目里的 Base.metadata，
#: 而不是在迁移里重复声明一份结构。
target_metadata = Base.metadata


def _database_url() -> str:
    """唯一的地址来源。"""
    return settings.database_url


# --------------------------------------------------------------------------- #
#  公共配置
# --------------------------------------------------------------------------- #
def _common_options() -> dict[str, object]:
    return {
        "target_metadata": target_metadata,
        # 列类型变了要生成 ALTER
        "compare_type": True,
        # server_default 变了也要生成 ALTER
        "compare_server_default": True,
        # 本项目只用 public schema，不做跨 schema 比对
        "include_schemas": False,
        # PG 不需要 batch 模式（batch 是给 SQLite 那种改不了列的场景用的）
        "render_as_batch": False,
    }


# --------------------------------------------------------------------------- #
#  离线模式（alembic upgrade head --sql）
# --------------------------------------------------------------------------- #
def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_common_options(),  # type: ignore[arg-type]
    )
    with context.begin_transaction():
        context.run_migrations()


# --------------------------------------------------------------------------- #
#  在线模式
# --------------------------------------------------------------------------- #
def run_migrations_online() -> None:
    # NullPool：迁移是一次性短任务，不需要连接池，也不该把连接留到进程结束。
    connectable = create_engine(_database_url(), poolclass=pool.NullPool, future=True)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            **_common_options(),  # type: ignore[arg-type]
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
