"""M3C-5：识别任务与候选应用。

识别只写 `stamp.identify_*`，不自动改 `catalog_id`；应用候选才建/选标准票。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from postmarktrip.config import get_settings
from postmarktrip.db.models import Asset, Stamp, StampCatalog
from postmarktrip.db.session import SessionLocal
from postmarktrip.domain.colnet_client import ColnetError, search_image
from postmarktrip.domain.stamp_catalog import (
    catalog_snapshot_values,
    generate_standard_code,
)
from postmarktrip.storage import AssetStore


class IdentifyApplyError(ValueError):
    """候选不存在/已关联未确认替换。"""


def _normalize_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    query_sha = payload.get("image_sha256")
    out: list[dict[str, Any]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        c_id = item.get("c_id")
        if c_id is None:
            continue
        out.append(
            {
                "c_id": int(c_id),
                "p_id": item.get("p_id"),
                "name": item.get("name"),
                "similarity": item.get("similarity"),
                "url": item.get("url"),
                "image": item.get("image"),
                "query_sha256": query_sha,
            }
        )
    return out


def run_identify_job(stamp_id: str, *, force: bool = False) -> None:
    """后台任务：读票图 → colnetApi /search → 写最近一次候选。"""
    db = SessionLocal()
    try:
        stamp = db.get(Stamp, uuid.UUID(stamp_id))
        if stamp is None:
            return
        if stamp.image_asset_id is None:
            stamp.identify_status = "error"
            stamp.identify_error = "没有票图"
            db.commit()
            return
        asset = db.get(Asset, stamp.image_asset_id)
        if asset is None:
            stamp.identify_status = "error"
            stamp.identify_error = "票图资产不存在"
            db.commit()
            return

        stamp.identify_status = "running"
        stamp.identify_error = None
        db.commit()

        settings = get_settings()
        store = AssetStore(settings.assets_dir, allowed_exts=settings.allowed_upload_exts)
        with store.open(asset.storage_path) as fh:
            image_bytes = fh.read()
        payload = search_image(
            image_bytes,
            filename=Path(asset.storage_path).name,
            content_type=asset.mime,
            force=force,
        )

        stamp = db.get(Stamp, uuid.UUID(stamp_id))
        if stamp is None:
            return
        stamp.identify_candidates = _normalize_candidates(payload)
        stamp.identify_status = "ok"
        stamp.identify_error = None
        stamp.identify_at = datetime.now(UTC)
        db.commit()
    except ColnetError as exc:
        db.rollback()
        stamp = db.get(Stamp, uuid.UUID(stamp_id))
        if stamp is not None:
            stamp.identify_status = "error"
            stamp.identify_error = f"{exc.code}: {exc.message}"[:1000]
            db.commit()
    except Exception as exc:
        db.rollback()
        stamp = db.get(Stamp, uuid.UUID(stamp_id))
        if stamp is not None:
            stamp.identify_status = "error"
            stamp.identify_error = f"{type(exc).__name__}: {exc}"[:1000]
            db.commit()
    finally:
        db.close()


def apply_candidate(
    db: Session,
    stamp: Stamp,
    c_id: int,
    *,
    replace_existing: bool = False,
) -> tuple[Stamp, StampCatalog, bool]:
    candidate = next(
        (item for item in (stamp.identify_candidates or []) if int(item.get("c_id", -1)) == c_id),
        None,
    )
    if candidate is None:
        raise IdentifyApplyError("候选不存在，请重新识别")
    if stamp.catalog_id is not None and not replace_existing:
        raise IdentifyApplyError("这枚票已关联标准票；确认替换后才可应用新候选")

    catalog = db.scalar(select(StampCatalog).where(StampCatalog.colnect_c_id == c_id))
    created = False
    if catalog is None:
        catalog = StampCatalog(
            standard_code=generate_standard_code(),
            colnect_c_id=c_id,
            colnect_url=candidate.get("url"),
            design_name=candidate.get("name"),
            source_state="draft",
            details={},
            note="M3C-5 候选识别：colnetApi 返回，未核实",
        )
        db.add(catalog)
        db.flush()
        created = True

    stamp.catalog_id = catalog.id
    for key, value in catalog_snapshot_values(catalog).items():
        setattr(stamp, key, value)
    db.commit()
    db.refresh(stamp)
    db.refresh(catalog)
    return stamp, catalog, created
