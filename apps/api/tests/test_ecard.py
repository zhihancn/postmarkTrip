"""电子卡（v1.1 M1）：场景纯函数、内置模板、框票输入校验、按 sha256 取图。

这些测试**不连数据库**：`domain/scene.py` 是纯函数，`_parse_box` 也是；
按 sha256 取图用一个最小替身（`test_asset_thumbnails.py` 的同一套做法）。
"""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from PIL import Image

from postmarktrip.api.routes_assets import _SHA256_RE, get_raw_by_sha256
from postmarktrip.api.routes_ecard import (
    _bind_for_card,
    _parse_box,
    load_builtin_templates,
)
from postmarktrip.domain.scene import (
    asset_ref,
    bind_scene,
    is_asset_ref,
    scene_fields,
    validate_scene,
)


def _slot(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "s1", "kind": "postmark", "x": 1, "y": 2, "w": 10, "h": 10}
    base.update(kw)
    return base


def _scene(*slots: dict[str, Any], **canvas: Any) -> dict[str, Any]:
    c = {"width_mm": 54, "height_mm": 90}
    c.update(canvas)
    return {"schema_version": 1, "canvas": c, "slots": list(slots)}


# --------------------------------------------------------------------------- #
#  契约与校验
# --------------------------------------------------------------------------- #


def test_asset_ref_shape() -> None:
    ref = asset_ref("A" * 64)
    assert ref == "sha256:" + "a" * 64
    assert is_asset_ref(ref)
    assert not is_asset_ref("sha256:abc")
    assert not is_asset_ref("A" * 64)


def test_valid_scene_passes() -> None:
    assert validate_scene(_scene(_slot())) == []


def test_missing_assets_are_valid() -> None:
    """模板实例化前没有 asset；卡片缺贴片也是合法状态（渲染器画占位）。"""
    assert validate_scene(_scene(_slot(kind="stamp"), _slot(id="s2", kind="postmark"))) == []


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"canvas": {"width_mm": 0, "height_mm": 90}}, "canvas.width_mm"),
        ({"slots": "nope"}, "slots"),
    ],
)
def test_validate_rejects_bad_top_level(patch: dict[str, Any], needle: str) -> None:
    scene = _scene(_slot())
    scene.update(patch)
    errors = validate_scene(scene)
    assert any(needle in e for e in errors), errors


@pytest.mark.parametrize(
    ("slot", "needle"),
    [
        (_slot(kind="hologram"), "kind"),
        (_slot(w=0), "w"),
        (_slot(x=float("nan")), "x"),
        (_slot(rotation=400), "rotation"),
        (_slot(opacity=2), "opacity"),
        (_slot(kind="text", color="red"), "color"),
        (_slot(kind="postmark", source="magic"), "source"),
        (_slot(asset="not-a-ref"), "asset"),
    ],
)
def test_validate_rejects_bad_slot(slot: dict[str, Any], needle: str) -> None:
    errors = validate_scene(_scene(slot))
    assert any(needle in e for e in errors), errors


def test_duplicate_slot_ids_are_rejected() -> None:
    errors = validate_scene(_scene(_slot(), _slot(kind="stamp")))
    assert any("重复" in e for e in errors), errors


# --------------------------------------------------------------------------- #
#  绑定（按 kind + 序号）
# --------------------------------------------------------------------------- #


def test_bind_by_kind_and_index() -> None:
    scene = _scene(
        _slot(id="pm1", kind="postmark"),
        _slot(id="st1", kind="stamp"),
        _slot(id="pm2", kind="postmark"),
    )
    a, b = asset_ref("a" * 64), asset_ref("b" * 64)
    bound, warnings = bind_scene(scene, postmark_assets=[a, None], stamp_assets=[b])

    slots = {s["id"]: s for s in bound["slots"]}
    assert slots["pm1"]["asset"] == a
    assert "asset" not in slots["pm2"]  # 缺图 → 不写 asset（渲染器画占位）
    assert slots["st1"]["asset"] == b
    assert warnings == []


def test_missing_cut_keeps_slot_order() -> None:
    """第 1 枚戳没有贴片时，第 2 枚的图**不许滑到第 1 个槽位**。"""
    scene = _scene(_slot(id="pm1"), _slot(id="pm2"))
    a = asset_ref("a" * 64)
    bound, _ = bind_scene(scene, postmark_assets=[None, a])
    slots = {s["id"]: s for s in bound["slots"]}
    assert "asset" not in slots["pm1"]
    assert slots["pm2"]["asset"] == a


def test_bind_does_not_mutate_the_template() -> None:
    scene = _scene(_slot(id="pm1"))
    snapshot = copy.deepcopy(scene)
    bind_scene(scene, postmark_assets=[asset_ref("a" * 64)])
    assert scene == snapshot


