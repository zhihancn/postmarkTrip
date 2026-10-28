"""请求级测试（第一个这类测试文件）。

此前所有测试都是**结构级**（端点是否注册、模型能否编译）或**纯函数级**
（UUID、存储、脱敏、摘要聚合）。这留了一个真空：**请求真的进来时会发生什么**。

这个真空是有代价的。例如本次修的这个问题：
`list_cards` 曾经把 `postmark_type` / `source` / `status` / `provenance`
声明成 `str`，再在函数体里构造 `CardQuery`。传入不认识的取值时，Pydantic 在
处理函数**内部**抛 `ValidationError` —— 而 FastAPI 只把**请求解析阶段**的错误
转成 422，函数体内抛出的会变成 **500**。于是 `?status=foo` 得到一个内部服务器
错误，而不是"取值不合法"。

下面的 `test_invalid_filter_value_is_422_not_500` 就是为它写的。

**不需要数据库**：只读端点用 `dependency_overrides` 注入一个最小替身即可。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from postmarktrip.db.session import get_db
from postmarktrip.main import app
from postmarktrip.security import (
    SESSION_COOKIE,
    _pw_fingerprint,
    current_user,
    hash_password,
    issue_session,
    read_session,
)

#: 替身用户的口令。用真实哈希（而不是占位串），这样改口令那条路径
#: 的 verify_password 才能真正走通。只哈希一次，之后在 fixture 里复位。
TEST_PASSWORD = "test-password-1234"
_ORIGINAL_HASH = hash_password(TEST_PASSWORD)

FAKE_USER = SimpleNamespace(
    id=uuid.uuid4(),
    username="tester",
    last_login_at=None,
    password_hash=_ORIGINAL_HASH,
)


class _StubResult:
    """`Session.execute()` 返回值的最小替身（`Result` 的形状）。

    提供 `.tuples()` 与 `.all()`：多列查询是 `execute(...).tuples().all()`，
    真实 `Result` 两个方法都有（**`ScalarResult` 没有 `.tuples()`** ——
    这个差别踩过两次，见 `recognition/vocab.py` 的说明）。
    """

    def tuples(self) -> _StubResult:
        return self

    def all(self) -> list[Any]:
        return []

    def __iter__(self) -> Any:  # noqa: ANN401
        return iter(())


class _StubSession:
    """只够让**只读**端点跑通的最小替身。

    刻意按语句内容分派：``count`` 查询返回 0，其他单值查询返回 None
    （例如"按用户名找账号"）。若一律返回 0，登录端点会拿到一个 int
    并尝试读它的 ``password_hash`` —— 那是测试替身的问题，不是被测代码的。
    """

    def scalar(self, stmt: Any) -> Any:  # noqa: ANN401
        return 0 if "count" in str(stmt).lower() else None

    def scalars(self, stmt: Any) -> list[Any]:
        return []

    def get(self, entity: Any, ident: Any) -> None:  # noqa: ANN401
        return None

    def execute(self, stmt: Any) -> _StubResult:  # noqa: ARG002
        """返回一个**空**的结果集（`Result` 的形状）。

        ⚠️ 这里曾经是 `raise AssertionError("只读路径不应执行到 execute()")`。
        那条守卫的动机是对的（挡住"顺手写一下"），但它把"读"也一起拦了 ——
        而 `Session.execute()` 是**合法的读**路径：多列查询只能走它
        （`db.scalars()` 传多列 select 时**只返回第一列**，见
        `recognition/vocab.py` 里那段教训）。

        所以改成返回一个**空结果**：写操作仍然拦不住（落库要走
        `add`/`flush`/`commit`，那些方法这里都有且是空实现），
        但"读"不会再被误伤。真正要盯"识别端点不许写库"的是
        `test_recognize_route.py` 里对模块源码的断言，那条更有针对性。
        """
        return _StubResult()

    # 改口令路径会写 —— 这些调用不需要真的落库，但必须存在
    def add(self, obj: Any) -> None:  # noqa: ANN401
        pass

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def refresh(self, obj: Any) -> None:  # noqa: ANN401
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def anon() -> Iterator[TestClient]:
    """**未登录**的客户端。"""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def client() -> Iterator[TestClient]:
    """已登录（依赖被覆盖）且数据库被替身接管的客户端。

    每个用例开始时复位替身用户的口令哈希 —— 改口令的用例会把它改掉，
    不复位会污染后续用例。
    """
    FAKE_USER.password_hash = _ORIGINAL_HASH
    app.dependency_overrides[current_user] = lambda: FAKE_USER
    app.dependency_overrides[get_db] = lambda: _StubSession()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        FAKE_USER.password_hash = _ORIGINAL_HASH


# --------------------------------------------------------------------------- #
#  免鉴权端点
# --------------------------------------------------------------------------- #


def test_health_is_reachable_without_auth(anon: TestClient) -> None:
    """运维自检与容器 healthcheck 都打这个端点，它必须免鉴权。"""
    r = anon.get("/api/health")
    assert r.status_code == 200, r.text
    body = r.json()
    # 刻意逐项返回而不是一个笼统的 ok —— 出问题时要知道是哪一环断了
    for key in ("ok", "database", "assets_writable", "inbox_present", "assets_dir"):
        assert key in body, f"/api/health 缺字段 {key}"


def test_vocab_is_reachable_without_auth(anon: TestClient) -> None:
    """前端所有下拉框的取值都来自这里，它必须在登录页就能用。"""
    r = anon.get("/api/vocab")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in (
        "postmark_types",
        "primary_postmark_types",
        "card_sources",
        "card_statuses",
        "catalog_systems",
        "provenances",
        "tag_categories",
        "level_labels",
        "face_value_suggestions",
    ):
        assert key in body, f"/api/vocab 缺字段 {key}"
    # 每个选项都要有 value 与中文 label，否则前端只能显示原始值
    assert all(o.get("value") and o.get("label") for o in body["postmark_types"])


# --------------------------------------------------------------------------- #
#  鉴权
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/cards"),
        ("GET", "/api/stats/overview"),
        ("GET", "/api/export/cards.csv"),
        ("POST", "/api/cards"),
        ("GET", "/api/auth/me"),
    ],
)
def test_private_endpoints_require_auth(anon: TestClient, method: str, path: str) -> None:
    r = anon.request(method, path)
    assert r.status_code == 401, f"{method} {path} 未要求鉴权（返回 {r.status_code}）"


def test_login_with_unknown_user_is_401_not_500(client: TestClient) -> None:
    """账号不存在与口令错误要返回同一个信息 —— 不泄露"该用户名是否存在"。"""
    r = client.post("/api/auth/login", json={"username": "nobody", "password": "whatever"})
    assert r.status_code == 401
    assert r.json()["detail"] == "用户名或口令错误"


# --------------------------------------------------------------------------- #
#  改口令：本机续用，其他设备登出
# --------------------------------------------------------------------------- #


def test_change_password_requires_the_current_one(client: TestClient) -> None:
    r = client.post(
        "/api/auth/password",
        json={"current_password": "not-the-password", "new_password": "whatever-new-1"},
    )
    assert r.status_code == 403


def test_change_password_rejects_an_identical_new_password(client: TestClient) -> None:
    r = client.post(
        "/api/auth/password",
        json={"current_password": TEST_PASSWORD, "new_password": TEST_PASSWORD},
    )
    assert r.status_code == 422


def test_change_password_reissues_a_session_for_this_device(client: TestClient) -> None:
    """★ 改完口令，**本机必须继续可用**。

    会话 token 里带着口令哈希的指纹，所以改口令会让所有旧 token 失效 ——
    包括发出这次请求的这一台。若不换发，用户会在改完口令的下一秒被自己的
    操作踢到登录页。正确的语义是"改口令即登出**其他**设备"。
    """
    r = client.post(
        "/api/auth/password",
        json={"current_password": TEST_PASSWORD, "new_password": "a-brand-new-passphrase"},
    )
    assert r.status_code == 200, r.text

    token = r.cookies.get(SESSION_COOKIE) or client.cookies.get(SESSION_COOKIE)
    assert token, "改口令后没有下发新的会话 cookie"

    session = read_session(token)
    assert session is not None
    assert session.user_id == str(FAKE_USER.id)
    # 指纹要对应**新**口令 —— 这样其他设备上的旧 token 才会失配
    assert session.pw_fingerprint == _pw_fingerprint(FAKE_USER.password_hash)


def test_password_change_makes_other_devices_tokens_unusable(client: TestClient) -> None:
    """别处签发的旧 token，在改口令之后必须不再匹配。

    这是"改口令即登出其他设备"的机制本身。没有它，改口令就只是一个仪式：
    可能已经泄露的旧会话会一直有效到 ``session_max_age`` 过期。
    """
    stale = issue_session(str(FAKE_USER.id), _ORIGINAL_HASH)
    assert read_session(stale) is not None

    r = client.post(
        "/api/auth/password",
        json={"current_password": TEST_PASSWORD, "new_password": "yet-another-passphrase"},
    )
    assert r.status_code == 200, r.text

    session = read_session(stale)
    assert session is not None, "签名本身仍然有效（这不是本用例要测的东西）"
    assert session.pw_fingerprint != _pw_fingerprint(FAKE_USER.password_hash), (
        "改口令后旧 token 仍然匹配 —— 其他设备不会被登出"
    )


# --------------------------------------------------------------------------- #
#  只读路径接通
# --------------------------------------------------------------------------- #


def test_cards_list_returns_an_empty_page(client: TestClient) -> None:
    """走通整条读取路径：依赖注入 → 过滤器 → 计数 → 分页。

    替身返回空集合，所以期望是一个结构完整的空页，而不是报错。
    """
    r = client.get("/api/cards")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"items": [], "total": 0, "page": 1, "size": 24}


def test_cards_list_accepts_all_documented_filters(client: TestClient) -> None:
    """所有文档里写过的筛选参数都要能被接受（合法取值）。"""
    r = client.get(
        "/api/cards",
        params={
            "q": "南平",
            "visit_date_from": "2026-01-01",
            "visit_date_to": "2026-12-31",
            "place_descendants": "true",
            "postmark_type": "daily",
            "source": "field_trip",
            "status": "collected",
            "is_color": "false",
            "provenance": "real_scan",
            "tag": "主题邮局",
            "order_by": "created_at",
            "order_desc": "false",
            "page": 2,
            "size": 50,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["page"] == 2
    assert r.json()["size"] == 50


# --------------------------------------------------------------------------- #
#  参数校验：422 而不是 500
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("param", "bad_value"),
    [
        ("status", "not-a-status"),
        ("source", "not-a-source"),
        ("postmark_type", "not-a-type"),
        ("provenance", "not-a-provenance"),
        ("order_by", "not-a-column"),
        ("visit_date_from", "2026-13-45"),
        ("page", "0"),
        ("size", "9999"),
    ],
)
def test_invalid_filter_value_is_422_not_500(
    client: TestClient, param: str, bad_value: str
) -> None:
    """非法取值必须是 422（客户端错误），不能是 500（服务端错误）。

    这条是本文件存在的主要理由：把枚举参数声明成 ``str``、再在函数体里
    构造 Pydantic 模型，会让非法取值变成 500 —— 因为 FastAPI 只把请求解析
    阶段的错误转成 422。
    """
    r = client.get("/api/cards", params={param: bad_value})
    assert r.status_code == 422, (
        f"?{param}={bad_value} 返回了 {r.status_code}，期望 422。"
        f"若是 500，说明该校验发生在处理函数内部，而不是 FastAPI 的参数解析阶段。"
    )


def _ref_name(schema: dict[str, Any]) -> str | None:
    """从 schema 里取出它引用的组件名。

    可选参数（``T | None``）生成的不是直接 ``$ref``，而是
    ``{"anyOf": [{"$ref": ...}, {"type": "null"}]}`` —— 两种形态都要认。
    """
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    for sub in schema.get("anyOf", []):
        if isinstance(sub, dict) and "$ref" in sub:
            return str(sub["$ref"]).rsplit("/", 1)[-1]
    return None


def test_openapi_documents_the_allowed_filter_values(client: TestClient) -> None:
    """OpenAPI 里要列出筛选参数的允许取值。

    这既是给前端开发者的文档，也是"校验发生在框架层"的直接证据：
    只有把参数**声明成枚举类型**，schema 里才会出现 enum。
    若哪天有人把它们改回 ``str``，这条会失败。
    """
    schema = app.openapi()
    components = schema["components"]["schemas"]
    by_name = {p["name"]: p for p in schema["paths"]["/api/cards"]["get"]["parameters"]}

    enum_params = {
        "postmark_type": "PostmarkType",
        "source": "CardSource",
        "status": "CardStatus",
        "provenance": "Provenance",
    }
    for name, component in enum_params.items():
        assert name in by_name, f"OpenAPI 里没有 {name} 参数"
        ref = _ref_name(by_name[name]["schema"])
        assert ref == component, f"{name} 指向 {ref}，期望 {component}"
        allowed = components[ref].get("enum")
        assert allowed, f"{name}（{component}）的 schema 里没有 enum 取值"


def test_enum_filters_accept_every_documented_value(client: TestClient) -> None:
    """枚举里列的每个取值都要能被接受 —— 别让文档与实际校验不一致。"""
    from postmarktrip.domain import vocab as V

    for value in V.PostmarkType:
        r = client.get("/api/cards", params={"postmark_type": value.value})
        assert r.status_code == 200, f"postmark_type={value.value} 被拒：{r.text}"
    for value in V.CardSource:
        r = client.get("/api/cards", params={"source": value.value})
        assert r.status_code == 200, f"source={value.value} 被拒：{r.text}"
    for value in V.Provenance:
        r = client.get("/api/cards", params={"provenance": value.value})
        assert r.status_code == 200, f"provenance={value.value} 被拒：{r.text}"


def test_cards_list_rejects_bad_date_range_types(client: TestClient) -> None:
    r = client.get("/api/cards", params={"visit_date_from": "不是日期"})
    assert r.status_code == 422
