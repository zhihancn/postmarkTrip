"""投递目录监听（spec v1.0 §2.1，路径 A）。

扫描仪 → 扫描软件 → **投递目录** → 本模块 → L0 资产 → 识别队列。

## 为什么是轮询而不是 inotify

这是本项目里唯一一个**为了可靠性牺牲优雅**的地方，值得写清楚。

在一个「扫描仪在 Windows、服务在容器里」的拓扑下，投递目录几乎总是跨文件系统
的（``D:\\...\\inbox`` → 容器内 ``/inbox``，或 SMB 挂载）。跨 drvfs / SMB / NFS 的
inotify 事件在各种 NAS 实现下表现不一，甚至完全不穿过来 —— 容器会"看不见"新文件，
而且是**静默地**看不见。

相比之下"每 5 秒列一次目录"的代价可以忽略（一天 17280 次 stat），
换来的是在任何挂载方式下都成立的行为。

## 三条容易写错、这里明确处理的规则

1. **写入完成判定**：文件大小在**连续两次轮询间不变**才认为写完了。
   否则会把一个正在被扫描软件写入的半张 JPEG 当成完整资产入库 ——
   而内容寻址意味着那半个文件会被永久固化。
2. **不删用户的文件**：处理成功后**移动**到 ``processed/`` 子目录。
   既保持了 inbox 的整洁（否则下次轮询还要重复 stat 全部历史文件），
   又保留了原文件。docs/ok/05 §六 承诺的是"inbox 里的原文件保留"。
3. **失败不无限重试**：写 ``ingest_record.status='failed'`` 并把文件移到
   ``failed/``。否则一个坏文件会让每一轮轮询都重试它，日志被刷满。

## 契约

原文件**只读、只移动**，绝不修改内容 —— 它是 L0 证据的来源。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from sqlalchemy.orm import Session

from postmarktrip.domain.ingest import ingest_path
from postmarktrip.storage import AssetStore, StorageError, UnsupportedFileType

logger = logging.getLogger("postmarktrip.ingest.inbox")

#: 处理成功后的去向（inbox 的子目录）
PROCESSED_SUBDIR: Final = "processed"
#: 处理失败后的去向
FAILED_SUBDIR: Final = "failed"
#: 我们的工作目录，扫描时跳过
SKIP_DIRS: Final = frozenset({PROCESSED_SUBDIR, FAILED_SUBDIR})
#: 这个后缀是被我们标记过的失败品，不再重试
FAILED_SUFFIX: Final = ".failed"


@dataclass(slots=True)
class _Fingerprint:
    """两次轮询之间用来判断"文件写完了"的指纹。"""

    size: int
    mtime_ns: int


@dataclass(slots=True)
class ScanReport:
    """一轮扫描的结果。"""

    seen: int = 0
    unstable: int = 0
    ingested: int = 0
    duplicate: int = 0
    failed: int = 0
    #: 失败文件 → 原因。调用方（worker）据此记日志。
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def touched(self) -> bool:
        return bool(self.ingested or self.failed)


class InboxScanner:
    """有状态的投递目录扫描器。

    **必须有状态**：「连续两次轮询大小不变」这件事无法在一轮里判断。
    状态只存在内存里，进程重启后第一轮会把现存文件都当成"第一次见到"而跳过 ——
    这是**有意的保守行为**：宁可晚 5 秒处理，也不要把半个文件当完整资产入库。
    """

    def __init__(self, inbox_dir: Path, allowed_exts: tuple[str, ...]) -> None:
        self.inbox_dir = Path(inbox_dir)
        self.allowed_exts = frozenset(e.lower() for e in allowed_exts)
        self._seen: dict[Path, _Fingerprint] = {}

    # ------------------------------------------------------------------ 工具
    def _candidates(self) -> list[Path]:
        """列出候选文件（排序保证处理顺序稳定，便于复现问题）。"""
        if not self.inbox_dir.is_dir():
            return []
        out: list[Path] = []
        for path in sorted(self.inbox_dir.rglob("*")):
            if not path.is_file():
                continue
            # 跳过我们自己建的工作目录
            if any(part in SKIP_DIRS for part in path.relative_to(self.inbox_dir).parts[:-1]):
                continue
            if path.name.endswith(FAILED_SUFFIX):
                continue
            if path.suffix.lower() not in self.allowed_exts:
                continue
            out.append(path)
        return out

    def _move(self, path: Path, subdir: str) -> Path | None:
        """把文件移进 inbox 的子目录。返回新路径，失败返回 None。

        **移动而不是删除** —— 原文件是用户的，程序不处置它。
        跨设备时 shutil.move 会降级为 copy+unlink，这里接受这一点。
        """
        try:
            dest_dir = self.inbox_dir / subdir
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / path.name
            # 同名冲突时加时间戳后缀，不覆盖既有文件
            if dest.exists():
                stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
                dest = dest_dir / f"{path.stem}_{stamp}{path.suffix}"
            shutil.move(str(path), str(dest))
            return dest
        except OSError as exc:  # pragma: no cover - 取决于文件系统权限
            logger.warning("移动文件失败 %s：%s", path, exc)
            return None

    # ------------------------------------------------------------------ 主流程
    def scan_once(self, db: Session, store: AssetStore) -> ScanReport:
        """扫描一轮。调用方负责 ``db.commit()``。"""
        report = ScanReport()
        candidates = self._candidates()

        # 清掉已不在目录里的路径指纹，避免 map 无限增长
        present = set(candidates)
        for stale in [p for p in self._seen if p not in present]:
            del self._seen[stale]

        for path in candidates:
            report.seen += 1
            try:
                st = path.stat()
            except FileNotFoundError:
                continue

            current = _Fingerprint(size=st.st_size, mtime_ns=st.st_mtime_ns)
            previous = self._seen.get(path)

            if previous != current:
                # 第一次见到，或者大小/时间戳变了 → 认为还在写，本轮不处理
                self._seen[path] = current
                report.unstable += 1
                continue

            # 两次轮询之间指纹一致 → 认为写入完成
            try:
                result = ingest_path(db, store, path, source="inbox")
            except UnsupportedFileType as exc:
                self._mark_failed(db, path, str(exc), report)
            except StorageError as exc:
                self._mark_failed(db, path, f"存储层拒绝：{exc}", report)
            except OSError as exc:
                self._mark_failed(db, path, f"读取失败：{exc}", report)
            except ValueError as exc:
                self._mark_failed(db, path, f"非法输入：{exc}", report)
            else:
                self._seen.pop(path, None)
                if result.asset_created:
                    report.ingested += 1
                    logger.info("已采集 %s → asset %s", path.name, result.asset.sha256[:12])
                else:
                    report.duplicate += 1
                    logger.info("内容已存在，跳过识别：%s", path.name)
                self._move(path, PROCESSED_SUBDIR)

        return report

    def _mark_failed(self, db: Session, path: Path, reason: str, report: ScanReport) -> None:
        """记录失败并把文件移到 failed/，避免每轮重试。"""
        report.failed += 1
        report.errors.append((path.name, reason))
        logger.warning("采集失败 %s：%s", path.name, reason)
        self._seen.pop(path, None)
        self._move(path, FAILED_SUBDIR)

    # ------------------------------------------------------------------ 兼容
    def reset(self) -> None:
        """丢掉全部指纹。测试用。"""
        self._seen.clear()
