"""电子卡（v1.1 M1）：模板列表、场景读写、套模板、框票贴片。

依据 `docs/spec/v1.1` §2–§4 与 `docs/spec/v1.1.1` 的边界：

* **模板**：内置的是 `public-data/templates/*.json`（只读、随仓库）；
  用户模板在 `template` 表（M2 的编辑器写入，M1 只读）。
* **场景**：`card.scene` 是快照，`template_id` 只记来源。
* **套模板** = 把模板场景复制一份、按"kind + 序号"把素材绑进去；
  `dry_run=true`（默认）**不写库**，只返回预览场景。
* **框票贴片**：在整卡图上人工框一次 → `stamp_cut` 派生物 +（必要时）建一条 `stamp`。
  **不是识别**：不调任何模型、不改切分/定位参数。
"""

from __future__ import annotations

import io
import json
import logging
import math
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from postmarktrip.api.deps import CurrentUser, DbDep, StoreDep
from postmarktrip.api.http import UNPROCESSABLE
from postmarktrip.db.models import Asset, Card, Postmark, Stamp, Template
from postmarktrip.domain import scene as scene_domain
from postmarktrip.domain.ingest import register_derived_asset
from postmarktrip.domain.schemas import (
    ApplyTemplateIn,
    ApplyTemplateOut,
    CardSceneRead,
    CardSceneWrite,
    StampCutIn,
    StampCutOut,
    TemplateDetail,
    TemplateRead,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["ecard"])

BUILTIN_PREFIX = "builtin:"
Scope = Literal["builtin", "user"]


# --------------------------------------------------------------------------- #
#  模板
# --------------------------------------------------------------------------- #


def _builtin_dir() -> Path:
    """内置模板随**代码**分发（`postmarktrip/templates/`），不放 `public-data/`。

    `public-data/` 的定位是第三方许可数据（行政区划/邮局名录），而且它在容器里
    是**只读挂载**；把项目自己的内容放进去，等于要求宿主机对那个目录有写权限 ——
    2026-10-21 在 WSL 部署时正是这里卡住（目录属主是容器用户 10001）。
    模板是代码的一部分，随包走就不会有这个问题。
    """
    return Path(__file__).resolve().parents[1] / "templates"


def load_builtin_templates() -> dict[str, dict[str, Any]]:
    """读 `public-data/templates/*.json`。

    **坏模板不该让整个列表 500**：校验失败的跳过并记一条日志，
    同时有一条测试保证"仓库里自带的模板全部能通过校验"
    （`tests/test_ecard.py::test_builtin_templates_validate`）。
    """
    out: dict[str, dict[str, Any]] = {}
    directory = _builtin_dir()
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            scene = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # pragma: no cover - 文件坏了才会走到
            log.warning("内置模板读不了：%s（%s）", path.name, exc)
            continue
        errors = scene_domain.validate_scene(scene)
        if errors:
            log.warning("内置模板 %s 不合法：%s", path.name, "; ".join(errors))
            continue
        out[f"{BUILTIN_PREFIX}{path.stem}"] = scene
    return out


