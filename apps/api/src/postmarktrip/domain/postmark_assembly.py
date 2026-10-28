"""确认时把"人确认过的字段"装配成 L3 的 `postmark` 行（`docs/ok/16` §五）。

## 这个模块存在的理由

`routes_confirm` 原来只写 `card` + `observation` + `correction` —— **从不建戳**，
于是"戳"这个实体在库里根本不存在（症状见 `docs/ok/15` §四：卡片页显示
「（缺戳面原文）」、卡列表摘要为空）。把一个实体从无到有地建起来，规则必须
**能脱离 HTTP 单测**，所以映射表与解析规则都放这里。

## 两条必须遵守的语义约束

1. **`inscription` 是必填的证据**（`Postmark.inscription` NOT NULL）。
   装配时它若为空 → 由调用方拒绝整个请求（服务端不替用户编一个原文）。
2. **`note` 列机器永不写**（`db/models.py` 的语义约束）。所以"同名多局没挂上"
   这类**解析结果**不进 `note`，而是回报给调用方（`ConfirmResult.notes`）——
   人可以在卡片页自己填备注、也可以自己选一家局。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from postmarktrip.db.models import Place, PostOffice, Tag
from postmarktrip.domain.vocab import PostmarkType, Provenance, TagCategory
from postmarktrip.recognition.vocab import office_forms

# --------------------------------------------------------------------------- #
#  字段映射（唯一的一处）
# --------------------------------------------------------------------------- #

#: 戳面字段名（确认载荷里的 `field_name`）→ `postmark` 列名。
#:
#: 为什么要一张显式的表而不是散在代码里：`docs/ok/16` §5.1 选的方案是
#: "**从 `final_value` 派生** `postmark`" —— 那么"哪些字段进哪个列"就是这套
#: 设计的接口本身，它必须能被测试逐条验（见 `tests/test_confirm_postmark.py`
#: 的完备性测试）。散着写会让"新增一个字段忘了落列"变成静默故障。
FIELD_TO_POSTMARK_COLUMN: Final[dict[str, str]] = {
    "postmark_type": "postmark_type",
    "inscription": "inscription",
    "date": "postmark_date",
    "date_text": "postmark_date_text",
    "time_code": "time_code",
    "office_no": "postmark_no",
}

#: 主题邮局的 tag 名。**库里已预置**（`vocab.DEFAULT_TAGS` 的
#: `("主题邮局", TagCategory.THEME)`，实测 `tag` 表里就有它）——
#: 所以 `docs/ok/16` §六 的裁定（只进 inscription + 打 tag）是**零 schema 变更**。
THEME_TAG_NAME: Final = "主题邮局"

#: 判据：戳面文字里出现这四个字（用户 2026-10-11 给的判据，原话"识别就是结果
#: 有没有带'主题邮局'4 个字"）。
THEME_MARKER: Final = "主题邮局"

_ISO_DATE_RE: Final = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


def postmark_values_from(final: Mapping[str, str]) -> dict[str, object]:
    """按 `final_value` 装配 `postmark` 的列值（不含 id、不含 tag）。

    `final` 是"人确认后的值"的表：`{field_name: value}`。**不要传机器值** ——
    机器值属于 L2（`observation`），两者混了就没有"人改了什么"可言了。
    """
    values: dict[str, object] = {}

    #: 戳种：非法/为空时落到 `other`。为什么不是拒绝提交：`postmark_type` 是
    #: NOT NULL，而"机器没判出来、人也没改"是一种**合法状态**（那枚戳就是没认出来）；
    #: `other` 诚实地表达"其他/未细分"，比编一个 `daily` 安全。
    raw_kind = final.get("postmark_type", "").strip()
    try:
        values["postmark_type"] = PostmarkType(raw_kind).value
    except ValueError:
        values["postmark_type"] = PostmarkType.OTHER.value

    values["inscription"] = final.get("inscription", "").strip()
    values["postmark_date"] = _parse_date(final.get("date", ""))
    values["postmark_date_text"] = _optional(final.get("date_text", ""), limit=64)
    values["time_code"] = _optional(final.get("time_code", ""), limit=16)
    values["postmark_no"] = _optional(final.get("office_no", ""), limit=16)
    #: 跑戳来源由人选的 `provenance` 不在确认载荷里，默认按真扫件处理。
    values["provenance"] = Provenance.REAL_SCAN.value
    return values


def _parse_date(value: str) -> date | None:
    """`2026-10-01` / `2026-10-01 09:00` → `date`。解析不出给 None（不猜）。"""
    m = _ISO_DATE_RE.search(value or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _optional(value: str, *, limit: int) -> str | None:
    """空串 → None（列可空）；超长截断到列宽，避免 500。"""
    text = (value or "").strip()
    return text[:limit] if text else None


# --------------------------------------------------------------------------- #
#  主题邮局
# --------------------------------------------------------------------------- #


def looks_like_theme_office(*texts: str | None) -> bool:
    """戳面文字里有没有「主题邮局」四个字。

    ⚠️ 判**两处**（用户 2026-10-11 同意）：`inscription` **与** `office`。
    因为实测模型有时会把「…主题邮局」放进 `office` 而不是原文。
    """
    return any(THEME_MARKER in (t or "") for t in texts)


def ensure_tag(db: Session, name: str, category: TagCategory) -> Tag:
    """按名字取 tag，没有就建一个。

    `tag.name` 上有唯一约束（`uq_tag_name`），所以这里可以安全地"查不到就建"：
    并发下最坏是唯一约束报错，而不是出现两个同名 tag。
    """
    tag = db.scalar(select(Tag).where(Tag.name == name))
    if tag is not None:
        return tag
    tag = Tag(name=name, category=category.value)
    db.add(tag)
    db.flush()
    return tag


# --------------------------------------------------------------------------- #
#  place_id / post_office_id 的解析
# --------------------------------------------------------------------------- #


def resolve_place_id(place_vocab: object, place_text: str) -> tuple[uuid.UUID | None, str]:
    """把地名文本解析成 `place.id`。

    **用与识别判定同一条规则**（`PlaceVocab.lookup`）—— 这样"界面上显示绿的那个值"
    与"库里挂上的那个 id"必然一致；另写一套匹配迟早会分歧。
    """
    text = (place_text or "").strip()
    if not text:
        return None, ""
    resolution = place_vocab.lookup(text)  # type: ignore[attr-defined]
    hit = getattr(resolution, "hit", None)
    if getattr(resolution, "known", False) and hit is not None and hit.place_id is not None:
        return uuid.UUID(str(hit.place_id)), f"地点：{text!r} → {' / '.join(hit.path)}"
    return None, f"地点：{text!r} 不是已登记地名 → `place_id` 留空（这是合法状态）"


#: 同名多局时报出来的样本上限（只为人看得懂，不参与判断）
_MAX_REPORTED_DUPLICATES: Final = 5


def resolve_office_id(
    db: Session, office_text: str, place_id: uuid.UUID | None
) -> tuple[uuid.UUID | None, str]:
    """把局名解析成 `post_office.id`，**只在该 place 的子树内找**。

    ## 为什么必须限定子树（用户 2026-10-11 明确要求）

    局名短名撞车极凶（实测「人民路」出现在 **104** 个县 —— `ok/14-B` §8.1 记的是
    99，实际更多）。全局匹配必然挂错，而挂错的 id 比留空有害得多：
    留空的人一眼能看出"没匹配上"，挂错的人会以为系统认出来了。

    ## 判据

    * 戳面写法 = **该局的任一形态**（全名 / 短名 / 别名），**复用
      `vocab.office_forms`** —— 与识别侧词表同一条规则。
      目录里存的是全名（`建溪邮政支局`），而戳面写短名（`建溪`）：
      实测 2026-10-11 端到端跑一次就撞到了"界面判绿、这里挂不上"的矛盾。
    * 命中**唯一** → 返回它
    * 命中 **0 个** → 留空（如 `三清山主题邮局`：名录里根本没有，`ok/14-B` §7.6）
    * 命中 **多个** → 留空 + 说明"同名几家"（**不猜**）
    * 地点没解析出来 → **不做全局匹配**，直接留空
    """
    text = (office_text or "").strip()
    if not text:
        return None, ""
    if place_id is None:
        return None, (
            f"局名：{text!r} 未匹配 —— 地点没解析出来，而局名**不做全局匹配**"
            f"（实测「人民路」出现在 104 个县）"
        )

    place = db.get(Place, place_id)
    if place is None:  # pragma: no cover - 调用方刚查过，防御性分支
        return None, f"局名：{text!r} 未匹配（地点 id 不存在）"

    #: 子树：`place.path` 形如 `/CN/<uuid>/<uuid>/`，**末尾带斜杠** ——
    #: 所以 `path LIKE 前缀%` 就是正确的子树判定（不会碰到兄弟节点的前缀）。
    subtree = (
        select(Place.id).where(Place.path.like(f"{place.path}%"))
        if place.path
        else select(Place.id).where(Place.id == place_id)
    )
    #: 先用 SQL 把候选缩到很少（精确名 / 别名 / **以戳面写法开头的全名**），
    #: 再用 `office_forms` 在 Python 里精确判"是不是这家"。
    #:
    #: 为什么不把 `office_forms` 那套规则写进 SQL：短名是**算出来的**
    #: （剥后缀），SQL 里表达它要么加列、要么写成等价的字符串拼接 ——
    #: 那是同一规则的第二份实现，迟早会不一致（这条教训本项目记过多次）。
    candidates = db.execute(
        select(PostOffice.id, PostOffice.name, PostOffice.aliases)
        .where(PostOffice.place_id.in_(subtree))
        .where(
            or_(
                PostOffice.name == text,
                PostOffice.aliases.contains([text]),
                #: `startswith` 会把 `%` `_` 自动转义（`autoescape=True`）
                PostOffice.name.startswith(text, autoescape=True),
            )
        )
        .order_by(PostOffice.name)
    ).all()
    rows = [r for r in candidates if text in office_forms(r.name, r.aliases)]

    if len(rows) == 1:
        return rows[0].id, f"局名：{text!r} → {rows[0].name}（在「{place.name}」子树内唯一）"
    if not rows:
        return None, (
            f"局名：{text!r} 在「{place.name}」子树内没有 → `post_office_id` 留空"
            f"（名录里没有的机构名，如主题邮局，留空是**正确**结果）"
        )
    names = "、".join(r.name for r in rows[:_MAX_REPORTED_DUPLICATES])
    more = f" 等 {len(rows)} 家" if len(rows) > _MAX_REPORTED_DUPLICATES else ""
    return None, (
        f"局名：{text!r} 在「{place.name}」子树内命中多家同名（{names}{more}）"
        f"→ **不挂**，请到卡片页自己选一家"
    )


@dataclass(slots=True)
class CardLinks:
    """一张卡"会自动挂到哪个 id"的结果 —— **识别页显示、确认时落库，同一条规则**。

    用户 2026-10-12 的诉求："在'识别'界面中，尽管不填【结构化】，只要能在
    【局名】【地名】命中，就能补充到结构化字段上面去"。

    这件事**服务端本来就在做**（确认时解析），缺的是**看得见**：不显示的话，
    人会以为必须手填 —— 实测 A5 那三趟里，005 的 7 张卡手填了 14 次，
    而其中大部分本来就解析得出来（`ok/18` §2.3）。
    """

    place_id: uuid.UUID | None = None
    place_name: str | None = None
    office_id: uuid.UUID | None = None
    office_name: str | None = None
    #: 局所属的地点名（显示成"福屿邮政所（福州市）"）
    office_place_name: str | None = None
    #: 人话说明：解析到哪、或为什么没解析到（**直接显示给用户**）
    note: str = ""

    @property
    def resolved_any(self) -> bool:
        return self.place_id is not None or self.office_id is not None


def resolve_card_links(
    db: Session,
    place_vocab: object,
    *,
    place_text: str,
    office_text: str,
) -> CardLinks:
    """把（地名文本, 局名文本）解析成 id **并带上给人看的名字与说明**。

    ## 为什么要有这个合并入口

    识别页要在**确认之前**就显示"会挂到哪儿"，确认侧要在落库时真的挂 —— 两处
    必须是**同一条规则**，否则界面显示的和库里挂上的会悄悄分叉（本项目在
    "同一规则两处实现"上已经踩过两次：`_vlm_kind` 的英文枚举、`office_forms`
    的短名 vs 全名）。所以两边都走这里，里面再调 `resolve_place_id` /
    `resolve_office_id`（真正的判据只有那一处）。

    `db` 只用来补**显示用**的名字，不参与判断。
    """
    place_id, place_note = resolve_place_id(place_vocab, place_text)
    office_id, office_note = resolve_office_id(db, office_text, place_id)

    links = CardLinks(place_id=place_id, office_id=office_id)
    if place_id is not None:
        place = db.get(Place, place_id)
        links.place_name = place.name if place is not None else None
    if office_id is not None:
        office = db.get(PostOffice, office_id)
        if office is not None:
            links.office_name = office.name
            if office.place_id is not None:
                office_place = db.get(Place, office.place_id)
                links.office_place_name = office_place.name if office_place is not None else None
    links.note = "；".join(n for n in (place_note, office_note) if n)
    return links
