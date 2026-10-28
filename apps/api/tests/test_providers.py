"""Provider 探测的测试。

**这个文件防的是一类特定的错**：配置写了、代码有了、但依赖其实没装 ——
于是系统"以为自己能识别"。docs/ok/06 §二 的未验证清单里，
大部分条目的根因都是这种假设，而不是代码 bug。

所以这里的关键断言是：``enabled``（用户意图）与 ``available``（探测结果）
必须能被**分别**观察到，而不是被压成一个布尔值。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import pytest

from postmarktrip import providers
from postmarktrip.providers import Capability, probe_all, summary

# --------------------------------------------------------------------------- #
#  结构
# --------------------------------------------------------------------------- #

EXPECTED_KINDS = {"cv", "ocr", "vlm", "catalog", "embed", "scan"}


def test_all_six_provider_kinds_are_declared() -> None:
    """docs/ok/04 §5.1 定义了六类 Provider。少了哪一类都说明有构想没落地。"""
    kinds = {c.kind for c in probe_all()}
    assert kinds == EXPECTED_KINDS, f"缺：{EXPECTED_KINDS - kinds}；多：{kinds - EXPECTED_KINDS}"


def test_probe_is_pure_and_repeatable() -> None:
    """探测必须无副作用：它会被 doctor 反复调用。"""
    first = probe_all()
    second = probe_all()
    assert [(c.name, c.enabled, c.available) for c in first] == [
        (c.name, c.enabled, c.available) for c in second
    ]


def test_never_raises() -> None:
    """探测永远不能抛 —— doctor 自己崩掉比它检查的东西坏掉更难查。"""
    for cap in probe_all():
        assert isinstance(cap, Capability)


# --------------------------------------------------------------------------- #
#  三态语义：ready / blocked / disabled
# --------------------------------------------------------------------------- #


def test_ready_and_blocked_are_mutually_exclusive() -> None:
    for cap in probe_all():
        assert not (cap.ready and cap.blocked)


def test_blocked_means_enabled_but_unavailable() -> None:
    """blocked 是最需要用户处理的状态，定义必须精确。"""
    for cap in probe_all():
        assert cap.blocked == (cap.enabled and not cap.available)


# --------------------------------------------------------------------------- #
#  CV：本地管线的必需品
# --------------------------------------------------------------------------- #


def test_cv_is_always_enabled() -> None:
    """CV 不是"用户可关"的东西 —— 没有它 v1.0 的核心能力无从谈起。

    所以它恒定 enabled=True，由 available 说明到底在不在。
    """
    cv = next(c for c in probe_all() if c.kind == "cv")
    assert cv.enabled is True


def test_cv_availability_matches_reality() -> None:
    """不硬编码期望值，而是**对着真实环境**断言。

    在没装 recognition extra 的环境里必须是 False；装了**且能真的 import** 时
    必须是 True。这条在两种环境下都成立，所以它测的是"探测有没有说谎"，
    而不是"依赖装没装"。
    """
    cv = next(c for c in probe_all() if c.kind == "cv")
    expected = (
        providers._import_problem("cv2") is None and providers._import_problem("numpy") is None
    )
    assert cv.available is expected


# --------------------------------------------------------------------------- #
#  探测必须"真的 import"，而不是只查模块在不在
# --------------------------------------------------------------------------- #


def test_module_available_really_imports() -> None:
    """**这条防的是探测本身说谎**，是一个真实踩过的坑。

    `find_spec("cv2")` 返回真只说明包在磁盘上，**不说明能被加载**。
    实测形态：`rapidocr-onnxruntime` 的元数据把 `opencv-python`（非 headless）
    拖了进来，而它需要 X11；在 `python:3.12-slim` 里 import cv2 报

        ImportError: libxcb.so.1: cannot open shared object file

    当时 `providers` 报 `[就绪] opencv`，于是"以为自己能识别" —— 而识别栈
    实际上完全不可用。这正是本模块开头写的"探测而非假设"要防的事。

    这里用一个**"找得到、但加载时会抛"**的假模块来复现那个形态 ——
    它和"缺 .so 的 C 扩展"在 import 机制上是同一回事。
    """
    import importlib.abc
    import importlib.machinery

    class _BoomLoader(importlib.abc.Loader):
        def create_module(self, spec: object) -> None:
            return None

        def exec_module(self, module: object) -> None:
            raise ImportError("libxcb.so.1: cannot open shared object file")

    class _BoomFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, name: str, path: object = None, target: object = None):
            if name != "pmtrip_broken_probe":
                return None
            return importlib.machinery.ModuleSpec(name, _BoomLoader())

    providers._cached_import.cache_clear()
    sys.meta_path.insert(0, _BoomFinder())
    try:
        # ① 模块"找得到"（这正是 find_spec 会误判为可用的情形）
        assert importlib.util.find_spec("pmtrip_broken_probe") is not None
        # ② 但探测必须报不可用，而不是报就绪
        assert providers._module_available("pmtrip_broken_probe") is False
        # ③ 而且要说清原因
        problem = providers._import_problem("pmtrip_broken_probe")
        assert problem is not None and "libxcb" in problem
    finally:
        sys.meta_path.pop(0)
        sys.modules.pop("pmtrip_broken_probe", None)
        providers._cached_import.cache_clear()


def test_failed_import_reason_is_reported() -> None:
    """不可用时必须**说出原因**，而不是一句"不可用"。

    "缺少 opencv-python-headless" 与 "libxcb.so.1 不存在" 是两条完全不同的
    排障路径；压成一句话会让用户去装一个已经装好的包。
    """
    providers._cached_import.cache_clear()
    problem = providers._import_problem("pmtrip_definitely_missing_module")
    assert problem is not None
    assert "Error" in problem, f"错误原因看起来不对：{problem!r}"

    providers._cached_import.cache_clear()
    assert providers._import_problem("json") is None


def test_probe_cv_explains_missing_system_library() -> None:
    """缺系统库时，detail 必须把"包已装但缺 .so"这层说出来。

    否则用户会去 `pip install opencv-python-headless` —— 而它已经装了。
    """
    providers._cached_import.cache_clear()
    original = providers._cached_import

    def fake(name: str) -> str | None:
        if name == "cv2":
            return "ImportError: libxcb.so.1: cannot open shared object file"
        return None

    providers._cached_import = fake  # type: ignore[assignment]
    try:
        cap = providers.probe_cv()
        assert cap.available is False
        assert "libxcb" in cap.detail
        assert "headless" in cap.detail, "应当提示'多半装上了非 headless 的 opencv-python'"
    finally:
        providers._cached_import = original  # type: ignore[assignment]
        providers._cached_import.cache_clear()


# --------------------------------------------------------------------------- #
#  OCR：none 是受支持的状态
# --------------------------------------------------------------------------- #


def test_ocr_none_is_not_an_error() -> None:
    """D5 要求"关掉所有云 Provider 后系统仍完整可用"。none 不是错误。"""
    ocr = next(c for c in probe_all() if c.kind == "ocr")
    if ocr.name == "ocr" and not ocr.enabled:
        assert "none" in ocr.detail
        assert not ocr.blocked, "未启用不等于阻塞"


def test_unknown_ocr_engine_is_reported_not_crashed(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法取值不该让 doctor 崩 —— 而要如实报"未知"。"""
    monkeypatch.setattr(providers.settings, "ocr_engine", "definitely-not-an-engine")
    cap = providers.probe_ocr()
    assert cap.available is False
    assert "definitely-not-an-engine" in cap.detail


