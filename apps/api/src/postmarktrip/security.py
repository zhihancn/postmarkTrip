"""认证与安全。

单用户系统，所以刻意保持简单：
* 口令用 Argon2 哈希（内存硬，抗 GPU 爆破）
* 会话用 itsdangerous 签名的 cookie（无服务端会话表 —— 单用户不需要）
* **绝不把 API Key 写进日志**（docs/ok/04 §5.2 的硬约束）
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Annotated, NamedTuple

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Cookie, Depends, HTTPException, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from postmarktrip.config import settings
from postmarktrip.db.models import AppUser
from postmarktrip.db.session import get_db

logger = logging.getLogger(__name__)

SESSION_COOKIE = "pmtrip_session"

_hasher = PasswordHasher()
_serializer = URLSafeTimedSerializer(settings.secret_key, salt="pmtrip-session")


# --------------------------------------------------------------------------- #
#  口令
# --------------------------------------------------------------------------- #


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False


# --------------------------------------------------------------------------- #
#  会话
# --------------------------------------------------------------------------- #


class SessionData(NamedTuple):
    """从会话 token 里解出来的内容。"""

    user_id: str
    pw_fingerprint: str


def _pw_fingerprint(password_hash: str) -> str:
    """口令哈希的短摘要，用来给会话"打上口令版本"。

    再取一层 SHA-256 是有意的：token 是**签名**的而不是加密的，
    客户端能读到载荷。直接放口令哈希等于把它送给客户端；放它的摘要则不然，
    而且摘要无法用来验证口令猜测（还得先有哈希本身，而哈希带 salt）。

    Argon2 每次哈希结果都不同（随机 salt），所以摘要在口令不变时稳定、
    口令一变就变 —— 正好是我们要的语义。
    """
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def issue_session(user_id: str, password_hash: str) -> str:
    """签发会话 token。

    把**当前口令哈希的指纹**一并放进去。这样改口令之后指纹随之改变，
    所有旧 token 立刻失效 —— 这是没有服务端会话表时实现
    "改口令即登出所有设备"的标准做法。

    若不这样做：cookie 里只有 uid，改口令后旧 cookie 会一直有效到
    ``session_max_age`` 到期。而单用户系统里改口令的主要动机恰恰是
    "这个口令可能已经泄露了"，不作废旧会话等于废掉一半目的。
    """
    return _serializer.dumps({"uid": user_id, "pw": _pw_fingerprint(password_hash)})


def read_session(token: str) -> SessionData | None:
    try:
        data = _serializer.loads(token, max_age=settings.session_max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    uid, pw = data.get("uid"), data.get("pw")
    if not isinstance(uid, str) or not isinstance(pw, str):
        return None
    return SessionData(user_id=uid, pw_fingerprint=pw)


# --------------------------------------------------------------------------- #
#  依赖
# --------------------------------------------------------------------------- #


def current_user(
    db: Annotated[Session, Depends(get_db)],
    pmtrip_session: Annotated[str | None, Cookie()] = None,
) -> AppUser:
    if not pmtrip_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    session = read_session(pmtrip_session)
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "会话已失效，请重新登录")
    # 注：这里传字符串给 UUID 列，依赖 PostgreSQL 从上下文推断 uuid 类型。
    # 已确认 SQLAlchemy 的 PGUUID 没有 bind_processor（值原样交给驱动），
    # 所以这条路径是通的 —— 见 tests/test_security.py 里对 uid 形状的断言。
    user = db.scalar(select(AppUser).where(AppUser.id == session.user_id))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    if session.pw_fingerprint != _pw_fingerprint(user.password_hash):
        # 口令变过 —— 旧会话一律作废。
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "口令已变更，请重新登录")
    return user


CurrentUser = Annotated[AppUser, Depends(current_user)]


# --------------------------------------------------------------------------- #
#  日志脱敏
# --------------------------------------------------------------------------- #

#: (模式, 替换)。**顺序有意义** —— 从最具体的形态到最宽泛的。
#:
#: 这组规则曾经对最常见的凭据格式完全失效：`Authorization: Bearer <token>`
#: 的 `\S+` 只吃到了 scheme 名 "Bearer"，真正的凭据原样留在日志里；
#: 而裸 Bearer 那条的替换是"在匹配串尾部追加 =***"，等于没删还多写了字。
#: 见 tests/test_security.py::test_redact_authorization_bearer。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 1) Authorization 头：把该键之后**整行剩余部分**都吃掉。
    #    刻意吃掉整行而不是一个 token —— 对密钥而言，宁可少记一点日志，
    #    也不能漏掉一个字符。
    (re.compile(r"(?i)\bauthorization\b\s*[=:]\s*[^\r\n]*"), "authorization=***"),
    # 2) 常见的键值形态：api_key / api-key / token / secret / password
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[=:]\s*\S+"), r"\1=***"),
    # 3) 没有键名、光秃秃的 Bearer / Basic（例如 httpx 请求头的 repr）
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 ***"),
)


def redact(text: str) -> str:
    """给日志用的脱敏。

    这是"Provider 的 Key 绝不进日志"（docs/ok/04 §5.2）的**唯一执行点**。
    写错了不会有任何报错，只会在日志里悄悄泄露密钥 —— 所以它有独立的测试，
    且那些测试断言的是"原文里不能出现密钥"，而不是"结果里要有 ***"。
    """
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out
