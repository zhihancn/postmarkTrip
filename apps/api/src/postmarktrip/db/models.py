"""数据模型。

设计要点（对应 docs/spec/v0.1 与 docs/ok/01 §2 的三层架构）：

* **枚举一律用文本列**，取值集中在 ``postmarktrip.domain.vocab`` 并由 API 层校验。
  邮戳种类明确需要持续扩充（docs/ok/03 §1.3 指出中国没有统一的官方戳种分类），
  用 PG 原生 enum 意味着每加一个取值都要写迁移。文本 + 集中词表更合适。
* **observation / correction 现在就建表**，虽然 v0.1 不写入。它们定义了
  L2(机器观察) / L3(人工确认) 的分层边界，事后加会牵动大量代码。
* **asset 不可变**：原始扫描件是唯一不可再生的证据。
  storage 层不提供覆盖/删除原始件的方法（编译期约束，不是纪律）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from postmarktrip.db.base import Base, SoftDelete, Timestamps, UUIDPrimaryKey

# --------------------------------------------------------------------------- #
#  账户
# --------------------------------------------------------------------------- #


class AppUser(Base, UUIDPrimaryKey, Timestamps):
    """单用户登录。表名用 app_user —— ``user`` 是 SQL 保留字。"""

    __tablename__ = "app_user"

    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- #
#  字典：行政区划 / 邮局 / 标签
# --------------------------------------------------------------------------- #


class Place(Base, UUIDPrimaryKey, Timestamps):
    """行政区划：自引用树，**不硬编码层级数**。

    中国的"省-市-县-乡-村"、美国的"state-county-city"、日本的"都道府県-市-区"
    都能装进同一张表。直辖市如实存为「省级 → 县级」，中间层留空——
    不做「北京市-北京市-东城区」的取巧（见 docs/need/03 §五）。
    """

    __tablename__ = "place"

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("place.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(200))
    #: 层级序号，0 = 国家。用 level + level_label 而非固定列，才能容纳各国不同的层级。
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: 层级名称，如"省""地级市""市辖区""都道府県"。UI 只显示这个，不显示"第 N 级"。
    level_label: Mapped[str | None] = mapped_column(String(40))
    country_code: Mapped[str | None] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    admin_code: Mapped[str | None] = mapped_column(String(32))  # 官方行政区划代码
    #: 物化路径，如 ``/CN/110000/110101/``。用于 O(1) 子树查询
    #: （"按北京市筛选要带出所有区"）。由应用层维护，见 domain/place_tree.py。
    path: Mapped[str | None] = mapped_column(String(500))
    lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    #: 数据来源与获取时间 —— 公共数据署名要求（见 public-data/README.md）
    source: Mapped[str | None] = mapped_column(String(200))
    source_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    parent: Mapped[Place | None] = relationship(remote_side="Place.id", back_populates="children")
    children: Mapped[list[Place]] = relationship(
        back_populates="parent", order_by="Place.name", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_place_parent_name", "parent_id", "name"),
        Index("ix_place_country_level", "country_code", "level"),
        # text_pattern_ops 让 ``path LIKE '/CN/11/%'`` 能走索引（C locale 下的前缀匹配）
        Index("ix_place_path_prefix", "path", postgresql_ops={"path": "text_pattern_ops"}),
        Index(
            "ix_place_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )


class PostOffice(Base, UUIDPrimaryKey, Timestamps):
    """邮局（支局 / 邮政所 / 主题邮局）。"""

    __tablename__ = "post_office"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    place_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("place.id", ondelete="SET NULL")
    )
    postal_code: Mapped[str | None] = mapped_column(String(16))
    #: 主题邮局从 tag 升格为结构化属性（D8），这样才能回答"我跑过多少主题邮局"
    is_theme: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    theme_name: Mapped[str | None] = mapped_column(String(100))
    #: 别名/简称，用于 OCR 结果的模糊匹配（局名字典约束是低成本高收益的外挂）
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    source: Mapped[str | None] = mapped_column(String(200))
    source_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: 备注。**人写给人看的自由文本**，机器永不自动填（见下）。
    #:
    #: 为什么机构也需要一个备注（用户 2026-10-11 提的）：
    #: 目录数据有**对不上的时候**，而"想填什么填什么"比替用户做决定安全。实测：
    #:
    #:   * 同一家所两个来源的邮编不同（`三清山邮政所` 334717 vs 334703）
    #:   * 目录里**查不到**某些戳上写的机构（`三清山主题邮局` —— 只有
    #:     `三清山邮政所`）——「生日快乐」那枚戳的下弧就写着前者的名字
    #:   * 名称在目录里带状态（你的那份有 384 条 `（暂停）`、4 条 `（撤销）`）
    #:
    #: ⚠️ **OCR 永不自动写这一列。** 与 `Postmark.note` 同一个原则：
    #: 机器填进去只会污染人写的东西（它是"解释与存疑"的落点，不是数据字段）。
    note: Mapped[str | None] = mapped_column(Text)

    place: Mapped[Place | None] = relationship()

    __table_args__ = (
        Index("ix_post_office_place", "place_id"),
        Index(
            "ix_post_office_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )


class Tag(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "tag"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    #: theme / quality / event / source … 用于 UI 分组
    category: Mapped[str | None] = mapped_column(String(32))
    color: Mapped[str | None] = mapped_column(String(9))


class TagLink(Base, UUIDPrimaryKey, Timestamps):
    """多态关联：tag 可挂到 card / postmark / stamp。

    没有用外键约束 target_id —— 多态关联的固有代价。代价是删除目标时要
    手动清理关联，这一点由 domain 层的服务方法统一处理。
    """

    __tablename__ = "tag_link"

    tag_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tag.id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(24), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    tag: Mapped[Tag] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("tag_id", "target_type", "target_id"),
        Index("ix_tag_link_target", "target_type", "target_id"),
    )


# --------------------------------------------------------------------------- #
#  资产
# --------------------------------------------------------------------------- #


class Asset(Base, UUIDPrimaryKey, Timestamps):
    """文件资产。内容寻址（sha256），原始件不可变。

    ``is_immutable=True`` 的资产是 **L0 层**：任何处理都只能派生新资产，
    绝不写回原资产。这一约束由 storage 模块的 API 形状强制保证。
    """

    __tablename__ = "asset"

    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    mime: Mapped[str] = mapped_column(String(100), nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    dpi: Mapped[int | None] = mapped_column(Integer)
    #: 相对 assets_dir 的路径，形如 ``ab/cd/<sha256>.jpg``
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    #: scan_raw | photo | postmark_cut | stamp_cut | thumbnail | derived
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="derived")
    is_immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: **派生物指回原始件**（迁移 `0003_asset_parent`，`docs/ok/18` §3.5）。
    #:
    #: L0 原始件为 NULL；卡片裁切 / 戳抠图这类派生物必须填 —— 否则"这张图是从
    #: 哪张原图出来的"就断了，而"原始件不可变、处理只能派生新资产"是 storage
    #: 的设计前提（见上面 `is_immutable` 那段）。
    parent_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("asset.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    original_filename: Mapped[str | None] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_asset_kind", "kind"),)


class IngestRecord(Base, UUIDPrimaryKey, Timestamps):
    """每次采集的来源记录（上传 / 投递目录）。

    存在两个目的：
    1. 幂等 —— 同一个文件重复投递不会重复处理（``source`` + ``sha256`` 唯一）
    2. 审计 —— 这个资产是从哪来的
    """

    __tablename__ = "ingest_record"

    source: Mapped[str] = mapped_column(String(32), nullable=False)  # upload | inbox
    original_filename: Mapped[str | None] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("asset.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="received")
    error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    asset: Mapped[Asset | None] = relationship()

    __table_args__ = (
        UniqueConstraint("source", "sha256", name="uq_ingest_record_source_sha256"),
        Index("ix_ingest_record_status", "status"),
    )


# --------------------------------------------------------------------------- #
#  核心记录
# --------------------------------------------------------------------------- #


class Card(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    """邮戳卡 —— 核心实体。

    以 Card 而非 Postmark 为核心，因为一张卡是**物理实物**、是你收藏与拍照的
    单位、也是虚拟卡片重建的单位。
    """

    __tablename__ = "card"

    #: 跑戳日（你的行为时间）。求戳信场景填**寄出日**。
    visit_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: 求戳信收回日（D14 要求：寄出 + 盖戳 + 收到 三个时间）
    received_date: Mapped[date | None] = mapped_column(Date)
    #: field_trip | mail_request | exchange | purchase | other
    source: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    #: collected | pending | wishlist
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(300))
    note: Mapped[str | None] = mapped_column(Text)
    image_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("asset.id", ondelete="SET NULL")
    )
    #: 电子卡的**场景快照**（`docs/spec/v1.1` §2.2）。为空 = 这张卡还没有电子卡。
    #:
    #: 存快照而不是只存 `template_id`：模板是会变的，只存 id 的话改一次模板
    #: 会让所有旧卡片的电子卡**跟着变** —— 与 `docs/ok/04` §4.2「引用 + 快照」
    #: 同一条理由。`template_id` 只记「来源」，权威是 `scene`。
    scene: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: 场景来自哪个模板。删模板不删卡片（SET NULL）。
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("template.id", ondelete="SET NULL")
    )

    postmarks: Mapped[list[Postmark]] = relationship(
        back_populates="card",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Postmark.created_at",
    )
    stamps: Mapped[list[Stamp]] = relationship(
        back_populates="card",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Stamp.created_at",
    )

    __table_args__ = (
        Index("ix_card_visit_date_status", "visit_date", "status"),
        Index(
            "ix_card_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_card_note_trgm",
            "note",
            postgresql_using="gin",
            postgresql_ops={"note": "gin_trgm_ops"},
        ),
    )


class Postmark(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    """邮戳。

    实测证据（docs/need/03 §三.6）：一张卡上 99.99% 只有一个戳，但模型用 1:N ——
    因为"一张 A4 纸贴满邮票盖满邮戳"是真实存在的玩法，而 1:N 并不比 1:1 更难写。
    """

    __tablename__ = "postmark"

    card_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("card.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: daily | scenic | commemorative | propaganda | postal_sub | meter
    #: business | agency | temporary | cultural | other
    postmark_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    #: 戳面原文，原样录入、不做任何规范化。这是**证据**，也是用户最在意的东西。
    #: 而 place_id 只是**索引**，允许为空 —— 因为"中关村""珠峰"不是行政区。
    inscription: Mapped[str] = mapped_column(Text, nullable=False)

    #: 戳面日期。实测有 3 种格式变体：2026.09.25.15 / 2026.10.01.09. / 2026.09.29
    postmark_date: Mapped[date | None] = mapped_column(Date, index=True)
    #: 日期原文，保留原格式（字钉缺字、格式异常时仍可追溯）
    postmark_date_text: Mapped[str | None] = mapped_column(String(64))
    #: 时间码（日戳与风景戳有，主题邮局戳无）
    time_code: Mapped[str | None] = mapped_column(String(16))
    #: 戳号，位于底弧、跟在局名之后（如「建溪 1」「四号航站楼 2」）
    postmark_no: Mapped[str | None] = mapped_column(String(16))

    post_office_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("post_office.id", ondelete="SET NULL"), index=True
    )
    place_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("place.id", ondelete="SET NULL"), index=True
    )

    #: 彩色戳。官方通用叫法是"彩（套）色邮戳 / 套色文化日戳"。
    is_color: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 套色戳的色层数 —— 套色戳由多色戳具按顺序叠加加盖而成（如太原"巳巳如意"）。
    color_layers: Mapped[int | None] = mapped_column(SmallInteger)

    #: real_scan | generated | third_party
    #: **语义是"分区"而不是"水印"**（D24）：区分"我真跑过"与"生成美化"，
    #: 从而让画廊能一键只看真实跑戳。无任何导出限制。
    provenance: Mapped[str] = mapped_column(String(24), nullable=False, default="real_scan")

    #: 盖印质量是否满意（服务"追求完美主义者换模板"的场景）
    is_clean: Mapped[bool | None] = mapped_column(Boolean)
    image_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("asset.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)

    card: Mapped[Card] = relationship(back_populates="postmarks")
    place: Mapped[Place | None] = relationship()
    post_office: Mapped[PostOffice | None] = relationship()

    __table_args__ = (
        # 戳墙按"有贴片"过滤（`docs/spec/m3/00` §3.7）。部分索引只收非空行：
        # 没有贴片的戳占多数时，比全列索引小得多。
        Index(
            "ix_postmark_image_asset",
            "image_asset_id",
            postgresql_where=text("image_asset_id IS NOT NULL"),
        ),
        Index(
            "ix_postmark_inscription_trgm",
            "inscription",
            postgresql_using="gin",
            postgresql_ops={"inscription": "gin_trgm_ops"},
        ),
        CheckConstraint("color_layers IS NULL OR color_layers >= 1", name="color_layers_positive"),
    )


class StampCatalog(Base, UUIDPrimaryKey, Timestamps):
    """标准邮票库（M3C）：一枚票的"目录身份"，不是用户手上那一枚。

    用户票 `stamp` 是**实例**（贴片、卡、品相、备注）；这里存**版本身份**
    （志号、图序、图名、面值、发行日、齿孔…）。两条通过 `stamp.catalog_id` 关联。

    3 个"人工维护入口"：
    * `standard_code`：人读编号（生成后冻结；UUID 主键才是外键）
    * `colnect_url` / `colnect_c_id`：候选来源/条目页
    * `issue_url`：官方发行页（权威字段来源，一套多枚可重复）

    其余字段允许为空：解析不出来就留空，人工复核后再补。
    """

    __tablename__ = "stamp_catalog"

    #: 人读编号，如 cn-2023-1-d02；生成后不改（外键用 id）
    standard_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    #: Colnect 的 c_id（目录条目）；p_id 是图片，不是身份
    colnect_c_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    colnect_url: Mapped[str | None] = mapped_column(Text)
    issue_url: Mapped[str | None] = mapped_column(Text)
    #: issue_url 归一化（host+path，去 query/fragment），用于查重
    issue_key: Mapped[str | None] = mapped_column(String(500), index=True)

    #: china | scott | michel | other
    catalog_system: Mapped[str | None] = mapped_column(String(24))
    catalog_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    #: 图序，如 (2-1)；原样保存，归一化后续再加
    design_no: Mapped[str | None] = mapped_column(String(32))
    #: 图名（单枚图案名）
    design_name: Mapped[str | None] = mapped_column(String(200))
    #: 套名（如"癸卯年"）
    issue_name: Mapped[str | None] = mapped_column(String(200))
    face_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    issue_date: Mapped[date | None] = mapped_column(Date)

    #: 齿孔/版别/设计者/印刷厂/发行量…（`docs/spec/m3/M3C` §5.2）
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: draft | partial | verified | stale
    source_state: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    #: idle | queued | running | ok | error（M3C-3 抓取状态；与 source_state 分开）
    fetch_status: Mapped[str] = mapped_column(String(16), nullable=False, default="idle")
    fetch_error: Mapped[str | None] = mapped_column(Text)
    fetch_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 原始页面快照（L0 证据）与解析版本
    raw_snapshot_path: Mapped[str | None] = mapped_column(String(500))
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    parse_version: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_stamp_catalog_issue_url", "issue_url"),
        Index("ix_stamp_catalog_design_name", "design_name"),
    )


class Stamp(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    """所用邮票。

    "钱币信息" = 面值 + 币种（D15）。用 Numeric 而非浮点 —— 8分 就是 0.08，
    浮点会让它变成 0.08000000000000002。
    """

    __tablename__ = "stamp"

    card_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("card.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: 志号，如 "普30"、"2023-1"、"特56"
    catalog_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    #: 关联的标准票（M3C）；null = 还没补录标准票（票墙的「未编目」）
    catalog_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("stamp_catalog.id", ondelete="SET NULL"), index=True
    )
    #: china | scott | michel | other
    catalog_system: Mapped[str | None] = mapped_column(String(24))
    name: Mapped[str | None] = mapped_column(String(200))
    face_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    #: ISO 4217，如 CNY
    currency: Mapped[str | None] = mapped_column(String(3))
    issue_date: Mapped[date | None] = mapped_column(Date)
    image_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("asset.id", ondelete="SET NULL")
    )
    #: 邮票识别（M3C-5）：只保留最近一次候选；不自动改 catalog_id
    identify_status: Mapped[str] = mapped_column(String(16), nullable=False, default="idle")
    identify_error: Mapped[str | None] = mapped_column(Text)
    identify_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    identify_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    note: Mapped[str | None] = mapped_column(Text)

    card: Mapped[Card] = relationship(back_populates="stamps")

    __table_args__ = (
        # 票墙同上（`docs/spec/m3/00` §3.7）
        Index(
            "ix_stamp_image_asset",
            "image_asset_id",
            postgresql_where=text("image_asset_id IS NOT NULL"),
        ),
        Index(
            "ix_stamp_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        CheckConstraint("face_value IS NULL OR face_value >= 0", name="face_value_non_negative"),
    )


# --------------------------------------------------------------------------- #
#  识别分层（v0.1 建表不写入）
# --------------------------------------------------------------------------- #


class Observation(Base, UUIDPrimaryKey, Timestamps):
    """L2 层：机器的一次"看法"。

    同一张图可以有 N 次识别的 N 组结果，互不覆盖。这样模型升级后可以对
    历史扫描件**一键重跑**并把新旧结果对比后择优采纳 —— 如果识别结果
    直接写进正式表，历史数据就永远无法享受模型升级的红利。
    """

    __tablename__ = "observation"

    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("asset.id", ondelete="CASCADE")
    )
    card_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("card.id", ondelete="CASCADE")
    )
    #: 识别管线的版本（用于区分"不同管线跑出来的结果"）
    pipeline_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(64))
    model_version: Mapped[str | None] = mapped_column(String(64))
    #: 字段名，如 postmark_date / inscription / face_value
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    #: 位置信息。注意：不要依赖多模态模型的 bbox（docs/ok/02 §4.3）
    bbox: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_observation_card_field", "card_id", "field_name"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
    )


class Correction(Base, UUIDPrimaryKey, Timestamps):
    """L2 → L3 的人工修正日志。

    **这是本项目长期最有价值的资产。** 每条"机器说是 X、人改成 Y"都是标注数据；
    积累一年后可以据此微调一个专属于本机的邮戳识别模型。
    所以它是**一等公民**，不是用完即弃的日志。
    """

    __tablename__ = "correction"

    card_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("card.id", ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    machine_value: Mapped[str | None] = mapped_column(Text)
    human_value: Mapped[str | None] = mapped_column(Text)
    machine_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    observation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("observation.id", ondelete="SET NULL")
    )
    corrected_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
    corrected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_correction_card_field", "card_id", "field_name"),)


# --------------------------------------------------------------------------- #
#  模板（v1.1：电子卡）
# --------------------------------------------------------------------------- #


class Template(Base, UUIDPrimaryKey, Timestamps):
    """模板 = 一份**具名的场景图**（`docs/spec/v1.1` §2.1）。

    * 内置模板**不在这张表里** —— 它们是 `public-data/templates/*.json`，
      随仓库分发、只读（`docs/spec/v1.1` §2.2）。加一个内置模板不该是一次 DDL。
    * 这张表只放**用户建的**模板（`scope='user'`），M2 的编辑器往里写。

    `scene` 用 JSONB 而不是拆成槽位表：场景图是**契约**（`contract/scene.schema.json`），
    拆表会让"导出/分享一份场景"变成一次多表 JOIN，也让 schema 版本迁移变得难。
    """

    __tablename__ = "template"

    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    #: builtin（保留，实际内置走文件）| user
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    scene: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (Index("ix_template_scope", "scope"),)
