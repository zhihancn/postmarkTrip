"""M3C-4：标准库导出 / 批量回填 / 重复检测与合并。

原则：
* 回填只用**用户票已有字段**（志号/票名/面值…），不做图像搜索 top-1 自动建条目；
* 匹配不到/有多个候选时跳过，留给人工；
* 合并只动标准票：把用户票 `catalog_id` 重指到保留条目，再删重复条目；
* 导出默认不含 HTML 快照/图片原图，只导出目录字段与关联关系。
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from postmarktrip.api.deps import CurrentUser, DbDep
from postmarktrip.api.routes_stamp_catalog import (
    _apply_catalog_to_stamp,
    _sync_linked_stamps,
)
from postmarktrip.db.models import Card, Stamp, StampCatalog
from postmarktrip.domain.catalog_parse import normalize_design_no
from postmarktrip.domain.schemas import (
    BackfillApplyResult,
    BackfillGroup,
    BackfillPreview,
    DuplicateGroup,
    MergeRequest,
    StampCatalogRead,
)
from postmarktrip.domain.stamp_catalog import generate_standard_code

router = APIRouter(tags=["stamp-catalog"])

EXPORT_FIELDS = [
    "standard_code",
    "colnect_c_id",
    "colnect_url",
    "issue_url",
    "catalog_system",
    "catalog_ref",
    "design_no",
    "design_name",
    "issue_name",
    "face_value",
    "currency",
    "issue_date",
    "source_state",
    "fetch_status",
    "note",
    "created_at",
    "updated_at",
]


def _stamp_group_key(stamp: Stamp) -> str:
    return f"{(stamp.catalog_system or '').lower()}|{(stamp.catalog_ref or '').strip().lower()}"


def _matching_catalogs(db: Session, stamp: Stamp) -> list[StampCatalog]:
    ref = (stamp.catalog_ref or "").strip()
    if not ref:
        return []
    stmt = select(StampCatalog).where(func.lower(StampCatalog.catalog_ref) == ref.lower())
    if stamp.catalog_system:
        stmt = stmt.where(StampCatalog.catalog_system == stamp.catalog_system)
    else:
        stmt = stmt.where(StampCatalog.catalog_system.is_(None))
    return list(db.scalars(stmt))


def _preview(db: Session) -> BackfillPreview:
    stamps = list(
        db.scalars(
            select(Stamp).where(
                Stamp.deleted_at.is_(None),
                Stamp.catalog_id.is_(None),
                Stamp.catalog_ref.is_not(None),
                func.trim(Stamp.catalog_ref) != "",
            )
        )
    )
    buckets: dict[str, list[Stamp]] = {}
    for stamp in stamps:
        buckets.setdefault(_stamp_group_key(stamp), []).append(stamp)

    groups: list[BackfillGroup] = []
    for key, rows in buckets.items():
        ref = (rows[0].catalog_ref or "").strip()
        matches = _matching_catalogs(db, rows[0])
        names = {r.name.strip() for r in rows if r.name and r.name.strip()}
        faces = {r.face_value for r in rows if r.face_value is not None}
        currencies = {r.currency for r in rows if r.currency}
        dates = {r.issue_date for r in rows if r.issue_date is not None}

        if len(matches) == 1:
            groups.append(
                BackfillGroup(
                    key=key,
                    catalog_system=rows[0].catalog_system,
                    catalog_ref=ref,
                    stamp_count=len(rows),
                    action="link",
                    catalog_id=matches[0].id,
                    standard_code=matches[0].standard_code,
                )
            )
        elif len(matches) > 1:
            groups.append(
                BackfillGroup(
                    key=key,
                    catalog_system=rows[0].catalog_system,
                    catalog_ref=ref,
                    stamp_count=len(rows),
                    action="manual",
                    reason=f"已有 {len(matches)} 条同志号标准票，需人工选",
                )
            )
        else:
            groups.append(
                BackfillGroup(
                    key=key,
                    catalog_system=rows[0].catalog_system,
                    catalog_ref=ref,
                    stamp_count=len(rows),
                    action="create",
                    design_name=next(iter(names)) if len(names) == 1 else None,
                    face_value=next(iter(faces)) if len(faces) == 1 else None,
                    currency=next(iter(currencies)) if len(currencies) == 1 else None,
                    issue_date=next(iter(dates)) if len(dates) == 1 else None,
                )
            )
    groups.sort(key=lambda g: g.key)
    return BackfillPreview(total_stamps=len(stamps), groups=groups)


@router.get("/stamp-catalog/backfill/preview", response_model=BackfillPreview)
def backfill_preview(db: DbDep, _: CurrentUser) -> BackfillPreview:
    return _preview(db)


@router.post("/stamp-catalog/backfill/apply", response_model=BackfillApplyResult)
def backfill_apply(db: DbDep, _: CurrentUser) -> BackfillApplyResult:
    """应用当前预览：能匹配就关联，匹配不到就建 draft；多个候选跳过。"""
    preview = _preview(db)
    created = linked = skipped = 0
    touched: dict[uuid.UUID, StampCatalog] = {}

    for group in preview.groups:
        if group.action == "manual":
            skipped += group.stamp_count
            continue

        if group.catalog_id is not None:
            catalog = db.get(StampCatalog, group.catalog_id)
            if catalog is None:
                skipped += group.stamp_count
                continue
        else:
            catalog = StampCatalog(
                standard_code=generate_standard_code(),
                catalog_system=group.catalog_system,
                catalog_ref=group.catalog_ref,
                design_name=group.design_name,
                face_value=group.face_value,
                currency=group.currency,
                issue_date=group.issue_date,
                details={},
                source_state="draft",
                note="M3C-4 批量回填：从用户票已有字段创建，未核实",
            )
            db.add(catalog)
            db.flush()
            created += 1

        ref = group.catalog_ref
        stmt = select(Stamp).where(
            Stamp.deleted_at.is_(None),
            Stamp.catalog_id.is_(None),
            func.lower(Stamp.catalog_ref) == ref.lower(),
        )
        if group.catalog_system:
            stmt = stmt.where(Stamp.catalog_system == group.catalog_system)
        else:
            stmt = stmt.where(Stamp.catalog_system.is_(None))
        rows = list(db.scalars(stmt))
        for stamp in rows:
            _apply_catalog_to_stamp(db, stamp, catalog.id)
            linked += 1
        touched[catalog.id] = catalog

    db.commit()
    for catalog in touched.values():
        db.refresh(catalog)
    return BackfillApplyResult(
        created=created,
        linked=linked,
        skipped=skipped,
        entries=[StampCatalogRead.model_validate(c) for c in touched.values()],
    )


def _catalog_entry(catalog: StampCatalog, links: list[tuple[uuid.UUID, uuid.UUID, Any]]) -> dict[str, Any]:
    return {
        "standard_code": catalog.standard_code,
        "colnect_c_id": catalog.colnect_c_id,
        "colnect_url": catalog.colnect_url,
        "issue_url": catalog.issue_url,
        "catalog_system": catalog.catalog_system,
        "catalog_ref": catalog.catalog_ref,
        "design_no": catalog.design_no,
        "design_name": catalog.design_name,
        "issue_name": catalog.issue_name,
        "face_value": str(catalog.face_value) if catalog.face_value is not None else None,
        "currency": catalog.currency,
        "issue_date": catalog.issue_date.isoformat() if catalog.issue_date else None,
        "source_state": catalog.source_state,
        "fetch_status": catalog.fetch_status,
        "details": catalog.details,
        "note": catalog.note,
        "created_at": catalog.created_at.isoformat(),
        "updated_at": catalog.updated_at.isoformat(),
        "linked_stamp_ids": [str(row[0]) for row in links],
        "linked_card_ids": sorted({str(row[1]) for row in links}),
    }


def _export_rows(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(StampCatalog, Stamp.id, Card.id, Card.visit_date)
        .outerjoin(Stamp, (Stamp.catalog_id == StampCatalog.id) & Stamp.deleted_at.is_(None))
        .outerjoin(Card, (Card.id == Stamp.card_id) & Card.deleted_at.is_(None))
        .order_by(StampCatalog.standard_code, Stamp.id)
    ).all()
    grouped: dict[uuid.UUID, tuple[StampCatalog, list[tuple[uuid.UUID, uuid.UUID, Any]]]] = {}
    for catalog, stamp_id, card_id, visit_date in rows:
        entry = grouped.setdefault(catalog.id, (catalog, []))
        if stamp_id is not None and card_id is not None:
            entry[1].append((stamp_id, card_id, visit_date))
    return [_catalog_entry(c, links) for c, links in grouped.values()]


@router.get("/stamp-catalog/export")
def export_catalog(
    db: DbDep,
    _: CurrentUser,
    format: Annotated[Literal["json", "csv"], Query()] = "json",
) -> Response:
    entries = _export_rows(db)
    exported_at = datetime.now(UTC).isoformat()
    if format == "json":
        payload = {
            "version": 1,
            "exported_at": exported_at,
            "count": len(entries),
            "entries": entries,
        }
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="stamp-catalog.json"'},
        )

    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            *EXPORT_FIELDS,
            "details_json",
            "linked_stamp_ids",
            "linked_card_ids",
        ],
    )
    writer.writeheader()
    for entry in entries:
        writer.writerow(
            {
                **{key: entry.get(key) for key in EXPORT_FIELDS},
                "details_json": json.dumps(entry["details"], ensure_ascii=False),
                "linked_stamp_ids": ";".join(entry["linked_stamp_ids"]),
                "linked_card_ids": ";".join(entry["linked_card_ids"]),
            }
        )
    return Response(
        "\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="stamp-catalog.csv"'},
    )


def _duplicates(db: Session) -> list[DuplicateGroup]:
    catalogs = list(db.scalars(select(StampCatalog).order_by(StampCatalog.standard_code)))
    buckets: dict[str, list[StampCatalog]] = {}
    reasons: dict[str, str] = {}

    def add_bucket(key: str, reason: str, catalog: StampCatalog) -> None:
        buckets.setdefault(key, []).append(catalog)
        reasons[key] = reason

    for catalog in catalogs:
        if catalog.catalog_ref and catalog.design_no:
            key = (
                f"catalog:{(catalog.catalog_system or '').lower()}:"
                f"{catalog.catalog_ref.lower()}:{normalize_design_no(catalog.design_no)}"
            )
            add_bucket(key, "同一目录体系 + 志号 + 图序", catalog)
        if catalog.issue_key and catalog.design_no:
            key = f"issue:{catalog.issue_key}:{normalize_design_no(catalog.design_no)}"
            add_bucket(key, "同一发行页 + 图序", catalog)

    output: list[DuplicateGroup] = []
    seen: set[tuple[str, ...]] = set()
    for key, rows in buckets.items():
        if len(rows) < 2:
            continue
        signature = tuple(sorted(row.standard_code for row in rows))
        if signature in seen:
            continue
        seen.add(signature)
        output.append(
            DuplicateGroup(
                key=key,
                reason=reasons[key],
                entries=[StampCatalogRead.model_validate(row) for row in rows],
            )
        )
    return output


@router.get("/stamp-catalog/duplicates", response_model=list[DuplicateGroup])
def list_duplicates(db: DbDep, _: CurrentUser) -> list[DuplicateGroup]:
    return _duplicates(db)


@router.post("/stamp-catalog/merge", response_model=StampCatalogRead)
def merge_catalog(payload: MergeRequest, db: DbDep, _: CurrentUser) -> StampCatalogRead:
    target = db.get(StampCatalog, payload.target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "保留条目不存在")
    source_ids = [sid for sid in payload.source_ids if sid != target.id]
    if not source_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "source_ids 不能只包含 target")

    sources = list(db.scalars(select(StampCatalog).where(StampCatalog.id.in_(source_ids))))
    if len(sources) != len(set(source_ids)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "有 source 条目不存在")

    now = datetime.now(UTC).isoformat()
    merged_from = list((target.details or {}).get("merged_from") or [])
    for source in sources:
        if (
            target.colnect_c_id is not None
            and source.colnect_c_id is not None
            and target.colnect_c_id != source.colnect_c_id
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"两条的 Colnect c_id 不同：{target.colnect_c_id} / {source.colnect_c_id}",
            )
        if target.colnect_c_id is None and source.colnect_c_id is not None:
            # unique 约束：先 flush source 的清空，再给 target 赋值
            c_id = source.colnect_c_id
            source.colnect_c_id = None
            db.flush()
            target.colnect_c_id = c_id
            db.flush()
        for field in (
            "colnect_url",
            "issue_url",
            "issue_key",
            "catalog_system",
            "catalog_ref",
            "design_no",
            "design_name",
            "issue_name",
            "face_value",
            "currency",
            "issue_date",
            "note",
        ):
            if getattr(target, field) in (None, "") and getattr(source, field) not in (None, ""):
                setattr(target, field, getattr(source, field))

        merged_details = dict(source.details or {})
        merged_details.update(target.details or {})
        merged_details["merged_from"] = [
            *merged_from,
            {
                "standard_code": source.standard_code,
                "id": str(source.id),
                "merged_at": now,
            },
        ]
        merged_from = merged_details["merged_from"]
        target.details = merged_details

        db.execute(update(Stamp).where(Stamp.catalog_id == source.id).values(catalog_id=target.id))
        db.delete(source)

    db.flush()
    _sync_linked_stamps(db, target)
    db.commit()
    db.refresh(target)
    return StampCatalogRead.model_validate(target)
