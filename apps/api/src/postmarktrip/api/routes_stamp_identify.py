"""M3C-5：邮票识别路由。

识别只写候选；应用候选才建/选标准票并关联。官方页解析仍走 M3C-3。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from sqlalchemy.orm import Session

from postmarktrip.api.deps import CurrentUser, DbDep
from postmarktrip.config import settings
from postmarktrip.db.models import Stamp
from postmarktrip.domain.schemas import (
    StampCatalogRead,
    StampIdentifyApply,
    StampIdentifyApplyResult,
    StampIdentifyRequest,
    StampRead,
)
from postmarktrip.domain.stamp_identify import (
    IdentifyApplyError,
    apply_candidate,
    run_identify_job,
)

router = APIRouter(tags=["stamp-identify"])

IDENTIFY_STALE_AFTER = timedelta(minutes=2)


def _get_stamp(db: Session, stamp_id: uuid.UUID) -> Stamp:
    stamp = db.get(Stamp, stamp_id)
    if stamp is None or stamp.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邮票不存在")
    return stamp


@router.post(
    "/stamps/{stamp_id}/identify",
    response_model=StampRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def identify_stamp(
    stamp_id: uuid.UUID,
    payload: StampIdentifyRequest,
    background_tasks: BackgroundTasks,
    db: DbDep,
    _: CurrentUser,
) -> StampRead:
    stamp = _get_stamp(db, stamp_id)
    if not settings.colnet_enabled:
        # 本仓库**不含** colnetApi：没有那套服务就不该让按钮可用。
        # 前端会先按 /api/health 的 stamp_identify.enabled 置灰，这里是第二道闸。
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "邮票识别服务未启用。它依赖本仓库之外的 colnetApi；"
            "自建了该服务时，在 .env 设 COLNET_ENABLED=true（及 COLNET_API_URL）。",
        )
    if stamp.image_asset_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "没有票图：请先在电子卡里框出邮票，或上传票图",
        )
    if (
        stamp.identify_status in ("queued", "running")
        and stamp.identify_at is not None
        and datetime.now(UTC) - stamp.identify_at < IDENTIFY_STALE_AFTER
    ):
        return StampRead.model_validate(stamp)
    if stamp.identify_status == "ok" and stamp.identify_candidates and not payload.force:
        return StampRead.model_validate(stamp)

    stamp.identify_status = "queued"
    stamp.identify_error = None
    stamp.identify_at = datetime.now(UTC)
    db.commit()
    db.refresh(stamp)
    background_tasks.add_task(run_identify_job, str(stamp.id), force=payload.force)
    return StampRead.model_validate(stamp)


@router.post(
    "/stamps/{stamp_id}/identify/apply",
    response_model=StampIdentifyApplyResult,
)
def apply_identify_candidate(
    stamp_id: uuid.UUID,
    payload: StampIdentifyApply,
    db: DbDep,
    _: CurrentUser,
) -> StampIdentifyApplyResult:
    stamp = _get_stamp(db, stamp_id)
    try:
        stamp, catalog, created = apply_candidate(
            db,
            stamp,
            payload.c_id,
            replace_existing=payload.replace_existing,
        )
    except IdentifyApplyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return StampIdentifyApplyResult(
        stamp=StampRead.model_validate(stamp),
        catalog=StampCatalogRead.model_validate(catalog),
        created=created,
    )
