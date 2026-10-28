"""缩略图端点的测试：缓存落在哪、会不会污染 L0 存储。

**这个文件的存在源于一个真实的设计缺陷**：缩略图生成曾经走
`AssetStore.put_bytes(...)`，于是可随时重建的派生物被写进了那个被文档
描述为"L0 原始证据、只进不出"的目录 —— 每个 `max_px` 都会永久占一份，
而且**没有任何办法清理**（存储层刻意不提供删除与覆盖）。同时路由又把
生成的字节复制一份到 `thumb-cache/`，等于同一份数据存两遍。

这里直接从路由函数入手（它就是一个普通函数），配一个真实的 `AssetStore`
和一个最小的数据库替身 —— 不需要数据库，也不需要 HTTP 层。
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import FileResponse
from PIL import Image

from postmarktrip.api.routes_assets import get_thumb
from postmarktrip.config import settings
from postmarktrip.main import app
from postmarktrip.storage import AssetStore


def test_the_default_max_px_must_satisfy_its_own_constraint() -> None:
    """`max_px` 的**默认值**必须落在它自己声明的区间里。

    ⚠️ 这条测的是**接口契约**（OpenAPI 里的 `minimum`/`maximum`/`default`），
    不是函数体 —— 因为 2026-10-12 的真 bug 恰恰在签名里：
    `Query(ge=80, le=1600)` 配默认值 `0` 是自相矛盾的，于是"**不带参数**调用"
    被 FastAPI 判 422；而前端 `Thumb`（卡片页、画廊）就是这么调的 →
    每一张图都 `onError` → 显示「暂无图片」。表现像"图片没上传成功"，
    实际是接口自己把自己判死了（`docs/ok/18` §3.1）。

    **为什么原来的测试没抓住**：它们全部**直接调 `get_thumb(...)`**，
    绕过了 FastAPI 的参数校验 —— 所以这个 bug 在 6 个测试全绿的情况下上了线。
    用 OpenAPI 契约来测就不会绕过它（FastAPI 自己会把 ge/le/默认值写进去）。

    （也曾试过读 `Annotated` 里的 `Query` 对象，但 pydantic v2 把 `ge`/`le`
    放进了 `FieldInfo.metadata`，不再是属性 —— 契约更稳。）
    """
    params = app.openapi()["paths"]["/api/assets/{asset_id}/thumb"]["get"]["parameters"]
    spec = next(p for p in params if p["name"] == "max_px")["schema"]
    lo, hi, default = spec["minimum"], spec["maximum"], spec.get("default")
    assert lo <= default <= hi, (
        f"默认值 {default} 不满足自己的约束 [{lo}, {hi}] —— 不带参数调用会 422"
    )
    #: 函数体是 `px = max_px or settings.thumbnail_max_px`，所以服务端默认值
    #: 也必须落在区间内，否则"带 0、用默认值"这条路一样会炸。
    assert lo <= settings.thumbnail_max_px <= hi


def _png_bytes(size: tuple[int, int] = (1200, 900)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (12, 90, 200)).save(buf, format="PNG", dpi=(600, 600))
    return buf.getvalue()


class _StubDb:
    """`_get_asset` 只用 `db.get(Asset, id)`。"""

    def __init__(self, asset: Any) -> None:
        self._asset = asset

    def get(self, entity: Any, ident: Any) -> Any:
        return self._asset


@pytest.fixture
def store(workdir: Path) -> AssetStore:
    return AssetStore(workdir / "assets", allowed_exts=(".png", ".jpg", ".jpeg"))


@pytest.fixture
def asset(store: AssetStore) -> SimpleNamespace:
    stored = store.put_bytes(_png_bytes(), original_filename="card.png", mime="image/png")
    return SimpleNamespace(
        id=uuid.uuid4(),
        sha256=stored.sha256,
        storage_path=stored.storage_path,
        mime=stored.mime,
    )


def _snapshot(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_thumbnail_is_written_to_the_cache_dir(
    store: AssetStore, asset: SimpleNamespace, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_dir", workdir)

    resp = get_thumb(asset.id, _StubDb(asset), store, None, 0)  # type: ignore[arg-type]

    assert isinstance(resp, FileResponse)
    assert resp.media_type == "image/jpeg"
    # 响应头是 immutable —— URL 内容永不变化，所以缓存一年是安全的
    assert "immutable" in resp.headers["cache-control"]

    produced = Path(resp.path)
    assert produced.parent == workdir / "thumb-cache"
    assert produced.name == f"{asset.sha256}_{settings.thumbnail_max_px}.jpg"
    assert produced.is_file() and produced.stat().st_size > 0


def test_generating_a_thumbnail_does_not_pollute_the_store(
    store: AssetStore, asset: SimpleNamespace, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 核心回归：缩略图不得往内容寻址存储里写任何东西。

    那个目录是 L0 原始证据，"只进不出"。派生物混进去会让每个尺寸
    永久占一份且无法清理。
    """
    monkeypatch.setattr(settings, "data_dir", workdir)
    before = _snapshot(store.root)

    get_thumb(asset.id, _StubDb(asset), store, None, 200)  # type: ignore[arg-type]
    get_thumb(asset.id, _StubDb(asset), store, None, 480)  # type: ignore[arg-type]

    after = _snapshot(store.root)
    assert before == after, f"生成缩略图污染了存储目录：{sorted(after - before)}"