# --------------------------------------------------------------------------- #
#  安全：Key 不能出现在探测结果里
# --------------------------------------------------------------------------- #


def test_keys_never_appear_in_probe_output() -> None:
    """硬约束（docs/ok/04 §5.2 第 2 条）：Key 绝不进日志。

    探测结果会被 /api/health 返回、被 doctor 打印。所以这里塞几个
    形状明显的假 Key，断言**一个都不能**出现在任何 detail 里。
    """
    sentinel_colnect = "COLNECT-SENTINEL-0123456789"
    sentinel_vlm = "VLM-SENTINEL-abcdefghij"
    originals = (
        providers.settings.colnect_api_key,
        providers.settings.vlm_api_key,
        providers.settings.vlm_enabled,
    )
    try:
        providers.settings.colnect_api_key = sentinel_colnect
        providers.settings.vlm_api_key = sentinel_vlm
        providers.settings.vlm_enabled = True

        # Capability 是 slots dataclass，没有 __dict__ —— 用 asdict
        blob = json.dumps([asdict(c) for c in probe_all()], ensure_ascii=False, default=str)
        assert sentinel_colnect not in blob, "Colnect Key 泄漏进了探测结果"
        assert sentinel_vlm not in blob, "VLM Key 泄漏进了探测结果"
    finally:
        (
            providers.settings.colnect_api_key,
            providers.settings.vlm_api_key,
            providers.settings.vlm_enabled,
        ) = originals


