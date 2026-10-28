"""资产路由：上传、原图、缩略图。

**没有删除端点** —— 原始件是 L0 证据，只进不出（见 storage 模块的说明）。
"""

from __future__ import annotations

import math
import re
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from postmarktrip.api.deps import CurrentUser, DbDep, StoreDep
from postmarktrip.api.http import UNPROCESSABLE
from postmarktrip.config import settings

# Asset 在类型注解之外还被 _get_asset 用作 ``db.get(Asset, ...)`` 的**运行时**实参，
# 所以它不是"仅用于注解的导入"，不能因为 linter 说未使用就删掉
# （删过一次，5 个缩略图测试立刻以 NameError 失败）。
from postmarktrip.db.models import Asset
from postmarktrip.domain.ingest import ingest_stream
from postmarktrip.domain.schemas import AssetRead
from postmarktrip.storage import StorageError, UnsupportedFileType

router = APIRouter(tags=["assets"])


@router.post("/assets", response_model=AssetRead, status_code=status.HTTP_201_CREATED)
async def upload_asset(
    db: DbDep,
    store: StoreDep,
    _: CurrentUser,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Query(pattern="^(scan_raw|photo)$")] = "scan_raw",
) -> Asset:
    """上传一个文件。

    内容寻址 → 同一个文件重复上传不会占额外空间，也不会产生新的 asset 行。

    落库逻辑与投递目录监听**共用** ``domain.ingest`` —— 之前这里是第二份
    独立实现（asset 去重 + ingest_record 幂等各写一遍）。两份等价的逻辑
    迟早只有一处是对的，这是本项目已经栽过的坑（docs/ok/06 缺陷 #9/#10）。
    """
    try:
        result = ingest_stream(
            db,
            store,
            file.file,
            source="upload",
            original_filename=file.filename,
            mime=file.content_type,
            kind=kind,
            max_bytes=settings.max_upload_bytes,
        )
    except UnsupportedFileType as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    except StorageError as exc:
        # 体积超限与存储层其他错误都落在这里。413 是"太大"，
        # 但 StorageError 也可能是别的（如写入失败），所以分开判断。
        if "超过上限" in str(exc):
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
        raise
    except ValueError as exc:
        raise HTTPException(UNPROCESSABLE, str(exc)) from exc

    db.commit()
    db.refresh(result.asset)
    return result.asset


def _get_asset(db: DbDep, asset_id: uuid.UUID) -> Asset:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在")
    return asset


@router.get("/assets/{asset_id}/raw")
def get_raw(asset_id: uuid.UUID, db: DbDep, store: StoreDep, _: CurrentUser) -> FileResponse:
    asset = _get_asset(db, asset_id)
    return _raw_response(asset, store)


#: `sha256:<64 hex>` 里的那 64 位。**请求先校验格式**再查库 ——
#: 否则任意字符串（比如一个 UUID）都会打到索引上，且 404 与 422 混在一起。
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@router.get("/assets/by-sha256/{sha256}/raw")
def get_raw_by_sha256(sha256: str, db: DbDep, store: StoreDep, _: CurrentUser) -> FileResponse:
    """按**内容**取原图。

    为什么需要它（`docs/spec/v1.1` §3.2）：场景图里的资产引用是
    `sha256:<64 hex>`（内容寻址，契约 `contract/scene.schema.json`），
    而库里其它地方用 UUID。没有这个端点，渲染器就得先查 UUID 再取图 ——
    两跳，而且 scene 无法脱离数据库独立存在（导出/分享场景时就断了）。
    """
    if not _SHA256_RE.match(sha256):
        raise HTTPException(UNPROCESSABLE, "sha256 必须是 64 位小写十六进制")
    asset = db.scalar(select(Asset).where(Asset.sha256 == sha256))
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在")
    return _raw_response(asset, store)


