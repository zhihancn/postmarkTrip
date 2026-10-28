"""ARQ worker 配置。

v0.1 的这里曾是一个**静默的故障**：``functions`` 是空列表，而 ARQ 拒绝启动
零任务的 worker，于是容器进入崩溃-重启循环（重启 219 次），同时
compose 的健康检查只 ping Redis，所以它显示 "healthy"。
现在 ``functions`` 由 ``worker.tasks`` 提供，两者不会再漂移。
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from arq.connections import RedisSettings

from postmarktrip.config import settings
from postmarktrip.worker.tasks import WORKER_CRON_JOBS, WORKER_FUNCTIONS

logger = logging.getLogger("postmarktrip.worker")


async def startup(ctx: dict[str, Any]) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s worker: %(message)s",
    )
    settings.ensure_dirs()
    logger.info(
        "worker 就绪 | 函数 %d 个 | cron %d 个 | 投递目录 %s",
        len(WORKER_FUNCTIONS),
        len(WORKER_CRON_JOBS),
        "启用" if settings.inbox_enabled else "未启用",
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    logger.info("worker 停止")


class WorkerSettings:
    """ARQ 入口点。``arq postmarktrip.worker.settings.WorkerSettings``"""

    #: 识别管线（ingest_scan / split_cards / recognize_postmark / cut_assets）
    #: 在 v1.0 后续阶段追加到 tasks.WORKER_FUNCTIONS。
    #:
    #: 标 ``ClassVar`` 是**语义正确**的（这是 ARQ 从类上读取的注册表，
    #: 不是实例状态），同时让"可变默认值"这条 lint 不再成立。
    #: 运行时不受影响：``ClassVar`` 只影响类型检查，值仍在类 ``__dict__`` 里，
    #: 而 ARQ 正是用 ``inspect.getmembers`` 从类属性取的。
    functions: ClassVar[list[Any]] = list(WORKER_FUNCTIONS)
    cron_jobs: ClassVar[list[Any]] = list(WORKER_CRON_JOBS)

    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    #: 任务失败时不无限重试 —— 识别失败通常是输入问题，重试无用
    max_tries = 2
    job_timeout = 600
    keep_result = 3600
