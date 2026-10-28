"""前端类型 ↔ 后端 OpenAPI 的契约测试。

**这个测试要解决什么**

`apps/web/src/shared/types.ts` 是**手写**的。手写类型可以"撒谎"：
后端把 `postmark_no` 改名成了别的、或前端拼错了一个字段名，
**TypeScript 不会报错**（它只相信自己的声明），运行时只会拿到 `undefined`。
这类问题在界面上表现为"某一列永远是空的"，而排查它要翻两边的代码。

前端已经有了 `tsc --noEmit`，但它校验的是"代码是否符合这些声明"，
不是"这些声明是否符合后端"。这个文件补上后半句。

**只比较字段名集合**，不比较类型 —— 完整类型比较需要真正的 TS 解析器，
而字段名对不上是这类问题的绝大多数形态。

依赖：需要装好后端依赖（`pip install -e apps/api`），不需要数据库。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

# 不写成 `from .conftest import ...`：那要求 tests/ 目录是 Python 包（各自带
# __init__.py），而 pytest 默认的 rootdir/importmode 组合下不一定成立。
# 直接按文件位置算，零依赖。
REPO_ROOT = Path(__file__).resolve().parents[2]

TS_FILE = REPO_ROOT / "apps" / "web" / "src" / "shared" / "types.ts"

INTERFACE_RE = re.compile(
    r"export\s+interface\s+(?P<name>\w+)(?P<generics><[^>]*>)?"
    r"(?:\s+extends\s+(?P<extends>[\w\s,<>]+?))?\s*\{",
    re.M,
)
FIELD_RE = re.compile(r"(?:^|[\n;])\s*(?P<name>[A-Za-z_]\w*)\s*\??\s*:")

#: TS 接口名 → OpenAPI schema 名。两边的命名不必一致，但必须显式登记，
#: 否则改名时这个测试会静默跳过而不是失败。
ALIASES = {
    "Paged": "Paged_CardListItem_",
    "ChangePasswordRequest": "PasswordChangeRequest",
}

#: 不参与比较：前端自有的辅助类型，或后端返回自由 dict 而没有独立 schema。
SKIP = {
    "HealthResponse",       # /api/health 返回 dict[str, object]
    "FaceValueSuggestion",  # VocabResponse 里是 list[dict]，前端给它起了名字
    "UUID",
}

#: 允许"后端有、前端不声明"的字段。每条都要写清楚理由。
ALLOW_API_ONLY = {
    # CardRead / PostmarkRead 继承自写入用的 Base，于是把 tag_ids 也暴露了。
    # 前端读卡片时用的是 tags（已解析的对象），不订阅 tag_ids —— 这是有意的。
    ("CardRead", "tag_ids"),
    ("PostmarkRead", "tag_ids"),
}


def _extract_body(text: str, brace_pos: int) -> str:
    """取出一对花括号之间的内容，跳过嵌套的 {}。"""
    depth = 0
    out: list[str] = []
    i = brace_pos
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        if depth >= 1:
            out.append(ch)
        i += 1
    return "".join(out)


def parse_interfaces(text: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for m in INTERFACE_RE.finditer(text):
        body = _extract_body(text, m.end() - 1)
        # 干掉嵌套的对象字面量，免得把它的键当成顶层字段
        flat = re.sub(r"\{[^{}]*\}", " ", body)
        result[m.group("name")] = {
            "fields": set(FIELD_RE.findall(flat)),
            "extends": [
                b.strip().split("<")[0]
                for b in (m.group("extends") or "").split(",")
                if b.strip()
            ],
        }
    return result


def resolve_fields(
    name: str, ifaces: dict[str, dict[str, Any]], seen: set[str] | None = None
) -> set[str]:
    """把 `extends` 链上的字段合并起来。"""
    seen = seen or set()
    if name in seen or name not in ifaces:
        return set()
    seen.add(name)
    entry = ifaces[name]
    fields: set[str] = set(entry["fields"])
    for base in entry["extends"]:
        fields |= resolve_fields(base, ifaces, seen)
    return fields


def api_schemas() -> dict[str, set[str]]:
    from postmarktrip.main import app

    schemas = app.openapi()["components"]["schemas"]
    return {
        name: set(schema["properties"])
        for name, schema in schemas.items()
        if schema.get("properties")
    }


@pytest.fixture(scope="module")
def ts_interfaces() -> dict[str, dict[str, Any]]:
    assert TS_FILE.is_file(), f"找不到前端类型文件：{TS_FILE}"
    parsed = parse_interfaces(TS_FILE.read_text(encoding="utf-8"))
    assert len(parsed) >= 25, f"只解析出 {len(parsed)} 个接口，解析器可能坏了"
    return parsed


@pytest.fixture(scope="module")
def schemas() -> dict[str, set[str]]:
    return api_schemas()


def _comparable(ts_interfaces: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        n for n in ts_interfaces if n not in SKIP and ALIASES.get(n, n) is not None
    )


def test_every_ts_interface_has_an_api_counterpart(
    ts_interfaces: dict[str, dict[str, Any]], schemas: dict[str, set[str]]
) -> None:
    """每个（不跳过的）TS 接口都要能在 OpenAPI 里找到对应模型。

    这条能抓住"后端把某个响应模型改名/删掉了，而前端还在声明它" ——
    那种情况下，下面逐字段比较的测试会因为找不到模型而**静默跳过**。
    """
    missing = [
        n
        for n in _comparable(ts_interfaces)
        if ALIASES.get(n, n) not in schemas
    ]
    assert missing == [], (
        f"这些 TS 接口在 OpenAPI 里找不到对应模型：{missing}。"
        f"要么后端改了名（请在 ALIASES 里登记映射），要么该模型已被删除。"
    )


def test_no_ts_field_is_absent_from_the_api(
    ts_interfaces: dict[str, dict[str, Any]], schemas: dict[str, set[str]]
) -> None:
    """核心断言：前端声明的字段，后端必须真的返回。

    前端多了字段 = 运行时 undefined，而且 TypeScript 不会报错 ——
    这正是这个测试存在的理由。
    """
    problems: list[str] = []
    for ts_name in _comparable(ts_interfaces):
        api_name = ALIASES.get(ts_name, ts_name)
        api_fields = schemas.get(api_name)
        if api_fields is None:
            continue  # 由上面那条测试负责报错
        ts_fields = resolve_fields(ts_name, ts_interfaces)
        extra = sorted(ts_fields - api_fields)
        if extra:
            problems.append(f"{ts_name} -> {extra}")
    assert problems == [], (
        "前端声明了后端不返回的字段（运行时会是 undefined）：\n  " + "\n  ".join(problems)
    )


def test_api_only_fields_are_intentional(
    ts_interfaces: dict[str, dict[str, Any]], schemas: dict[str, set[str]]
) -> None:
    """后端多出的字段必须是**登记过的**。

    不要求前端声明后端返回的每个字段（前端可以不用），但差异要显式登记，
    否则"后端加了字段前端没跟上"和"前端故意不看"就分不清了。
    """
    unexpected: list[str] = []
    for ts_name in _comparable(ts_interfaces):
        api_name = ALIASES.get(ts_name, ts_name)
        api_fields = schemas.get(api_name)
        if api_fields is None:
            continue
        ts_fields = resolve_fields(ts_name, ts_interfaces)
        for field in sorted(api_fields - ts_fields):
            if (ts_name, field) not in ALLOW_API_ONLY:
                unexpected.append(f"{ts_name}.{field}")
    assert unexpected == [], (
        "后端返回了前端未声明的字段，且未登记在 ALLOW_API_ONLY 里：\n  "
        + "\n  ".join(unexpected)
    )


# --------------------------------------------------------------------------- #
#  端点路径
#
#  上面的检查只比较**模型内的字段名**。这一组补上另一半：前端调用的
#  **URL 与 HTTP 方法**是否真的存在。
#
#  它能抓到两类问题，而 `tsc` 与字段名对账都抓不到：
#    * 路径拼错（`/post-offices` 写成 `/postoffices`）—— 运行时报 404；
#    * 后端把某个端点改名或删掉了，前端还在调。
#
#  参数名允许不同（前端用 `/cards/${id}` 而后端是 `/cards/{card_id}`），
#  所以只比较**去掉参数名之后的形状**。
# --------------------------------------------------------------------------- #

API_TS = REPO_ROOT / "apps" / "web" / "src" / "shared" / "api.ts"

#: http.get<T>('/cards') / http.post(`/cards/${id}/postmarks`)
#:
#: 泛型部分用 `[^(]*` 而不是 `[^>]*`：后者遇到嵌套泛型
#: `http.get<Paged<CardListItem>>('/cards')` 会在第一个 `>` 处提前闭合，
#: 于是匹配不到这个调用 —— 第一版就这么漏掉了 `/cards`、`/post-offices`
#: 两个端点，还反过来报"前端没调用它们"。
HTTP_CALL_RE = re.compile(
    r"""http\s*\.\s*(get|post|patch|put|delete|head|options)\s*"""
    r"""(?:<[^(]*>)?\s*\(\s*[`'"]([^`'"]+)[`'"]""",
    re.IGNORECASE | re.VERBOSE,
)
#: 直接写在字符串里的同源地址（assetRawUrl / exportCardsCsvUrl 这类）
ABS_PATH_RE = re.compile(r"""[`'"](/api/[^`'"]*)[`'"]""")
#: OpenAPI 的参数写法
BRACE_PARAM_RE = re.compile(r"\{(\w+)\}")
#: 前端模板里「纯标识符」形式的路径参数
SIMPLE_PARAM_RE = re.compile(r"\$\{(\w+)\}")


def _normalize(path: str) -> str:
    """把参数段统一成 ``{}``，好让前端与后端的命名差异不影响比较。

    要处理的两种前端写法：

      ``/cards/${id}/postmarks``                 纯标识符 → 是路径参数，变成 ``{}``
      ``/export/cards.csv${buildQueryString(...)}``  复杂表达式 → 是拼出来的查询串，
                                                 从那里**截断**

    第一版用 ``\\$\\{[^}]*\\}`` 一把替换，把后者切成了
    ``/api/export/cards.csv{})}``，于是报了一个根本不存在的"路径不存在"。
    **检查器宁可漏报也不能乱报** —— 乱报会让人开始忽略它。
    """
    path = path.split("?", 1)[0]

    out: list[str] = []
    i = 0
    while i < len(path):
        if path.startswith("${", i):
            m = SIMPLE_PARAM_RE.match(path, i)
            if m:
                out.append("{}")
                i = m.end()
                continue
            break  # 复杂表达式：后面是查询串，路径到此为止
        out.append(path[i])
        i += 1

    return BRACE_PARAM_RE.sub("{}", "".join(out))


#: 后端提供、但当前前端**有意不调用**的端点。每条都要写明理由 ——
#: 否则这个白名单会变成"把问题藏起来"的地方。
#:
#: ⚠️ 这份名单曾经有过一条 `("GET", "/api/post-offices/{}")`，理由是
#:    "编辑流程用不到：PATCH 已经返回更新后的对象"。**那个判断是错的** ——
#:    前端不是用不到，而是 `PostOfficePicker` 里留着一句注释说"后端没有这个
#:    端点"，于是它从没去调用过；而那句注释早就是过时的（端点是为它加的）。
#:    这就是"白名单会藏问题"的样子：一条本该被修的不一致，被一句理由盖住了。
#:    现在前端真的用上了它，白名单也随之清空。
ALLOW_BACKEND_ONLY: set[tuple[str, str]] = set()  # 注意：{} 是空 dict，不是空 set


def frontend_endpoints() -> set[tuple[str, str]]:
    text = API_TS.read_text(encoding="utf-8")
    out: set[tuple[str, str]] = set()
    for m in HTTP_CALL_RE.finditer(text):
        method = m.group(1).upper()
        raw = m.group(2)
        full = raw if raw.startswith("/api") else "/api" + raw
        out.add((method, _normalize(full)))
    # 这些是 <img src> / 下载链接用的地址，都是 GET
    for m in ABS_PATH_RE.finditer(text):
        out.add(("GET", _normalize(m.group(1))))
    return out


def backend_endpoints() -> set[tuple[str, str]]:
    from postmarktrip.main import app

    return {
        (method.upper(), _normalize(path))
        for path, ops in app.openapi()["paths"].items()
        for method in ops
        if method.lower() in {"get", "post", "patch", "put", "delete", "head", "options"}
    }


def test_frontend_only_calls_endpoints_that_exist() -> None:
    """★ 前端调用的每个 (方法, 路径) 都必须出现在 OpenAPI 里。

    这条能抓到 `tsc` 与字段名对账都抓不到的问题：路径拼错（运行时报 404）、
    后端改名或删掉了端点而前端还在调。
    """
    called = frontend_endpoints()
    assert len(called) >= 15, f"只从 api.ts 里解析出 {len(called)} 个端点，解析器可能坏了"

    known = backend_endpoints()
    missing = sorted(f"{m} {p}" for m, p in called if (m, p) not in known)
    assert missing == [], (
        "前端调用了 OpenAPI 里不存在的端点（会 404 或 405）：\n  " + "\n  ".join(missing)
    )


def test_every_backend_endpoint_is_reachable_from_the_frontend() -> None:
    """反向：后端暴露的端点，前端应当有对应的调用。

    不算错但值得知道 —— 常见于"后端加了端点，前端还没接上"，
    也常见于"端点其实没人用"。差异必须显式登记，否则两种情况分不清。
    """
    known = backend_endpoints()
    called = frontend_endpoints()
    unused = sorted(
        f"{m} {p}"
        for m, p in known - called - ALLOW_BACKEND_ONLY
    )
    assert unused == [], (
        "后端有、但前端 api.ts 里没有调用的端点（若是有意保留请登记到 "
        "ALLOW_BACKEND_ONLY）：\n  " + "\n  ".join(unused)
    )
