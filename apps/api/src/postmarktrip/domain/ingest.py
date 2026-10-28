"""采集落库：把一个已经存进内容寻址存储的文件登记为 asset + ingest_record。

**为什么抽出来**：上传（``POST /api/assets``）与投递目录监听会把同一件事做两遍，
而这件事有两处容易被写错：

1. ``asset`` 按 sha256 **去重**（同内容同路径，不该产生第二行）
2. ``ingest_record`` 按 ``(source, sha256)`` **幂等**（同一文件反复投递只记一次）

写两遍就迟早只有一处是对的 —— 这是本项目已经栽过的坑（见 docs/ok/06 缺陷 #9/#10：
两处独立维护的等价逻辑会静默漂移）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from postmarktrip.db.models import Asset, IngestRecord
from postmarktrip.storage import AssetStore, StoredAsset

#: ingest_record.source 的合法取值。upload = 网页上传，inbox = 投递目录。
VALID_SOURCES: Final = frozenset({"upload", "inbox"})

#: 允许入库的 kind。``postmark_cut`` / ``stamp_cut`` / ``derived`` 是管线派生物，
#: 采集层不产生它们。
VALID_KINDS: Final = frozenset({"scan_raw", "photo"})


@dataclass(frozen=True, slots=True)
class IngestResult:
    """一次采集的落库结果。"""

    asset: Asset
    record: IngestRecord
    #: True = 这个 asset 是**这次**新建的；False = 内容已存在，复用了原行。
    #: 调用方据此决定要不要为它排识别任务（重复投递不该重复识别）。
    asset_created: bool
    #: True = 这次新建了 ingest_record；False = 该来源已记录过同一内容。
    record_created: bool


def _register_asset(
    db: Session,
    stored: StoredAsset,
    *,
    kind: str,
    original_filename: str | None,
    parent_asset_id: uuid.UUID | None = None,
) -> tuple[Asset, bool]:
    """按 sha256 取回或新建 asset 行。

    `parent_asset_id` 只对**派生物**有意义（卡片裁切、戳抠图）：它指回原始件，
    这样"这张图从哪张原图出来"断不了（`docs/ok/18` §3.5，迁移 `0003_asset_parent`）。
    """
    existing = db.scalar(select(Asset).where(Asset.sha256 == stored.sha256))
    if existing is not None:
        return existing, False

    asset = Asset(
        sha256=stored.sha256,
        mime=stored.mime,
        bytes=stored.bytes,
        width=stored.width,
        height=stored.height,
        dpi=stored.dpi,
        storage_path=stored.storage_path,
        kind=kind,
        is_immutable=stored.is_immutable,
        original_filename=original_filename,
        parent_asset_id=parent_asset_id,
    )
    db.add(asset)
    db.flush()
    return asset, True


def register_derived_asset(
    db: Session,
    store: AssetStore,
    data: bytes,
    *,
    kind: str,
    mime: str,
    parent_asset_id: uuid.UUID,
) -> Asset:
    """把一个**派生**出来的图登记成资产（内容寻址，重复内容复用同一行）。

    与采集层共用 `_register_asset` 的去重逻辑 —— 这里曾经是"两份等价的落库
    逻辑"，本项目为此栽过（`ok/06` 缺陷 #9/#10），所以不再写第二份。

    `kind` 用 `postmark_cut` / `stamp_cut` / `card_crop` 这类值
    （`db/models.py` 的 `Asset.kind` 注释里已列出前两个）。

    ⚠️ `put_stream` 只收 `original_filename` / `mime` / `kind` / `max_bytes`
    —— 尺寸与 dpi 是**它自己探测**的（`_probe_image`），不要试图从外面塞
    （2026-10-12 实测：多传一个 `dpi=` 就是 500）。
    """
    stored = store.put_bytes(data, mime=mime, kind=kind)
    asset, _created = _register_asset(
        db, stored, kind=kind, original_filename=None, parent_asset_id=parent_asset_id
    )
    return asset


def _register_record(
    db: Session,
    *,
    source: str,
    sha256: str,
    asset_id: uuid.UUID,
    original_filename: str | None,
    error: str | None = None,
) -> tuple[IngestRecord, bool]:
    """按 (source, sha256) 取回或新建 ingest_record 行。"""
    existing = db.scalar(
        select(IngestRecord).where(IngestRecord.source == source, IngestRecord.sha256 == sha256)
    )
    if existing is not None:
        return existing, False

    record = IngestRecord(
        source=source,
        original_filename=original_filename,
        sha256=sha256,
        asset_id=asset_id,
        status="failed" if error else "stored",
        error=error,
        processed_at=datetime.now(UTC),
    )
    db.add(record)
    db.flush()
    return record, True


def ingest_stream(
    db: Session,
    store: AssetStore,
    stream: BinaryIO,
    *,
    source: str,
    original_filename: str | None = None,
    mime: str | None = None,
    kind: str = "scan_raw",
    max_bytes: int | None = None,
) -> IngestResult:
    """把一条流存进内容寻址存储并登记进数据库。

    调用方负责 ``db.commit()`` —— 这样上传端点可以把"落库"与它自己的
    其他写入放进同一个事务。
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"未知的采集来源 {source!r}；允许：{sorted(VALID_SOURCES)}")
    if kind not in VALID_KINDS:
        raise ValueError(f"采集层不接受 kind={kind!r}；允许：{sorted(VALID_KINDS)}")

    stored = store.put_stream(
        stream,
        original_filename=original_filename,
        mime=mime,
        kind=kind,
        max_bytes=max_bytes,
    )
    asset, asset_created = _register_asset(
        db, stored, kind=kind, original_filename=original_filename
    )
    record, record_created = _register_record(
        db,
        source=source,
        sha256=stored.sha256,
        asset_id=asset.id,
        original_filename=original_filename,
    )
    return IngestResult(
        asset=asset,
        record=record,
        asset_created=asset_created,
        record_created=record_created,
    )


def ingest_path(
    db: Session,
    store: AssetStore,
    path: Path,
    *,
    source: str = "inbox",
    kind: str = "scan_raw",
    max_bytes: int | None = None,
) -> IngestResult:
    """从磁盘上的一个文件入库。

    投递目录监听走这条路。**文件不会被删除或移动** —— 用户的文件不由程序处置
    （spec v1.0 §2.1）。唯一的例外是识别失败时由调用方加 ``.failed`` 后缀，
    目的是避免每一轮轮询都重试同一个坏文件。
    """
    with path.open("rb") as fh:
        return ingest_stream(
            db,
            store,
            fh,
            source=source,
            original_filename=path.name,
            mime=None,
            kind=kind,
            max_bytes=max_bytes,
        )
