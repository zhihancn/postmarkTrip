"""场景图（电子卡）的领域逻辑：校验、槽位绑定、字段取值。

**为什么单独成域**：场景是**契约**（`contract/scene.schema.json`），渲染器、编辑器、
将来的服务端渲染器都消费它。这里只做**纯函数** —— 不碰数据库、不碰图像 ——
所以每条规则都能用测试钉住（布局算错在图上只表现为"歪/缺"，很难一眼看出）。

三件事的边界（`docs/spec/v1.1.1`）：

* **场景**是数据（这里处理的）；
* **渲染器**是 `scene → 像素` 的纯函数（前端 `apps/web/src/render/`）；
* **编辑器**是 `手势 → 新场景` 的状态机（M2）。

本模块不做"模板从哪来"（文件/数据库）也不做"存到哪"（路由层的事）。
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, TypeGuard

#: 契约版本。破坏性变更时递增；渲染器必须拒绝不认识的版本。
SCHEMA_VERSION = 1

SLOT_KINDS = frozenset({"image", "postmark", "stamp", "text"})
FITS = frozenset({"contain", "cover", "fill", "none"})
POSTMARK_SOURCES = frozenset({"scan_cut", "generated"})
ALIGNS = frozenset({"left", "center", "right"})
VALIGNS = frozenset({"top", "middle", "bottom"})
WRITING_MODES = frozenset({"horizontal", "vertical"})

MAX_SLOTS = 500
MAX_CANVAS_MM = 2000.0
MIN_CANVAS_MM = 1.0
MAX_DPI = 1200
MIN_DPI = 72

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ASSET_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")

#: 文字槽能绑定的字段。**白名单**：`field` 来自场景 JSON（会从文件/数据包进来），
#: 允许任意属性路径等于让数据决定读哪个属性；而场景可能会被分享、导入。
TEXT_FIELDS = frozenset(
    {
        "card.visit_date",
        "card.title",
        "postmark.inscription",
        "postmark.date",
        "postmark.place",
        "postmark.office",
    }
)


def asset_ref(sha256: str) -> str:
    """把 64 位 sha256 变成契约里的资产引用。"""
    return f"sha256:{sha256.lower()}"


def is_asset_ref(value: object) -> bool:
    return isinstance(value, str) and bool(_ASSET_RE.match(value))


def _is_num(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _err(errors: list[str], path: str, msg: str) -> None:
    errors.append(f"{path}: {msg}")


def validate_scene(scene: object) -> list[str]:
    """校验一份场景图。返回**人话错误列表**（空 = 通过）。

    刻意**不**在第一个错误处抛异常：一次把所有问题报出来，编辑器才能一起修。
    也刻意不检查"资产到底存不存在" —— 那要查库，不属于纯函数；
    缺图由渲染器画占位（`docs/spec/v1.1` §2.3）。

    ⚠️ **槽位可以没有 asset**：模板在实例化前本来就没有（资产是按卡绑的），
    而卡片上的"这枚戳没有贴片"也是合法状态。契约里 `StampSlot/ImageSlot`
    原本把 `asset` 标成 required，那是与"缺图必须可见"自相矛盾的 ——
    已在契约 v1 内放宽（见 `contract/scene.schema.json` 的变更说明）。
    """
    errors: list[str] = []
    if not isinstance(scene, dict):
        return ["scene 必须是对象"]

    if scene.get("schema_version") != SCHEMA_VERSION:
        _err(errors, "schema_version", f"必须是 {SCHEMA_VERSION}")

    canvas = scene.get("canvas")
    if not isinstance(canvas, dict):
        _err(errors, "canvas", "必须是对象")
    else:
        for key in ("width_mm", "height_mm"):
            v = canvas.get(key)
            if not _is_num(v) or not (MIN_CANVAS_MM <= float(v) <= MAX_CANVAS_MM):
                _err(errors, f"canvas.{key}", f"必须是 {MIN_CANVAS_MM}–{MAX_CANVAS_MM} 之间的数")
        dpi = canvas.get("dpi")
        if dpi is not None and (
            not isinstance(dpi, int) or isinstance(dpi, bool) or not (MIN_DPI <= dpi <= MAX_DPI)
        ):
            _err(errors, "canvas.dpi", f"必须是 {MIN_DPI}–{MAX_DPI} 的整数")

    bg = scene.get("background")
    if bg is not None:
        _validate_background(bg, errors)

    slots = scene.get("slots")
    if not isinstance(slots, list):
        _err(errors, "slots", "必须是数组")
        return errors
    if len(slots) > MAX_SLOTS:
        _err(errors, "slots", f"最多 {MAX_SLOTS} 个槽位")

    seen: set[str] = set()
    for i, slot in enumerate(slots):
        _validate_slot(slot, f"slots[{i}]", seen, errors)
    return errors


def _validate_background(bg: object, errors: list[str]) -> None:
    if not isinstance(bg, dict):
        _err(errors, "background", "必须是对象")
        return
    kind = bg.get("type")
    if kind == "solid":
        color = bg.get("color")
        if not isinstance(color, str) or not _HEX_RE.match(color):
            _err(errors, "background.color", "必须是 #RRGGBB 或 #RRGGBBAA")
        return
    if kind == "image":
        if not is_asset_ref(bg.get("asset")):
            _err(errors, "background.asset", "必须是 sha256:<64 hex>")
        fit = bg.get("fit", "cover")
        if fit not in FITS:
            _err(errors, "background.fit", f"必须是 {sorted(FITS)} 之一")
        return
    _err(errors, "background.type", "必须是 solid 或 image")


def _validate_slot(slot: object, path: str, seen: set[str], errors: list[str]) -> None:
    if not isinstance(slot, dict):
        _err(errors, path, "必须是对象")
        return

    sid = slot.get("id")
    if not isinstance(sid, str) or not _ID_RE.match(sid):
        _err(errors, f"{path}.id", "必须是 1–64 位字母/数字/下划线/连字符")
    elif sid in seen:
        _err(errors, f"{path}.id", f"重复：{sid}")
    else:
        seen.add(sid)

    kind = slot.get("kind")
    if kind not in SLOT_KINDS:
        _err(errors, f"{path}.kind", f"必须是 {sorted(SLOT_KINDS)} 之一")
        return

    for key in ("x", "y"):
        if not _is_num(slot.get(key)):
            _err(errors, f"{path}.{key}", "必须是有限数字（mm）")
    for key in ("w", "h"):
        v = slot.get(key)
        if not _is_num(v) or float(v) <= 0:
            _err(errors, f"{path}.{key}", "必须是正数（mm）")

    z = slot.get("z", 0)
    if not isinstance(z, int) or isinstance(z, bool):
        _err(errors, f"{path}.z", "必须是整数")
    rot = slot.get("rotation", 0)
    if not _is_num(rot) or not (-360 <= float(rot) <= 360):
        _err(errors, f"{path}.rotation", "必须是 -360–360 的数")
    opacity = slot.get("opacity", 1)
    if not _is_num(opacity) or not (0 <= float(opacity) <= 1):
        _err(errors, f"{path}.opacity", "必须在 0–1")

    fit = slot.get("fit")
    if fit is not None and fit not in FITS:
        _err(errors, f"{path}.fit", f"必须是 {sorted(FITS)} 之一")

    if kind == "postmark":
        source = slot.get("source", "scan_cut")
        if source not in POSTMARK_SOURCES:
            _err(errors, f"{path}.source", f"必须是 {sorted(POSTMARK_SOURCES)} 之一")
    if kind in {"postmark", "stamp", "image"}:
        asset = slot.get("asset")
        if asset is not None and not is_asset_ref(asset):
            _err(errors, f"{path}.asset", "必须是 sha256:<64 hex>（或省略）")
    if kind == "text":
        if slot.get("color") is not None and (
            not isinstance(slot["color"], str) or not _HEX_RE.match(slot["color"])
        ):
            _err(errors, f"{path}.color", "必须是 #RRGGBB")
        if slot.get("font_size_mm") is not None and (
            not _is_num(slot["font_size_mm"]) or float(slot["font_size_mm"]) <= 0
        ):
            _err(errors, f"{path}.font_size_mm", "必须是正数（mm）")
        if slot.get("align") is not None and slot["align"] not in ALIGNS:
            _err(errors, f"{path}.align", f"必须是 {sorted(ALIGNS)} 之一")
        if slot.get("valign") is not None and slot["valign"] not in VALIGNS:
            _err(errors, f"{path}.valign", f"必须是 {sorted(VALIGNS)} 之一")
        if slot.get("writing_mode") is not None and slot["writing_mode"] not in WRITING_MODES:
            _err(errors, f"{path}.writing_mode", f"必须是 {sorted(WRITING_MODES)} 之一")


def bind_scene(
    scene: Mapping[str, Any],
    *,
    postmark_assets: Sequence[str | None] = (),
    stamp_assets: Sequence[str | None] = (),
    fields: Mapping[str, str | None] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """把**素材与字段**绑进场景，返回 (新场景, 提示列表)。

    绑定规则（`docs/spec/v1.1` §4.1）：**按 kind + 出现序号**。第 i 个 `postmark`
    槽位拿第 i 枚戳的贴片；`stamp` 同理。槽位比素材多 → 多出来的槽位**留空**
    （渲染器画占位）；素材比槽位多 → 记一条"有 N 枚没上卡"，**不自动挤进去**。

    返回新对象（深拷贝）：调用方可能拿它做 dry-run 预览，不能污染原场景。
    """
    out: dict[str, Any] = copy.deepcopy(dict(scene))
    warnings: list[str] = []

    if fields:
        for slot in out.get("slots", []):
            field = slot.get("field")
            if isinstance(field, str) and field in TEXT_FIELDS and field in fields:
                value = fields[field]
                if value is None or value == "":
                    slot.pop("text", None)
                else:
                    slot["text"] = value

    pm_i = 0
    st_i = 0
    for slot in out.get("slots", []):
        kind = slot.get("kind")
        if kind == "postmark":
            asset = postmark_assets[pm_i] if pm_i < len(postmark_assets) else None
            pm_i += 1
            _set_or_clear(slot, "asset", asset)
        elif kind == "stamp":
            asset = stamp_assets[st_i] if st_i < len(stamp_assets) else None
            st_i += 1
            _set_or_clear(slot, "asset", asset)

    if len(postmark_assets) > pm_i:
        warnings.append(f"这张卡有 {len(postmark_assets) - pm_i} 枚戳没有槽位（未上卡）")
    if len(stamp_assets) > st_i:
        warnings.append(f"这张卡有 {len(stamp_assets) - st_i} 枚票没有槽位（未上卡）")
    return out, warnings


def _set_or_clear(slot: dict[str, Any], key: str, value: str | None) -> None:
    if value:
        slot[key] = value
    else:
        slot.pop(key, None)


def scene_fields(
    *,
    visit_date: date | None,
    title: str | None,
    inscription: str | None,
    postmark_date: date | None,
    place_name: str | None,
    office_name: str | None,
) -> dict[str, str | None]:
    """文字槽可绑定的字段白名单。

    **白名单而不是任意属性路径**：`field` 来自场景 JSON（会从文件/数据包进来），
    允许任意路径等于让数据决定读哪个属性 —— 白名单是这里唯一便宜的防线。
    """
    return {
        "card.visit_date": visit_date.isoformat() if visit_date else None,
        "card.title": title,
        "postmark.inscription": inscription,
        "postmark.date": postmark_date.isoformat() if postmark_date else None,
        "postmark.place": place_name,
        "postmark.office": office_name,
    }


def scene_summary(scene: Mapping[str, Any]) -> dict[str, Any]:
    """列表用的概要（不返回整份场景）。"""
    raw_canvas = scene.get("canvas")
    canvas: dict[str, Any] = raw_canvas if isinstance(raw_canvas, dict) else {}
    raw_slots = scene.get("slots")
    slots: list[Any] = raw_slots if isinstance(raw_slots, list) else []
    return {
        "canvas": canvas,
        "slot_count": len(slots),
    }
