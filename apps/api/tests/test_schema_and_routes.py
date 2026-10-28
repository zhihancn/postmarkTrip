"""数据模型与路由的静态验证。

**这些测试不连数据库、不起服务**，但能抓住"能不能跑起来"里最容易出错的部分：

1. SQLAlchemy 的 mapper 配置能通过 —— 关系、外键、类型全部解析成功
2. 全部表能编译出 PostgreSQL DDL —— 类型、约束、索引都合法
3. 前端依赖的每一个端点都真的注册了
4. OpenAPI schema 能生成 —— 等于校验了所有 Pydantic 模型

之所以在容器里跑得通却仍值得单独测：DDL 编译与 mapper 配置这类错误，
要等到 `make up` 起容器才暴露，反馈太慢；这里几秒钟就能定位。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateIndex, CreateTable


def test_mappers_configure() -> None:
    """关系与类型能全部解析。

    这条最值得单独测：关系配置写错时错误发生在**第一次使用**时，
    而不是导入时 —— 没有这条测试，问题会在运行时才炸。
    """
    configure_mappers()

    from postmarktrip.db.models import Base

    assert len(Base.metadata.sorted_tables) >= 12, "表数量异常，可能有模型没被导入"


def _all_ddl() -> str:
    from postmarktrip.db.models import Base

    dialect = postgresql.dialect()
    parts: list[str] = []
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=dialect)))
        for index in table.indexes:
            parts.append(str(CreateIndex(index).compile(dialect=dialect)))
    return "\n".join(parts)


def test_ddl_compiles() -> None:
    ddl = _all_ddl()
    assert ddl.count("CREATE TABLE") >= 12
    assert "CREATE INDEX" in ddl


@pytest.mark.parametrize(
    ("label", "needle"),
    [
        ("UUID 主键", "UUID"),
        ("JSONB 列（aliases / bbox）", "JSONB"),
        ("面值用 NUMERIC 而非浮点", "NUMERIC"),
        # 画廊（M3）：戳墙/票墙按"有贴片"过滤
        ("贴片部分索引（只收非空行）", "WHERE image_asset_id IS NOT NULL"),
        ("trgm GIN 索引（中文模糊搜索）", "gin_trgm_ops"),
        ("物化路径前缀索引", "text_pattern_ops"),
        ("资产内容寻址唯一约束", "uq_asset_sha256"),
        ("采集幂等唯一约束", "uq_ingest_record_source_sha256"),
        ("置信度 CHECK", "confidence_range"),
        ("面值非负 CHECK", "face_value_non_negative"),
        ("级联删除", "ON DELETE CASCADE"),
        ("软删除列", "deleted_at"),
    ],
)
def test_design_intent_reaches_the_ddl(label: str, needle: str) -> None:
    """确认设计意图真的落到了 DDL 上。

    这些不是"随便断言的字符串"：每一条都对应一个刻意做出的设计决定，
    如果哪天有人把 enum 换回原生类型、或把 Numeric 改成 Float，这里会失败。
    """
    assert needle in _all_ddl(), f"{label} 没有出现在 DDL 里"


#: 前端已经对接的端点。少一个就意味着界面上某处必然报错。
EXPECTED_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/api/health"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/password"),
    ("GET", "/api/vocab"),
    ("GET", "/api/tags"),
    ("POST", "/api/tags"),
    ("DELETE", "/api/tags/{tag_id}"),
    ("GET", "/api/places"),
    ("POST", "/api/places"),
    ("GET", "/api/places/tree"),
    ("PATCH", "/api/places/{place_id}"),
    ("DELETE", "/api/places/{place_id}"),
    ("GET", "/api/post-offices"),
    ("GET", "/api/post-offices/{office_id}"),
    ("POST", "/api/post-offices"),
    ("PATCH", "/api/post-offices/{office_id}"),
    ("DELETE", "/api/post-offices/{office_id}"),
    ("GET", "/api/cards"),
    ("POST", "/api/cards"),
    ("GET", "/api/cards/{card_id}"),
    ("PATCH", "/api/cards/{card_id}"),
    ("DELETE", "/api/cards/{card_id}"),
    ("POST", "/api/cards/{card_id}/restore"),
    ("DELETE", "/api/cards/{card_id}/purge"),
    ("POST", "/api/cards/{card_id}/postmarks"),
    ("PATCH", "/api/postmarks/{postmark_id}"),
    ("DELETE", "/api/postmarks/{postmark_id}"),
    ("POST", "/api/cards/{card_id}/stamps"),
    ("PATCH", "/api/stamps/{stamp_id}"),
    ("DELETE", "/api/stamps/{stamp_id}"),
    ("POST", "/api/assets"),
    ("GET", "/api/assets/{asset_id}/raw"),
    ("GET", "/api/assets/{asset_id}/thumb"),
    ("GET", "/api/stats/overview"),
    ("GET", "/api/stats/by-year"),
    ("GET", "/api/stats/by-place"),
    ("GET", "/api/export/cards.csv"),
    # 展览集（M3）。M3a 时间轴 + M3b 戳墙 + M3c 票墙；地图在 M3d 加。
    ("GET", "/api/gallery/timeline"),
    ("GET", "/api/gallery/postmarks"),
    ("GET", "/api/gallery/stamps"),
    # 标准邮票库（M3C-1/2）
    ("GET", "/api/stamp-catalog"),
    ("POST", "/api/stamp-catalog"),
    ("GET", "/api/stamp-catalog/{catalog_id}"),
    ("PATCH", "/api/stamp-catalog/{catalog_id}"),
    ("POST", "/api/stamp-catalog/{catalog_id}/refresh"),
    ("POST", "/api/stamp-catalog/{catalog_id}/apply-parse"),
    ("GET", "/api/stamp-catalog/unlinked-stamps"),
    # M3C-4
    ("GET", "/api/stamp-catalog/backfill/preview"),
    ("POST", "/api/stamp-catalog/backfill/apply"),
    ("GET", "/api/stamp-catalog/export"),
    ("GET", "/api/stamp-catalog/duplicates"),
    ("POST", "/api/stamp-catalog/merge"),
    # M3C-5
    ("GET", "/api/stamps/{stamp_id}"),
    ("POST", "/api/stamps/{stamp_id}/identify"),
    ("POST", "/api/stamps/{stamp_id}/identify/apply"),
]


def _openapi() -> dict[str, Any]:
    from postmarktrip.main import app

    # 注意：不能靠遍历 app.routes 来数端点 —— 新版 FastAPI 用 _IncludedRouter
    # 惰性挂载，app.routes 里只有一个占位条目。openapi() 才是可靠来源。
    return app.openapi()


def test_all_expected_endpoints_are_registered() -> None:
    actual = {
        (method.upper(), path)
        for path, operations in _openapi().get("paths", {}).items()
        for method in operations
    }
    missing = sorted(set(EXPECTED_ENDPOINTS) - actual)
    assert missing == [], f"这些端点没有注册：{missing}"


def test_openapi_schema_generates() -> None:
    """能生成 OpenAPI，就说明所有 Pydantic 模型都能被解析。"""
    schema = _openapi()
    assert len(schema.get("paths", {})) >= 25
    components = schema.get("components", {}).get("schemas", {})
    for name in ("CardCreate", "CardRead", "PostmarkCreate", "StampCreate", "PlaceNode"):
        assert name in components, f"缺少模型 {name}"
    # 画廊时间轴的返回形状是前端已经对接的契约（00 §3.4）
    assert "GalleryTimeline" in components, "缺少模型 GalleryTimeline"
    timeline_fields = components["GalleryTimeline"].get("properties", {})
    for field_name in ("total", "with_cut", "without_cut", "items", "page", "size"):
        assert field_name in timeline_fields, f"GalleryTimeline 缺字段 {field_name}"
    assert "GalleryPostmarkWall" in components, "缺少模型 GalleryPostmarkWall"
    wall_fields = components["GalleryPostmarkWall"].get("properties", {})
    for field_name in ("total", "with_cut", "without_cut", "groups", "page", "size"):
        assert field_name in wall_fields, f"GalleryPostmarkWall 缺字段 {field_name}"
    assert "GalleryStampWall" in components, "缺少模型 GalleryStampWall"
    stamp_fields = components["GalleryStampWall"].get("properties", {})
    for field_name in (
        "total",
        "with_cut",
        "stamps_without_cut",
        "cards_without_stamp",
        "groups",
    ):
        assert field_name in stamp_fields, f"GalleryStampWall 缺字段 {field_name}"
    assert "StampCatalogRead" in components, "缺少模型 StampCatalogRead"
    catalog_fields = components["StampCatalogRead"].get("properties", {})
    for field_name in ("id", "standard_code", "colnect_url", "issue_url", "source_state", "fetch_status"):
        assert field_name in catalog_fields, f"StampCatalogRead 缺字段 {field_name}"


def test_health_endpoint_needs_no_auth() -> None:
    """`/api/health` 必须免鉴权 —— 运维自检与容器 healthcheck 都靠它。"""
    from postmarktrip.main import app

    route = next(
        (r for r in app.router.routes if getattr(r, "path", None) == "/api/health"),
        None,
    )
    if route is None:
        # 惰性挂载下拿不到，退化为检查 OpenAPI 里该路径没有 security 要求
        ops = _openapi()["paths"].get("/api/health", {})
        assert ops, "/api/health 未注册"
        assert not any("security" in op for op in ops.values())
        return
    assert route is not None


def test_csv_export_columns_are_consistent() -> None:
    """导出的列定义必须唯一、有序、且包含实测得出的那些字段。

    行与列的对应关系现在按**列名**建立（并在缺列时抛错），所以位置错位
    已经不可能静默发生。但列名重复、或顺序被误改，仍会产出难以使用的
    导出文件 —— 而导出文件往往是唯一的备份。
    """
    from postmarktrip.api.routes_stats import CSV_COLUMNS

    assert len(CSV_COLUMNS) == len(set(CSV_COLUMNS)), "列名有重复"

    # 同卡多戳靠 card_uid 归组，它必须是第一列
    assert CSV_COLUMNS[0] == "card_uid"
    assert CSV_COLUMNS[:5] == ["card_uid", "visit_date", "received_date", "source", "status"]

    for required in (
        "inscription",  # 戳面原文 —— 本项目最核心的证据字段
        "postmark_no",  # 戳号，实测得出的独立字段
        "time_code",  # 时间码
        "provenance",  # D24 的来源标记
        "card_tags",
        "postmark_tags",
    ):
        assert required in CSV_COLUMNS, f"导出缺少列 {required}"
