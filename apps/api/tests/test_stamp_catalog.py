"""标准邮票库（M3C-1/2）的单元测试。

不连数据库：测的是 URL 归一化、编号生成、快照字段、查重分支和关联逻辑。
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from postmarktrip.api.routes_stamp_catalog import (
    _apply_catalog_to_stamp,
    _check_duplicates,
)
from postmarktrip.domain.schemas import StampCatalogCreate, StampCreate
from postmarktrip.domain.stamp_catalog import (
    catalog_snapshot_values,
    generate_standard_code,
    normalize_issue_url,
)

# --------------------------------------------------------------------------- #
#  URL / 编号
# --------------------------------------------------------------------------- #


def test_normalize_issue_url_removes_query_fragment_and_trailing_slash() -> None:
    assert (
        normalize_issue_url("HTTPS://CPI.ChinaPost.com.cn/jy/stamp/1.htm/?utm=1#top")
        == "https://cpi.chinapost.com.cn/jy/stamp/1.htm"
    )
    assert normalize_issue_url("https://example.com/") == "https://example.com/"
    assert normalize_issue_url("  ") is None
    assert normalize_issue_url(None) is None


def test_normalize_issue_url_keeps_path_case() -> None:
    # path 可能大小写敏感，不能因为 host 小写就顺手把 path 也改了
    assert (
        normalize_issue_url("https://Example.com/Stamps/A-1")
        == "https://example.com/Stamps/A-1"
    )


def test_generate_standard_code_is_stable_and_readable() -> None:
    fixed = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert generate_standard_code(fixed) == "sc-12345678"
    code = generate_standard_code()
    assert code.startswith("sc-") and len(code) == 11


# --------------------------------------------------------------------------- #
#  快照 / 关联
# --------------------------------------------------------------------------- #


def _catalog(**over: Any) -> Any:
    values = {
        "catalog_system": "china",
        "catalog_ref": "普30",
        "design_name": "保护森林",
        "issue_name": "普30 保护森林",
        "face_value": Decimal("0.40"),
        "currency": "CNY",
        "issue_date": date(2002, 2, 1),
    }
    values.update(over)
    return SimpleNamespace(**values)


def test_catalog_snapshot_prefers_design_name_and_skips_nulls() -> None:
    values = catalog_snapshot_values(_catalog(design_name=None))
    assert values["name"] == "普30 保护森林"
    assert values["face_value"] == Decimal("0.40")

    empty = catalog_snapshot_values(
        _catalog(
            catalog_system=None,
            catalog_ref=None,
            design_name=None,
            issue_name=None,
            face_value=None,
            currency=None,
            issue_date=None,
        )
    )
    assert empty == {}


def test_apply_catalog_to_stamp_sets_fk_and_snapshot() -> None:
    catalog = _catalog()
    catalog.id = uuid.uuid4()

    class _Db:
        def get(self, model: Any, key: Any) -> Any:
            return catalog

    stamp = SimpleNamespace(catalog_id=None, catalog_ref=None, name=None)
    _apply_catalog_to_stamp(_Db(), stamp, catalog.id)  # type: ignore[arg-type]
    assert stamp.catalog_id == catalog.id
    assert stamp.catalog_ref == "普30"
    assert stamp.name == "保护森林"


def test_apply_catalog_to_stamp_can_unlink() -> None:
    stamp = SimpleNamespace(catalog_id=uuid.uuid4(), catalog_ref="普30")

    class _Db:
        def get(self, model: Any, key: Any) -> Any:  # pragma: no cover - 不应调用
            raise AssertionError("unlink 不该查库")

    _apply_catalog_to_stamp(_Db(), stamp, None)  # type: ignore[arg-type]
    assert stamp.catalog_id is None


# --------------------------------------------------------------------------- #
#  查重
# --------------------------------------------------------------------------- #


class _ScalarDb:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = list(rows)
        self.seen: list[str] = []

    def scalar(self, stmt: Any) -> Any:
        self.seen.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        return self.rows.pop(0) if self.rows else None


def test_check_duplicates_rejects_existing_code() -> None:
    db = _ScalarDb([SimpleNamespace(standard_code="cn-2023-1-d02")])
    with pytest.raises(HTTPException) as exc:
        _check_duplicates(
            db,  # type: ignore[arg-type]
            standard_code="cn-2023-1-d02",
            colnect_c_id=None,
            issue_key=None,
            design_no=None,
        )
    assert exc.value.status_code == 409
    assert "标准编号已存在" in exc.value.detail


def test_check_duplicates_rejects_same_issue_and_design() -> None:
    db = _ScalarDb(
        [
            None,  # standard_code 不冲突
            SimpleNamespace(standard_code="cn-2023-1-d01"),
        ]
    )
    with pytest.raises(HTTPException) as exc:
        _check_duplicates(
            db,  # type: ignore[arg-type]
            standard_code="cn-2023-1-d02",
            colnect_c_id=None,
            issue_key="https://example.com/set",
            design_no="(2-2)",
        )
    assert exc.value.status_code == 409
    assert "同一发行页 + 图序" in exc.value.detail


def test_check_duplicates_allows_no_soft_key() -> None:
    db = _ScalarDb([None, None])
    _check_duplicates(
        db,  # type: ignore[arg-type]
        standard_code="cn-x",
        colnect_c_id=None,
        issue_key="https://example.com/set",
        design_no=None,
    )
    assert len(db.seen) == 1


# --------------------------------------------------------------------------- #
#  Schema
# --------------------------------------------------------------------------- #


def test_stamp_catalog_create_upcases_currency_and_accepts_only_three_columns() -> None:
    payload = StampCatalogCreate(
        colnect_url="https://colnect.com/zt/stamps/stamp/1",
        issue_url="https://example.com/issue",
        currency="cny",
    )
    assert payload.currency == "CNY"
    assert payload.standard_code is None
    assert payload.details == {}


def test_stamp_create_accepts_catalog_id() -> None:
    catalog_id = uuid.uuid4()
    payload = StampCreate(catalog_id=catalog_id)
    assert payload.catalog_id == catalog_id
