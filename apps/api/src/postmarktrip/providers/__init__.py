"""Provider 抽象（spec v1.0 §3.6、docs/ok/04 §5）。

## 这个模块现在**不做什么**

P1 阶段刻意**不写任何识别实现**。一个"看起来能跑但结果是编的"的 stub，
比一个诚实报"未实现"的空缺有害得多 —— 它会让人以为精度问题只是调参问题。

所以这里只提供三件真的东西：

1. **能力声明**（``Capability``）—— 每个 Provider 声明自己的标识、是否必需、
   以及当前**是否真的可用**（探测而非假设）。
2. **注册表**（``PROBES`` / ``probe_all``）—— 一个地方回答"这台机器上现在有什么"。
3. **可执行的探测**（CLI）—— 把"依赖装了吗、模型能加载吗"从猜测变成
   一条可以跑的命令。

## 为什么"是否可用"必须探测而不是读配置

``OCR_ENGINE=rapidocr`` 只说明**用户想要** RapidOCR。它是否真的装进镜像、
onnxruntime 能否加载模型、模型缓存目录里有没有文件 —— 都是另外的事。
docs/ok/06 §二 里"未验证"清单的根因全是这类假设：配置写了、代码有了、
但没有任何一条命令真的跑过。

## 与 D5 的关系

三条硬约束（docs/ok/04 §5.2）在这里落地：

* Key 只存本地 → 本模块**不读也不打印**任何 Key，只报告"是否已配置"。
* Key 绝不进日志 → ``Capability`` 里放的是布尔值，不是值本身。
* 每个 Provider 必须能单独关闭 → ``enabled`` 是每个 Provider 的独立开关，
  且**全部关掉后系统仍完整可用**（只是精度下降）。
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Literal

from postmarktrip.config import settings
from postmarktrip.recognition.review import PIPELINE_VERSION as REVIEW_PIPELINE_VERSION

logger = logging.getLogger("postmarktrip.providers")

#: Provider 的六种类别（docs/ok/04 §5.1）
ProviderKind = Literal["ocr", "cv", "vlm", "catalog", "embed", "scan"]

#: 当前管线的版本号，写进每条 observation 的 pipeline_version。
#:
#: ⚠️ **这个常量不在这里定义**，它从 `recognition.review` 转发过来。
#:
#: 原因是一次实测撞出来的：这里原本自己写着 `"p1-0.1.0"`，而真正写进
#: `observation` 的是 `review.PIPELINE_VERSION = "p2-0.1.0"` —— 于是
#: `/api/health`（运维自检的入口）报出一个**过期**的管线版本，看到的人会以为
#: 跑的是 P1 的代码。两个常量各自维护必然会漂移，所以只留一份。
PIPELINE_VERSION: Final = REVIEW_PIPELINE_VERSION


@dataclass(frozen=True, slots=True)
class Capability:
    """一个 Provider 的现状声明。"""

    name: str
    kind: ProviderKind
    #: 用户是否启用了它（来自配置）
    enabled: bool
    #: 依赖是否真的可用（探测结果）。enabled=True 但 available=False
    #: 意味着"想用但用不了"—— 这是最需要在 doctor 里暴露的状态。
    available: bool
    #: 人类可读的说明：为什么不可用
    detail: str = ""
    #: 依赖缺失时，安装它的 extra 名
    requires_extra: str | None = None

    @property
    def ready(self) -> bool:
        return self.enabled and self.available

    @property
    def blocked(self) -> bool:
        """启用了但不可用 —— 需要用户处理。"""
        return self.enabled and not self.available


def _module_available(name: str) -> bool:
    """依赖是否**真的可用**（不只是"装了"）。

    这里刻意**真的 import 一次**，而不是只 ``find_spec``。

    原因是踩出来的：``find_spec("cv2")`` 返回真只说明包在磁盘上，
    **不说明它能被加载**。在只装了 ``python:3.12-slim`` 的镜像里，
    ``opencv-python``（非 headless）需要 X11 库，import 时会报

        ImportError: libxcb.so.1: cannot open shared object file

    而 ``find_spec`` 对此一无所知 —— 于是这个探测报"cv 就绪"，
    等到真正跑识别管线时才炸。

    **这正是"探测而非假设"这条设计原则要防的事，而第一版自己犯了这个错。**

    结果做缓存：真加载 cv2/onnxruntime 要几百毫秒，而 ``/api/health``
    会被反复调用。失败结果也缓存 —— 依赖状态在一次进程生命周期内不会变，
    而反复重试一个会失败的加载只会让健康检查变慢。
    """
    return _cached_import(name) is None


@lru_cache(maxsize=32)
def _cached_import(name: str) -> str | None:
    """真的 import 一次，返回错误说明；成功返回 ``None``。"""
    try:
        importlib.import_module(name)
    except Exception as exc:
        # 宽捕异常是有意的：缺共享库（ImportError）、版本不兼容
        # （AttributeError）、平台不支持（OSError）都算"不可用"。
        return f"{type(exc).__name__}: {exc}"
    return None


def _import_problem(name: str) -> str | None:
    """尝试 import，返回错误说明；成功返回 ``None``。

    与 ``_module_available`` 共用缓存，但**把不可用的原因说出来** ——
    "缺少 opencv-python-headless"和"libxcb.so.1 不存在"是两条完全不同的
    排障路径，压成一句"不可用"会让用户去装一个已经装好的包。
    """
    return _cached_import(name)


# --------------------------------------------------------------------------- #
#  探测
# --------------------------------------------------------------------------- #


def probe_cv() -> Capability:
    """OpenCV：切分、圆检测、形态学的载体。"""
    cv2_problem = _import_problem("cv2")
    numpy_problem = _import_problem("numpy")

    if cv2_problem is not None:
        # 缺共享库是最常见的一种，且它看起来像"没装"，其实装了。
        hint = ""
        if "libxcb" in cv2_problem or "libGL" in cv2_problem or ".so" in cv2_problem:
            hint = (
                "（包已装但缺系统库 —— 多半是装上了非 headless 的 opencv-python，"
                "它需要 X11。本项目的 pyproject 声明的是 opencv-python-headless）"
            )
        detail = f"cv2 无法 import：{cv2_problem}{hint}"
    elif numpy_problem is not None:
        detail = f"numpy 无法 import：{numpy_problem}"
    else:
        detail = "cv2 与 numpy 都能真的 import"

    return Capability(
        name="opencv",
        kind="cv",
        # CV 不是"用户可关"的东西：它是本地管线的必需品，没有它 v1.0 的核心
        # 能力（切分 / 戳型判别）根本无从谈起。所以 enabled 恒为 True，
        # 由 available 说明它到底在不在、能不能用。
        enabled=True,
        available=cv2_problem is None and numpy_problem is None,
        detail=detail,
        requires_extra="recognition",
    )


def probe_ocr() -> Capability:
    """OCR 引擎。``none`` 是受支持的状态，不是错误。"""
    engine = settings.ocr_engine
    if engine == "none":
        return Capability(
            name="ocr",
            kind="ocr",
            enabled=False,
            available=False,
            detail="OCR_ENGINE=none —— 受支持的默认值。识别预填在 v1.0 后续阶段接入。",
        )
    if engine == "rapidocr":
        ok = _module_available("rapidocr_onnxruntime")
        return Capability(
            name="rapidocr",
            kind="ocr",
            enabled=True,
            available=ok,
            detail="rapidocr_onnxruntime 可导入" if ok else "缺少 rapidocr-onnxruntime",
            requires_extra="recognition",
        )
    if engine == "paddleocr":
        ok = _module_available("paddleocr")
        return Capability(
            name="paddleocr",
            kind="ocr",
            enabled=True,
            available=ok,
            detail="paddleocr 可导入" if ok else "缺少 paddleocr",
            requires_extra="recognition",
        )
    # Literal 上不可达，但配置来自环境变量，运行时不保证
    return Capability(  # pragma: no cover - 防御性分支
        name=str(engine),
        kind="ocr",
        enabled=True,
        available=False,
        detail=f"未知的 OCR_ENGINE={engine!r}",
    )


def probe_vlm() -> Capability:
    """云端多模态。

    **只校验"是否已配置"，绝不校验 Key 有效性** —— 那需要发网络请求，
    而 doctor 必须离线可跑。这也是"Key 不进日志"的一部分：
    本模块从头到尾没有机会打印 Key。
    """
    configured = bool(settings.vlm_base_url and settings.vlm_api_key and settings.vlm_model)
    if not settings.vlm_enabled:
        return Capability(
            name="vlm",
            kind="vlm",
            enabled=False,
            available=False,
            detail="VLM_ENABLED=false（D5：疑难件必须**手动**送云端，不自动 fallback）",
        )
    return Capability(
        name="vlm",
        kind="vlm",
        enabled=True,
        available=configured,
        detail=(
            "BASE_URL / API_KEY / MODEL 均已填写"
            if configured
            else "VLM_ENABLED=true 但 BASE_URL / API_KEY / MODEL 未填齐"
        ),
    )


def probe_catalog() -> Capability:
    """邮票目录（Colnect CAPI）。同理：不上网验 Key。"""
    has_key = bool(settings.colnect_api_key.strip())
    return Capability(
        name="colnect",
        kind="catalog",
        enabled=has_key,
        available=has_key,
        detail=(
            "已配置 COLNECT_API_KEY（可用性未联网校验）"
            if has_key
            else "未配置 COLNECT_API_KEY —— 不启用目录查询，邮票信息只能靠 OCR + 手工"
        ),
    )


def probe_embed() -> Capability:
    """本地向量模型（以图搜戳 / 搜票的粗召回）。v1.0 后续阶段接入。"""
    return Capability(
        name="dinov2",
        kind="embed",
        enabled=False,
        available=False,
        detail="以图搜戳/搜票在 v1.0 后续阶段接入；与 v1.1 公共戳库共用同一套基础设施",
    )


def probe_scan() -> Capability:
    """扫描 Agent（路径 B）。**接口在 v1.0 定，实现在 v1.1**。"""
    url = settings.agent_url.strip()
    return Capability(
        name="scan-agent",
        kind="scan",
        enabled=bool(url),
        # 不联网探测：Agent 只绑 127.0.0.1，服务端本来就够不到它。
        # 前端负责"本机有没有 Agent"的优雅降级（spec v1.0 §2.3）。
        available=False,
        detail=(
            "AGENT_URL 已配置；Agent 本体属 v1.1，且从服务端探测它既不必要也不可靠"
            if url
            else "未配置（v1.0 的采集走投递目录 + 手工上传）"
        ),
    )


#: 探测顺序 = doctor 与 CLI 的展示顺序
PROBES: Final = (
    probe_cv,
    probe_ocr,
    probe_vlm,
    probe_catalog,
    probe_embed,
    probe_scan,
)


def probe_all() -> list[Capability]:
    """探测全部 Provider。纯本地、无网络、无副作用。"""
    return [probe() for probe in PROBES]


def summary() -> dict[str, object]:
    """给 /api/health 与 doctor 用的紧凑摘要。"""
    caps = probe_all()
    return {
        "pipeline_version": PIPELINE_VERSION,
        "ocr_ready": any(c.ready and c.kind == "ocr" for c in caps),
        "cv_ready": any(c.ready and c.kind == "cv" for c in caps),
        #: 启用了但不可用 —— 这些才是需要用户动手的
        "blocked": [c.name for c in caps if c.blocked],
        #: 是否至少有一个识别通道可用
        "recognition_ready": any(c.ready for c in caps if c.kind in ("ocr", "cv")),
    }


def format_report() -> str:
    """人类可读的探测报告。``python -m postmarktrip.providers`` 输出这个。"""
    caps = probe_all()
    width = max(len(c.name) for c in caps)
    lines = [f"pipeline_version = {PIPELINE_VERSION}", ""]
    for cap in caps:
        if cap.ready:
            mark = "就绪"
        elif cap.blocked:
            mark = "阻塞"
        elif cap.enabled:
            mark = "启用但不可用"
        else:
            mark = "未启用"
        lines.append(f"  [{mark:^12}] {cap.name:<{width}}  ({cap.kind})")
        lines.append(f"{'':18}{cap.detail}")
        if cap.blocked and cap.requires_extra:
            lines.append(f"{'':18}安装：pip install -e '.[{cap.requires_extra}]'")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m postmarktrip.providers",
        description="探测识别 Provider 的可用性（纯本地，不联网）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是人类可读报告")
    args = parser.parse_args(argv)

    if args.json:
        import json
        from dataclasses import asdict

        print(json.dumps([asdict(c) for c in probe_all()], ensure_ascii=False, indent=2))
    else:
        print(format_report())

    # 退出码：有阻塞项就非 0 —— 让 CI / doctor 能直接判失败
    return 1 if any(c.blocked for c in probe_all()) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
