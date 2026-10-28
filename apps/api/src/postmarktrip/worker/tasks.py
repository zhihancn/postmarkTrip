"""worker 注册的任务。

**为什么这里必须有至少一个任务**：ARQ 的 ``Worker`` 构造器里有

    if not functions and not cron_jobs:
        raise RuntimeError('at least one function or cron_job must be registered')

v0.1 的 ``WorkerSettings.functions`` 是空列表，于是 v0.1 的 worker 容器
从第一次启动起就一直在崩溃-重启循环里（``ExitCode=1``，重启 219 次），
而 compose 的 healthcheck 只 ping 了 Redis，所以它同时显示 "healthy"。
两件事叠加的结果是：**一个明确坏掉的组件看起来完全正常**。

现在注册两个真有职责的任务，而不是塞一个占位符号：

* ``worker_heartbeat`` —— cron，每 10 秒写一次 Redis 心跳键。
  这是 worker 容器 healthcheck 的依据（见 docker-compose.yml）。
  它校验的是"worker **进程**活着且事件循环在转"，而 ping Redis 校验的是
  "Redis 活着" —— 后者在本容器挂掉时依然为真，所以它根本不能当 worker 的健康检查。
* ``sweep_timeouts`` —— 清理卡在 running 状态的旧任务记录。

识别管线（切分 / 戳型判别 / OCR）在 v1.0 的后续阶段接入，
届时**只需往 ``WORKER_FUNCTIONS`` 里追加**，不必再动基础设施。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from arq import create_pool, cron, func
from arq.connections import ArqRedis, RedisSettings
from arq.worker import Function

from postmarktrip.config import settings
from postmarktrip.db.session import session_scope
from postmarktrip.ingest.inbox import InboxScanner
from postmarktrip.storage import AssetStore

logger = logging.getLogger("postmarktrip.worker.tasks")

#: worker 心跳键。healthcheck 与 /api/health 都读它。
HEARTBEAT_KEY: Final = "pmtrip:worker:heartbeat"
#: 心跳有效期（秒）。必须明显大于心跳间隔，否则会因一次抖动误报。
#: 心跳每 10 秒写一次，这里给 45 秒 —— 容忍 4 次连续丢失。
HEARTBEAT_TTL: Final = 45
#: 心跳间隔（秒）
HEARTBEAT_INTERVAL: Final = 10


async def worker_heartbeat(ctx: dict[str, Any]) -> str:
    """cron 任务：刷新 worker 心跳。

    用 ``SET key value EX ttl`` 而不是先写再 expire —— 单条命令是原子的，
    不会出现"写成功但 expire 失败"从而留下一个永不过期的僵尸心跳。
    """
    stamp = datetime.now(UTC).isoformat()
    redis: ArqRedis | None = ctx.get("redis")
    if redis is None:  # pragma: no cover - ARQ 正常注入
        logger.warning("心跳任务没有拿到 redis 连接")
        return stamp
    await redis.set(HEARTBEAT_KEY, stamp, ex=HEARTBEAT_TTL)
    return stamp


async def sweep_timeouts(ctx: dict[str, Any]) -> int:
    """清理卡住的占位任务。

    v0.1/v1.0-P1 阶段还没有长任务，所以它现在只做一件事：写一条 debug 日志
    并返回 0。保留它是因为 compose 的 worker 健康检查与队列巡检需要一个
    **可被手动触发**的任务，用 ``arq`` CLI 或后续的运维入口就能验证
    "任务真的能跑完"—— 而这一点在 worker 崩了 219 次却显示 healthy 之后，
    值得有一个能被证明的路径。
    """
    logger.debug("队列巡检：当前无长任务")
    return 0


#: 扫描器实例挂在 worker 进程的 ctx 上 —— 它必须跨轮询存活，
#: 因为"连续两次轮询大小不变"这件事无法在一轮里判断。
_SCANNER_KEY: Final = "inbox_scanner"


def _get_scanner(ctx: dict[str, Any]) -> InboxScanner:
    scanner = ctx.get(_SCANNER_KEY)
    if scanner is None:
        scanner = InboxScanner(settings.inbox_dir, settings.allowed_upload_exts)
        ctx[_SCANNER_KEY] = scanner
    return scanner


async def scan_inbox(ctx: dict[str, Any]) -> dict[str, int]:
    """cron 任务：扫一轮投递目录（spec v1.0 §2.1）。

    时间开销很小（一次列目录 + 少量 stat），所以按秒级间隔跑也没问题。
    ``INBOX_ENABLED=false`` 时直接返回 —— 一个未预期的开关不该在升级后
    悄悄开始动用户的文件。
    """
    if not settings.inbox_enabled:
        return {"enabled": 0}

    store = ctx.get("store")
    if store is None:
        settings.ensure_dirs()
        store = AssetStore(settings.assets_dir, allowed_exts=settings.allowed_upload_exts)
        ctx["store"] = store

    scanner = _get_scanner(ctx)
    with session_scope() as db:
        report = scanner.scan_once(db, store)

    if report.errors:
        for name, reason in report.errors:
            logger.warning("投递失败 %s：%s", name, reason)

    return {
        "enabled": 1,
        "seen": report.seen,
        "unstable": report.unstable,
        "ingested": report.ingested,
        "duplicate": report.duplicate,
        "failed": report.failed,
    }


async def enqueue_ping() -> str | None:
    """从 API 侧入队一个心跳任务，返回 job id。

    这是"API 能真的把活丢给 worker"的最小可证伪证据。
    没有它，整条队列的连通性就只是假设。
    """
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        job = await pool.enqueue_job("worker_heartbeat")
        return job.job_id if job is not None else None
    finally:
        await pool.aclose()


#: 注册给 ARQ 的函数任务。**不能为空**（见模块开头）。
#:
#: 必须用 ``arq.func()`` 包一层：ARQ 0.28 的 ``Worker`` 只接受 ``Function`` 实例，
#: 而 ``func()`` 返回的正是它（名字默认取 ``__qualname__``）。
#: 直接把裸协程函数放进列表，会以 ``RuntimeError: cron_jobs, must be instances of
#: CronJob`` 之类的形式在**运行时**才失败 —— 而不是在导入时。
WORKER_FUNCTIONS: Final[list[Function]] = [
    func(worker_heartbeat),
    func(sweep_timeouts),
    func(scan_inbox),
]

#: 注册给 ARQ 的 cron 任务。必须用 ``arq.cron()`` 构造 ``CronJob``。
WORKER_CRON_JOBS: Final[list[Any]] = [
    # 每 10 秒：worker 心跳。healthcheck 与 /api/health 都读这个键。
    # 显式列出秒值 —— cron 不接受"每 N 秒"这种表达，只接受具体取值集合。
    cron(worker_heartbeat, second={0, 10, 20, 30, 40, 50}, name="cron:heartbeat"),
    # 每 10 分钟：队列巡检
    cron(sweep_timeouts, minute={0, 10, 20, 30, 40, 50}, name="cron:sweep"),
    # 每 5 秒：投递目录。第二参数是 cron 的字段名集合，
    # INBOX_ENABLED=false 时任务会立刻返回，不产生任何 I/O。
    cron(
        scan_inbox,
        second={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        name="cron:inbox",
    ),
]


def heartbeat_is_fresh(payload: str | None, *, now: datetime | None = None) -> bool:
    """心跳值是否还在容忍窗口内。

    抽成纯函数是为了能在**不连 Redis** 的情况下测它 —— 健康检查的判断逻辑
    本身出错的代价，比它检查的那个东西出错的代价还高
    （worker 崩了 219 次却显示 healthy 就是这么来的）。
    """
    if not payload:
        return False
    try:
        stamp = datetime.fromisoformat(payload)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return reference - stamp < timedelta(seconds=HEARTBEAT_TTL)


async def worker_alive() -> bool | None:
    """异步：worker 心跳是否新鲜。

    返回 ``None`` 表示**无法判断**（Redis 连不上），而不是 False ——
    "worker 挂了"与"我查不到"是两件事，混成一个布尔值会让排障走错方向。
    调用方（/api/health）必须能区分它们。
    """
    try:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    except Exception:  # pragma: no cover - 取决于网络/Redis 状态
        logger.debug("无法连接 Redis，worker 存活状态未知", exc_info=True)
        return None
    try:
        raw = await pool.get(HEARTBEAT_KEY)
    except Exception:  # pragma: no cover
        logger.debug("读取心跳键失败", exc_info=True)
        return None
    finally:
        await pool.aclose()

    if raw is None:
        return False
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    return heartbeat_is_fresh(value)