def _resolve_template(db: Session, template_id: str) -> tuple[dict[str, Any], uuid.UUID | None]:
    """返回 (场景, 用户模板 id 或 None)。内置模板没有数据库行。"""
    if template_id.startswith(BUILTIN_PREFIX):
        scene = load_builtin_templates().get(template_id)
        if scene is None:
            name = template_id[len(BUILTIN_PREFIX) :]
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"没有这个内置模板：{name}")
        return scene, None
    try:
        tid = uuid.UUID(template_id)
    except ValueError as exc:
        raise HTTPException(
            UNPROCESSABLE, f"模板 id 既不是 builtin:<name> 也不是 UUID：{template_id}"
        ) from exc
    row = db.get(Template, tid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    return dict(row.scene), row.id


def _template_read(
    template_id: str, name: str, scene: dict[str, Any], scope: Scope
) -> TemplateRead:
    summary = scene_domain.scene_summary(scene)
    return TemplateRead(
        id=template_id,
        name=name,
        scope=scope,
        canvas=summary["canvas"],
        slot_count=summary["slot_count"],
    )


@router.get("/templates", response_model=list[TemplateRead])
def list_templates(db: DbDep, _: CurrentUser) -> list[TemplateRead]:
    out = [
        _template_read(tid, str(scene.get("name") or tid), scene, "builtin")
        for tid, scene in load_builtin_templates().items()
    ]
    rows = db.scalars(select(Template).order_by(Template.name)).all()
    out.extend(_template_read(str(row.id), row.name, dict(row.scene), "user") for row in rows)
    return out


@router.get("/templates/{template_id}", response_model=TemplateDetail)
def get_template(template_id: str, db: DbDep, _: CurrentUser) -> TemplateDetail:
    scene, template_uuid = _resolve_template(db, template_id)
    base = _template_read(
        template_id,
        str(scene.get("name") or template_id),
        scene,
        "builtin" if template_uuid is None else "user",
    )
    return TemplateDetail(**base.model_dump(), scene=scene)


# --------------------------------------------------------------------------- #
#  卡片场景
# --------------------------------------------------------------------------- #


def _get_card(db: Session, card_id: uuid.UUID) -> Card:
    card = db.get(Card, card_id)
    if card is None or card.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "卡片不存在")
    return card


