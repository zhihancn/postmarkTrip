"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from postmarktrip import __version__
from postmarktrip.api import api_router
from postmarktrip.config import settings
from postmarktrip.storage import StorageError, UnsupportedFileType

logger = logging.getLogger("postmarktrip")


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # 这些库在 INFO 级别太吵
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    settings.ensure_dirs()

    # SECRET_KEY 缺失时 config 会为本次进程生成临时密钥。必须明确告警 ——
    # 否则"每次重启都要重新登录"会被当成 bug 去排查。
    if settings.secret_key_generated:
        logger.warning(
            "SECRET_KEY 未设置，已为本次进程生成临时密钥。"
            "服务重启后所有登录会话都会失效。请在 .env 中设一个固定值"
            "（openssl rand -base64 48）。"
        )

    if settings.app_env == "prod":
        if not settings.admin_password:
            logger.warning("ADMIN_PASSWORD 未设置 —— 首次启动将无法创建管理员，请检查 .env")
        if settings.cookie_secure is False:
            logger.info(
                "COOKIE_SECURE=false。局域网 HTTP 部署下这是正确的；"
                "若通过 HTTPS 反向代理暴露，请改为 true。"
            )

    logger.info(
        "postmarkTrip %s 启动 | env=%s | port=%s | ocr=%s | vlm=%s | agent=%s",
        __version__,
        settings.app_env,
        settings.app_port,
        settings.ocr_engine,
        "on" if settings.vlm_enabled else "off",
        "on" if settings.agent_url else "off",
    )
    yield
    logger.info("postmarkTrip 已停止")


app = FastAPI(
    title="跑戳日志 · postmarkTrip",
    version=__version__,
    description="自托管的跑戳记录与管理系统",
    lifespan=lifespan,
    # 生产环境由网关反代，文档端点只在开发环境开放
    docs_url="/api/docs" if settings.is_dev else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.is_dev else None,
)

if settings.is_dev:
    # 仅开发环境：Vite dev server 是另一个源，需要放行
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(api_router)


@app.exception_handler(UnsupportedFileType)
async def _unsupported_file(_: Request, exc: UnsupportedFileType) -> JSONResponse:
    return JSONResponse(status_code=415, content={"detail": str(exc)})


@app.exception_handler(StorageError)
async def _storage_error(_: Request, exc: StorageError) -> JSONResponse:
    logger.error("存储错误：%s", exc)
    return JSONResponse(status_code=500, content={"detail": "存储层错误，请查看服务日志"})


def run() -> None:  # pragma: no cover - 由控制台脚本调用
    import uvicorn

    uvicorn.run(
        "postmarktrip.main:app",
        host=settings.app_host_bind,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    run()
