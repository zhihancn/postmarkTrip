"""标准邮票库（M3C）的纯逻辑。

刻意不依赖 SQLAlchemy / FastAPI：URL 归一化、编号生成、快照字段这些规则
可以被单测直接钉住，不需要数据库。
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def normalize_issue_url(url: str | None) -> str | None:
    """把官方发行 URL 归一化，用于查重。

    规则：小写 scheme/host、去掉 query/fragment、去掉末尾 `/`（根路径除外）。
    不改变 path 的大小写 —— 有些站点 path 是大小写敏感的。
    """
    if url is None:
        return None
    raw = url.strip()
    if raw == "":
        return None
    parts = urlsplit(raw)
    if not parts.scheme and not parts.netloc:
        # 允许用户只填路径；至少保持非空
        return raw.rstrip("/") or raw
    path = parts.path.rstrip("/")
    if path == "":
        path = "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def generate_standard_code(catalog_id: uuid.UUID | None = None) -> str:
    """兜底人读编号：`sc-<uuid8>`。语义编号由用户/导入脚本提供。"""
    value = catalog_id or uuid.uuid4()
    return f"sc-{value.hex[:8]}"


def catalog_snapshot_values(catalog: Any) -> dict[str, Any]:
    """把标准票的"可快照字段"映射到用户票 `stamp` 的列。

    只映射 `stamp` 真实存在的列；`design_name` 优先作为票名，其次套名。
    返回的 dict 只含非空值，避免把用户已经填好的字段清成 None。
    """
    values: dict[str, Any] = {}
    if catalog.catalog_system:
        values["catalog_system"] = catalog.catalog_system
    if catalog.catalog_ref:
        values["catalog_ref"] = catalog.catalog_ref
    name = catalog.design_name or catalog.issue_name
    if name:
        values["name"] = name
    if catalog.face_value is not None:
        values["face_value"] = catalog.face_value
    if catalog.currency:
        values["currency"] = catalog.currency
    if catalog.issue_date is not None:
        values["issue_date"] = catalog.issue_date
    return values
