"""M3C-3 服务层：抓取 → 快照 → 解析 → 复核应用。

抓取失败**不覆盖**已有字段：只写 `fetch_status/fetch_error`，旧快照/旧字段保留。
解析结果先写 `details.last_parse`，人工点“应用”后才改目录字段。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from postmarktrip.config import get_settings
from postmarktrip.db.models import StampCatalog
from postmarktrip.db.session import SessionLocal
from postmarktrip.domain.catalog_fetch import FetchedPage, fetch_page, save_snapshot
from postmarktrip.domain.catalog_parse import (
    ParsedCatalog,
    ParsedDesign,
    parse_catalog_html,
    pick_design,
)

Fetcher = Callable[[str], FetchedPage]


class ApplyParseError(ValueError):
    """解析结果不能应用（没有结果/一页多枚未选/数据不合法）。"""


def _design_from_dict(raw: dict[str, Any]) -> ParsedDesign:
    value = raw.get("face_value")
    face: Decimal | None = None
    if value not in (None, ""):
        try:
            face = Decimal(str(value))
        except (InvalidOperation, ValueError):
            face = None
    return ParsedDesign(
        design_no=raw.get("design_no"),
        design_name=raw.get("design_name"),
        face_value=face,
        currency=raw.get("currency"),
    )


def _parsed_from_details(details: dict[str, Any]) -> ParsedCatalog | None:
    raw = details.get("last_parse")
    if not isinstance(raw, dict):
        return None
    issue_date = None
    if raw.get("issue_date"):
        try:
            issue_date = date.fromisoformat(str(raw["issue_date"]))
        except ValueError:
            issue_date = None
    designs = [
        _design_from_dict(item) for item in raw.get("designs") or [] if isinstance(item, dict)
    ]
    return ParsedCatalog(
        issue_name=raw.get("issue_name"),
        catalog_ref=raw.get("catalog_ref"),
        issue_date=issue_date,
        details={k: str(v) for k, v in (raw.get("details") or {}).items() if v not in (None, "")},
        designs=designs,
        field_sources=raw.get("field_sources") or {},
        source_url=str(raw.get("source_url") or ""),
        parse_version=str(raw.get("parse_version") or "unknown"),
    )


def fetch_and_store(
    db: Session,
    catalog_id: uuid.UUID,
    *,
    fetcher: Fetcher = fetch_page,
    data_dir: Path | None = None,
) -> StampCatalog:
    """同步执行一次抓取；调用方负责在后台线程里跑它。

    成功：写快照 + `details.last_parse`，**不动目录字段**。
    失败：只写 `fetch_status=error` / `fetch_error`，旧字段和旧快照保留。
    """
    catalog = db.get(StampCatalog, catalog_id)
    if catalog is None:
        raise LookupError("标准票不存在")
    if not catalog.issue_url:
        raise ValueError("没有原始发行网址")

    catalog.fetch_status = "running"
    catalog.fetch_error = None
    db.commit()

    try:
        fetched = fetcher(catalog.issue_url)
        parsed = parse_catalog_html(fetched.html, source_url=fetched.final_url)
        snapshot_dir = Path(data_dir) if data_dir is not None else Path(get_settings().data_dir)
        snapshot_path = save_snapshot(snapshot_dir, fetched)

        details = dict(catalog.details or {})
        details["last_parse"] = {
            **parsed.to_dict(),
            "fetched_at": fetched.fetched_at.isoformat(),
            "final_url": fetched.final_url,
            "snapshot_sha256": fetched.sha256,
            "snapshot_bytes": fetched.byte_size,
            "snapshot_path": snapshot_path,
        }
        catalog.details = details
        catalog.raw_snapshot_path = snapshot_path
        catalog.raw_sha256 = fetched.sha256
        catalog.last_fetched_at = fetched.fetched_at
        catalog.parse_version = parsed.parse_version
        catalog.fetch_status = "ok"
        catalog.fetch_error = None
        if catalog.source_state == "draft":
            catalog.source_state = "partial"
        db.commit()
        db.refresh(catalog)
        return catalog
    except Exception as exc:
        db.rollback()
        fresh = db.get(StampCatalog, catalog_id)
        if fresh is not None:
            fresh.fetch_status = "error"
            fresh.fetch_error = f"{type(exc).__name__}: {exc}"[:1000]
            db.commit()
        raise


def run_fetch_job(catalog_id: str) -> None:
    """FastAPI BackgroundTasks 入口：自己开/关 session，异常不再外抛。"""
    db = SessionLocal()
    try:
        fetch_and_store(db, uuid.UUID(catalog_id))
    except Exception:
        pass
    finally:
        db.close()


def apply_last_parse(
    db: Session, catalog: StampCatalog, design_no: str | None = None
) -> StampCatalog:
    """人工点击“应用解析结果”：只写非空字段，状态回到 partial。"""
    details = dict(catalog.details or {})
    parsed = _parsed_from_details(details)
    if parsed is None:
        raise ApplyParseError("没有可应用的解析结果，请先抓取")

    chosen = pick_design(parsed, design_no, catalog.design_no)
    if chosen is None:
        choices = "、".join(d.design_no or "（无图序）" for d in parsed.designs)
        raise ApplyParseError(f"这一页有 {len(parsed.designs)} 枚，请先选择图序：{choices}")

    apply: dict[str, Any] = {
        "catalog_ref": parsed.catalog_ref,
        "issue_name": parsed.issue_name,
        "issue_date": parsed.issue_date,
        "design_no": chosen.design_no,
        "design_name": chosen.design_name,
        "face_value": chosen.face_value,
        "currency": chosen.currency,
    }
    for key, value in apply.items():
        if value not in (None, ""):
            setattr(catalog, key, value)

    merged = dict(catalog.details or {})
    merged.update({k: v for k, v in parsed.details.items() if v not in (None, "")})
    merged["field_sources"] = parsed.field_sources
    merged["applied_parse"] = {
        **(details.get("last_parse") or {}),
        "applied_at": datetime.now(UTC).isoformat(),
        "applied_design_no": chosen.design_no,
    }
    catalog.details = merged
    catalog.source_state = "partial"
    db.commit()
    db.refresh(catalog)
    return catalog
