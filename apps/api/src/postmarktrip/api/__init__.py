"""API 路由聚合。

统一挂在 ``/api`` 前缀下 —— 网关只反代这一个前缀，静态资源与接口互不干扰。

**鉴权策略**（有意为之）：
* **参考数据（词表 / 行政区划 / 邮局 / 标签）的读取不要求登录** ——
  它们是共享的公共数据，v1.1 的公共戳库也会把它们暴露出去。
* **其余一切（卡片、资产、统计、导出、所有写操作）都要求登录。**
"""

from fastapi import APIRouter

from postmarktrip.api import (
    routes_assets,
    routes_auth,
    routes_cards,
    routes_catalog_tools,
    routes_confirm,
    routes_dict,
    routes_ecard,
    routes_gallery,
    routes_recognize,
    routes_stamp_catalog,
    routes_stamp_identify,
    routes_stats,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(routes_auth.router)
api_router.include_router(routes_dict.router)
api_router.include_router(routes_cards.router)
api_router.include_router(routes_stamp_catalog.router)
api_router.include_router(routes_catalog_tools.router)
api_router.include_router(routes_stamp_identify.router)
api_router.include_router(routes_confirm.router)
api_router.include_router(routes_assets.router)
api_router.include_router(routes_ecard.router)
api_router.include_router(routes_gallery.router)
api_router.include_router(routes_stats.router)
api_router.include_router(routes_recognize.router)

__all__ = ["api_router"]
