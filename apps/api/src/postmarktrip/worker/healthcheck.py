"""worker 容器存活自检。

用法（在容器内）::

    python -m postmarktrip.worker.healthcheck

退出码：
    0  worker 心跳新鲜 —— 队列在转
    1  没有心跳、心跳过期、或读不到 Redis

## 它和"只 ping Redis"的区别

最初的 worker 健康检查是 ``redis.ping()`` —— 那校验的是 **Redis 活着**。
在 worker 容器已经崩溃、重启 219 次的情况下，这个检查**一直为真**，
于是 compose 报告 ``Up (healthy)``，一个明确坏掉的组件看起来完全正常。

本脚本读的是 ``worker_heartbeat`` cron 每 10 秒刷新的心跳键，校验的是
"worker 进程活着**且事件循环在转**"。

## 为什么是一个文件而不是 compose 里的一段 `python -c`

我原先把它写在 compose 的 ``- >-`` 折叠标量里，**被 YAML 的缩进咬了一次**：
折叠会把每行前面的空白留在值里，于是 ``python -c`` 收到一段
每行都有前导空格的代码，报 ``IndentationError: unexpected indent``，
健康检查从此恒为失败。

放进真文件之后：

* 没有缩进歧义 —— 它就是一个普通的 .py
* 能被 ``ruff`` 检查、能被 ``compile()`` 与单元测试直接调用，
  而不必再从 YAML 里"抠"出脚本来测（抠出来的那次就假失败过一次）
* ``python -m`` 明确，不依赖 shell 的引号转义
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from postmarktrip.config import settings
from postmarktrip.worker.tasks import HEARTBEAT_KEY, HEARTBEAT_TTL


def heartbeat_age_seconds(raw: bytes | str | None) -> float | None:
    """心跳键的值 → 已过去多少秒。无法判定时返回 ``None``。

    抽成纯函数，便于在**不连 Redis** 的情况下测它。健康检查的判断逻辑
    本身出错的代价，比它检查的那个东西出错的代价还高 ——
    这一点在 worker 崩了 219 次却显示 healthy 之后已经验证过一次了。

    返回 ``None`` 的三种情况：没有值、不是合法时间戳、时间戳没有时区。
    调用方一律按"不健康"处理：**宁可误报，不可漏报** —— 漏报的代价是
    再次出现"坏掉但显示正常"。
    """
    if not raw:
        return None

    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None

    if stamp.tzinfo is None:
        # worker 写的一律带 UTC 时区。不带时区的值我们不猜它的含义 ——
        # 猜错的后果是"本地时间被当成 UTC"，在 UTC+8 下等于把 8 小时前的
        # 心跳算成刚刚，于是早已死掉的 worker 继续报健康。
        return None

    now = datetime.now(UTC)
    return (now - stamp).total_seconds()


def is_healthy(age: float | None) -> bool:
    """年龄在容忍窗口内才算健康。

    ``age`` 为负（时钟回拨、或 worker 的时钟快于这里）时**不判健康** ——
    健康检查宁可多报一次问题，也不要因为时钟问题而掩盖真问题。
    """
    if age is None:
        return False
    return 0 <= age < HEARTBEAT_TTL


def main() -> int:
    try:
        import redis
    except ImportError:  # pragma: no cover - 依赖缺失时给一条能读懂的出路
        print("缺少 redis 依赖，无法检查 worker 心跳", file=sys.stderr)
        return 1

    try:
        client = redis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
        raw = client.get(HEARTBEAT_KEY)
    except Exception as exc:
        # 连不上 Redis 时**不能**当作健康：那时我们根本不知道 worker 的状态。
        # 宽捕异常是有意的 —— 连接失败的种类（超时、拒绝、DNS、认证）不重要，
        # 重要的是"这一次我查不出来"，而它必须表现为不健康。
        print(f"无法读取心跳（Redis 不可达？）：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    age = heartbeat_age_seconds(raw)
    if is_healthy(age):
        return 0

    if raw is None:
        print("没有 worker 心跳键 —— worker 从未成功启动，或已停止刷新", file=sys.stderr)
    elif age is None:
        print(f"心跳值无法解析：{raw!r}", file=sys.stderr)
    else:
        print(f"心跳已过期：{age:.1f}s ≥ TTL {HEARTBEAT_TTL}s", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
