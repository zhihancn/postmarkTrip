"""投递目录监听的测试（spec v1.0 §2.1）。

三个最容易写错、且错了以后**很贵**的地方，各有专门的测试：

1. **写入完成判定** —— 若不等"连续两次轮询大小不变"，正在被扫描软件写入的半个
   JPEG 会被当成完整资产入库。而内容寻址意味着**它会以那个 sha256 被永久固化**，
   正常的那次后来反而成了"重复内容"。所以这条必须有测试。
2. **不删用户的文件** —— 原文件只能被移动，不能被删除或修改。
3. **幂等** —— 同一内容重复投递不产生第二个 asset。

用假 session（与 test_card_summaries.py 同一路子），只需要 ``add`` / ``flush``
/ ``scalar(select(...))`` 三个能力。假 session 会**真的实现那两个唯一约束**
（asset.sha256 唯一、ingest_record.(source,sha256) 唯一）—— 否则"去重"这条
测试就成了自证：假 session 说去重了，真数据库可能并不去重。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from postmarktrip.db.models import Asset, IngestRecord
from postmarktrip.ingest.inbox import (
    FAILED_SUBDIR,
    PROCESSED_SUBDIR,
    InboxScanner,
)
from postmarktrip.storage import AssetStore

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _StubSession:
    """最小可用的 session 替身，**带唯一约束**。"""

    def __init__(self) -> None:
        self.assets: dict[str, Asset] = {}
        self.records: dict[tuple[str, str], IngestRecord] = {}
        self.commits = 0
        self._pending: list[Any] = []

    # --- 写入路径 ---
    def add(self, obj: Any) -> None:
        self._pending.append(obj)

    def flush(self) -> None:
        for obj in self._pending:
            if isinstance(obj, Asset):
                # 模拟 asset.sha256 的 unique 约束
                if obj.sha256 in self.assets:
                    raise AssertionError(f"asset.sha256 唯一约束被违反：{obj.sha256}")
                if getattr(obj, "id", None) is None:
                    obj.id = uuid.uuid4()
                self.assets[obj.sha256] = obj
            elif isinstance(obj, IngestRecord):
                # 模拟 uq_ingest_record_source_sha256
                key = (obj.source, obj.sha256)
                if key in self.records:
                    raise AssertionError(f"ingest_record 唯一约束被违反：{key}")
                if getattr(obj, "id", None) is None:
                    obj.id = uuid.uuid4()
                self.records[key] = obj
            else:  # pragma: no cover - 不该出现
                raise AssertionError(f"意外的对象类型：{type(obj)}")
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
        self._pending.clear()

    def commit(self) -> None:
        self.commits += 1

    # --- 查询路径 ---
    def scalar(self, stmt: Any) -> Any:
        sql = str(stmt)
        params = stmt.compile().params
        if "FROM asset" in sql:
            for value in params.values():
                if isinstance(value, str) and value in self.assets:
                    return self.assets[value]
            return None
        if "FROM ingest_record" in sql:
            values = [v for v in params.values() if isinstance(v, str)]
            if len(values) >= 2:
                source, sha = values[0], values[1]
                return self.records.get((source, sha))
            return None
        raise AssertionError(f"未预期的查询：{sql}")  # pragma: no cover


@pytest.fixture
def store(workdir) -> AssetStore:
    return AssetStore(workdir / "assets", allowed_exts=(".jpg", ".jpeg", ".png", ".pdf"))


@pytest.fixture
def inbox(workdir):
    d = workdir / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _scanner(inbox) -> InboxScanner:
    return InboxScanner(inbox, (".jpg", ".jpeg", ".png", ".pdf"))


def _put(inbox, name: str, data: bytes = PNG):
    p = inbox / name
    p.write_bytes(data)
    return p


# --------------------------------------------------------------------------- #
#  1. 写入完成判定 —— 最贵的一条
# --------------------------------------------------------------------------- #


def test_first_scan_does_not_ingest(inbox, store) -> None:
    """第一次见到就入库 = 把正在写入的半个文件当完整资产。

    而且因为内容寻址，那半个文件会被**永久固化**在 assets 里，
    真正完整的那次反倒成了"重复内容"被跳过。
    """
    _put(inbox, "a.png")
    db = _StubSession()

    report = _scanner(inbox).scan_once(db, store)

    assert report.unstable == 1
    assert report.ingested == 0
    assert not db.assets, "第一次扫描不该产生任何资产"


def test_second_scan_ingests(inbox, store) -> None:
    _put(inbox, "a.png")
    scanner = _scanner(inbox)
    db = _StubSession()

    scanner.scan_once(db, store)
    report = scanner.scan_once(db, store)

    assert report.ingested == 1
    assert len(db.assets) == 1


def test_growing_file_is_not_ingested(inbox, store) -> None:
    """文件在两轮之间变大 → 还在写 → 继续等。"""
    p = _put(inbox, "a.png")
    scanner = _scanner(inbox)
    db = _StubSession()

    scanner.scan_once(db, store)  # 第一轮记指纹
    p.write_bytes(PNG + b"\x00" * 4096)  # 仍在增长
    report = scanner.scan_once(db, store)  # 第二轮：变了，不入库

    assert report.unstable == 1
    assert report.ingested == 0
    assert not db.assets

    # 稳定后第三轮才入库
    report = scanner.scan_once(db, store)
    assert report.ingested == 1


def test_content_change_is_reingested(inbox, store) -> None:
    """同一路径内容变了（用户重扫覆盖）→ 是新内容，要入库。

    指纹里必须含 size/mtime，否则"路径见过"会被误当成"内容处理过"。
    """
    p = _put(inbox, "a.png")
    scanner = _scanner(inbox)
    db = _StubSession()
    scanner.scan_once(db, store)
    scanner.scan_once(db, store)
    assert len(db.assets) == 1

    p.write_bytes(PNG + b"different")
    scanner.scan_once(db, store)  # 指纹变了
    report = scanner.scan_once(db, store)

    assert report.ingested == 1
    assert len(db.assets) == 2, "内容不同 → 应该是两个不同的 sha256"


# --------------------------------------------------------------------------- #
#  2. 幂等
# --------------------------------------------------------------------------- #


def test_same_content_twice_is_not_a_second_asset(inbox, store) -> None:
    """内容相同、文件名不同 → **各表都只有一行**。

    这里两个唯一约束同时在起作用，而且它们管的是不同的东西：

    * ``asset.sha256`` 唯一 —— 内容寻址，同样的字节只存一份
    * ``ingest_record.(source, sha256)`` 唯一 —— **幂等**，同一来源的同一内容
      只记一次（spec v1.0 §2.1 的"同一文件重复投递不重复处理"）

    第二条**不含文件名**，所以第二个文件名不会产生第二条记录。
    这不是信息丢失：原文件被保留在 ``processed/``（名字都在），
    而首条记录的 ``original_filename`` 记下了第一个名字。
    """
    _put(inbox, "a.png")
    scanner = _scanner(inbox)
    db = _StubSession()
    scanner.scan_once(db, store)
    scanner.scan_once(db, store)

    _put(inbox, "copy.png", PNG)  # 同内容，不同文件名
    scanner.scan_once(db, store)
    report = scanner.scan_once(db, store)

    assert report.duplicate == 1
    assert report.ingested == 0
    assert len(db.assets) == 1, "同内容不该产生第二个 asset（内容寻址）"
    assert len(db.records) == 1, (
        "同来源同内容只记一条 ingest_record —— 唯一约束是 (source, sha256)，不含文件名"
    )

    # 两个文件都已归档，名字没有丢
    processed = sorted(p.name for p in (inbox / PROCESSED_SUBDIR).iterdir())
    assert processed == ["a.png", "copy.png"]


# --------------------------------------------------------------------------- #
#  3. 不删用户的文件
# --------------------------------------------------------------------------- #


def test_processed_file_is_moved_not_deleted(inbox, store) -> None:
    p = _put(inbox, "a.png")
    scanner = _scanner(inbox)
    db = _StubSession()
    scanner.scan_once(db, store)
    scanner.scan_once(db, store)

    assert not p.exists(), "处理完应该离开 inbox 根目录"
    moved = inbox / PROCESSED_SUBDIR / "a.png"
    assert moved.is_file(), "原文件必须被保留（只是移到了 processed/）"
    assert moved.read_bytes() == PNG, "内容不能被改动 —— 它是 L0 证据的来源"


def test_processed_file_is_not_rescanned(inbox, store) -> None:
    """processed/ 是我们的工作目录，下一轮不该再看它。"""
    _put(inbox, "a.png")
    scanner = _scanner(inbox)
    db = _StubSession()
    scanner.scan_once(db, store)
    scanner.scan_once(db, store)

    report = scanner.scan_once(db, store)
    assert report.seen == 0


# --------------------------------------------------------------------------- #
#  4. 失败处理
# --------------------------------------------------------------------------- #


def test_unsupported_extension_is_ignored(inbox, store) -> None:
    """白名单之外的扩展名根本不进候选：不是"失败"，是不该看它。"""
    _put(inbox, "notes.txt", b"hello")
    scanner = _scanner(inbox)
    db = _StubSession()

    report = scanner.scan_once(db, store)
    assert report.seen == 0
    assert (inbox / "notes.txt").is_file(), "不该动它"


def test_failed_ingest_is_moved_and_not_retried(inbox, store) -> None:
    """存储层拒绝（此处用超出体积上限模拟）的文件要移到 failed/，
    否则每一轮轮询都会重试它、把日志刷满。"""
    _put(inbox, "huge.png", PNG)
    scanner = _scanner(inbox)
    db = _StubSession()

    # 用一个 max_bytes 极小的 store 逼出 StorageError
    small = AssetStore(store.root, allowed_exts=(".png",))
    scanner.scan_once(db, small)  # 第一轮记指纹

    # 直接把体积上限压到 1 字节：走 ingest_path 的 max_bytes 参数做不到，
    # 所以这里改用"扩展名白名单不一致"的 store —— 它会在存储层抛 UnsupportedFileType
    picky = AssetStore(store.root, allowed_exts=(".jpg",))
    report = scanner.scan_once(db, picky)

    assert report.failed == 1
    assert report.ingested == 0
    assert (inbox / FAILED_SUBDIR / "huge.png").is_file()
    assert not (inbox / "huge.png").exists()

    # 下一轮不该再看见它
    report = scanner.scan_once(db, picky)
    assert report.seen == 0


def test_failure_records_an_error_message(inbox, store) -> None:
    _put(inbox, "a.png")
    scanner = _scanner(inbox)
    db = _StubSession()
    scanner.scan_once(db, store)
    picky = AssetStore(store.root, allowed_exts=(".jpg",))

    report = scanner.scan_once(db, picky)
    assert report.errors
    name, reason = report.errors[0]
    assert name == "a.png"
    assert reason, "失败必须带原因，否则排障只能靠猜"


# --------------------------------------------------------------------------- #
#  5. 边界
# --------------------------------------------------------------------------- #


def test_missing_inbox_dir_is_not_an_error(workdir, store) -> None:
    """目录不存在（还没来得及建）不该崩 —— 它会每 5 秒被调用一次。"""
    scanner = InboxScanner(workdir / "nope", (".png",))
    report = scanner.scan_once(_StubSession(), store)
    assert report.seen == 0


def test_empty_inbox(workdir, store) -> None:
    d = workdir / "empty"
    d.mkdir()
    report = InboxScanner(d, (".png",)).scan_once(_StubSession(), store)
    assert report.seen == 0
    assert not report.touched


def test_non_recursive_dirs_are_skipped(inbox, store) -> None:
    """子目录里的候选会被扫（rglob），但我们自己的 processed/failed 不会。"""
    _put(inbox, "a.png")
    scanner = _scanner(inbox)
    db = _StubSession()
    scanner.scan_once(db, store)
    scanner.scan_once(db, store)

    (inbox / "sub").mkdir()
    _put(inbox / "sub", "b.png", PNG + b"other")

    report = scanner.scan_once(db, store)
    # sub/b.png 是第一次见到 → unstable，但确实被"看见"了
    assert report.seen == 1


def test_fingerprint_cache_is_pruned(inbox, store) -> None:
    """文件消失后指纹要被清掉，否则内存里会留下永不回收的路径。"""
    p = _put(inbox, "a.png")
    scanner = _scanner(inbox)
    scanner.scan_once(_StubSession(), store)
    assert len(scanner._seen) == 1

    p.unlink()
    scanner.scan_once(_StubSession(), store)
    assert len(scanner._seen) == 0


def test_failed_suffix_is_never_picked_up(inbox, store) -> None:
    """``.failed`` 后缀是明确的"别再碰我"标记。"""
    (inbox / "a.png.failed").write_bytes(PNG)
    report = _scanner(inbox).scan_once(_StubSession(), store)
    assert report.seen == 0
