"""认证与脱敏的测试。

这些都不需要数据库：口令哈希（Argon2）、会话签名（itsdangerous）、
日志脱敏都是纯函数。但它们**安全相关**，值得单独覆盖 ——
尤其 `redact()`：它是"API Key 绝不进日志"这条硬约束（docs/ok/04 §5.2）
的唯一执行点，写错了不会有任何报错，只会在日志里悄悄泄露密钥。
"""

from __future__ import annotations

import base64

from itsdangerous import URLSafeTimedSerializer

from postmarktrip.config import settings
from postmarktrip.security import (
    _pw_fingerprint,
    hash_password,
    issue_session,
    read_session,
    redact,
    verify_password,
)

# --------------------------------------------------------------------------- #
#  口令
# --------------------------------------------------------------------------- #


def test_password_roundtrip() -> None:
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)


def test_password_is_salted() -> None:
    """同一口令两次哈希必须不同 —— 否则可以靠比对哈希值判断两人口令相同。"""
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a)
    assert verify_password("same-password", b)


def test_wrong_password_rejected() -> None:
    h = hash_password("right")
    assert not verify_password("wrong", h)
    assert not verify_password("", h)


def test_invalid_hash_returns_false_not_raises() -> None:
    """库里存了垃圾（或换过哈希算法）时，登录不该 500，只该失败。"""
    assert not verify_password("x", "not-a-valid-argon2-hash")
    assert not verify_password("x", "")


# --------------------------------------------------------------------------- #
#  会话
# --------------------------------------------------------------------------- #


def test_session_roundtrip() -> None:
    h = hash_password("pw")
    session = read_session(issue_session("user-123", h))
    assert session is not None
    assert session.user_id == "user-123"
    assert session.pw_fingerprint == _pw_fingerprint(h)


def test_tampered_token_rejected() -> None:
    token = issue_session("user-123", hash_password("pw"))
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert read_session(tampered) is None


def test_token_from_another_key_rejected() -> None:
    """换过 SECRET_KEY 之后，旧会话必须失效 —— 这正是"改密钥即全员登出"的机制。"""
    other = URLSafeTimedSerializer("a-completely-different-secret", salt="pmtrip-session")
    assert read_session(other.dumps({"uid": "user-123", "pw": "deadbeef"})) is None


def test_garbage_token_rejected() -> None:
    assert read_session("") is None
    assert read_session("not-a-token") is None
    assert read_session("a.b.c") is None


def test_token_missing_the_password_fingerprint_is_rejected() -> None:
    """旧版本签发的 token（载荷里只有 uid）必须被拒，而不是当作有效会话。

    这是升级路径上的安全性：宁可让所有人重新登录一次，
    也不能让"没有口令指纹"的 token 绕过失效检查。
    """
    legacy = URLSafeTimedSerializer(settings.secret_key, salt="pmtrip-session")
    assert read_session(legacy.dumps({"uid": "user-123"})) is None


def test_password_change_revokes_existing_sessions() -> None:
    """★ 改口令必须让所有已签发的会话失效。

    cookie 里只有 uid 时做不到这件事：旧会话会一直有效到 ``session_max_age``
    过期。而单用户系统里改口令的主要动机恰恰是"这个口令可能已经泄露了"——
    不作废旧会话等于废掉一半目的。
    """
    old_hash = hash_password("old-password")
    new_hash = hash_password("new-password")

    session = read_session(issue_session("user-123", old_hash))
    assert session is not None

    # 口令没变 → 会话仍然匹配
    assert session.pw_fingerprint == _pw_fingerprint(old_hash)
    # 口令变了 → 不再匹配（current_user 会返回 401）
    assert session.pw_fingerprint != _pw_fingerprint(new_hash)


def test_session_token_does_not_leak_the_password_hash() -> None:
    """token 是**签名**的、不是加密的 —— 客户端能读到载荷。

    所以载荷里绝不能放口令哈希原文，只能放它的摘要。
    """
    h = hash_password("whatever")
    token = issue_session("user-123", h)
    payload = token.split(".")[0]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))

    assert h.encode() not in decoded, "会话 token 里出现了口令哈希原文"
    assert _pw_fingerprint(h).encode() in decoded, "摘要应当出现在载荷里"


# --------------------------------------------------------------------------- #
#  日志脱敏
# --------------------------------------------------------------------------- #


def test_redact_key_value_forms() -> None:
    cases = [
        ("api_key=abcd1234", "abcd1234"),
        ("api-key: abcd1234", "abcd1234"),
        ("API_KEY=abcd1234", "abcd1234"),
        ("token=abcd1234", "abcd1234"),
        ("secret=abcd1234", "abcd1234"),
        ("password=abcd1234", "abcd1234"),
    ]
    for raw, secret in cases:
        out = redact(raw)
        assert secret not in out, f"{raw!r} 里的密钥没有被脱敏：{out!r}"
        assert "***" in out


def test_redact_authorization_bearer() -> None:
    """最常见的形态：`Authorization: Bearer <token>`。

    这一条要格外小心 —— 如果只把 "Bearer" 当成值吃掉，
    后面的 token 会原样留在日志里。
    """
    token = "eyJhbGciOiJIUzI1NiJ9.verysecret.signature"
    for raw in (
        f"Authorization: Bearer {token}",
        f"authorization=Bearer {token}",
        f"Authorization: {token}",
    ):
        out = redact(raw)
        assert token not in out, f"{raw!r} 泄露了 token：{out!r}"


def test_redact_bare_bearer() -> None:
    """没有键名、光秃秃一段 Bearer —— 例如 httpx 的请求头 repr。"""
    token = "abcdef1234567890"
    out = redact(f"sent Bearer {token} to upstream")
    assert token not in out, out


def test_redact_is_idempotent() -> None:
    once = redact("api_key=abcd1234")
    assert redact(once) == once


def test_redact_leaves_normal_text_alone() -> None:
    """别把正常日志吃掉 —— 脱敏过度会让日志失去价值。"""
    for raw in (
        "识别完成：12 个字段，耗时 3.2s",
        "GET /api/cards 200 12ms",
        "数据库已就绪。",
        "写入了 29 个索引",
    ):
        assert redact(raw) == raw


def test_redact_multiple_secrets_in_one_line() -> None:
    raw = "colnect api_key=colnect-secret-1 vlm token: vlm-secret-2"
    out = redact(raw)
    assert "colnect-secret-1" not in out
    assert "vlm-secret-2" not in out


def test_settings_does_not_log_placeholder_password() -> None:
    """健全性检查：测试环境里的 SECRET_KEY 不该是示例值。"""
    assert not settings.secret_key.startswith("CHANGE_ME")
    assert len(settings.secret_key) >= 32