def test_catalog_reports_configured_but_not_verified() -> None:
    """有 Key 只说明"已配置" —— 可用性没联网校验过，文案必须说清这一点。"""
    original = providers.settings.colnect_api_key
    try:
        providers.settings.colnect_api_key = "some-key"
        cap = providers.probe_catalog()
        assert cap.enabled is True
        assert "未联网校验" in cap.detail
    finally:
        providers.settings.colnect_api_key = original


# --------------------------------------------------------------------------- #
#  summary
# --------------------------------------------------------------------------- #


def test_summary_shape() -> None:
    s = summary()
    for key in ("pipeline_version", "ocr_ready", "cv_ready", "blocked", "recognition_ready"):
        assert key in s, f"summary 缺字段 {key}"
    assert isinstance(s["blocked"], list)


def test_pipeline_version_is_not_empty() -> None:
    """pipeline_version 要写进每条 observation；空值会让"重跑并对比新旧结果"失效。"""
    assert providers.PIPELINE_VERSION.strip()
    assert summary()["pipeline_version"] == providers.PIPELINE_VERSION


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #


def test_cli_json_output_is_parseable() -> None:
    """``python -m postmarktrip.providers --json`` 必须产出可解析的 JSON。

    用子进程跑，因为要验证的是 ``__main__`` 子模块真的存在 ——
    缺它时报的是 "is a package and cannot be directly executed"，
    那条信息不会告诉你要改用 python -c。
    """
    result = subprocess.run(
        [sys.executable, "-m", "postmarktrip.providers", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert {item["kind"] for item in payload} == EXPECTED_KINDS


def test_cli_text_report_mentions_every_provider() -> None:
    text = providers.format_report()
    for cap in probe_all():
        assert cap.name in text


def test_cli_exit_code_signals_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """有阻塞项就非 0 —— 让 CI / doctor 能直接判失败。

    用 monkeypatch 造一个阻塞态，这样在"依赖已装"的环境里这条测试依然有效。
    """
    blocked = Capability(
        name="fake", kind="ocr", enabled=True, available=False, detail="假的阻塞项"
    )
    monkeypatch.setattr(providers, "PROBES", (lambda: blocked,))
    assert providers.main([]) == 1


def test_cli_exit_code_zero_when_nothing_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    ready = Capability(name="fake", kind="ocr", enabled=True, available=True)
    monkeypatch.setattr(providers, "PROBES", (lambda: ready,))
    assert providers.main([]) == 0


def test_capability_is_frozen() -> None:
    """探测结果是快照，不该被下游改。

    断言 ``FrozenInstanceError`` 而不是笼统的 ``Exception``：后者对"这里到底
    该抛什么"没有表态，连属性名打错都会让它通过。
    """
    cap = next(iter(probe_all()))
    with pytest.raises(FrozenInstanceError):
        cap.name = "changed"  # type: ignore[misc]


def test_report_path_is_importable_as_module() -> None:
    """``providers/__main__.py`` 必须存在（上面 CLI 测试真正验证的也是它）。"""
    main_module = Path(providers.__file__).parent / "__main__.py"
    assert main_module.is_file(), "缺 providers/__main__.py，python -m 会失败"
