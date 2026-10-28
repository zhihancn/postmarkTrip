"""M3C-3：抓取解析的纯逻辑 + 服务层（用 fake session，不连数据库）。"""

from __future__ import annotations

import gzip
import hashlib
import socket
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from postmarktrip.domain.catalog_fetch import (
    FetchedPage,
    _decompress,
    is_safe_public_url,
)
from postmarktrip.domain.catalog_parse import (
    normalize_design_no,
    parse_catalog_html,
    parse_face_value,
    parse_issue_date,
    pick_design,
)
from postmarktrip.domain.catalog_service import (
    ApplyParseError,
    apply_last_parse,
    fetch_and_store,
)

FIXTURE = Path(__file__).parent / "fixtures" / "cn_cpi_2026_11.html"


@pytest.fixture(scope="module")
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8-sig")


def parsed(html: str) -> Any:
    return parse_catalog_html(
        html, source_url="https://cpi.chinapost.com.cn/jy/stamp/1024/889210-1.htm"
    )


def test_parse_china_official_table(html: str) -> None:
    page = parsed(html)
    assert page.issue_name == "中国人民解放军军旗、军徽、军歌"
    assert page.catalog_ref == "2026-11"
    assert page.issue_date is not None and page.issue_date.isoformat() == "2026-08-01"
    assert len(page.designs) == 3
    assert page.designs[0].design_no == "（3-1）J"
    assert page.designs[0].design_name == "中国人民解放军军旗"
    assert page.designs[0].face_value == Decimal("1.20")
    assert page.designs[0].currency == "CNY"
    assert page.details["perforation"] == "13.5 度"
    assert page.details["printing"] == "胶雕套印"
    assert page.details["designer"] == "郝欧"
    assert page.details["printer"] == "北京邮票厂有限公司"


def test_parse_face_value_units() -> None:
    assert parse_face_value("1.20 元") == (Decimal("1.20"), "CNY")
    assert parse_face_value("40 分") == (Decimal("0.40"), "CNY")
    assert parse_face_value("1.2 元") == (Decimal("1.2"), "CNY")
    assert parse_face_value("USD 4.50") == (Decimal("4.50"), "USD")
    assert parse_face_value("") == (None, None)


def test_parse_issue_date_formats() -> None:
    assert parse_issue_date("2026-08-01").isoformat() == "2026-08-01"  # type: ignore[union-attr]
    assert parse_issue_date("2026年8月1日").isoformat() == "2026-08-01"  # type: ignore[union-attr]
    assert parse_issue_date("不是日期") is None


def test_design_no_normalization_and_pick(html: str) -> None:
    page = parsed(html)
    assert normalize_design_no("（3-2）J") == "(3-2)J"
    assert normalize_design_no(" (3-2)J ") == "(3-2)J"
    picked = pick_design(page, "(3-2)J", None)
    assert picked is not None and picked.design_name == "中国人民解放军军徽"
    # 没有指定、库里也没有图序：一页多枚不能猜
    assert pick_design(page, None, None) is None


def test_public_url_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    # 不在单测里做真实 DNS；只验证判断逻辑。
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    assert is_safe_public_url("https://cpi.chinapost.com.cn/jy/stamp/1024/889210-1.htm")
    assert not is_safe_public_url("http://127.0.0.1/")
    assert not is_safe_public_url("http://localhost/")
    assert not is_safe_public_url("file:///etc/passwd")
    assert not is_safe_public_url("http://10.0.0.8/")


def test_decompress_gzip_response() -> None:
    body = "<html><title>邮票</title></html>".encode()
    assert _decompress(gzip.compress(body), "gzip") == body
    assert _decompress(body, "") == body


# --------------------------------------------------------------------------- #
#  service（fake session）
# --------------------------------------------------------------------------- #


class FakeSession:
    def __init__(self, catalog: Any) -> None:
        self.catalog = catalog
        self.commits = 0
        self.rollbacks = 0

    def get(self, model: Any, key: Any) -> Any:
        return self.catalog

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def refresh(self, obj: Any) -> None:
        return None


def catalog(**over: Any) -> Any:
    values = {
        "id": "00000000-0000-0000-0000-000000000001",
        "issue_url": "https://example.com/issue",
        "catalog_ref": None,
        "issue_name": None,
        "issue_date": None,
        "design_no": None,
        "design_name": None,
        "face_value": None,
        "currency": None,
        "details": {},
        "source_state": "draft",
        "fetch_status": "idle",
        "fetch_error": None,
        "raw_snapshot_path": None,
        "raw_sha256": None,
        "last_fetched_at": None,
        "parse_version": None,
    }
    values.update(over)
    return SimpleNamespace(**values)


def fetched(html: str) -> FetchedPage:
    body = html.encode("utf-8")
    return FetchedPage(
        html=html,
        body=body,
        final_url="https://example.com/issue",
        content_type="text/html; charset=utf-8",
        sha256=hashlib.sha256(body).hexdigest(),
        byte_size=len(body),
        fetched_at=datetime.now(UTC),
    )


def test_fetch_and_store_writes_parse_but_not_fields(html: str, tmp_path: Path) -> None:
    row = catalog()
    db = FakeSession(row)
    fetch_and_store(db, row.id, fetcher=lambda url: fetched(html), data_dir=tmp_path)
    assert row.fetch_status == "ok"
    assert row.fetch_error is None
    assert row.catalog_ref is None  # 解析结果先不写目录字段
    assert row.details["last_parse"]["catalog_ref"] == "2026-11"
    assert len(row.details["last_parse"]["designs"]) == 3
    assert row.raw_snapshot_path and (tmp_path / row.raw_snapshot_path).exists()
    assert row.source_state == "partial"


def test_fetch_failure_keeps_fields_and_sets_error(html: str) -> None:
    row = catalog(catalog_ref="已有志号", design_name="已有名字")
    db = FakeSession(row)

    def boom(url: str) -> FetchedPage:
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        fetch_and_store(db, row.id, fetcher=boom)
    assert row.fetch_status == "error"
    assert "network down" in (row.fetch_error or "")
    assert row.catalog_ref == "已有志号"
    assert row.design_name == "已有名字"


def test_apply_requires_design_when_multiple(html: str) -> None:
    page = parsed(html)
    row = catalog(details={"last_parse": page.to_dict()})
    with pytest.raises(ApplyParseError) as exc:
        apply_last_parse(FakeSession(row), row, None)
    assert "请先选择图序" in str(exc.value)


def test_apply_selected_design_and_sources(html: str) -> None:
    page = parsed(html)
    row = catalog(details={"last_parse": page.to_dict()})
    apply_last_parse(FakeSession(row), row, "(3-2)J")
    assert row.catalog_ref == "2026-11"
    assert row.design_no == "（3-2）J"
    assert row.design_name == "中国人民解放军军徽"
    assert row.face_value == Decimal("1.20")
    assert row.currency == "CNY"
    assert row.source_state == "partial"
    assert row.details["field_sources"]["catalog_ref"]["confidence"] == 0.95
    assert row.details["applied_parse"]["applied_design_no"] == "（3-2）J"
