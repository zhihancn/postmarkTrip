"""识别栈依赖的**契约测试**。

## 这个文件防的是一类特别隐蔽的错

`rapidocr-onnxruntime` 的元数据声明的是 `opencv-python`（**非** headless），
而本项目用的是 `opencv-python-headless`。两者同时装时**非 headless 的那个生效**，
它需要 X11 —— 在 `python:3.12-slim` 里 import cv2 直接报

    ImportError: libxcb.so.1: cannot open shared object file

后果是**识别栈完全不可用，而 `pip list` 看起来一切正常**。这个形态实测撞到过。

Dockerfile 的解法是"分两步 + `--no-deps`"（见 apps/api/Dockerfile 的注释）。
代价是 **rapidocr 将来新增的依赖不会自动跟上** —— 于是它成了一份需要
**显式维护**的契约，放在下面这张表里。

## 为什么把 rapidocr 的依赖表"抄"进测试

因为它是**外部包的元数据**：我们既不能改它，也不该在每次测试时去联网读它
（测试必须离线可跑）。把已知的那一份冻结在这里，让**变更变成一次显式编辑**，
而不是"某天镜像悄悄坏了"。

这份表来自 PyPI 上 `rapidocr-onnxruntime` 1.4.4 的 `requires_dist`。
升级 rapidocr 时，对着 PyPI 页面更新 `RAPIDOCR_REQUIRES` 即可。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
PYPROJECT = APP_DIR / "pyproject.toml"
DOCKERFILE = APP_DIR / "Dockerfile"

#: rapidocr-onnxruntime 1.4.4 的 requires_dist（原文照抄，含版本约束）。
#: 升级 rapidocr 时必须**人工**更新这里 —— 见模块开头。
RAPIDOCR_REQUIRES: tuple[str, ...] = (
    "pyclipper>=1.2.0",
    "opencv-python>=4.5.1.48",
    "numpy<3.0.0,>=1.19.5",
    "six>=1.15.0",
    "Shapely!=2.0.4,>=1.7.1",
    "PyYAML",
    "Pillow",
    "onnxruntime>=1.7.0",
    "tqdm",
)

#: 这些由**基础依赖**（非 extra）提供，或由 headless 版替代，因此不出现在 extra 里。
PROVIDED_ELSEWHERE = {
    # 基础依赖里有 pillow>=11.0
    "pillow",
    # 由 extra 里的 opencv-python-headless 替代（**这正是整个问题的核心**）
    "opencv-python",
}


def _normalize(name: str) -> str:
    """PEP 503 规范化：下划线/点/连字符等价，大小写不敏感。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(spec: str) -> str:
    """从 "shapely!=2.0.4,>=1.7.1" 里取出包名。"""
    return _normalize(re.split(r"[<>=!~\[; ]", spec.strip(), maxsplit=1)[0])


def _extra_names(extra: str) -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = data["project"]["optional-dependencies"][extra]
    return {_requirement_name(d) for d in deps}


@pytest.fixture(scope="module")
def recognition_extra() -> set[str]:
    return _extra_names("recognition")


def test_extra_exists() -> None:
    assert _extra_names("recognition"), "pyproject 里没有 recognition extra"


def test_extra_covers_every_rapidocr_dependency(recognition_extra: set[str]) -> None:
    """rapidocr 声明的每个依赖，都要在 extra 里（或由别处提供）。

    **这条是防"识别栈悄悄坏掉"的核心。** 若 rapidocr 升级后新增了依赖，
    这里会精确报出缺哪个名字 —— 而不是等镜像构建成功后才发现 import 失败。
    """
    required = {_requirement_name(r) for r in RAPIDOCR_REQUIRES}
    missing = required - recognition_extra - PROVIDED_ELSEWHERE
    assert not missing, (
        f"recognition extra 缺 rapidocr 的依赖：{sorted(missing)}\n"
        f"（rapidocr {len(RAPIDOCR_REQUIRES)} 个依赖，extra 有 {len(recognition_extra)} 个）\n"
        f"修法：在 pyproject.toml 的 recognition 里补上；"
        f"若 rapidocr 升级引入的，同时更新本文件的 RAPIDOCR_REQUIRES。"
    )


def test_extra_does_not_pull_in_headless_conflict(recognition_extra: set[str]) -> None:
    """**extra 里绝不能出现 opencv-python（非 headless）。**

    它就是那个需要 X11、在 slim 镜像里让 cv2 import 失败的包。
    一旦它进了 extra，`pip install -e '.[recognition]'` 就会把它装上，
    而它会**覆盖** headless 版 —— 识别栈再次变成不可用。
    """
    assert "opencv-python" not in recognition_extra, (
        "recognition extra 里出现了 opencv-python（非 headless）—— "
        "它会覆盖 opencv-python-headless 并让 cv2 import 失败（缺 libxcb）"
    )
    assert "opencv-python-headless" in recognition_extra, (
        "recognition extra 必须显式钉住 opencv-python-headless"
    )


def test_extra_includes_rapidocr_itself(recognition_extra: set[str]) -> None:
    """extra 必须包含 rapidocr 本体。

    第一版把它漏了 —— 只在 Dockerfile 里用 `--no-deps` 装它，
    于是**在容器里能跑、在开发者 venv 里 import 失败**。
    那是典型的"两处各装各的"漂移。
    """
    assert "rapidocr-onnxruntime" in recognition_extra


def test_dockerfile_installs_rapidocr_with_no_deps() -> None:
    """Dockerfile 必须用 `--no-deps` 装 rapidocr 本体。

    这是阻止 `opencv-python` 被拖进来的**唯一**手段：
    `--no-deps` 让 pip 不去读 rapidocr 的元数据。
    去掉它，镜像里的 cv2 会重新变成 import 失败。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "pip install --no-deps rapidocr-onnxruntime" in text, (
        "Dockerfile 不再用 --no-deps 装 rapidocr —— opencv-python 会被拖进来，"
        "cv2 将因缺 libxcb 而 import 失败"
    )


def test_dockerfile_self_checks_imports() -> None:
    """构建期必须真的 import 一次识别栈。

    "装上了"与"能 import"是两件事，而后者才是可用的判据。
    没有这条自检，坏掉的镜像会被构建成功并推出去。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "import cv2" in text and "rapidocr_onnxruntime" in text, (
        "Dockerfile 缺少识别栈的 import 自检 —— 装不上应当让构建失败，而不是留到运行时"
    )


def test_ci_does_not_install_shape_that_breaks() -> None:
    """CI 若安装识别栈，必须用与 Dockerfile 相同的方式。

    否则 CI 绿而镜像坏（或反过来），两种都浪费时间。
    """
    ci = APP_DIR.parents[1] / ".github" / "workflows" / "ci.yml"
    if not ci.is_file():
        pytest.skip("没有 CI 工作流")
    text = ci.read_text(encoding="utf-8")
    if "recognition" not in text:
        pytest.skip("CI 没有安装识别栈")
    assert "--no-deps rapidocr-onnxruntime" in text or "recognition" not in text, (
        "CI 里装了识别栈但没有用 --no-deps 装 rapidocr —— 与 Dockerfile 的做法不一致"
    )