def test_extra_assets_warn_instead_of_squeezing() -> None:
    scene = _scene(_slot(id="pm1"))
    a, b = asset_ref("a" * 64), asset_ref("b" * 64)
    _, warnings = bind_scene(scene, postmark_assets=[a, b])
    assert any("1 枚戳没有槽位" in w for w in warnings), warnings


def test_text_field_binding_and_clearing() -> None:
    scene = _scene(
        _slot(id="t1", kind="text", field="postmark.inscription"),
        _slot(id="t2", kind="text", field="postmark.place"),
    )
    bound, _ = bind_scene(scene, fields={"postmark.inscription": "建溪1", "postmark.place": None})
    slots = {s["id"]: s for s in bound["slots"]}
    assert slots["t1"]["text"] == "建溪1"
    assert "text" not in slots["t2"]


def test_unknown_text_field_is_ignored() -> None:
    """`field` 是白名单：它来自场景 JSON（可能从数据包进来），不能读任意属性。"""
    scene = _scene(_slot(id="t1", kind="text", field="secret.token"))
    bound, _ = bind_scene(scene, fields={"secret.token": "x"})
    assert "text" not in bound["slots"][0]


def test_scene_fields_whitelist() -> None:
    fields = scene_fields(
        visit_date=None,
        title="卡",
        inscription="建溪1",
        postmark_date=None,
        place_name="南平市",
        office_name="建溪邮政支局",
    )
    assert set(fields) == {
        "card.visit_date",
        "card.title",
        "postmark.inscription",
        "postmark.date",
        "postmark.place",
        "postmark.office",
    }
    assert fields["postmark.office"] == "建溪邮政支局"


# --------------------------------------------------------------------------- #
#  内置模板
# --------------------------------------------------------------------------- #


def test_builtin_templates_validate() -> None:
    builtins = load_builtin_templates()
    assert set(builtins) == {"builtin:blank-card", "builtin:poster"}
    for tid, scene in builtins.items():
        assert validate_scene(scene) == [], tid


def test_builtin_templates_have_no_assets() -> None:
    """模板必须是**未绑定**的：带死 asset 的模板在别的卡上就是错的图。"""
    for tid, scene in load_builtin_templates().items():
        for slot in scene["slots"]:
            assert "asset" not in slot, (tid, slot["id"])


def test_builtin_templates_live_in_the_package() -> None:
    """模板随代码分发（不在 `public-data/`）：容器里那个目录是只读的。"""
    from postmarktrip.api.routes_ecard import _builtin_dir

    assert _builtin_dir().parent.name == "postmarktrip"
    assert (_builtin_dir() / "blank-card.json").is_file()


# --------------------------------------------------------------------------- #
#  框票：归一化坐标校验
# --------------------------------------------------------------------------- #


def test_parse_box_ok() -> None:
    assert _parse_box((0.1, 0.2, 0.3, 0.4)) == (0.1, 0.2, 0.3, 0.4)


@pytest.mark.parametrize(
    "box",
    [
        (0, 0, 0, 0.2),
        (-0.1, 0, 0.2, 0.2),
        (0.9, 0, 0.2, 0.2),
        (0, 0, float("nan"), 0.2),
    ],
)
def test_parse_box_rejects(box: tuple[float, float, float, float]) -> None:
    with pytest.raises(ValueError):
        _parse_box(box)


# --------------------------------------------------------------------------- #
#  按 sha256 取图
# --------------------------------------------------------------------------- #


def _png(path: Path) -> SimpleNamespace:
    sha = (uuid.uuid4().hex + uuid.uuid4().hex)[:64]
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (10, 10), (1, 2, 3)).save(path, format="PNG")
    return SimpleNamespace(id=uuid.uuid4(), sha256=sha, storage_path=str(path), mime="image/png")


def test_sha256_route_rejects_bad_format() -> None:
    assert not _SHA256_RE.match("abc")
    with pytest.raises(HTTPException) as ei:
        get_raw_by_sha256("not-a-sha", None, None, None)  # type: ignore[arg-type]
    assert ei.value.status_code == 422


def test_sha256_route_404_when_absent() -> None:
    class _Db:
        def scalar(self, _stmt: Any) -> None:
            return None

    with pytest.raises(HTTPException) as ei:
        get_raw_by_sha256("a" * 64, _Db(), None, None)  # type: ignore[arg-type]
    assert ei.value.status_code == 404


def test_sha256_route_returns_file(workdir: Path) -> None:
    from postmarktrip.storage import AssetStore

    assets_dir = workdir / "assets"
    store = AssetStore(assets_dir, allowed_exts=(".png",))
    target = assets_dir / "card.png"
    asset = _png(target)
    asset.storage_path = target.relative_to(assets_dir).as_posix()

    class _Db:
        def scalar(self, _stmt: Any) -> Any:
            return asset

    resp = get_raw_by_sha256(asset.sha256, _Db(), store, None)  # type: ignore[arg-type]
    assert resp.path.is_file()


