"""pytest 全局配置。

在任何 `postmarktrip` 模块被导入之前把环境变量安排好 —— `config.settings`
是模块级单例，导入后再设就晚了。

这里设的都是**测试专用**的值：
* ``SECRET_KEY`` 随机生成，不用真实密钥
* ``DATA_DIR`` 等指到**仓库内**的 ``apps/api/.test-tmp``，绝不碰宿主机的 /data
* ``DATABASE_URL`` 指向一个不存在的库：这些测试**不连数据库**，
  连不上也无所谓（见 test_schema_and_routes.py 的说明）

⚠️ **刻意不用 `tempfile.gettempdir()`**。在若干受限环境里（本项目的开发沙箱、
部分容器的 /tmp 策略）它可能不可写；而 `TestClient` 会执行应用的 lifespan、
进而调用 `settings.ensure_dirs()`，于是抛 `PermissionError` —— 更糟的是，
那个异常发生在 anyio 的 portal 线程里，表现为**测试卡住**而不是清晰报错，
排查起来很费时间（这个坑踩过一次）。
放在仓库内一个可预测的位置，就没有这层不确定性；该目录已加入 .gitignore。
"""

from __future__ import annotations

import os
import secrets
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"

os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(32))
os.environ.setdefault("DATA_DIR", str(_TMP / "data"))
os.environ.setdefault("INBOX_DIR", str(_TMP / "inbox"))
os.environ.setdefault("PUBLIC_DATA_DIR", str(_TMP / "public-data"))
os.environ.setdefault("MODELS_DIR", str(_TMP / "models"))
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://nobody:nobody@127.0.0.1:1/nonexistent",
)


@pytest.fixture
def workdir() -> Iterator[Path]:
    """一次性使用的临时工作目录。

    刻意不用 pytest 的 ``tmp_path``：它内部走 ``tempfile.mkdtemp``，
    而 ``mkdtemp`` 以 ``mode=0o700`` 建目录。在若干受限环境（容器化的 CI、
    带写入策略的沙箱）里，这种目录随后会不可写，测试会以 PermissionError
    失败 —— 那反映的是环境策略，不是被测代码的问题。

    这里用默认 mode 建目录，语义相同但不引入这个环境耦合。
    """
    base = _TMP / f"work-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)
