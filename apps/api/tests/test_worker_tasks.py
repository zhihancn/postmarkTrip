"""worker 注册与心跳的测试。

**这个文件存在的直接原因是一个潜伏了整个 v0.1 周期的缺陷**：
``WorkerSettings.functions`` 是空列表，而 ARQ 的 ``Worker`` 构造器拒绝零任务
worker，于是 worker 容器从第一次启动起就在崩溃-重启循环里
（``ExitCode=1``，重启 219 次）—— 而 compose 的 healthcheck 只 ping 了 Redis，
所以它**同时显示 healthy**。

一个明确坏掉的组件看起来完全正常，代价是这个故障一直没人发现。
所以这里锁两件事：

1. **注册表不能为空**，且 ``Worker`` 能被真的构造出来（复现原先抛异常的那一步）。
2. **健康检查的判断逻辑本身是对的** —— 它的正确性不能比它检查的东西更不可靠。
"""

from __future__ import annotations

import inspect
import sys
import types
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arq.cron import CronJob
from arq.worker import Function, Worker

from postmarktrip.worker import healthcheck, tasks
from postmarktrip.worker.settings import WorkerSettings


@contextmanager
def _quiet_worker() -> Iterator[None]:
    """临时压掉 asyncio 的 "There is no current event loop" DeprecationWarning。

    ARQ 0.28 的 ``Worker.__init__`` 内部调用 ``asyncio.get_event_loop()``，
    而 Python 3.12 在**没有运行中事件循环**时会对它发 DeprecationWarning。
    本套件按项目约定以 ``-W error::DeprecationWarning`` 运行，于是这条警告
    会变成一个指向 ARQ、与本项目无关的失败。

    压掉它是**有意的**，而且范围仅限构造 Worker 的这几行：
    这里要验证的是"ARQ 会不会因为注册表为空而拒绝启动"，
    不是"ARQ 内部有没有用弃用 API"。真实异常（RuntimeError 等）不受影响，
    照旧会抛出并被测试看见。
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"asyncio\..*")
        warnings.filterwarnings("ignore", message="There is no current event loop")
        yield


def _construct_worker() -> Any:
    with _quiet_worker():
        return Worker(
            functions=list(WorkerSettings.functions),
            cron_jobs=list(WorkerSettings.cron_jobs),
            redis_settings=WorkerSettings.redis_settings,
            burst=True,
        )


# --------------------------------------------------------------------------- #
#  回归：注册表不能为空
# --------------------------------------------------------------------------- #


def test_functions_are_not_empty() -> None:
    """v0.1 的根因断言。

    空列表会让 ``arq`` 以 ``RuntimeError: at least one function or cron_job
    must be registered`` 退出。这条测试把"至少有一个任务"钉住。
    """
    assert WorkerSettings.functions, (
        "WorkerSettings.functions 为空 —— ARQ 会拒绝启动，容器将进入崩溃循环"
    )


def test_cron_jobs_are_not_empty() -> None:
    assert WorkerSettings.cron_jobs


def test_function_entries_are_arq_wrapped() -> None:
    """ARQ 0.28 只接受 ``Function`` 实例，不接受裸协程函数。

    直接把裸函数放进 ``functions`` 不会在导入时报错，而是在**运行时**
    以 ``cron_jobs, must be instances of CronJob`` / ``func_registry`` 之类的
    形式失败。所以这里要求每个条目都是 ``arq.func()`` 的产物。
    """
    for entry in WorkerSettings.functions:
        assert isinstance(entry, Function), (
            f"{entry!r} 不是 arq.worker.Function —— 需要用 arq.func() 包一层"
        )
        assert inspect.iscoroutinefunction(entry.coroutine)


def test_cron_entries_are_cronjob_instances() -> None:
    for entry in WorkerSettings.cron_jobs:
        assert isinstance(entry, CronJob), (
            f"{entry!r} 不是 arq.cron.CronJob —— 需要用 arq.cron() 构造，"
            f"元组形式（如 (fn, 'second', {{0}})）会被 Worker 拒绝"
        )


def test_registered_names_are_unique() -> None:
    """重名会让后注册的静默覆盖先注册的。"""
    names = [f.name for f in WorkerSettings.functions]
    names += [c.name for c in WorkerSettings.cron_jobs]
    assert len(names) == len(set(names)), f"注册名重复：{names}"


def test_worker_can_be_constructed() -> None:
    """直接复现原先把容器搞崩的那一步。

    不 mock 任何东西 —— 就是要让 ``Worker.__init__`` 里的那个
    ``if not functions and not cron_jobs: raise`` 以及
    ``if not all(isinstance(cj, CronJob) ...): raise`` 真的被执行一遍。
    """
    worker = _construct_worker()
    assert worker is not None


def test_every_registered_function_is_known_to_the_worker() -> None:
    """注册的名字必须都能在构造出的 Worker 里找到。

    名字对不上时，入队会成功但执行时报 "function not found" ——
    那是运行时才发现的，而且只在真的投递任务时才发现。
    """
    worker = _construct_worker()
    for fn in WorkerSettings.functions:
        assert fn.name in worker.functions, (
            f"{fn.name} 不在 Worker.functions 里：{sorted(worker.functions)}"
        )


# --------------------------------------------------------------------------- #
#  心跳判断逻辑（纯函数，不连 Redis）
# --------------------------------------------------------------------------- #


def _stamp(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


def test_fresh_heartbeat() -> None:
    assert tasks.heartbeat_is_fresh(_stamp(timedelta(seconds=-5)))


def test_stale_heartbeat() -> None:
    """超过 TTL 就是过期。TTL 必须大于心跳间隔，否则一次抖动就误报。"""
    assert not tasks.heartbeat_is_fresh(_stamp(timedelta(seconds=-(tasks.HEARTBEAT_TTL + 1))))


def test_ttl_has_headroom_over_interval() -> None:
    """容错度：至少能连续丢 2 次心跳仍判活。"""
    assert tasks.HEARTBEAT_TTL >= tasks.HEARTBEAT_INTERVAL * 3


@pytest.mark.parametrize("payload", [None, "", "not-a-timestamp", "2026-13-45T99:99:99"])
def test_malformed_heartbeat_is_not_fresh(payload: str | None) -> None:
    """脏值必须判为"不新鲜"，而不是抛异常。

    健康检查自己抛异常会让 compose 把容器判成 unhealthy，
    而且错误信息会指向健康检查而不是真正的问题。
    """
    assert tasks.heartbeat_is_fresh(payload) is False


def test_naive_timestamp_is_treated_as_utc() -> None:
    """不带时区的历史值不应被判为过期（按 UTC 解释）。"""
    naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
    assert tasks.heartbeat_is_fresh(naive)


def test_future_timestamp_is_fresh() -> None:
    """时钟回拨/未来值：仍在窗口内，判活。"""
    assert tasks.heartbeat_is_fresh(_stamp(timedelta(seconds=5)))


# --------------------------------------------------------------------------- #
#  两处 TTL 必须一致
# --------------------------------------------------------------------------- #


# 注：worker 健康检查的测试在下面的专章里。
# 这里原先有两条"读 compose 源码字符串"的测试 —— 它们依赖健康检查写在
# compose 的折叠标量里，逻辑抽成 healthcheck.py 之后自然失效，已移除。
# 它们的教训留在那条注释里：**字符串断言放行了真 bug，后来又自己假失败。**


def test_heartbeat_key_is_the_documented_one() -> None:
    assert tasks.HEARTBEAT_KEY == "pmtrip:worker:heartbeat"


# --------------------------------------------------------------------------- #
#  worker 健康检查（postmarktrip.worker.healthcheck）
#
#  这一组的存在是因为**健康检查本身写错过两次**，而两次的表现完全不同：
#
#  1. 最初是 `redis.ping()` —— 测的是"Redis 活着"，于是 worker 崩了 219 次
#     仍报 healthy（恒为假阳性）。
#  2. 改成读心跳后，我写成了比较值的**长度**，于是 32 字符的时间戳被判
#     unhealthy，而 worker 明明是好的（恒为假阴性）。
#
#  两次都说明同一件事：健康检查的判断逻辑必须被**真的执行**过。
#  所以这里直接 import 那个模块调它的纯函数，而不是从 YAML 里抠脚本文本 ——
#  抠文本的写法本身也假失败过一次（脚本换了排版就炸）。
# --------------------------------------------------------------------------- #


def _fresh(offset_seconds: float = -5) -> bytes:
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat().encode()


def test_healthcheck_age_of_fresh_heartbeat() -> None:
    age = healthcheck.heartbeat_age_seconds(_fresh(-5))
    assert age is not None
    assert 4 < age < 8, f"5 秒前的心跳算出来是 {age}s"


def test_healthcheck_accepts_fresh_heartbeat() -> None:
    """**这条正是假阴性那个 bug 的正面用例。**

    心跳值是真实长度的时间戳（32 字符）。按长度比较的实现会判死。
    """
    assert healthcheck.is_healthy(healthcheck.heartbeat_age_seconds(_fresh(-5))) is True


def test_healthcheck_rejects_stale_heartbeat() -> None:
    age = healthcheck.heartbeat_age_seconds(_fresh(-(tasks.HEARTBEAT_TTL + 10)))
    assert healthcheck.is_healthy(age) is False


@pytest.mark.parametrize("raw", [None, b"", b"not-a-timestamp", b"2026-13-45T99:99:99"])
def test_healthcheck_rejects_unparseable_heartbeat(raw: bytes | None) -> None:
    """脏值一律判"无法判定" → 不健康。

    宁可误报也不漏报：漏报就是再次出现"坏掉但显示正常"。
    """
    assert healthcheck.heartbeat_age_seconds(raw) is None
    assert healthcheck.is_healthy(None) is False


def test_healthcheck_rejects_naive_timestamp() -> None:
    """**不带时区的时间戳必须判失败**，不能猜它是本地时间。

    worker 写的值一律带 UTC 时区。若把朴素时间当成 UTC 解释，
    在 UTC+8 下等于把 8 小时前的心跳算成"刚刚"，早已死掉的 worker
    会继续报健康 —— 那正是这一类缺陷最难发现的一种。
    """
    naive = datetime.now(UTC).replace(tzinfo=None).isoformat().encode()
    assert healthcheck.heartbeat_age_seconds(naive) is None


def test_healthcheck_rejects_negative_age() -> None:
    """时钟回拨/未来值不判健康 —— 不掩盖真问题。"""
    future = _fresh(+600)
    age = healthcheck.heartbeat_age_seconds(future)
    assert age is not None and age < 0
    assert healthcheck.is_healthy(age) is False


def test_healthcheck_threshold_uses_the_constant() -> None:
    """阈值必须来自 ``tasks.HEARTBEAT_TTL``，不能各写一份。

    两处漂移的后果很隐蔽：过松 → 崩溃的 worker 仍报 healthy；
    过紧 → 正常 worker 被反复重启。
    """
    just_in = tasks.HEARTBEAT_TTL - 5
    just_out = tasks.HEARTBEAT_TTL + 5
    assert healthcheck.is_healthy(just_in) is True
    assert healthcheck.is_healthy(just_out) is False


# --------------------------------------------------------------------------- #
#  main() 的退出码 —— 用假 redis 客户端，不连真实 Redis
# --------------------------------------------------------------------------- #


class _FakeRedis:
    def __init__(self, value: bytes | None, *, boom: bool = False) -> None:
        self._value = value
        self._boom = boom

    def get(self, _key: str) -> bytes | None:
        if self._boom:
            raise ConnectionError("假装 Redis 不可达")
        return self._value


def _run_main_with(
    monkeypatch: pytest.MonkeyPatch, value: bytes | None, *, boom: bool = False
) -> int:
    fake_redis = types.ModuleType("redis")
    fake_redis.from_url = lambda _url: _FakeRedis(value, boom=boom)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    return healthcheck.main()


def test_main_exit_zero_on_fresh_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_main_with(monkeypatch, _fresh(-5)) == 0


def test_main_exit_one_on_stale_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_main_with(monkeypatch, _fresh(-(tasks.HEARTBEAT_TTL + 10))) == 1


def test_main_exit_one_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有心跳键 → 失败。否则它又退回成"什么都不检查"。"""
    assert _run_main_with(monkeypatch, None) == 1


