"""容器启动期权限处理的测试。

**这个文件防的是一类"开发时看不出来"的错**：容器自己写得很顺，
而宿主上的用户悄悄失去了写权限。

原实现里 `prepare_dirs` 对绑定挂载的宿主目录做了

    chown pmtrip:pmtrip "$ASSETS_DIR"

于是 `./data/assets` 在宿主上的属主变成 10001、宿主用户（通常是 1000）
**不再可写**。容器自己能写，所以任何容器内的测试都不会失败 ——
但坏掉的是：

* `make backup` 生成资产清单（要遍历 `data/assets`）
* 宿主侧的 rsync / restic 备份
* 手工往 `assets/` 或 `inbox/` 里放文件

而 `inbox/` 尤其要紧：**手工投递本来就是 v1.0 的一条正经用法**。
所以这里断言的不是"容器能写"，而是"**两边都能写**"。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ENTRYPOINT = (
    Path(__file__).resolve().parents[1] / "src" / "postmarktrip" / ".." / ".." / "entrypoint.sh"
).resolve()
DOCKERFILE = (Path(__file__).resolve().parents[1] / "Dockerfile").resolve()
COMPOSE = (Path(__file__).resolve().parents[3] / "docker-compose.yml").resolve()


@pytest.fixture(scope="module")
def entrypoint_text() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
#  不许对宿主目录 chown
# --------------------------------------------------------------------------- #


def test_entrypoint_does_not_chown_assets_dir(entrypoint_text: str) -> None:
    """核心回归：宿主目录的**属主**不能被动。

    只查真正的命令，不查注释 —— 注释里正解释着为什么不要这么做。
    """
    code = "\n".join(
        line for line in entrypoint_text.splitlines() if not line.lstrip().startswith("#")
    )
    for var in ("ASSETS_DIR", "INBOX_DIR"):
        pattern = re.compile(rf"chown\s+[^\n]*\$\{{{var}\}}|chown\s+[^\n]*\${var}\b")
        assert not pattern.search(code), (
            f"entrypoint 对 ${var}（绑定挂载的宿主目录）做了 chown —— "
            f"那会夺走宿主用户的写权限，并连带弄坏 make backup / rsync / 手工投递"
        )


def test_mounted_dirs_get_group_write_and_setgid(entrypoint_text: str) -> None:
    """替代方案必须真的存在：组写 + setgid，而不是只是删掉了 chown。"""
    assert "chmod g+ws" in entrypoint_text, (
        "没有给宿主目录加组写 + setgid —— 删掉 chown 之后容器就写不进去了"
    )
    assert "chgrp" in entrypoint_text


def test_host_gid_is_read_from_env(entrypoint_text: str) -> None:
    assert "HOST_GID" in entrypoint_text
    # 允许 0 = 未提供，作为安全退化
    assert re.search(r'HOST_GID="\$\{HOST_GID:-0\}"', entrypoint_text), (
        "HOST_GID 应默认为 0（未提供），而不是硬编码某个 gid"
    )


def test_pmtrip_ids_are_not_hardcoded_in_the_chgrp_path(entrypoint_text: str) -> None:
    """uid/gid 走变量，别在多处硬编码 10001。"""
    assert 'PMTRIP_UID="${PMTRIP_UID:-10001}"' in entrypoint_text
    assert 'PMTRIP_GID="${PMTRIP_GID:-10001}"' in entrypoint_text


# --------------------------------------------------------------------------- #
#  镜像里要有那个组
# --------------------------------------------------------------------------- #


def test_dockerfile_accepts_host_gid_build_arg() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG HOST_GID" in text, "Dockerfile 缺 HOST_GID build arg"
    assert "usermod --append --groups" in text, (
        "没有把 pmtrip 加入宿主组 —— 组写权限对容器用户就不生效，等于白加"
    )


def test_dockerfile_skips_group_creation_for_gid_zero_and_self() -> None:
    """HOST_GID=0（未提供）或与 pmtrip 相同时不该建组。

    不判的话会在 gid 冲突时让**构建直接失败**，而默认值恰好就是 0。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert '"$HOST_GID" != "0"' in text
    assert '"$HOST_GID" != "10001"' in text


# --------------------------------------------------------------------------- #
#  compose 必须把 HOST_GID 传下去
# --------------------------------------------------------------------------- #


def test_compose_passes_host_gid_as_build_arg() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"args:\s*\n(?:\s*#[^\n]*\n)*\s*HOST_GID:", text), (
        "compose 没有把 HOST_GID 作为 build arg 传给镜像构建"
    )


def test_compose_passes_host_gid_to_both_services() -> None:
    """api 与 worker 都要拿到 HOST_GID。

    ⚠️ 而且**必须写在各服务的 environment 里**：YAML 锚点（``<<: *api-common``）
    的 ``environment`` 会被服务的同名键**整体覆盖**，不是合并。
    写在锚点里对两个服务都不生效 —— 这个坑很容易踩，
    因为读 compose 文件时会以为锚点里的东西都继承下去了。
    """
    text = COMPOSE.read_text(encoding="utf-8")
    # 去掉锚点定义段，只在服务定义里找
    body = text.split("x-api-common:", 1)[1].split("services:", 1)[1]
    occurrences = [line for line in body.splitlines() if "HOST_GID:" in line and "args" not in line]
    assert len(occurrences) >= 2, (
        f"HOST_GID 只出现 {len(occurrences)} 次；api 与 worker 各自都需要一条"
    )
