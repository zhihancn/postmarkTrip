"""`tests/contract/` 的公共准备。

契约测试跨了两个应用：读 `apps/web/src/shared/types.ts`（手写的前端类型），
也导入后端的 FastAPI 应用来取 OpenAPI schema。所以这里要：
  * 把 `apps/api/src` 放进 sys.path（src-layout，包不在项目根下）
  * 在任何 `postmarktrip` 模块被导入之前安排好环境变量
    （`config.settings` 是模块级单例，导入后再设就晚了）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API_SRC = REPO_ROOT / "apps" / "api" / "src"

if API_SRC.is_dir() and str(API_SRC) not in sys.path:
    sys.path.insert(0, str(API_SRC))

# 这些测试不连数据库；给一个语法合法但连不上的地址即可。
os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("SECRET_KEY", "contract-test-only-not-a-real-secret")
os.environ.setdefault("DATA_DIR", str(REPO_ROOT / "tmp" / "rtdata-contract"))
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://nobody:nobody@127.0.0.1:1/nonexistent"
)
