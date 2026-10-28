"""API 的输入输出模型（Pydantic v2）。

约定：
* 读模型一律 ``from_attributes=True``，可直接由 ORM 对象构造。
* 枚举取自 ``domain.vocab`` —— 词表只有一个归属地。
* 面值用 ``Decimal``。Pydantic 在 JSON 模式下把它序列化为字符串，
  以保留精度 —— 前端要按字符串处理，不要 parseFloat 后再算钱。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from postmarktrip.domain.vocab import (
    AssetKind,
    CardSource,
    CardStatus,
    CatalogSystem,
    PostmarkType,
    Provenance,
    TagCategory,
)

T = TypeVar("T")

ORM = ConfigDict(from_attributes=True)


class Paged(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    size: int

    @property
    def pages(self) -> int:
        return (self.total + self.size - 1) // self.size if self.size else 0


class OkResponse(BaseModel):
    ok: bool = True
    message: str | None = None


# --------------------------------------------------------------------------- #
#  资产
# --------------------------------------------------------------------------- #


class AssetRead(BaseModel):
    model_config = ORM

    id: uuid.UUID
    sha256: str
    mime: str
    bytes: int
    width: int | None = None
    height: int | None = None
    dpi: int | None = None
    kind: AssetKind
    is_immutable: bool
    original_filename: str | None = None
    created_at: datetime

    @property
    def url(self) -> str:
        return f"/api/assets/{self.id}/raw"


# --------------------------------------------------------------------------- #
#  地点
# --------------------------------------------------------------------------- #


class PlaceBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    name_en: str | None = Field(default=None, max_length=200)
    level: int = Field(ge=0, le=10)
    level_label: str | None = Field(default=None, max_length=40)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    admin_code: str | None = Field(default=None, max_length=32)
    lat: Decimal | None = None
    lng: Decimal | None = None


class PlaceCreate(PlaceBase):
    parent_id: uuid.UUID | None = None


class PlaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    name_en: str | None = Field(default=None, max_length=200)
    level_label: str | None = Field(default=None, max_length=40)
    admin_code: str | None = Field(default=None, max_length=32)
    lat: Decimal | None = None
    lng: Decimal | None = None


class PlaceRead(PlaceBase):
    model_config = ORM

    id: uuid.UUID
    parent_id: uuid.UUID | None = None
    path: str | None = None


class PlaceNode(PlaceRead):
    """树节点。``children`` 由服务端按需装配（按需展开，不一次拉全树）。"""

    children: list[PlaceNode] = []
    post_office_count: int = 0


PlaceNode.model_rebuild()


# --------------------------------------------------------------------------- #
#  邮局
# --------------------------------------------------------------------------- #


class PostOfficeBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    place_id: uuid.UUID | None = None
    postal_code: str | None = Field(default=None, max_length=16)
    is_theme: bool = False
    theme_name: str | None = Field(default=None, max_length=100)
    aliases: list[str] = []
    lat: Decimal | None = None
    lng: Decimal | None = None
    #: 备注（自由文本）。**机器永不自动填** —— 它是"解释与存疑"的落点，
    #: 不是数据字段。理由见 `models.PostOffice.note` 的注释。
    note: str | None = None


class PostOfficeCreate(PostOfficeBase):
    pass


class PostOfficeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    place_id: uuid.UUID | None = None
    postal_code: str | None = Field(default=None, max_length=16)
    is_theme: bool | None = None
    theme_name: str | None = Field(default=None, max_length=100)
    aliases: list[str] | None = None
    lat: Decimal | None = None
    lng: Decimal | None = None
    note: str | None = None


class PostOfficeRead(PostOfficeBase):
    model_config = ORM

    id: uuid.UUID
    place: PlaceRead | None = None


# --------------------------------------------------------------------------- #
#  标签
# --------------------------------------------------------------------------- #


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    category: TagCategory | None = None
    color: str | None = Field(default=None, max_length=9)


class TagRead(BaseModel):
    model_config = ORM

    id: uuid.UUID
    name: str
    category: TagCategory | None = None
    color: str | None = None


# --------------------------------------------------------------------------- #
#  邮戳
# --------------------------------------------------------------------------- #


class PostmarkBase(BaseModel):
    postmark_type: PostmarkType
    #: 戳面原文 —— 证据，原样录入。是唯一必填的文本字段。
    inscription: str = Field(min_length=1)
    postmark_date: date | None = None
    postmark_date_text: str | None = Field(default=None, max_length=64)
    time_code: str | None = Field(default=None, max_length=16)
    postmark_no: str | None = Field(default=None, max_length=16)
    post_office_id: uuid.UUID | None = None
    place_id: uuid.UUID | None = None
    is_color: bool = False
    color_layers: int | None = Field(default=None, ge=1, le=12)
    provenance: Provenance = Provenance.REAL_SCAN
    is_clean: bool | None = None
    image_asset_id: uuid.UUID | None = None
    note: str | None = None
    tag_ids: list[uuid.UUID] = []


class PostmarkCreate(PostmarkBase):
    pass


class PostmarkUpdate(BaseModel):
    postmark_type: PostmarkType | None = None
    inscription: str | None = Field(default=None, min_length=1)
    postmark_date: date | None = None
    postmark_date_text: str | None = Field(default=None, max_length=64)
    time_code: str | None = Field(default=None, max_length=16)
    postmark_no: str | None = Field(default=None, max_length=16)
    post_office_id: uuid.UUID | None = None
    place_id: uuid.UUID | None = None
    is_color: bool | None = None
    color_layers: int | None = Field(default=None, ge=1, le=12)
    provenance: Provenance | None = None
    is_clean: bool | None = None
    image_asset_id: uuid.UUID | None = None
    note: str | None = None
    tag_ids: list[uuid.UUID] | None = None


class PostmarkRead(PostmarkBase):
    model_config = ORM

    id: uuid.UUID
    card_id: uuid.UUID
    place: PlaceRead | None = None
    post_office: PostOfficeRead | None = None
    tags: list[TagRead] = []
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- #
#  邮票
# --------------------------------------------------------------------------- #


class StampBase(BaseModel):
    catalog_ref: str | None = Field(default=None, max_length=64)
    catalog_system: CatalogSystem | None = None
    name: str | None = Field(default=None, max_length=200)
    face_value: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    issue_date: date | None = None
    image_asset_id: uuid.UUID | None = None
    note: str | None = None

    @field_validator("currency", mode="after")
    @classmethod
    def _upper_currency(cls, v: str | None) -> str | None:
        # ISO 4217 一律大写（"cny" → "CNY"）
        return v.upper() if v else v


class StampCreate(StampBase):
    #: 关联的标准票；后端会在关联时把目录字段快照到这条 stamp 上
    catalog_id: uuid.UUID | None = None


class StampUpdate(BaseModel):
    catalog_ref: str | None = Field(default=None, max_length=64)
    catalog_system: CatalogSystem | None = None
    name: str | None = Field(default=None, max_length=200)
    face_value: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    issue_date: date | None = None
    image_asset_id: uuid.UUID | None = None
    note: str | None = None
    catalog_id: uuid.UUID | None = None


class StampIdentifyCandidate(BaseModel):
    c_id: int
    p_id: int | None = None
    name: str | None = None
    similarity: float | None = None
    url: str | None = None
    image: str | None = None
    query_sha256: str | None = None


class StampRead(StampBase):
    model_config = ORM

    id: uuid.UUID
    card_id: uuid.UUID
    catalog_id: uuid.UUID | None = None
    identify_status: str = "idle"
    identify_error: str | None = None
    identify_at: datetime | None = None
    identify_candidates: list[StampIdentifyCandidate] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- #
#  标准邮票库（M3C）
# --------------------------------------------------------------------------- #


class StampCatalogCreate(BaseModel):
    """手工维护入口：3 列 + 可选的目录字段。

    3 列之外的字段允许为空；`source_state` 由服务端控制（默认 draft）。
    """

    #: 人读编号；不填由服务端生成 `sc-<uuid8>`
    standard_code: str | None = Field(default=None, max_length=64)
    colnect_c_id: int | None = Field(default=None, ge=1)
    colnect_url: str | None = Field(default=None, max_length=1000)
    issue_url: str | None = Field(default=None, max_length=1000)
    catalog_system: CatalogSystem | None = None
    catalog_ref: str | None = Field(default=None, max_length=64)
    design_no: str | None = Field(default=None, max_length=32)
    design_name: str | None = Field(default=None, max_length=200)
    issue_name: str | None = Field(default=None, max_length=200)
    face_value: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    issue_date: date | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None

    @field_validator("currency", mode="after")
    @classmethod
    def _upper_currency(cls, v: str | None) -> str | None:
        return v.upper() if v else v


class StampCatalogUpdate(BaseModel):
    """更新标准票。`standard_code` 刻意不在这里：生成后冻结。"""

    colnect_c_id: int | None = Field(default=None, ge=1)
    colnect_url: str | None = Field(default=None, max_length=1000)
    issue_url: str | None = Field(default=None, max_length=1000)
    catalog_system: CatalogSystem | None = None
    catalog_ref: str | None = Field(default=None, max_length=64)
    design_no: str | None = Field(default=None, max_length=32)
    design_name: str | None = Field(default=None, max_length=200)
    issue_name: str | None = Field(default=None, max_length=200)
    face_value: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    issue_date: date | None = None
    details: dict[str, Any] | None = None
    note: str | None = None
    #: 人工确认后 → verified；stale 也可手动标记
    source_state: Literal["draft", "partial", "verified", "stale"] | None = None


class StampCatalogRead(BaseModel):
    model_config = ORM

    id: uuid.UUID
    standard_code: str
    colnect_c_id: int | None = None
    colnect_url: str | None = None
    issue_url: str | None = None
    issue_key: str | None = None
    catalog_system: CatalogSystem | None = None
    catalog_ref: str | None = None
    design_no: str | None = None
    design_name: str | None = None
    issue_name: str | None = None
    face_value: Decimal | None = None
    currency: str | None = None
    issue_date: date | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    source_state: str
    fetch_status: str
    fetch_error: str | None = None
    last_fetched_at: datetime | None = None
    last_verified_at: datetime | None = None
    raw_snapshot_path: str | None = None
    raw_sha256: str | None = None
    parse_version: str | None = None
    note: str | None = None
    created_at: datetime
    updated_at: datetime


class StampCatalogApplyParse(BaseModel):
    """应用 `details.last_parse`；一页多枚时必须指定 design_no。"""

    design_no: str | None = Field(default=None, max_length=32)


class StampIdentifyRequest(BaseModel):
    force: bool = False


class StampIdentifyApply(BaseModel):
    c_id: int = Field(ge=1)
    replace_existing: bool = False


class StampIdentifyApplyResult(BaseModel):
    stamp: StampRead
    catalog: StampCatalogRead
    created: bool


class UnlinkedStampRead(BaseModel):
    """票墙「未编目」清单的一行：有票记录、但还没关联标准票。"""

    stamp_id: uuid.UUID
    card_id: uuid.UUID
    card_visit_date: date
    card_title: str | None = None
    catalog_ref: str | None = None
    name: str | None = None
    face_value: Decimal | None = None
    currency: str | None = None
    issue_date: date | None = None


# --------------------------------------------------------------------------- #
#  M3C-4：回填 / 合并 / 导出
# --------------------------------------------------------------------------- #


class BackfillGroup(BaseModel):
    key: str
    catalog_system: str | None = None
    catalog_ref: str
    stamp_count: int
    action: Literal["link", "create", "manual"]
    catalog_id: uuid.UUID | None = None
    standard_code: str | None = None
    design_name: str | None = None
    face_value: Decimal | None = None
    currency: str | None = None
    issue_date: date | None = None
    reason: str | None = None


class BackfillPreview(BaseModel):
    total_stamps: int
    groups: list[BackfillGroup]


class BackfillApplyResult(BaseModel):
    created: int
    linked: int
    skipped: int
    entries: list[StampCatalogRead]


class DuplicateGroup(BaseModel):
    key: str
    reason: str
    entries: list[StampCatalogRead]


class MergeRequest(BaseModel):
    target_id: uuid.UUID
    source_ids: list[uuid.UUID]

    @field_validator("source_ids")
    @classmethod
    def _non_empty_sources(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if not value:
            raise ValueError("至少给一个 source_id")
        return value


# --------------------------------------------------------------------------- #
#  卡片
# --------------------------------------------------------------------------- #


class CardBase(BaseModel):
    #: 跑戳日。求戳信场景填**寄出日**。
    visit_date: date
    #: 求戳信收回日
    received_date: date | None = None
    source: CardSource = CardSource.FIELD_TRIP
    status: CardStatus = CardStatus.COLLECTED
    title: str | None = Field(default=None, max_length=300)
    note: str | None = None
    image_asset_id: uuid.UUID | None = None
    tag_ids: list[uuid.UUID] = []

    @field_validator("visit_date", mode="after")
    @classmethod
    def _not_far_future(cls, v: date) -> date:
        # 未来的跑戳日只对"想要/在途"有意义；这里只挡住明显离谱的值。
        # 注意：不校验 postmark_date 与 visit_date 的关系 —— 求戳信可差数周，
        # 纪念戳可能是往年日期，强行相等是错的（docs/need/01 §3.2）。
        if v.year > date.today().year + 1:
            raise ValueError("跑戳日期过于遥远")
        return v


class CardCreate(CardBase):
    postmarks: list[PostmarkCreate] = []
    stamps: list[StampCreate] = []


class CardUpdate(BaseModel):
    visit_date: date | None = None
    received_date: date | None = None
    source: CardSource | None = None
    status: CardStatus | None = None
    title: str | None = Field(default=None, max_length=300)
    note: str | None = None
    image_asset_id: uuid.UUID | None = None
    tag_ids: list[uuid.UUID] | None = None


class CardRead(CardBase):
    model_config = ORM

    id: uuid.UUID
    postmarks: list[PostmarkRead] = []
    stamps: list[StampRead] = []
    tags: list[TagRead] = []
    created_at: datetime
    updated_at: datetime


class CardListItem(BaseModel):
    """列表项。刻意不含 postmarks/stamps 的完整对象 —— 列表页不需要。"""

    model_config = ORM

    id: uuid.UUID
    visit_date: date
    received_date: date | None = None
    source: CardSource
    status: CardStatus
    title: str | None = None
    image_asset_id: uuid.UUID | None = None
    #: 便于列表直接显示"哪个戳"而不用 N+1 查询
    postmark_summary: str | None = None
    stamp_summary: str | None = None
    is_color: bool = False
    postmark_type: PostmarkType | None = None
    place_names: list[str] = []


# --------------------------------------------------------------------------- #
#  查询
# --------------------------------------------------------------------------- #


class CardQuery(BaseModel):
    q: str | None = None
    visit_date_from: date | None = None
    visit_date_to: date | None = None
    place_id: uuid.UUID | None = None
    #: 为真时按 ``place.path`` 前缀匹配，把该地点的所有下级一并纳入
    #: （"按北京市筛选要带出所有区"）
    place_descendants: bool = False
    post_office_id: uuid.UUID | None = None
    postmark_type: PostmarkType | None = None
    source: CardSource | None = None
    status: CardStatus | None = None
    is_color: bool | None = None
    provenance: Provenance | None = None
    tag: str | None = None
    order_by: Literal["visit_date", "created_at", "title"] = "visit_date"
    order_desc: bool = True
    page: int = Field(default=1, ge=1)
    size: int = Field(default=24, ge=1, le=200)


# --------------------------------------------------------------------------- #
#  展览集 / 画廊（v1.1 M3，只读）
# --------------------------------------------------------------------------- #


class GalleryTimelineItem(BaseModel):
    """时间轴的一行 = 一张卡（卡片级聚合，避免前端每行再查）。

    刻意**不返回完整 PostmarkRead**：时间轴只需要"哪个戳、哪里、什么类型"，
    整份邮戳数据会让 30 行的响应膨胀，也让前端被迫理解它不用的字段。
    """

    card_id: uuid.UUID
    visit_date: date
    title: str | None = None
    #: 实拍卡图（`card.image_asset_id`）。缺图时前端显示占位，**不隐藏整行**。
    thumb_asset_id: uuid.UUID | None = None
    #: 从粗到细：["中华人民共和国", "福建省", "南平市"]
    place_path: list[str] = Field(default_factory=list)
    office_names: list[str] = Field(default_factory=list)
    postmark_count: int = 0
    #: 戳面原文，原样返回（证据，不做规范化）
    inscriptions: list[str] = Field(default_factory=list)
    postmark_types: list[str] = Field(default_factory=list)
    tag_names: list[str] = Field(default_factory=list)
    #: 是否已有电子卡（`card.scene` 非空）—— 决定行内是否显示小图标
    has_scene: bool = False
    source: CardSource


class GalleryTimeline(BaseModel):
    """`GET /api/gallery/timeline`。

    `without_cut` / `without_postmark` 是**显式计数**（00 §3.8、验收 G4/G7）：
    缺图、未建戳的记录仍然出现在 `items` 里，但数量必须能被看见 ——
    "少一行而且不说"是最坏的一种看起来对。
    """

    total: int
    #: 有卡片图、能直接上缩略图的卡
    with_cut: int
    #: 有记录但没有卡片图（技术性缺失）
    without_cut: int
    #: 一张戳都还没建的卡
    without_postmark: int
    items: list[GalleryTimelineItem]
    page: int
    size: int


class GalleryPostmarkItem(BaseModel):
    """戳墙的一块瓦片 = 一枚戳（`GET /api/gallery/postmarks`）。

    ⚠️ **`asset_id=null` 表示"没有抠图"**（技术性缺失），不是"实物缺票"。
    按 00 §3.8，戳必在票上，实物不存在空戳；墙上不出现"缺票/补票"字样。
    """

    postmark_id: uuid.UUID
    card_id: uuid.UUID
    #: 戳贴片；null = 这枚戳没有抠图
    asset_id: uuid.UUID | None = None
    inscription: str
    #: 来自 card.visit_date（戳本身没有"跑戳日"）
    visit_date: date
    #: "福建省 / 南平市 / 延平区"（不含国家）；没有结构化地点时为 null
    place_name: str | None = None
    office_name: str | None = None
    is_color: bool = False
    color_layers: int | None = None
    tag_names: list[str] = Field(default_factory=list)
    postmark_type: str


class GalleryPostmarkGroup(BaseModel):
    key: str
    label: str
    #: 本页里这一组的瓦片数（不是全库计数）
    count: int
    items: list[GalleryPostmarkItem]


class GalleryPostmarkWall(BaseModel):
    """`GET /api/gallery/postmarks`。

    `groups` 而不是平铺 `items`：墙需要组头。分页在**组前**做（SQL 取一页、
    内存分组），否则每组都要单独 count，页数一多就退化成 N 次查询。
    """

    total: int
    with_cut: int
    #: 有戳记录但没有抠图（技术性）
    without_cut: int
    groups: list[GalleryPostmarkGroup]
    page: int
    size: int


class GalleryStampItem(BaseModel):
    """票墙的一块瓦片 = 一枚票（`GET /api/gallery/stamps`）。

    `face_value` 是 `Decimal` → JSON 字符串（不转 float）。`face_label` 是
    已经按币种/数值整理好的人话（"40分" / "1.20元"），前端不要再算一遍。
    """

    stamp_id: uuid.UUID
    card_id: uuid.UUID
    #: 票贴片；null = 这枚票没有框出图（技术性，少见）
    asset_id: uuid.UUID | None = None
    catalog_ref: str | None = None
    catalog_system: str | None = None
    name: str | None = None
    #: Decimal → JSON 字符串
    face_value: Decimal | None = None
    currency: str | None = None
    #: "40分" / "1.20元" / "面值未知"
    face_label: str
    issue_date: date | None = None
    #: 来自 card.visit_date
    visit_date: date
    tag_names: list[str] = Field(default_factory=list)
    #: 贴片像素尺寸（瀑布流预留宽高比，避免图片加载后跳版）
    asset_width: int | None = None
    asset_height: int | None = None


class GalleryStampGroup(BaseModel):
    key: str
    label: str
    #: 本页里这一组的瓦片数（不是全库计数）
    count: int
    items: list[GalleryStampItem]


class GalleryStampWall(BaseModel):
    """`GET /api/gallery/stamps`。

    `cards_without_stamp` 是**票未录**（有戳记录、没有任何票记录），
    与 `stamps_without_cut`（有票记录、没有图）是两件事（00 §3.8）。
    """

    total: int
    with_cut: int
    #: 有票记录但没有贴片图（技术性）
    stamps_without_cut: int
    #: 有戳记录但没有任何票记录的卡 —— 主要缺口（票未录）
    cards_without_stamp: int
    #: 最近一张"缺票"的卡，供空态直接跳去框票（没有则为 null）
    first_card_without_stamp: uuid.UUID | None = None
    groups: list[GalleryStampGroup]
    page: int
    size: int


class GalleryMapProvince(BaseModel):
    """省份图的一格 = 一个省级行政区（`GET /api/gallery/map`）。

    **计数口径按卡去重**：一张卡若有多个跨省戳，它在每个省各出现一次，
    但 `card_count` 在每个省内只 +1。所以 `sum(card_count) >= total_cards`，
    前端不能把两者当同一个数（口径见 `docs/spec/m3/04-地图.md` §五）。

    `code2` 是 SVG 的映射键（`admin_code` 前两位）；缺失时为 null，
    前端画成浅灰而不是崩掉。`city_count` 只数真正的市级节点
    （level=2）；直辖市的区是 level=3，不会让"北京市 → 北京市"出现两次。
    """

    place_id: uuid.UUID
    name: str
    admin_code: str | None = None
    #: `350000` → `35`；SVG `<path data-code="35">`
    code2: str | None = None
    #: 该省里的卡数（`COUNT(DISTINCT card.id)`）
    card_count: int
    #: 该省里去重的市级 `place.id` 数（不含区/县）
    city_count: int = 0
    #: 该省里的戳数（`COUNT(DISTINCT postmark.id)`）
    postmark_count: int


class GalleryMapPoint(BaseModel):
    """真地图的预留点（形态 A）。**当前恒不返回**（`points` 为 null）。

    定义在这里是为了让"以后补经纬度"是加法：`lat/lng` 一旦有覆盖率，
    端点只要开始填 `points`，前端列表 / 筛选 / URL 都不用改。
    在没有坐标时返回 [] 或 0 会被误读成"这里没有戳"，所以现在必须是 null。
    """

    place_id: uuid.UUID
    lat: float
    lng: float
    #: 这个点上的卡数
    count: int


class GalleryMap(BaseModel):
    """`GET /api/gallery/map` —— 形态 B（省级清单 + 静态省份图）。

    * `total_cards` 与时间轴**同一组筛选条件**下的卡数；
    * `without_place` 是**没有任何带地点戳**的卡（不是"没坐标"）——
      包含"一张戳都没建"和"有戳但 place_id 为空"；
    * `points` 恒为 null；见 `GalleryMapPoint`。
    """

    total_cards: int
    #: 没有可归属省份的卡（无戳 / 戳无 place_id）。与"没有坐标"无关。
    without_place: int
    provinces: list[GalleryMapProvince]
    #: 为形态 A 预留；当前恒为 null，不要伪造坐标。
    points: list[GalleryMapPoint] | None = None


# --------------------------------------------------------------------------- #
#  统计
# --------------------------------------------------------------------------- #


class StatsOverview(BaseModel):
    cards: int
    postmarks: int
    stamps: int
    post_offices: int
    places_by_level: dict[str, int]
    first_visit_date: date | None = None
    last_visit_date: date | None = None


class CountBucket(BaseModel):
    key: str
    label: str | None = None
    count: int


class StatsByYear(BaseModel):
    buckets: list[CountBucket]


class StatsByPlace(BaseModel):
    level_label: str | None = None
    buckets: list[CountBucket]


# --------------------------------------------------------------------------- #
#  认证
# --------------------------------------------------------------------------- #


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    """改口令。

    **刻意不复用 ``LoginRequest``。** 复用会得到
    "``username`` = 新口令、``password`` = 旧口令" 这种反直觉的字段映射，
    调用方几乎必然写反，而且读代码的人无法从字段名看出意图。
    """

    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=200)


class MeResponse(BaseModel):
    username: str
    last_login_at: datetime | None = None


# --------------------------------------------------------------------------- #
#  词表（供前端渲染下拉框，避免前端硬编码）
# --------------------------------------------------------------------------- #


class VocabOption(BaseModel):
    value: str
    label: str


class VocabResponse(BaseModel):
    model_config = ORM

    postmark_types: list[VocabOption]
    primary_postmark_types: list[str]
    card_sources: list[VocabOption]
    card_statuses: list[VocabOption]
    catalog_systems: list[VocabOption]
    provenances: list[VocabOption]
    tag_categories: list[VocabOption]
    level_labels: list[str]
    face_value_suggestions: list[dict[str, str]]


#: 中文标签，供 /api/vocab 返回给前端
LABELS: dict[str, dict[str, str]] = {
    "postmark_type": {
        "daily": "日戳",
        "scenic": "风景戳",
        "commemorative": "纪念戳",
        "propaganda": "宣传戳",
        "postal_sub": "邮政副戳",
        "meter": "邮资机戳",
        "business": "营业戳",
        "agency": "代办所戳",
        "temporary": "临时邮政局戳",
        "cultural": "文化日戳",
        "other": "其他",
    },
    "card_source": {
        "field_trip": "实地跑戳",
        "mail_request": "求戳信",
        "exchange": "交换",
        "purchase": "购入",
        "other": "其他",
    },
    "card_status": {
        "collected": "已收",
        "pending": "在途",
        "wishlist": "想要",
    },
    "catalog_system": {
        "china": "中国志号",
        "scott": "Scott",
        "michel": "Michel",
        "other": "其他",
    },
    "provenance": {
        "real_scan": "真实扫描",
        "generated": "生成/美化",
        "third_party": "第三方来源",
    },
    "tag_category": {
        "theme": "主题",
        "quality": "品相",
        "event": "事件",
        "source": "来源",
    },
}

# --------------------------------------------------------------------------- #
#  识别（P3 阶段 0）
# --------------------------------------------------------------------------- #


class RecognizedFieldRead(BaseModel):
    """一个字段的识别结果**加上它的决策**。

    ``decision`` 是给 UI 上色/排队的**唯一依据** —— 而不是 ``confidence``。
    理由是实测出来的：OCR 给「福建南火」的置信度是 **0.90**，
    真值却是「福建南平」。**只看置信度会把错值显示成可信。**
    所以决策由 ``recognition.review.decide`` 给出，它额外查了词表。
    """

    field_name: str
    value: str
    confidence: float
    #: ``auto_accept`` / ``needs_review`` / ``reject``
    decision: str
    #: 为什么是这个决策（**直接显示给用户**）
    reason: str = ""
    pipeline_version: str = ""
    #: 命中行政区划时，**从区划树里补出来的层级链**（从粗到细）。
    #:
    #: 例：值 ``福建南平`` → ``["中华人民共和国", "福建省", "南平市"]``；
    #: 值 ``南宁`` → ``["中华人民共和国", "广西壮族自治区", "南宁市"]``。
    #:
    #: ⚠️ **只有最后一级是 OCR 读到的**，上面几级是数据库按父子关系补的。
    #: 用户要的"智能识别它属于哪个省"就是这一段；但做 A8 的 `correction`
    #: 标注时不能把补出来的当成人改的（OCR 原样在 ``value`` 里）。
    #:
    #: 空列表 = 没命中区划（或该字段不参与区划匹配，如 ``date``）。
    place_path: list[str] = Field(default_factory=list)


class ReviewItemRead(BaseModel):
    """校对队列里的一项。"""

    field_name: str
    value: str
    confidence: float
    why: str


class RecognizedCircleRead(BaseModel):
    """定位到的邮戳外圆（原图坐标）。

    给 UI 画出来 —— 因为"圆定位对不对"是最难从字段值看出来的失效模式：
    实测有一张卡的圆落在了**明信片印刷抬头**上，字段全错但置信度不低。
    把圆画出来，这种错一眼可见。
    """

    cx: float
    cy: float
    r: float
    #: 半径 / 卡宽，用于判断是否为**卡尺度**的误检（正常约 0.23）
    radius_over_card_width: float = 0.0


class CardLinksRead(BaseModel):
    """这张卡**会自动挂到哪个 id**（识别页显示；确认时按同一条规则落库）。

    用户 2026-10-12："尽管不填【结构化】，只要能在【局名】【地名】命中，
    就能补充到结构化字段上面去"。服务端确认时**本来就在做**这件事，
    这里是把结果**提前告诉界面**（`docs/ok/18` §3.3）。

    ⚠️ 与 `fields[place].place_path` 的分工：`place_path` 是**词表判定**
    （"这个地名属于哪个省"），这里给的是**要挂的那个 id**（以及挂不上的原因）。

    `ORM` 配置是必须的：路由拿到的是 `postmark_assembly.CardLinks` **dataclass**，
    没有它 `model_validate` 会以 `model_type` 报错（2026-10-12 实测 500）。
    """

    model_config = ORM

    place_id: uuid.UUID | None = None
    place_name: str | None = None
    office_id: uuid.UUID | None = None
    office_name: str | None = None
    #: 局所属地点（界面显示成"福屿邮政所（福州市）"）
    office_place_name: str | None = None
    #: 人话说明：挂到哪、或为什么没挂上
    note: str = ""


class RecognizedCardRead(BaseModel):
    """一张卡的识别结果。"""

    index: int
    bbox: tuple[int, int, int, int]
    #: 检测到的四边形（原图坐标，4 个点）。UI 用它画**实际**边界。
    quad: list[tuple[float, float]] = Field(default_factory=list)
    card_size: tuple[int, int]
    #: 尺寸与期望卡型差得很远时为 True（**只标注，不丢弃** —— 异形卡是合法的）
    size_suspect: bool = False
    postmark_type: str = ""
    postmark_reason: str = ""
    circle: RecognizedCircleRead | None = None
    #: 全部字段（含被 reject 的）—— UI 按 ``decision`` 决定怎么显示
    fields: list[RecognizedFieldRead] = Field(default_factory=list)
    #: 需要人工确认的项
    queue: list[ReviewItemRead] = Field(default_factory=list)
    #: OCR 原文行，供校对时对照
    ocr_lines: list[tuple[str, float]] = Field(default_factory=list)
    #: ``local`` 或 ``vlm`` —— **必须回传**，云端结果不可复现
    field_source: str = "local"
    #: **这一张卡的几何**来自哪条通道：``local`` 或 ``vlm``（B 方案的云端角点兜底）。
    #:
    #: ⚠️ 与 `field_source` 是两件事，刻意分开：完全可能"几何本地给的、字段云端读的"。
    #: 界面标这个标记的唯一目的是回答"这张要不要多看一眼"，而那个问题的答案
    #: 取决于**几何**是谁给的（一张歪的卡图不会报错，只会让后面所有读数都歪）。
    geometry_source: str = "local"
    notes: list[str] = Field(default_factory=list)
    #: 结构化地点/邮局的**自动解析结果**（给界面显示 + 预填，见 `CardLinksRead`）
    links: CardLinksRead = Field(default_factory=CardLinksRead)


class RecognizedPageRead(BaseModel):
    """一页（一张扫描图）的识别结果。

    ## 这个端点**不写库**

    spec A6 要求"所有自动结果**无一例外**经过人工确认才落库"。
    所以识别只返回**预览**，落库发生在用户确认之后的另一个端点里。
    这样 A6 是**结构上不可能违反**的，而不是靠人记得别写。
    """

    pipeline_version: str
    work_scale: float
    work_size: tuple[int, int]
    card_count: int
    cards: list[RecognizedCardRead] = Field(default_factory=list)
    #: 各级降级/异常说明（**不静默**）
    stage_notes: list[str] = Field(default_factory=list)
    #: 本次是否用了云端
    used_vlm: bool = False
    #: 这一页的**几何**最终来自哪条通道：``local`` 或 ``vlm``。
    #: ``vlm`` = 云端角点兜底**被触发并且真的用上了**（不是"调过云端"）。
    geometry_source: str = "local"
    #: 当前生效的几何模式（`split.GEOMETRY_MODE`）：``local_first``（B）或
    #: ``cloud_first``（A）。**回传它**是为了让界面与排障直接看到"现在跑的是
    #: 哪条路"，而不用翻代码 —— 两条路的成本、"该不该信这页几何"都不同。
    geometry_mode: str = ""


# --------------------------------------------------------------------------- #
#  确认落库（P3 · A6/A7/A8 的落点）
# --------------------------------------------------------------------------- #


class ConfirmDefaults(BaseModel):
    """每张卡的默认归类。

    为什么必须有：`Card.source` 与 `Card.status` 是 **NOT NULL** 的列，
    而它们是**人的选择**、不是识别输出 —— 所以请求必须带。
    """

    # 复用既有的枚举（**不要**再定义一套 Literal —— 那会成为第二个真相来源，
    # 而且并行的类型名会遮蔽它、让 mypy 报 "Cannot assign to a type"）
    source: CardSource = CardSource.MAIL_REQUEST
    status: CardStatus = CardStatus.COLLECTED


class ConfirmFieldIn(BaseModel):
    """一个字段的**机器值**与**最终值**。

    两者都给，是因为：
      * 机器值 -> 写 L2 `observation`（无论人有没有改）
      * 最终值 ≠ 机器值 -> 额外写 L3 `correction`

    这样"人改了什么"是**请求本身**说明的，而不是服务端去比对新旧行 ——
    后者在 A7 允许多批 observation 并存时会误判。
    """

    field_name: str
    #: 机器读到的值（可能为空：机器没读到）
    machine_value: str | None = None
    machine_confidence: float | None = None
    machine_decision: str = ""
    #: 人确认后的值。`None` 表示"这一项不采纳"（与空串不同）
    final_value: str | None = None
    pipeline_version: str = ""
    model_name: str | None = None
    model_version: str | None = None
    bbox: dict[str, Any] | None = None


class ConfirmCardIn(BaseModel):
    index: int
    #: 屏蔽（不录入）。被屏蔽的卡**完全不动库** —— 识别端点本来就没写任何东西，
    #: 所以"不录入"不需要删
    excluded: bool = False
    #: **跑戳日**。识别给出日期时由前端预填，但它仍是**人的确认值**。
    #: 为空则拒绝 —— 服务端不会替用户编一个日期（会污染按日期的统计）
    visit_date: date | None = None
    title: str | None = None
    note: str | None = None
    fields: list[ConfirmFieldIn] = Field(default_factory=list)
    #: 人已经选定/改定的地点与邮局（`docs/ok/16` §5.3）。**都可以不给** ——
    #: 不给就由服务端按"与识别判定同一条规则"解析 `place` / `office` 的文本，
    #: 解析不出来就留空（`place_id` / `post_office_id` 本来就是索引，允许为空）。
    #: 将来扫识别页接上选择器（第 6 条）之后，人选的 id 就从这里进来。
    place_id: uuid.UUID | None = None
    post_office_id: uuid.UUID | None = None
    #: 要挂到**这枚戳**上的 tag（`tag_link.target_type='postmark'`）。
    #: ⚠️「主题邮局」**不在这里**：它是服务端按戳面文字自动判的
    #: （`postmark_assembly.looks_like_theme_office`），避免前端各写一份判据。
    tag_ids: list[uuid.UUID] = Field(default_factory=list)
    #: 这张卡的**四边形**（**工作尺度**坐标，4 个点）。服务端据此裁出"切好的那张卡"
    #: 并落成派生资产（`docs/ok/18` §3.5）。不给 = 不落图，老客户端照常可用。
    #: ⚠️ 坐标是工作尺度，所以要配 `ConfirmSubmission.work_scale` 才能回到原图。
    quad: list[tuple[float, float]] | None = None
    #: 戳圆 **(cx, cy, r)**（**工作尺度**）—— 服务端据此从**整页原图**抠出戳贴片
    #: （`kind='postmark_cut'`）挂到 `Postmark.image_asset_id`（`docs/ok/19` §三 3.1）。
    #: 不给 = 不抠图，老客户端照常可用。
    circle: tuple[float, float, float] | None = None


class ConfirmSubmission(BaseModel):
    """确认提交。**这是唯一会写库的识别相关请求。**"""

    asset_id: uuid.UUID
    pipeline_version: str = ""
    defaults: ConfirmDefaults = Field(default_factory=ConfirmDefaults)
    cards: list[ConfirmCardIn] = Field(default_factory=list)
    #: 该资产下已有卡时，必须显式带 true 才继续（挡双击提交）
    confirm_again: bool = False
    #: 识别时的工作尺度（`RecognizedPageRead.work_scale`）：把载荷里的工作尺度
    #: 坐标（`quad`）换算回**原始分辨率**时要用它。缺省 = 不落图。
    work_scale: float | None = None


class ConfirmResult(BaseModel):
    created_cards: int
    excluded_cards: int
    #: 写入的 L2 observation 行数
    observations: int
    #: 写入的 L3 correction 行数（**只在人真改了的时候才有**）
    corrections: int
    #: **写入的 L3 `postmark` 行数**（`docs/ok/16` §五：确认时建戳）。
    #: 正常等于 `created_cards` —— 一卡一戳（多戳到卡片页补，见 `ok/16` §二）。
    postmarks: int = 0
    #: 解析结果的人话说明（地点/局名匹配没匹配上、自动打了什么 tag）。
    #:
    #: ⚠️ 为什么**不写进** `postmark.note`：那一列的语义是"人写给人看的备注，
    #: 机器永不自动填"（`db/models.py`）。所以解析结果只能回报给调用方，
    #: 由界面转达给人 —— 人再决定要不要自己写进备注。
    notes: list[str] = Field(default_factory=list)
    card_ids: list[uuid.UUID] = Field(default_factory=list)


AnnotatedUUID = Annotated[uuid.UUID, Field()]


# --------------------------------------------------------------------------- #
#  电子卡：模板与场景（v1.1 M1）
# --------------------------------------------------------------------------- #


class TemplateRead(BaseModel):
    """模板列表项：**不带整份场景**（列表页不需要，且场景可能几百个槽位）。"""

    #: 内置模板形如 ``builtin:blank-card``；用户模板是 UUID。
    id: str
    name: str
    scope: Literal["builtin", "user"]
    canvas: dict[str, Any] = Field(default_factory=dict)
    slot_count: int = 0


class TemplateDetail(TemplateRead):
    scene: dict[str, Any]


class CardSceneRead(BaseModel):
    """卡片场景。

    ``scene`` 为 null 表示**这张卡还没有电子卡** —— 与"有一份空场景"是两件事，
    渲染器必须能区分（`docs/spec/v1.1` §2.2）。
    """

    card_id: uuid.UUID
    scene: dict[str, Any] | None = None
    template_id: uuid.UUID | None = None


class CardSceneWrite(BaseModel):
    scene: dict[str, Any]


class ApplyTemplateIn(BaseModel):
    #: ``builtin:<name>`` 或用户模板 UUID。
    template_id: str
    #: **默认预览，不写库**。写库必须显式 dry_run=false。
    dry_run: bool = True


class ApplyTemplateOut(BaseModel):
    scene: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    #: dry_run=false 时才有值。
    saved: bool = False


class StampCutIn(BaseModel):
    """在**整卡图**上框一次邮票。

    坐标是**归一化的 0–1**（相对 `card.image_asset_id` 的宽高），不是像素：
    前端拿到的是缩略图/工作尺度，用它自己的像素坐标会随显示尺寸变化 ——
    归一化把"显示多大"这件事彻底移出协议。
    """

    box: tuple[float, float, float, float]
    #: 不传 = 这张卡还没有票，服务端建一条字段为空的 `stamp`。
    stamp_id: uuid.UUID | None = None


class StampCutOut(BaseModel):
    stamp_id: uuid.UUID
    asset_id: uuid.UUID
    sha256: str
    width: int
    height: int
    #: True = 这次新建了一条 `stamp` 行（V1.0 的确认从不建票）。
    stamp_created: bool = False
