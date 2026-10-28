"""首次启动的幂等初始化。

由 ``entrypoint.sh`` 在迁移之后、启动 uvicorn 之前调用::

    python -m postmarktrip.bootstrap

做两件事：

1. **管理员账号** —— 若 ``app_user`` 表为空，用 ``ADMIN_USER`` / ``ADMIN_PASSWORD``
   创建第一个账号（口令以 Argon2 哈希存储）。
2. **预置标签** —— 把 ``domain.vocab.DEFAULT_TAGS`` 补齐（按名称去重，只增不改）。

**幂等**是硬要求：容器每次启动都会执行它，重复运行必须既不报错也不产生重复行。

为什么单独成一个模块而不是塞进 entrypoint 的 shell：口令哈希要用
``postmarktrip.security.hash_password``（Argon2），在 shell 里做不了；而且这段
逻辑需要被测试覆盖。
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from postmarktrip.config import settings
from postmarktrip.db.models import AppUser, Tag
from postmarktrip.db.session import session_scope
from postmarktrip.domain.vocab import DEFAULT_TAGS
from postmarktrip.security import hash_password

#: ``.env.example`` 里的占位值统一以此开头。``pmtrip.ps1 doctor`` / ``make doctor``
#: 也按这个前缀识别"还没改过示例值"。
PLACEHOLDER_PREFIX = "CHANGE_ME"

#: 口令最短长度。与 ``security`` 层改口令的校验保持一致。
MIN_PASSWORD_LEN = 8

#: 自动生成口令用的字母表：去掉 0/O/1/l/I 这些手抄时容易混的字符。
#: 这个口令要被人从容器日志里抄到浏览器，可读性优先于熵。
PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _log(message: str) -> None:
    print(f"[bootstrap] {message}", flush=True)


def _warn(message: str) -> None:
    print(f"[bootstrap][警告] {message}", file=sys.stderr, flush=True)


def _is_placeholder(value: str) -> bool:
    return value.strip().startswith(PLACEHOLDER_PREFIX)


def _generate_password(length: int = 16) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def _announce_generated_credentials(username: str, password: str) -> None:
    """打印自动生成的口令，并落一份到 ``data_dir``。

    ⚠️ 口令会出现在 ``docker compose logs api`` 里 —— 这是有意的一次性权衡：
    没有它，用 ``docker compose up`` 直接起（不经安装脚本）的人拿不到口令，
    实例等于没有管理员。所以必须同时要求"登录后改口令 + 删掉这个文件"。
    """
    bar = "=" * 72
    print(bar, flush=True)
    print(f"  首次启动：管理员账号 {username} / 口令 {password}", flush=True)
    print("  登录后请立刻修改口令，并删除 data/initial-credentials.txt", flush=True)
    print(bar, flush=True)

    try:
        path = Path(settings.data_dir) / "initial-credentials.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "postmarkTrip 首次启动生成的凭据\n"
            f"用户名: {username}\n"
            f"口令:   {password}\n\n"
            "登录后请立刻修改口令，并删除本文件。\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        _log(f"凭据已写入 {path}（登录后请删除）")
    except OSError as exc:  # 只影响便利性，不影响启动
        _warn(f"无法写入初始凭据文件：{exc}")


# --------------------------------------------------------------------------- #
#  管理员
# --------------------------------------------------------------------------- #
def ensure_admin(session: Session) -> int:
    """确保至少存在一个账号。返回退出码（0 = 成功）。"""
    existing = session.scalar(select(func.count()).select_from(AppUser)) or 0
    if existing > 0:
        _log(f"已存在 {existing} 个账号，跳过管理员创建。")
        return 0

    username = (settings.admin_user or "").strip() or "admin"
    password = settings.admin_password or ""
    generated = False

    if not password:
        # 兜底：安装脚本会先写好 .env；这里是"有人直接 compose up"的情况。
        # 早前这里是 return 1，结果是容器起来了但没有账号，登录页永远 401，
        # 日志里只有一行警告 —— 正是"装不上"的最大来源。
        password = _generate_password()
        generated = True
        _warn("ADMIN_PASSWORD 为空 —— 已自动生成一个随机口令（见下方横幅）。")

    if _is_placeholder(password):
        _warn("ADMIN_PASSWORD 仍是 .env.example 里的示例值。")
        _warn("这台实例在局域网内等于**没有口令保护** —— 请立刻改成强口令并重启。")
    elif len(password) < MIN_PASSWORD_LEN:
        _warn(f"ADMIN_PASSWORD 短于 {MIN_PASSWORD_LEN} 位，请改成更强的口令。")

    if _is_placeholder(settings.secret_key):
        _warn("SECRET_KEY 仍是 .env.example 里的示例值，这是一个**公开的密钥**：")
        _warn("任何人都能伪造登录会话。请生成一个真密钥后重启：")
        _warn("  Linux/macOS/WSL:  openssl rand -base64 48")
        _warn("  Windows PowerShell:  [Convert]::ToBase64String((1..48|%{Get-Random -Max 256}))")

    session.add(AppUser(username=username, password_hash=hash_password(password)))
    session.flush()
    _log(f"已创建管理员账号：{username}")

    if generated:
        _announce_generated_credentials(username, password)
    return 0


# --------------------------------------------------------------------------- #
#  标签
# --------------------------------------------------------------------------- #
def ensure_tags(session: Session) -> None:
    """补齐预置标签。按名称去重，只增不改 —— 用户改过的分类/颜色不会被覆盖。"""
    wanted: dict[str, str | None] = {name: str(category) for name, category in DEFAULT_TAGS}
    present = set(session.scalars(select(Tag.name)).all())

    created = 0
    for name, category in wanted.items():
        if name in present:
            continue
        session.add(Tag(name=name, category=category))
        created += 1

    if created:
        session.flush()
        _log(f"预置标签：新建 {created} 个，已存在 {len(present)} 个。")
    else:
        _log(f"预置标签：全部已存在（{len(present)} 个），无需处理。")


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #
def main() -> int:
    try:
        with session_scope() as session:
            code = ensure_admin(session)
            if code != 0:
                # 让 session_scope 正常提交（标签仍然有价值），但以失败码退出，
                # 这样 compose 不会把一个"没有管理员"的实例当成健康实例。
                ensure_tags(session)
                return code
            ensure_tags(session)
    except SQLAlchemyError as exc:
        _warn(f"数据库操作失败：{type(exc).__name__}: {exc}")
        _warn("排查：① docker compose logs db ② DATABASE_URL 与 POSTGRES_* 是否一致")
        return 1

    _log("初始化完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