def test_main_exit_one_when_redis_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """连不上 Redis **不能**当作健康 —— 那时我们根本不知道 worker 的状态。"""
    assert _run_main_with(monkeypatch, _fresh(-5), boom=True) == 1


def test_healthcheck_module_is_invokable_as_module() -> None:
    """compose 里用的是 ``python -m postmarktrip.worker.healthcheck``。

    路径写错时 compose 只会报 healthcheck 失败，不会说"模块找不到"，
    而且会在 60 秒 start_period 之后才暴露。所以直接断言这个模块可导入。
    """
    module_file = Path(healthcheck.__file__ or "")
    assert module_file.is_file()
    assert module_file.name == "healthcheck.py"


def test_compose_invokes_the_healthcheck_module() -> None:
    compose = (Path(__file__).resolve().parents[3] / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "python -m postmarktrip.worker.healthcheck" in compose, (
        "compose 的 worker healthcheck 没有调用 healthcheck 模块 —— "
        "别把逻辑再写回折叠标量里的 python -c（被 YAML 缩进咬过一次）"
    )

    # 反面对照：确认没有退回成"只 ping Redis"。
    #
    # ⚠️ 必须先把 **YAML 注释行去掉**再匹配。worker 的 healthcheck 段落里
    # 正有一段注释解释"为什么当初不能只 ping Redis"，全文匹配会把注释里
    # 那几个字当成实现 —— 这个假阳性在这条测试的第一版里就出现了。
    worker_part = compose.split("worker:", 1)[1].split("gateway:", 1)[0]
    test_block = worker_part.split("healthcheck:", 1)[1].split("test:", 1)[1]
    code = "\n".join(line for line in test_block.splitlines() if not line.strip().startswith("#"))
    assert "redis.ping()" not in code, (
        "worker 健康检查退回了只 ping Redis —— 那测的是 Redis，不是 worker"
    )


def test_healthcheck_is_wired_into_api_health() -> None:
    """``/api/health`` 必须暴露 worker 存活，否则 doctor 看不到它。

    光有 compose 的 healthcheck 不够：``make doctor`` 读的是 /api/health，
    而"worker 崩了却没人知道"这件事正是从 doctor 看不见它开始的。
    """
    source = (
        Path(__file__).resolve().parents[1] / "src" / "postmarktrip" / "api" / "routes_auth.py"
    ).read_text(encoding="utf-8")
    assert '"worker_alive"' in source
    assert "worker_alive" in source
