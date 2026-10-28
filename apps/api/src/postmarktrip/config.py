"""应用配置。

所有配置都来自环境变量（见 docs/ok/05-部署架构与安装指南（规范）.md §四）。
环境变量名刻意不加统一前缀——每个容器有独立的环境命名空间，加前缀只增加噪音。

**唯一的例外**是 `OCR_ENGINE`：它额外接受 `PMTRIP_OCR_ENGINE`。那不是设计，
而是历史遗留 —— 文档与识别端点的 503 报错一直让用户设带前缀的那个名字，
只有它被认才不至于"照文档做还是 503"。详见该字段的注释。
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- 基础
    app_name: str = "postmarkTrip"
    app_env: Literal["dev", "prod"] = "prod"
    app_port: int = 9999
    app_host_bind: str = "0.0.0.0"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    # 会话签名密钥。生产环境必须由 .env 提供；开发环境缺失时自动生成（重启即失效）。
    secret_key: str = ""
    # 会话有效期（秒）。默认 30 天。
    session_max_age: int = 60 * 60 * 24 * 30
    cookie_secure: bool = False  # 局域网 HTTP 部署时为 False；走 HTTPS 反代时置 True

    # 首次启动时创建的管理员
    admin_user: str = "admin"
    admin_password: str = ""

    # ---------------------------------------------------------------- 数据
    data_dir: Path = Path("/data")
    inbox_dir: Path = Path("/inbox")
    public_data_dir: Path = Path("/public-data")
    models_dir: Path = Path("/models")
    #: 标定 / 回归用的**样本集**（只读）。
    #:
    #: compose 把它以 ``:ro`` 挂到 ``/samples``（见 docker-compose.yml 的注释：
    #: 样本是识别管线的参照集，程序只该读 —— 把"不写参照集"变成挂载层面的约束，
    #: 而不是靠纪律）。在宿主机（容器外）跑工具时指向仓库里的 ``docs/need/pic``。
    samples_dir: Path = Path("/samples")

    database_url: str = "postgresql+psycopg://postmarktrip:postmarktrip@db:5432/postmarktrip"
    redis_url: str = "redis://redis:6379/0"

    # ---------------------------------------------------------------- 图像
    # 扫描件元数据缺失时的兜底 DPI。管线本身兼容 300/600/1200（N7）。
    default_scan_dpi: int = 600
    # 卡片标准尺寸（mm）。仅用于切分结果的合理性校验，不做硬拒绝（N8）。
    card_width_mm: float = 54.0
    card_height_mm: float = 90.0
    # 上传体积上限（字节）。默认 100 MB —— 一张 A4 @600dpi 约 3 MB，留足余量。
    max_upload_bytes: int = 100 * 1024 * 1024
    # 缩略图长边
    thumbnail_max_px: int = 480

    # ---------------------------------------------------------------- 投递目录（v1.0）
    # 监听扫描件投递箱。**默认关** —— 打开它意味着后台会持续轮询目录，
    # 一个未预期的开关不该在升级后悄悄开始动用户的文件。
    inbox_enabled: bool = False
    # 轮询间隔（秒）。刻意用轮询而不是 inotify：投递箱常常是 SMB 挂载，
    # 网络文件系统上的 inotify 事件在各种 NAS 实现下表现不一（spec v1.0 §2.1）。
    # 一天 17280 次列目录，代价可忽略。
    inbox_poll_seconds: int = 5

    # ---------------------------------------------------------------- 识别 Provider
    # 一个都不填，系统仍完整可用（D5：默认全本地）。
    #
    # ⚠️ 这个字段有**两个**环境变量名，而且两个都要认 —— 实测踩过一次：
    #
    #   * `OCR_ENGINE`        —— `.env` / `.env.example` 里一直是这个名字
    #   * `PMTRIP_OCR_ENGINE` —— 文档（`ok/08`、`ok/12`）与识别端点的 503 报错
    #                            都叫用户设这个名字
    #
    # 而 `probe_ocr()` 读 `settings.ocr_engine`，所以**只认前者**。后果是
    # "照文档做还是 503"：设了 `PMTRIP_OCR_ENGINE=rapidocr`，门禁仍是
    # `ready=False`，而 503 的报错信息又让你去设它 —— 死循环。
    #
    # 这里用 `validation_alias` 同时接受两个名字（pydantic-settings 对
    # `AliasChoices` 是按**给定顺序**在环境变量里找，第一个不存在的才看第二个）。
    # 注意：`AliasChoices` 只作用于**环境变量**；`.env` 文件仍按字段名
    # （`OCR_ENGINE`）读，所以 `.env` 两边都不需要改。
    #
    # `infra/scripts/check-config.py` 也随之改成"一个字段可以有多个可接受名字"，
    # 否则新名字会被它报成"没记录在 .env.example 里"。
    ocr_engine: Annotated[
        Literal["rapidocr", "paddleocr", "none"],
        Field(validation_alias=AliasChoices("PMTRIP_OCR_ENGINE", "OCR_ENGINE")),
    ] = "none"
    colnect_api_key: str = ""
    #: M3C-5：colnetApi（独立服务）地址；默认走 Docker Desktop 的宿主别名。
    colnet_api_url: str = "http://host.docker.internal:8765"
    colnet_api_timeout_s: float = 90.0
    #: 是否启用「以图搜票」。它依赖一个**本仓库之外**的服务（colnetApi）：
    #: 装好那套服务的人自己开，没装的人别开 —— 否则按钮点了才报错。
    #: 默认 False，与「干净部署里没有该服务」这个事实一致。
    colnet_enabled: bool = False

    vlm_enabled: bool = False
    #: 云厂商预设（见 recognition/vlm.py 的 PROVIDERS）。选它就会自动填好
    #: base_url 与默认模型，并从该厂商约定的环境变量取 Key。
    #: 加一家新厂商 = 在预设表里加一条，不改这里的字段。
    vlm_provider: str = "deepseek"
    #: 以下三项**显式覆盖**预设，任何 OpenAI 兼容端点都能接。
    vlm_base_url: str = ""
    vlm_api_key: str = ""
    vlm_model: str = ""
    #: 单次调用的超时与输出上限。实测一张图约 300 输入 tokens（厂商上限 384）。
    vlm_timeout_s: float = 120.0
    vlm_max_tokens: int = 512

    # 扫描 Agent（v1.1）。形如 http://host.docker.internal:18080
    agent_url: str = ""
    agent_token: str = ""

    # 相机 / 扫描器：允许上传的扩展名（docs/ok/05 §六）
    allowed_upload_exts: tuple[str, ...] = (
        ".jpg",
        ".jpeg",
        ".png",
        ".tif",
        ".tiff",
        ".pdf",
    )

    # ---------------------------------------------------------------- 备份
    backup_keep_days: int = 30

    #: 是否由本进程自动生成了 SECRET_KEY（即用户没有提供）。
    #: 启动钩子据此告警 —— 临时密钥意味着重启后所有登录会话失效。
    secret_key_generated: bool = False

    # ---------------------------------------------------------------- 派生
    @model_validator(mode="after")
    def _ensure_secret(self) -> Settings:
        """SECRET_KEY 缺失时：生成临时密钥让它能起来，但**记录这件事**。

        为什么不直接报错退出：用户会连界面都打不开，更看不到解释，
        于是只能去翻容器日志。相反，让它起来 + 显著告警，
        用户能在界面上正常用，同时知道自己该去补一个固定密钥。

        为什么不让默认值写死一个"公开的开发密钥"：那会被原样带上生产，
        而写死的密钥等于没有密钥。
        """
        if not self.secret_key.strip():
            object.__setattr__(self, "secret_key", secrets.token_urlsafe(48))
            object.__setattr__(self, "secret_key_generated", True)
        return self

    @property
    def assets_dir(self) -> Path:
        """内容寻址的资产根目录。"""
        return self.data_dir / "assets"

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    @property
    def ocr_enabled(self) -> bool:
        """OCR 引擎是否已配置。

        ``none`` 是**受支持的状态**而不是错误 —— D5 要求"关掉所有云端 Provider
        后系统仍完整可用，只是精度下降"。v0.1 默认就是 none。
        """
        return self.ocr_engine != "none"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.assets_dir, self.inbox_dir, self.models_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
