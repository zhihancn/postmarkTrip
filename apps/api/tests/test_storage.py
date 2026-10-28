"""内容寻址存储的测试。

这里是 L0（原始证据）的守门人，所以测试重点是：
去重、不可变、越界防御、以及「同一个文件重复上传不产生第二份」。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from postmarktrip.storage import AssetStore, StorageError, UnsupportedFileType


def _png_bytes(
    size: tuple[int, int] = (40, 30), color: tuple[int, int, int] = (10, 120, 40)
) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG", dpi=(600, 600))
    return buf.getvalue()


@pytest.fixture
def store(workdir: Path) -> AssetStore:
    return AssetStore(workdir / "assets", allowed_exts=(".png", ".jpg", ".jpeg"))


def test_put_is_content_addressed(store: AssetStore) -> None:
    data = _png_bytes()
    a = store.put_bytes(data, original_filename="card.png", mime="image/png", kind="scan_raw")

    assert len(a.sha256) == 64
    # 两级散列目录：ab/cd/<sha>.ext
    assert a.storage_path == f"{a.sha256[:2]}/{a.sha256[2:4]}/{a.sha256}.png"
    assert store.abs_path(a.storage_path).is_file()
    assert a.bytes == len(data)
    assert a.is_immutable is True


def test_metadata_is_probed(store: AssetStore) -> None:
    a = store.put_bytes(_png_bytes(size=(123, 45)), original_filename="x.png", mime="image/png")
    assert (a.width, a.height) == (123, 45)
    assert a.dpi == 600, "DPI 必须被读出来 —— 管线要按它归一化所有像素阈值"


def test_dedup_same_content_same_path(store: AssetStore) -> None:
    data = _png_bytes()
    a = store.put_bytes(data, original_filename="one.png", mime="image/png")
    b = store.put_bytes(data, original_filename="two.png", mime="image/png")
    assert a.sha256 == b.sha256
    assert a.storage_path == b.storage_path

    files = [p for p in store.root.rglob("*") if p.is_file()]
    assert len(files) == 1, "同内容只能有一个文件"


def test_different_content_different_path(store: AssetStore) -> None:
    a = store.put_bytes(_png_bytes(color=(1, 2, 3)), original_filename="a.png", mime="image/png")
    b = store.put_bytes(_png_bytes(color=(9, 9, 9)), original_filename="b.png", mime="image/png")
    assert a.sha256 != b.sha256


def test_extension_whitelist(store: AssetStore) -> None:
    with pytest.raises(UnsupportedFileType):
        store.put_bytes(b"x", original_filename="evil.exe", mime="application/octet-stream")


def test_size_limit_and_no_partial_file_left(store: AssetStore) -> None:
    with pytest.raises(StorageError):
        store.put_stream(
            io.BytesIO(b"x" * 5000),
            original_filename="big.png",
            mime="image/png",
            max_bytes=1024,
        )
    leftovers = [p.name for p in store.root.iterdir() if p.name.startswith(".upload-")]
    assert leftovers == [], "失败的上传不能留下临时文件"


def test_path_traversal_is_rejected(store: AssetStore) -> None:
    for evil in ("../../etc/passwd", "..\\..\\windows\\system32", "/etc/shadow"):
        with pytest.raises(StorageError):
            store.abs_path(evil)


def test_no_mutation_api_exists(store: AssetStore) -> None:
    """把「原始件不可变」当成**编译期约束**来验证。

    这不是在测行为，而是在测**API 形状**：存储层不允许提供修改或删除原始件的方法。
    如果有人日后加了这样一个方法，这个测试会失败，逼他面对这个决定。
    """
    forbidden = {"update", "overwrite", "delete", "remove", "unlink", "replace", "write"}
    present = {name for name in dir(store) if not name.startswith("_")} & forbidden
    assert present == set(), f"存储层出现了破坏不可变性的方法：{sorted(present)}"


def test_thumbnail_is_derived_and_smaller(store: AssetStore) -> None:
    src = store.put_bytes(
        _png_bytes(size=(1200, 900)), original_filename="big.png", mime="image/png"
    )
    data = store.render_thumbnail(src.storage_path, max_px=200)
    assert len(data) < src.bytes
    with Image.open(io.BytesIO(data)) as im:
        assert max(im.size) <= 200


def test_thumbnail_is_not_written_into_the_store(store: AssetStore) -> None:
    """★ 回归测试：生成缩略图**不得**往内容寻址存储里写任何东西。

    存储目录是"L0 原始证据、只进不出"。把可重建的派生物写进去，等于让
    每个 max_px 永久占一份，而且**没有任何办法清理** —— 存储层刻意不提供
    删除与覆盖 API。

    早期版本这里走的是 `self.put_bytes(...)`，缩略图确实被写了进去；
    同时路由又把它复制一份到 thumb-cache，等于同一份数据存两遍。
    """
    src = store.put_bytes(
        _png_bytes(size=(1200, 900)), original_filename="big.png", mime="image/png"
    )

    def snapshot() -> list[str]:
        return sorted(
            p.relative_to(store.root).as_posix() for p in store.root.rglob("*") if p.is_file()
        )

    before = snapshot()
    store.render_thumbnail(src.storage_path, max_px=200)
    store.render_thumbnail(src.storage_path, max_px=480)
    after = snapshot()

    assert before == after, f"生成缩略图往存储里写了文件：{sorted(set(after) - set(before))}"


def test_thumbnail_of_non_image_raises(store: AssetStore) -> None:
    a = store.put_bytes(b"not an image", original_filename="note.png", mime="image/png")
    with pytest.raises(UnsupportedFileType):
        store.render_thumbnail(a.storage_path)


def test_metadata_kwargs_are_not_positional() -> None:
    """API 形状检查：``put_*`` 的元数据必须是关键字参数。

    位置参数会让 ``put_bytes(data, "scan_raw")`` 这种调用看起来像是对的，
    实际上把 ``kind`` 传到了 ``original_filename`` 上 —— 静默且难查。

    只允许"数据本身"这一个位置参数；``**kwargs`` 是合法的转发形式。
    """
    import inspect

    from postmarktrip.storage.content_addressed import AssetStore as S

    for name in ("put_stream", "put_bytes"):
        params = [
            p for p in inspect.signature(getattr(S, name)).parameters.values() if p.name != "self"
        ]
        positional = [p.name for p in params if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD]
        assert len(positional) <= 1, f"{name} 有多个位置参数：{positional}"