def test_thumbnail_is_cached_across_calls(
    store: AssetStore, asset: SimpleNamespace, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一个 (sha256, max_px) 第二次请求要命中缓存，而不是重新生成。"""
    monkeypatch.setattr(settings, "data_dir", workdir)

    first = Path(get_thumb(asset.id, _StubDb(asset), store, None, 240).path)  # type: ignore[arg-type]
    stamp = first.stat().st_mtime_ns
    second = Path(get_thumb(asset.id, _StubDb(asset), store, None, 240).path)  # type: ignore[arg-type]

    assert first == second
    assert second.stat().st_mtime_ns == stamp, "第二次请求重新生成了缩略图（缓存没命中）"


def test_different_max_px_produce_different_cache_entries(
    store: AssetStore, asset: SimpleNamespace, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_dir", workdir)

    a = Path(get_thumb(asset.id, _StubDb(asset), store, None, 240).path)  # type: ignore[arg-type]
    b = Path(get_thumb(asset.id, _StubDb(asset), store, None, 480).path)  # type: ignore[arg-type]

    assert a != b
    assert a.is_file() and b.is_file()


def test_thumbnail_is_smaller_than_the_original(
    store: AssetStore, asset: SimpleNamespace, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_dir", workdir)

    resp = get_thumb(asset.id, _StubDb(asset), store, None, 160)  # type: ignore[arg-type]
    original = store.abs_path(asset.storage_path)

    assert Path(resp.path).stat().st_size < original.stat().st_size
    with Image.open(resp.path) as im:
        assert max(im.size) <= 160


def _rgba_png_bytes(size: tuple[int, int] = (600, 600)) -> bytes:
    """一张**圆外透明**的贴片：中心不透明，四角 alpha=0。"""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    for x in range(size[0] // 4, size[0] * 3 // 4):
        for y in range(size[1] // 4, size[1] * 3 // 4):
            im.putpixel((x, y), (200, 30, 30, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_alpha_thumbnail_is_png_and_keeps_transparency(
    store: AssetStore, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 戳墙需要"圆外透明"可辨：缩略图必须能保留 alpha。

    默认的 JPEG 会把透明区域拍成黑/白底，墙上就看不出戳的边界了。
    """
    monkeypatch.setattr(settings, "data_dir", workdir)
    stored = store.put_bytes(_rgba_png_bytes(), original_filename="cut.png", mime="image/png")
    asset = SimpleNamespace(
        id=uuid.uuid4(), sha256=stored.sha256, storage_path=stored.storage_path, mime=stored.mime
    )

    resp = get_thumb(asset.id, _StubDb(asset), store, None, 240, True)  # type: ignore[arg-type]

    assert isinstance(resp, FileResponse)
    assert resp.media_type == "image/png"
    produced = Path(resp.path)
    assert produced.name == f"{asset.sha256}_240_alpha.png"
    with Image.open(produced) as im:
        assert im.mode == "RGBA"
        # 四角仍是透明的 —— alpha 没被拍平
        assert im.getchannel("A").getextrema()[0] == 0
        assert max(im.size) <= 240


def test_alpha_and_jpeg_thumbnails_use_different_cache_entries(
    store: AssetStore, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_dir", workdir)
    stored = store.put_bytes(_rgba_png_bytes(), original_filename="cut.png", mime="image/png")
    asset = SimpleNamespace(
        id=uuid.uuid4(), sha256=stored.sha256, storage_path=stored.storage_path, mime=stored.mime
    )

    jpeg = Path(get_thumb(asset.id, _StubDb(asset), store, None, 240).path)  # type: ignore[arg-type]
    png = Path(
        get_thumb(asset.id, _StubDb(asset), store, None, 240, True).path  # type: ignore[arg-type]
    )

    assert jpeg != png
    assert jpeg.suffix == ".jpg"
    assert png.suffix == ".png"