def _raw_response(asset: Asset, store: StoreDep) -> FileResponse:
    path = store.abs_path(asset.storage_path)
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "资产文件缺失（可能是磁盘或挂载问题）")
    # 原图可能很大，交给浏览器缓存；内容寻址意味着 URL 内容永不变化
    return FileResponse(
        path,
        media_type=asset.mime,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


def _write_atomic(dest: Path, data: bytes) -> None:
    """先写同目录下的临时文件再 rename。

    直接把字节写进缓存文件的话，并发的第二个请求可能读到**写了一半的 JPEG**
    （浏览器会显示成破图，而且因为响应头是 immutable，它会一直坏下去）。
    同一目录内的 rename 在 POSIX 与 Windows 上都是原子的。
    """
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def _parse_quad(raw: str, work_scale: float) -> list[tuple[float, float]]:
    """把 `"x1,y1,...,x4,y4"`（**工作尺度**）解析成原图坐标的 4 个点。

    纯函数、无 cv2 依赖 —— 所以这条路径的输入校验**可以被测试钉住**
    （几何算错在图上只会表现为"歪/缺/黑"，很难一眼看出）。

    拒绝的情况（都给人话）：
    * 不是 8 个数 / 有 NaN、inf
    * `work_scale` 不是正数（没法换算回原图）
    """
    if not (work_scale > 0) or not math.isfinite(work_scale):
        raise ValueError(f"work_scale 必须是正数：{work_scale!r}")
    parts = [p for p in raw.replace(" ", "").split(",") if p != ""]
    if len(parts) != 8:
        raise ValueError(f"quad 需要 8 个数字（4 个点 x,y），收到 {len(parts)} 个")
    try:
        values = [float(p) for p in parts]
    except ValueError as exc:
        raise ValueError(f"quad 里有不是数字的项：{raw[:80]!r}") from exc
    if not all(math.isfinite(v) for v in values):
        raise ValueError("quad 里出现 NaN/inf")
    scale = 1.0 / work_scale
    return [(values[i] * scale, values[i + 1] * scale) for i in range(0, 8, 2)]


@router.get("/assets/{asset_id}/card-crop")
def get_card_crop(
    asset_id: uuid.UUID,
    db: DbDep,
    store: StoreDep,
    _: CurrentUser,
    #: 这张卡的四边形（**工作尺度**，`x1,y1,...,x4,y4`）—— 来自识别响应
    quad: Annotated[str, Query(min_length=15, max_length=200)],
    work_scale: Annotated[float, Query(gt=0, le=8)] = 1.0,
    max_px: Annotated[int, Query(ge=0, le=2400)] = 1600,
) -> Response:
    """**按需**裁出"这一张卡"（透视校正，无黑边）—— `docs/ok/19` §三 3.2 的路径 C。

    为什么要有它（而不是在前端用 canvas 做）：识别返回的是**四边形**，而弹层的
    「整卡」以前用直角外接框裁原图 → 歪卡必然带出深色垫底（用户 2026-10-12 报的）。
    canvas 透视校正也能做，但**渲染结果没法用测试看**；而服务端这条路径
    与 `⑤` 确认落库用的是**同一个** `warp_card`，出图可以直接查（尺寸/字节/无黑边）。

    ⚠️ 它**不写库**：纯读取 + 现算（识别端点保持只读，spec A6 不受影响）。
    几何由调用方给（识别响应里就有）—— 这样不必重跑切分，也就没有缓存失效问题。
    """
    try:
        points = _parse_quad(quad, work_scale)
    except ValueError as exc:
        raise HTTPException(UNPROCESSABLE, str(exc)) from exc

    asset = _get_asset(db, asset_id)
    path = store.abs_path(asset.storage_path)
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "资产文件缺失（可能是磁盘或挂载问题）")

    try:
        data = _render_card_crop(path, points, max_px=max_px or 1600)
    except _CropUnavailableError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    return Response(
        content=data,
        media_type="image/jpeg",
        #: 几何在 URL 里 → 同一 URL 的内容是确定的（可缓存）。但它是**私有**资产。
        headers={"Cache-Control": "private, max-age=3600"},
    )


class _CropUnavailableError(RuntimeError):
    """裁不了（识别栈没装 / 图读不出来 / 四边形退化）。"""