# --------------------------------------------------------------------------- #
#  套模板时的绑定（不写库）
# --------------------------------------------------------------------------- #


def test_bind_for_card_without_assets() -> None:
    card = SimpleNamespace(
        visit_date=None,
        title=None,
        postmarks=[
            SimpleNamespace(
                image_asset_id=None,
                inscription="建溪1",
                postmark_date=None,
                place=None,
                post_office=None,
            )
        ],
        stamps=[],
    )
    template = _scene(_slot(id="pm1"), _slot(id="t1", kind="text", field="postmark.inscription"))
    bound, warnings = _bind_for_card(None, card, template)  # type: ignore[arg-type]
    assert "asset" not in bound["slots"][0]
    assert bound["slots"][1]["text"] == "建溪1"
    assert warnings == []


def test_bind_for_card_keeps_slot_index_for_missing_cut() -> None:
    pm = SimpleNamespace
    card = SimpleNamespace(
        visit_date=None,
        title=None,
        postmarks=[
            pm(
                image_asset_id=None,
                inscription="a",
                postmark_date=None,
                place=None,
                post_office=None,
            ),
            pm(
                image_asset_id=uuid.uuid4(),
                inscription="b",
                postmark_date=None,
                place=None,
                post_office=None,
            ),
        ],
        stamps=[],
    )

    class _Db:
        def scalars(self, _stmt: Any) -> Any:
            class _R:
                def all(self) -> list[Any]:
                    return [SimpleNamespace(id=card.postmarks[1].image_asset_id, sha256="b" * 64)]

            return _R()

    bound, _ = _bind_for_card(
        _Db(), card, _scene(_slot(id="p1"), _slot(id="p2"))
    )  # type: ignore[arg-type]
    assert "asset" not in bound["slots"][0]
    assert bound["slots"][1]["asset"] == asset_ref("b" * 64)


# --------------------------------------------------------------------------- #
#  路由：套模板（dry-run 不写库）、读场景
# --------------------------------------------------------------------------- #


def _card_for_routes() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        deleted_at=None,
        visit_date=None,
        title=None,
        scene=None,
        template_id=None,
        postmarks=[
            SimpleNamespace(
                image_asset_id=None,
                inscription="建溪1",
                postmark_date=None,
                place=None,
                post_office=None,
            )
        ],
        stamps=[],
    )


class _RouteDb:
    def __init__(self, card: Any) -> None:
        self.card = card
        self.commits = 0
        self.scalars_calls = 0

    def get(self, entity: Any, _ident: Any) -> Any:
        return self.card

    def scalars(self, _stmt: Any) -> Any:
        self.scalars_calls += 1

        class _R:
            def all(self) -> list[Any]:
                return []

        return _R()

    def commit(self) -> None:
        self.commits += 1


def test_apply_template_dry_run_does_not_write() -> None:
    from postmarktrip.api.routes_ecard import apply_template
    from postmarktrip.domain.schemas import ApplyTemplateIn

    card = _card_for_routes()
    db = _RouteDb(card)
    out = apply_template(
        card.id,
        ApplyTemplateIn(template_id="builtin:blank-card", dry_run=True),
        db,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    assert out.saved is False
    assert card.scene is None, "dry-run 不许写 card.scene"
    assert db.commits == 0
    assert out.scene["slots"], out.scene


def test_apply_template_saves_when_explicit() -> None:
    from postmarktrip.api.routes_ecard import apply_template
    from postmarktrip.domain.schemas import ApplyTemplateIn

    card = _card_for_routes()
    db = _RouteDb(card)
    out = apply_template(
        card.id,
        ApplyTemplateIn(template_id="builtin:blank-card", dry_run=False),
        db,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    assert out.saved is True
    assert card.scene is not None
    assert card.scene["template_id"] == "builtin:blank-card"
    assert db.commits == 1


def test_get_card_scene_returns_null_for_no_scene() -> None:
    from postmarktrip.api.routes_ecard import get_card_scene

    card = _card_for_routes()
    out = get_card_scene(card.id, _RouteDb(card), None)  # type: ignore[arg-type]
    assert out.scene is None
    assert out.card_id == card.id


#: 前端已经对接的电子卡端点。少一个 = 界面上某处必然 404。
ECARD_PATHS = [
    "/api/templates",
    "/api/templates/{template_id}",
    "/api/cards/{card_id}/scene",
    "/api/cards/{card_id}/apply-template",
    "/api/cards/{card_id}/stamp-cut",
    "/api/assets/by-sha256/{sha256}/raw",
]


def test_ecard_endpoints_are_registered() -> None:
    from postmarktrip.main import app

    paths = app.openapi()["paths"]
    missing = [p for p in ECARD_PATHS if p not in paths]
    assert not missing, f"没注册：{missing}"
