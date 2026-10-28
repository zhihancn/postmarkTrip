"""认证与健康检查路由。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select

from postmarktrip import providers
from postmarktrip.api.deps import CurrentUser, DbDep, StoreDep
from postmarktrip.api.http import UNPROCESSABLE
from postmarktrip.config import settings
from postmarktrip.db import ping as db_ping
from postmarktrip.db.models import AppUser
from postmarktrip.domain.schemas import (
    LoginRequest,
    MeResponse,
    OkResponse,
    PasswordChangeRequest,
)
from postmarktrip.security import (
    SESSION_COOKIE,
    hash_password,
    issue_session,
    verify_password,
)
from postmarktrip.worker.tasks import worker_alive

router = APIRouter(tags=["auth"])


def _set_session_cookie(response: Response, user: AppUser) -> None:
    """下发会话 cookie。

    `login` 与 `change_password` 共用这一处 —— 会话 cookie 的属性
    （httponly / samesite / secure / max_age）是安全相关的，两个入口
    各写一份迟早会漂移。
    """
    response.set_cookie(
        key=SESSION_COOKIE,
        # 带上口令指纹：改口令后，指纹变化的旧会话一律失效
        value=issue_session(str(user.id), user.password_hash),
        max_age=settings.session_max_age,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


@router.post("/auth/login", response_model=MeResponse)
def login(payload: LoginRequest, response: Response, db: DbDep) -> MeResponse:
    user = db.scalar(select(AppUser).where(AppUser.username == payload.username))
    # 用户不存在与口令错误返回同一个信息 —— 不泄露"该用户名是否存在"
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或口令错误")

    user.last_login_at = datetime.now(UTC)
    db.commit()

    _set_session_cookie(response, user)
    return MeResponse(username=user.username, last_login_at=user.last_login_at)


@router.post("/auth/logout", response_model=OkResponse)
def logout(response: Response) -> OkResponse:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return OkResponse()


@router.get("/auth/me", response_model=MeResponse)
def me(user: CurrentUser) -> MeResponse:
    return MeResponse(username=user.username, last_login_at=user.last_login_at)


@router.post("/auth/password", response_model=OkResponse)
def change_password(
    payload: PasswordChangeRequest, user: CurrentUser, db: DbDep, response: Response
) -> OkResponse:
    """改口令。单用户系统，必须校验旧口令。

    成功后**换发本机的新会话**：会话 token 里带着口令哈希的指纹，所以改口令
    会让所有旧 token 失效 —— 包括发出这次请求的那一台。若不换发，用户会
    在改完口令的下一秒被自己的操作踢到登录页，那是很差的体验。

    于是实际语义是"改口令即登出**其他**设备"，这既安全又符合直觉：
    在别的机器上登录过的旧会话全部作废，本机继续可用。
    """
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "当前口令不正确")
    if payload.new_password == payload.current_password:
        raise HTTPException(UNPROCESSABLE, "新口令不能与当前口令相同")
    user.password_hash = hash_password(payload.new_password)
    db.commit()

    _set_session_cookie(response, user)
    return OkResponse(message="口令已更新，其他设备需要重新登录")


@router.get("/health", tags=["system"])
async def health(store: StoreDep) -> dict[str, object]:
    """运维自检用（``make doctor`` 会打这个端点）。

    刻意返回细项而不是一个笼统的 ok —— 出问题时要知道是哪一环断了。

    ``worker_alive`` 的取值是 **三态**：``True`` / ``False`` / ``None``。
    ``None`` 表示"查不到"（Redis 不可达），与 ``False``（worker 真的没心跳）
    是两回事 —— 把它们混成一个布尔值会让排障一开始就走错方向。
    """
    db_ok = db_ping()
    asset_writable = False
    try:
        probe = store.root / ".write-probe"
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
        asset_writable = True
    except OSError:
        asset_writable = False

    inbox_ok = settings.inbox_dir.exists() and settings.inbox_dir.is_dir()
    alive = await worker_alive()
    caps = providers.summary()

    # 核心判据仍是数据库与资产目录可写：worker 没起来不该让整个应用报"不健康"，
    # 因为 v0.x 的录入/上传/导出完全不依赖 worker。
    # worker 状态单独暴露，由调用方决定算不算致命。
    return {
        "ok": db_ok and asset_writable,
        "database": db_ok,
        "assets_writable": asset_writable,
        "assets_dir": str(settings.assets_dir),
        "inbox_present": inbox_ok,
        "inbox_dir": str(settings.inbox_dir),
        "inbox_enabled": settings.inbox_enabled,
        "worker_alive": alive,
        # ---- 识别 Provider 现状（探测而非假设） ----
        **caps,
        "vlm_enabled": settings.vlm_enabled,
        # 以图搜票依赖仓库之外的服务；前端据此决定「识别邮票」是否可点。
        "stamp_identify": {"enabled": settings.colnet_enabled},
        "agent_configured": bool(settings.agent_url),
        "default_scan_dpi": settings.default_scan_dpi,
    }
