"""OCR 层：把文字区域图变成可用的文本（spec v1.0 §3.6）。

## 设计约束（都来自决策，不是偏好）

* **Provider 可插拔**（D5）：本模块只依赖一个 ``_Engine`` 协议，
  RapidOCR 只是当前实现。将来接 PaddleOCR / 云端 VLM 时不该改调用方。
* **默认全本地**（D5）：默认引擎是 RapidOCR（离线、CPU 可跑）。
* **Key 不进日志**（`ok/04` §5.2）：本模块不接触任何 Key。
* **失败要能被区分**（spec §3.7 的置信度模型）：
  "引擎起不来"与"读到但不确定"是两件事，所以
  :class:`OcrResult` 用一个显式的 ``available`` 标志区分它们 ——
  而不是用"空文本"同时表示两种情况。

## 为什么引擎要懒加载 + 缓存

RapidOCR 构造时会加载 ONNX 模型（数百毫秒到数秒）。
`/api/health` 与批量识别都会调用这里，所以进程内只建一次。
**并且把"建不起来"也缓存** —— 依赖缺失或模型下载失败时，
反复重试只会让每个请求都卡几秒。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import numpy as np

logger = logging.getLogger("postmarktrip.recognition.ocr")

#: 低于这个置信度视为"噪声"，不进入字段抽取。
#: 取 0.30 而不是更高的值：OCR 的置信度对**短文本**偏低是常态
#: （一两个字的区域算不出稳定的概率），而把这些丢掉会让
#: "顶弧只有两个字"的戳永远读不出来。真正的取舍交给字段层
#: （它按字段分别设阈值，见 parse.py）。
DEFAULT_MIN_CONFIDENCE: Final = 0.30

#: 可选的 OCR 引擎。**V2 最终实测**（见 ok/10 §七之十一）：
#:
#: | 引擎 | 日期命中 | 平均耗时 |
#: | --- | --- | --- |
#: | ``rapidocr`` | 5/6 = **83.3%** | **0.42s** |
#: | ``paddleocr`` | 5/6 = **83.3%** | 0.28s |
#: | ``vlm``（云端） | 5/6 = 83.3% | 0.86s |
#:
#: **三个引擎在日期上并列** —— 而这一点本身就是结论：
#: **说明问题不在引擎，而在区域定位**（见 ok/10 §七之十五）。
#: 那唯一一张失败的样本，根因是圆定位落在了明信片印刷抬头而不是邮戳上，
#: 所以**换任何引擎都读不到**；修好定位后 A4 就是 100%。
#:
#: RapidOCR 与 PaddleOCR 之所以并列，还因为它们**本质上跑同一套模型**
#: （RapidOCR 用 ONNXRuntime 跑 PaddleOCR 的模型）。
#:
#: 差异在**地名**上：RapidOCR 给「福建南火」（真值「福建南平」）、
#: PaddleOCR 给「牡丹江」（真值「福建厦门」）—— 两者都会给出
#: **看起来合理的错字**。真正读对的是云端 VLM（地名 5/6 = 83%）。
#:
#: 所以默认仍是 ``rapidocr``（快、日期够用），而地名交给云端
#: （见 ok/11 §三 的实验与决定）。
#:
#: 切换用环境变量 ``PMTRIP_OCR_ENGINE=rapidocr|paddleocr``。
ENGINE_RAPIDOCR: Final = "rapidocr"
ENGINE_PADDLEOCR: Final = "paddleocr"
SUPPORTED_ENGINES: Final = (ENGINE_RAPIDOCR, ENGINE_PADDLEOCR)


@dataclass(frozen=True, slots=True)
class OcrLine:
    """一行识别结果。"""

    text: str
    confidence: float
    #: 在**输入图**里的四点包围盒（不是原图）。
    #: 单位是输入图的像素 —— 调用方要还原到原图得自己换算，
    #: 因为只有它知道给的是哪一块。
    box: list[list[float]]


@dataclass(slots=True)
class OcrResult:
    """一次识别。"""

    lines: list[OcrLine] = field(default_factory=list)
    #: 引擎是否可用。**False 时 lines 必然为空**，而原因写在 ``reason``。
    available: bool = True
    reason: str = ""
    #: 各阶段耗时（秒），来自 RapidOCR。排障时有用。
    elapsed: list[float] = field(default_factory=list)

    @property
    def text(self) -> str:
        """把所有行拼成一段文本（按阅读顺序，行间用换行）。

        **不在这里做任何"清洗"**：去掉空格/标点属于字段层的事，
        而且不同字段需要不同的清洗（日期要保留点，局名要去掉空格）。
        """
        return "\n".join(line.text for line in self.lines)

    @property
    def mean_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return float(np.mean([line.confidence for line in self.lines]))


class _Engine(Protocol):
    """OCR 引擎协议。RapidOCR 只是其中一个实现。"""

    def __call__(self, img: np.ndarray) -> Any:  # pragma: no cover - 协议
        ...


_lock = threading.Lock()
_engine: _Engine | None = None
_engine_error: str | None = None
_engine_ready = False


def _build_engine(name: str = ENGINE_RAPIDOCR) -> tuple[_Engine | None, str]:
    """构造引擎。返回 (引擎, 错误说明)。**只调用一次**。

    ``name`` 见 :data:`SUPPORTED_ENGINES`。
    """
    if name == ENGINE_PADDLEOCR:
        return _build_paddle()
    return _build_rapidocr()


def _build_paddle() -> tuple[_Engine | None, str]:
    """构造 PaddleOCR。**必须关掉 oneDNN**（否则完全不能用）。

    ⚠️ ``enable_mkldnn=False`` **不是可选性能调优**。

    实测（见 ok/10 §七之十一）：paddlepaddle 3.3.1 的 oneDNN 后端在推理时抛
    ``NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support``
    （``onednn_instruction.cc:116``）—— 那是 paddle 内部 PIR 属性与 oneDNN
    路径没实现，**不是我们的用法问题**。

    关掉之后立刻可用，且准确率明显更高（同一张图上读出了 RapidOCR 读错的
    「福建南平」，而 RapidOCR 给「福建南火」）。所以这个参数**必须留着**：
    为了"更快"把它改成 True 会得到一个完全不能用的引擎，而报错是
    PIR/oneDNN 术语，与"识别中文邮戳"毫无字面关联。详见 ok/09 的 E-11。
    """
    try:
        # 不加 inline `type: ignore`：paddleocr 是**可选手工安装**依赖，
        # 装了/没装会分别触发 import-untyped / import-not-found，而
        # strict + warn_unused_ignores 下无论写哪个都会有一半情形报
        # "unused ignore"。由 pyproject 的 [[tool.mypy.overrides]]
        # （module = paddleocr.*, ignore_missing_imports = true）统一兜住。
        from paddleocr import PaddleOCR
    except Exception as exc:
        return None, f"无法导入 paddleocr：{type(exc).__name__}: {exc}"
    last = ""
    for kwargs in (
        {
            "lang": "ch",
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "enable_mkldnn": False,
        },
        {"lang": "ch", "enable_mkldnn": False},
    ):
        try:
            return _PaddleAdapter(PaddleOCR(**kwargs)), ""
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
    return None, f"PaddleOCR 构造失败：{last}"


def _build_rapidocr() -> tuple[_Engine | None, str]:
    try:
        # rapidocr-onnxruntime 不带类型标注（无 py.typed），strict mypy 会报
        # import-untyped。用行级 ignore 而不是放宽整个模块 ——
        # 只有这一行是"外部无类型边界"，别处仍受 strict 检查。
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-untyped]
    except Exception as exc:
        # 宽捕异常是有意的：缺包（ImportError）、缺共享库（OSError）、
        # onnxruntime 版本不兼容（AttributeError）都算"引擎不可用"，
        # 而调用方只需要知道"能不能用"与"为什么"。
        return None, f"无法导入 rapidocr_onnxruntime：{type(exc).__name__}: {exc}"
    try:
        return RapidOCR(), ""
    except Exception as exc:
        return None, f"RapidOCR 初始化失败（模型加载？）：{type(exc).__name__}: {exc}"


class _PaddleAdapter:
    """把 PaddleOCR 3.x 的返回**归一成 RapidOCR 的形状** ``[box, text, conf]``。

    为什么要归一：调用方（:func:`recognize`）只该知道一种数据形状。
    两个引擎的 API 差异（`predict()` 返回带 `rec_texts`/`rec_scores` 的对象，
    而 RapidOCR 直接返回 `[box, text, conf]`）挡在这一层里，
    否则调用方要为每个引擎写一遍分支。
    """

    def __init__(self, engine: object) -> None:
        self._engine = engine

    def __call__(self, img: np.ndarray) -> tuple[list[list[object]], list[float]]:
        fn = getattr(self._engine, "predict", None) or getattr(self._engine, "ocr", None)
        if fn is None:  # pragma: no cover - 不会发生，但不静默
            raise RuntimeError("PaddleOCR 没有 predict/ocr 入口")
        raw = fn(img)
        out: list[list[object]] = []
        for item in raw if isinstance(raw, list) else [raw]:
            texts = item.get("rec_texts") if hasattr(item, "get") else None
            if not texts:
                continue
            scores = item.get("rec_scores") if hasattr(item, "get") else None
            polys = None
            if hasattr(item, "get"):
                polys = item.get("rec_polys") or item.get("dt_polys")
            for i, text in enumerate(texts):
                box = (
                    [[float(p[0]), float(p[1])] for p in polys[i]]
                    if polys is not None and i < len(polys)
                    else [[0.0, 0.0]] * 4
                )
                conf = float(scores[i]) if scores is not None and i < len(scores) else 1.0
                out.append([box, str(text), conf])
        return out, []


def get_engine(name: str | None = None) -> tuple[_Engine | None, str]:
    """取进程级单例引擎。失败结果也被缓存 —— 见模块开头。

    ``name`` 为 None 时用环境变量 ``PMTRIP_OCR_ENGINE``（默认 rapidocr）。
    """
    global _engine, _engine_error, _engine_ready
    resolved = (name or os.environ.get("PMTRIP_OCR_ENGINE") or ENGINE_RAPIDOCR).strip().lower()
    if resolved not in SUPPORTED_ENGINES:
        logger.warning("未知的 OCR 引擎 %r，回落到 %s", resolved, ENGINE_RAPIDOCR)
        resolved = ENGINE_RAPIDOCR

    with _lock:
        if not _engine_ready:
            _engine, _engine_error = _build_engine(resolved)
            _engine_ready = True
            if _engine is None:
                logger.warning("OCR 引擎不可用（%s）：%s", resolved, _engine_error)
            else:
                logger.info("OCR 引擎就绪：%s", resolved)
        return _engine, _engine_error or ""


def reset_engine() -> None:
    """丢掉缓存的引擎。测试用（也让"装好依赖后不必重启进程"成为可能）。"""
    global _engine, _engine_error, _engine_ready
    with _lock:
        _engine = None
        _engine_error = None
        _engine_ready = False


def _prepare(image: np.ndarray, *, orientation: str) -> np.ndarray:
    """喂给 OCR 前的几何预处理。

    ## 三个都是实测撞出来的

    **① 竖向文字要转 90°** —— 不转的话 OCR 会把每个字当成一个独立的
    噪声块，结果是**空文本而不是错文本**，所以这个错误不会报错，
    只会让"竖排名称的戳"永远读不出来。

    **② 留白边（pad）—— 这是效果最显著的一条。**
    实测同一块展开条带（97×334，肉眼可清楚读出「福建南平」）：

    | 处理 | RapidOCR 结果 |
    | --- | --- |
    | 原样喂 | 只读出「本」(0.64) |
    | 放大 2 倍 | 只读出「本」(0.63) |
    | 二值化 + 放大 | **None**（什么都没读到） |
    | **上下留白后喂** | **「南」(0.999)** |

    **放大没用、二值化有害、留白最有效。** 原因是检测网络按输入的高度
    归一化：条带正好被字填满时，笔画被当成巨大的块，检测框只能框住
    最右侧一个字；留白之后字恢复到正常比例，检测与识别都正常。
    所以这里**先 pad 再考虑放大**。

    **③ 灰度要转三通道** —— RapidOCR 期望 3 通道输入；直接喂灰度图
    会得到空结果（同样是静默失败）。
    """
    img = image
    if orientation == "vertical":
        # 逆时针 90°：把竖排文字转成横排
        img = np.rot90(img, k=3)

    if img.ndim == 2:
        img = np.stack([img] * 3, axis=-1)
    elif img.shape[2] == 4:
        img = img[:, :, :3]

    h, w = img.shape[:2]
    if h == 0 or w == 0:
        return np.ascontiguousarray(img)

    # ② 留白：上下各 35% 高度，左右各 2% 宽度。
    # 上下给得多，因为问题出在"字占满高度"。
    pad_y = max(8, int(h * 0.35))
    pad_x = max(4, int(w * 0.02))
    canvas = np.full((h + 2 * pad_y, w + 2 * pad_x, 3), 255, np.uint8)
    canvas[pad_y : pad_y + h, pad_x : pad_x + w] = img
    img = canvas

    # 短边仍偏小时再放大，保证检测网络有足够的绝对尺寸
    h2, w2 = img.shape[:2]
    if min(h2, w2) < 48:
        scale = max(1.5, 48.0 / max(1, min(h2, w2)))
        import cv2

        img = cv2.resize(img, (int(w2 * scale), int(h2 * scale)), interpolation=cv2.INTER_CUBIC)
    return np.ascontiguousarray(img)


def recognize(
    image: np.ndarray,
    *,
    orientation: str = "horizontal",
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> OcrResult:
    """识别一块区域。**永不抛异常** —— 不可用时返回 ``available=False``。

    为什么永不抛：这块的调用方是管线，而"引擎没装好"是一个**预期内的
    运行状态**（`OCR_ENGINE=none`、依赖缺失、模型下载失败），
    不是异常。管线该做的是把它记为"该字段未识别"并继续，
    而不是整条任务失败。
    """
    engine, err = get_engine()
    if engine is None:
        return OcrResult(available=False, reason=err)

    if image.size == 0:
        return OcrResult(available=True, reason="输入为空", lines=[])

    prepared = _prepare(image, orientation=orientation)
    try:
        raw, elapsed = engine(prepared)
    except Exception as exc:
        # 引擎在**推理时**抛异常（输入尺寸不合、内存不足）也算不可用，
        # 但原因要说清是"这一次失败"还是"引擎没起来"。
        return OcrResult(
            available=False,
            reason=f"推理失败：{type(exc).__name__}: {exc}",
        )

    lines: list[OcrLine] = []
    for item in raw or []:
        # RapidOCR 的每项是 [box, text, confidence]
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        box, text, conf = item[0], item[1], item[2]
        try:
            confidence = float(conf)
        except (TypeError, ValueError):
            continue
        text = str(text).strip()
        if not text or confidence < min_confidence:
            continue
        lines.append(OcrLine(text=text, confidence=confidence, box=list(box)))

    return OcrResult(
        lines=lines,
        available=True,
        elapsed=list(elapsed) if isinstance(elapsed, (list, tuple)) else [],
    )
