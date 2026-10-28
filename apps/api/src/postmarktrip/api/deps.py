"""FastAPI 依赖项。"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from postmarktrip.config import settings
from postmarktrip.db.session import get_db
from postmarktrip.security import CurrentUser
from postmarktrip.storage import AssetStore

DbDep = Annotated[Session, Depends(get_db)]


@lru_cache(maxsize=1)
def get_store() -> AssetStore:
    """资产存储是进程级单例 —— 它只是路径 + 配置，无状态。"""
    settings.ensure_dirs()
    return AssetStore(settings.assets_dir, allowed_exts=settings.allowed_upload_exts)


StoreDep = Annotated[AssetStore, Depends(get_store)]

__all__ = ["CurrentUser", "DbDep", "StoreDep", "get_store"]