def _render_card_crop(
    path: Path, points: list[tuple[float, float]], *, max_px: int
) -> bytes:
    """把四个点围出的区域**透视校正**成一张正向 jpeg（`warp_card` 的输出框就是卡自身）。

    cv2/numpy 是**可选依赖**（`INSTALL_RECOGNITION`）：没装就明确报"不可用"，
    而不是 500（与 `/api/recognize` 的 503 同一哲学：不假装能跑）。
    """
    try:
        import cv2
        import numpy as np

        from postmarktrip.recognition.split import warp_card
    except Exception as exc:  # pragma: no cover - 识别栈没装时的正常分支
        raise _CropUnavailableError(
            "服务端没有识别栈（cv2/numpy），无法裁图 —— 装 `[recognition]` extra 后可用"
        ) from exc

    image = cv2.imread(str(path))
    if image is None:
        raise _CropUnavailableError("读不出这张原图（不支持或损坏的图片）")
    quad = np.asarray(points, dtype=np.float32)
    height, width = image.shape[:2]
    #: 四边形必须落在图内（越界的点裁出来会是一块黑；那是静默的错图）
    if (
        quad[:, 0].min() < -1
        or quad[:, 1].min() < -1
        or quad[:, 0].max() > width + 1
        or quad[:, 1].max() > height + 1
    ):
        raise _CropUnavailableError(f"四边形超出图像范围（图 {width}x{height}）")
    try:
        cropped = warp_card(image, quad, max_px=max_px)
    except Exception as exc:  # pragma: no cover - 退化四边形等
        raise _CropUnavailableError(f"裁图失败：{exc}") from exc
    ok, buf = cv2.imencode(".jpg", cropped, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:  # pragma: no cover - 编码失败极罕见
        raise _CropUnavailableError("jpeg 编码失败")
    return bytes(buf)


@router.get("/assets/{asset_id}/thumb")
def get_thumb(
    asset_id: uuid.UUID,
    db: DbDep,
    store: StoreDep,
    _: CurrentUser,
    #: ⚠️ **必须是 `ge=0`，不能是 `ge=80`**（2026-10-12 修，见 `docs/ok/18` §3.1）。
    #:
    #: 原来的 `Query(ge=80, le=1600)` + 默认值 `0` 是**自相矛盾**的：**默认值本身
    #: 违反约束**，所以"不带参数调用"必然 422。而前端 `Thumb` 组件恰恰就是这么调的
    #: （只有识别页的缩略图传了 `max_px=1600`）→ 卡片页/画廊**每一张**图都走
    #: `onError` → 显示「暂无图片」。这个 bug 的表现像"图片没传上去"，
    #: 实际是接口签名自己把自己判死了。
    #:
    #: 语义是"0 = 用服务端默认值"（下一行 `max_px or ...` 就是照这个写的）。
    max_px: Annotated[int, Query(ge=0, le=1600)] = 0,
    #: 保留 alpha（戳贴片用）。默认 False 仍是 JPEG —— 卡片图不需要透明，
    #: 而 JPEG 更小。开启后走 PNG，缓存键也带上 `_alpha`。
    alpha: Annotated[bool, Query()] = False,
) -> FileResponse:
    """缩略图。按 (sha256, max_px, alpha) 缓存在磁盘上，不进数据库 —— 可随时重建的派生物。"""
    asset = _get_asset(db, asset_id)
    px = max_px or settings.thumbnail_max_px

    cache_dir = settings.data_dir / "thumb-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_alpha" if alpha else ""
    extension = "png" if alpha else "jpg"
    cached = cache_dir / f"{asset.sha256}_{px}{suffix}.{extension}"

    if not cached.is_file():
        try:
            data = store.render_thumbnail(asset.storage_path, max_px=px, keep_alpha=alpha)
        except UnsupportedFileType as exc:
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
        _write_atomic(cached, data)

    return FileResponse(
        cached,
        media_type="image/png" if alpha else "image/jpeg",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