def _assets_sha(db: Session, asset_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not asset_ids:
        return {}
    rows = db.scalars(select(Asset).where(Asset.id.in_(asset_ids))).all()
    return {row.id: row.sha256 for row in rows}


def _refs(items: list[Postmark] | list[Stamp], sha: dict[uuid.UUID, str]) -> list[str | None]:
    """每条记录占一个槽位序号 —— **缺图的那条要留 None**，不能把它挤掉。

    否则"第 2 枚戳没有贴片"会让第 3 枚戳的图滑到第 2 个槽位上（错位比缺图更糟）。
    """
    out: list[str | None] = []
    for item in items:
        asset_id = item.image_asset_id
        out.append(scene_domain.asset_ref(sha[asset_id]) if asset_id in sha else None)
    return out


@router.get("/cards/{card_id}/scene", response_model=CardSceneRead)
def get_card_scene(card_id: uuid.UUID, db: DbDep, _: CurrentUser) -> CardSceneRead:
    card = _get_card(db, card_id)
    return CardSceneRead(card_id=card.id, scene=card.scene, template_id=card.template_id)


@router.put("/cards/{card_id}/scene", response_model=CardSceneRead)
def put_card_scene(
    card_id: uuid.UUID, payload: CardSceneWrite, db: DbDep, _: CurrentUser
) -> CardSceneRead:
    card = _get_card(db, card_id)
    errors = scene_domain.validate_scene(payload.scene)
    if errors:
        raise HTTPException(UNPROCESSABLE, "场景不合法：" + "；".join(errors[:5]))
    card.scene = payload.scene
    db.commit()
    return CardSceneRead(card_id=card.id, scene=card.scene, template_id=card.template_id)


def _bind_for_card(
    db: Session, card: Card, template_scene: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """把这张卡的素材/字段绑进模板场景。"""
    postmarks = list(card.postmarks)
    stamps = list(card.stamps)
    ids = [p.image_asset_id for p in postmarks if p.image_asset_id is not None]
    ids += [s.image_asset_id for s in stamps if s.image_asset_id is not None]
    sha = _assets_sha(db, [i for i in ids if i is not None])

    pm0 = postmarks[0] if postmarks else None
    fields = scene_domain.scene_fields(
        visit_date=card.visit_date,
        title=card.title,
        inscription=pm0.inscription if pm0 else None,
        postmark_date=pm0.postmark_date if pm0 else None,
        place_name=pm0.place.name if pm0 is not None and pm0.place is not None else None,
        office_name=(
            pm0.post_office.name if pm0 is not None and pm0.post_office is not None else None
        ),
    )
    bound, warnings = scene_domain.bind_scene(
        template_scene,
        postmark_assets=_refs(postmarks, sha),
        stamp_assets=_refs(stamps, sha),
        fields=fields,
    )
    return bound, warnings


@router.post("/cards/{card_id}/apply-template", response_model=ApplyTemplateOut)
def apply_template(
    card_id: uuid.UUID, payload: ApplyTemplateIn, db: DbDep, _: CurrentUser
) -> ApplyTemplateOut:
    """套模板：默认只预览（`dry_run=true`），写库要显式。"""
    card = _get_card(db, card_id)
    template_scene, template_uuid = _resolve_template(db, payload.template_id)
    bound, warnings = _bind_for_card(db, card, template_scene)
    bound["template_id"] = payload.template_id
    if payload.dry_run:
        return ApplyTemplateOut(scene=bound, warnings=warnings, saved=False)

    errors = scene_domain.validate_scene(bound)
    if errors:
        raise HTTPException(UNPROCESSABLE, "场景不合法：" + "；".join(errors[:5]))
    card.scene = bound
    card.template_id = template_uuid
    db.commit()
    return ApplyTemplateOut(scene=bound, warnings=warnings, saved=True)


# --------------------------------------------------------------------------- #
#  框票贴片（人工框选，不是识别）
# --------------------------------------------------------------------------- #


def _parse_box(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x, y, w, h = (float(v) for v in box)
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        raise ValueError("box 里有 NaN/inf")
    if w <= 0 or h <= 0:
        raise ValueError("box 的宽高必须为正")
    if x < 0 or y < 0 or x + w > 1.0 + 1e-6 or y + h > 1.0 + 1e-6:
        raise ValueError("box 必须落在 0–1 的归一化坐标内（相对整卡图）")
    return x, y, w, h


@router.post("/cards/{card_id}/stamp-cut", response_model=StampCutOut)
def create_stamp_cut(
    card_id: uuid.UUID, payload: StampCutIn, db: DbDep, store: StoreDep, _: CurrentUser
) -> StampCutOut:
    card = _get_card(db, card_id)
    if card.image_asset_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "这张卡还没有整卡图，无法框票")
    source = db.get(Asset, card.image_asset_id)
    if source is None:
        raise HTTPException(status.HTTP_410_GONE, "整卡图的资产行不见了")

    try:
        x, y, w, h = _parse_box(payload.box)
    except (TypeError, ValueError) as exc:
        raise HTTPException(UNPROCESSABLE, str(exc)) from exc

    path = store.abs_path(source.storage_path)
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "整卡图文件缺失（磁盘/挂载问题）")
    try:
        with Image.open(path) as im:
            width, height = im.size
            left, top = round(x * width), round(y * height)
            right, bottom = round((x + w) * width), round((y + h) * height)
            if right <= left or bottom <= top:
                raise HTTPException(UNPROCESSABLE, "框选区小于 1 像素")
            crop = im.crop((left, top, right, bottom))
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            data = buf.getvalue()
            crop_w, crop_h = crop.size
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(UNPROCESSABLE, f"读整卡图失败：{exc}") from exc

    derived = register_derived_asset(
        db,
        store,
        data,
        kind="stamp_cut",
        mime="image/png",
        parent_asset_id=source.id,
    )

    stamp: Stamp | None = None
    stamp_created = False
    if payload.stamp_id is not None:
        stamp = db.get(Stamp, payload.stamp_id)
        if stamp is None or stamp.card_id != card.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "这张卡上没有这枚邮票")
    elif card.stamps:
        # 优先填**还没有图**的那一条；都已有图就复用第一张（换图语义）。
        stamp = next((s for s in card.stamps if s.image_asset_id is None), card.stamps[0])
    else:
        stamp = Stamp(card_id=card.id)
        db.add(stamp)
        db.flush()
        stamp_created = True

    stamp.image_asset_id = derived.id
    db.commit()
    return StampCutOut(
        stamp_id=stamp.id,
        asset_id=derived.id,
        sha256=derived.sha256,
        width=crop_w,
        height=crop_h,
        stamp_created=stamp_created,
    )
