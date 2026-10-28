"""集中词表。

**为什么枚举不放在数据库里**：邮戳种类明确需要持续扩充——docs/ok/03 §1.3 指出
中国不存在覆盖全部戳种的统一官方分类标准，只能自建枚举。用 PG 原生 enum 意味着
每加一个取值都要写一次迁移。文本列 + 这里的集中词表 + API 层校验更合适。

代价：绕过 API 直接写库时没有约束。这个代价可接受（本系统只有自己的 API 会写库）。
"""

from __future__ import annotations

from enum import StrEnum


class PostmarkType(StrEnum):
    """戳种。依据集邮界通用叫法与两份官方标准整理（docs/ok/03 §1.3）。

    注意：**不存在官方的完整分类目录**，这个枚举是自建的、可持续扩充的。
    """

    DAILY = "daily"  # 日戳（营业日戳）—— 唯一有行业标准的戳种
    SCENIC = "scenic"  # 风景戳 / 风景日戳
    COMMEMORATIVE = "commemorative"  # 集邮纪念戳
    PROPAGANDA = "propaganda"  # 宣传戳
    POSTAL_SUB = "postal_sub"  # 邮政副戳
    METER = "meter"  # 邮资机戳
    BUSINESS = "business"  # 营业戳
    AGENCY = "agency"  # 代办所戳
    TEMPORARY = "temporary"  # 临时邮政局戳
    CULTURAL = "cultural"  # 文化日戳
    OTHER = "other"


#: v0.1 的 UI 只主推这两种。
#: D18：用户不使用集邮纪念戳（"预制菜"，不是现盖的）；
#: N4：先只做圆形戳。其余取值保留为可选，不阻塞录入。
PRIMARY_POSTMARK_TYPES = (PostmarkType.DAILY, PostmarkType.SCENIC)


class CardSource(StrEnum):
    FIELD_TRIP = "field_trip"  # 实地跑戳
    MAIL_REQUEST = "mail_request"  # 求戳信
    EXCHANGE = "exchange"  # 交换
    PURCHASE = "purchase"  # 购入
    OTHER = "other"


class CardStatus(StrEnum):
    COLLECTED = "collected"  # 已收
    PENDING = "pending"  # 在途
    WISHLIST = "wishlist"  # 想要


class Provenance(StrEnum):
    """来源标记。

    语义是**分区**而不是水印（D24）：区分"我真跑过"与"生成美化"，
    让画廊能一键只看真实跑戳。**没有任何导出限制。**
    """

    REAL_SCAN = "real_scan"  # 从原始扫描件抠出的位图 —— 事实
    GENERATED = "generated"  # 参数化/生成 —— 美化
    THIRD_PARTY = "third_party"  # 第三方来源


class CatalogSystem(StrEnum):
    CHINA = "china"  # 中国志号（YYYY-N，如 2023-1；或"普30"）
    SCOTT = "scott"
    MICHEL = "michel"
    OTHER = "other"


class AssetKind(StrEnum):
    SCAN_RAW = "scan_raw"  # 原始扫描件（不可变）
    PHOTO = "photo"  # 照片
    POSTMARK_CUT = "postmark_cut"  # 戳贴片
    STAMP_CUT = "stamp_cut"  # 票贴片
    THUMBNAIL = "thumbnail"
    DERIVED = "derived"


class IngestSource(StrEnum):
    UPLOAD = "upload"
    INBOX = "inbox"


class IngestStatus(StrEnum):
    RECEIVED = "received"
    STORED = "stored"
    FAILED = "failed"


class TagCategory(StrEnum):
    THEME = "theme"  # 主题
    QUALITY = "quality"  # 品相
    EVENT = "event"  # 事件
    SOURCE = "source"  # 来源


#: 预置标签。D8 只列了两个，其余是常见集邮标签，用户可自由增删。
DEFAULT_TAGS: list[tuple[str, TagCategory]] = [
    ("主题邮局", TagCategory.THEME),
    ("彩色戳", TagCategory.THEME),
    ("套色戳", TagCategory.THEME),
    ("风景戳", TagCategory.THEME),
    ("生肖", TagCategory.THEME),
    ("首日", TagCategory.EVENT),
    ("纪念日", TagCategory.EVENT),
    ("极限片", TagCategory.QUALITY),
]


class LevelLabel(StrEnum):
    """中国行政区划层级名。UI 只显示层级名，不显示"第 N 级"。"""

    COUNTRY = "国家"
    PROVINCE = "省级"
    PREFECTURE = "地级市"
    COUNTY = "县级"
    TOWNSHIP = "乡级"
    VILLAGE = "村级"


#: 邮票面值的**软提示**（N9）。
#: 明确**不做硬约束** —— 用户不知道别人会贴什么票，硬约束会让开源后的用户录不进去。
#: 这里只用于：① 输入时的候选下拉 ② OCR 结果置信度低且不在候选集合内时温和提示。
FACE_VALUE_SUGGESTIONS: list[tuple[str, str]] = [
    # (面值, 币种)  —— 中国普票常见面值 + 用户实际常用值
    ("0.01", "CNY"),
    ("0.015", "CNY"),
    ("0.05", "CNY"),
    ("0.10", "CNY"),
    ("0.30", "CNY"),
    ("0.40", "CNY"),
    ("0.50", "CNY"),
    ("0.60", "CNY"),
    ("0.80", "CNY"),
    ("1.20", "CNY"),
    ("1.50", "CNY"),
    ("4.50", "CNY"),
]

#: 用户实际常用的面值，排在候选最前（D31-5）
PREFERRED_FACE_VALUES: list[tuple[str, str]] = [
    ("0.10", "CNY"),
    ("0.30", "CNY"),
    ("0.40", "CNY"),
    ("1.20", "CNY"),
]
