#!/usr/bin/env python3
"""配置一致性检查：代码读取的环境变量 ↔ 文档与编排里记录的变量。

**为什么需要**：这三处是各自独立维护的 ——

  * ``config.py`` 的 Settings 决定**实际读取**什么
  * ``.env.example`` 告诉用户**该设置**什么
  * compose 文件（含 dev 与 infra/compose 下的 overlay）决定把什么**传进容器**

任何一处漏了，症状都是"首次启动以一个难懂的错误失败"，或者更糟 ——
应用**静默地用了默认值**，而用户以为自己设置的那个生效了。这与编码问题是
同一类：开发时完全看不见。

检查项：

1. **必填却没记录** —— 没有默认值的字段必须出现在 ``.env.example`` 里，
   否则用户无从知道要设置它。（这是最严重的一类。）
2. **compose 引用但没记录** —— 会在启动时报"必需变量缺失"。
3. **声明了但代码从不读** —— 死配置，会让人以为调它有用。
4. **文档里有但没人用** —— 过时的文档。

用法：
    python infra/scripts/check-config.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api" / "src"))

os.environ.setdefault("SECRET_KEY", "config-check-only-not-a-real-secret")
os.environ.setdefault("DATA_DIR", str(ROOT / "tmp" / "rtdata-configcheck"))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

#: 这些不是用户配置项，是从环境读取的内部/派生的量，不要求在 .env.example 里出现
INTERNAL = {
    "secret_key_generated",  # 由 model_validator 根据 SECRET_KEY 是否提供来设置
}

COMPOSE_GLOBS = (
    "docker-compose.yml",
    "docker-compose.dev.yml",
    "infra/compose/*.yml",
    "infra/compose/*.yml.example",
)

#: 允许出现在 .env.example 但不被 config.py / compose 使用的前缀。
#: 每一条都要写清楚理由 —— 否则这个白名单会变成"把问题藏起来"的地方。
ALLOWED_UNUSED: dict[str, str] = {}


def strip_comments(text: str) -> str:
    """去掉 YAML 的整行注释。

    必须去掉：`docker-compose.yml` 的注释里就有一句
    "本文件只用 ${VAR} 插值" —— 那是**字面示例**，不是真的引用。
    第一版没去注释，于是报了一个假问题。
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def compose_vars() -> dict[str, list[str]]:
    """变量名 -> 引用它的文件列表。"""
    found: dict[str, list[str]] = {}
    for pattern in COMPOSE_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            text = strip_comments(path.read_text(encoding="utf-8"))
            for var in set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", text)):
                found.setdefault(var, []).append(path.name)
    return found


def env_example_keys() -> list[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=", text)


def searchable_text(exclude_config: bool) -> str:
    """所有可能"使用"某个配置项的文件拼在一起。

    不能只扫 Python —— `BACKUP_KEEP_DAYS` 是用在 `pmtrip.ps1` / `Makefile` 里的，
    `MODELS_DIR` / `PUBLIC_DATA_DIR` 是被 compose 的卷挂载引用的。第一版只扫了
    `apps/api/src/**/*.py`，于是报了十几条假阳性。
    """
    exts = {
        ".py", ".sh", ".ps1", ".yml", ".yaml", ".toml", ".ini", ".mako", ".md",
        # 前端也可能读环境变量（Vite 的 import.meta.env、vite.config.ts 的 process.env）
        ".ts", ".tsx", ".js", ".jsx", ".json",
    }
    skip_dirs = {".git", "node_modules", "dist", "tmp", ".test-tmp", "__pycache__"}
    parts: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.name == "check-config.py":
            continue
        parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def field_is_used(field: str, haystack: str, config_text: str) -> bool:
    """字段是否真的被用到。

    在 `config.py` 里要**排除它自己的声明行**（`app_name: str = ...`），
    否则声明本身就会被算成一次"使用"。
    """
    pattern = re.compile(rf"\b{re.escape(field)}\b", re.IGNORECASE)
    if pattern.search(haystack):
        return True
    # config.py 内部以 self.<field> 使用的情况
    config_without_decls = "\n".join(
        line
        for line in config_text.splitlines()
        if not re.match(rf"^\s*{re.escape(field)}\s*:", line)
    )
    return bool(pattern.search(config_without_decls))


def main() -> int:
    from postmarktrip.config import Settings

    prefix = str(Settings.model_config.get("env_prefix") or "")

    #: 每个字段**所有可接受**的环境变量名。
    #:
    #: 曾经只按 `prefix + 字段名` 推一个名字。加了 `validation_alias` 之后
    #: （见 `config.ocr_engine`：它同时接受 `OCR_ENGINE` 与 `PMTRIP_OCR_ENGINE`），
    #: 那样推出来的名字会漏掉别名 —— 于是别名被报成"没记录在 .env.example 里"，
    #: 而那正是这次要修的那个坑。所以改成把别名一起列出来，逐个核对。
    def acceptable_names(name: str, field: object) -> list[str]:
        names = [(prefix + name).upper()]
        alias = getattr(field, "validation_alias", None)
        choices = getattr(alias, "choices", None)
        if choices:
            for choice in choices:
                if isinstance(choice, str):
                    names.append(choice.upper())
        # 去重且保持顺序
        seen: list[str] = []
        for item in names:
            if item not in seen:
                seen.append(item)
        return seen

    fields: dict[str, tuple[str, bool]] = {}
    for name, field in Settings.model_fields.items():
        if name in INTERNAL:
            continue
        for env_name in acceptable_names(name, field):
            fields[env_name] = (name, field.is_required())

    documented = set(env_example_keys())
    composed = compose_vars()
    haystack = searchable_text(exclude_config=True)
    config_text = (ROOT / "apps" / "api" / "src" / "postmarktrip" / "config.py").read_text(
        encoding="utf-8"
    )

    problems: list[str] = []
    notes: list[str] = []

    # 1. 必填却没记录
    for env_name, (field, required) in sorted(fields.items()):
        if required and env_name not in documented:
            problems.append(
                f"必填的 {env_name}（config.py 的 {field}）不在 .env.example 里"
                f" —— 用户无从知道要设置它"
            )

    # 2. compose 引用但没记录
    for var, where in sorted(composed.items()):
        if var not in documented:
            problems.append(
                f"compose 引用了 {var}（{', '.join(sorted(set(where))) }），"
                f"但它不在 .env.example 里"
            )

    # 3. 声明了但代码从不读
    #
    #    一个字段有多个可接受名字时，**只要有任意一个名字在仓库里被用到就够了**。
    #    否则 `OCR_ENGINE` 会因为"别名 `PMTRIP_OCR_ENGINE` 被用到"这件事
    #    被误报成死配置。
    used_fields = {
        field_name
        for env_name, (field_name, _required) in fields.items()
        if field_is_used(field_name, haystack, config_text)
    }
    reported: set[str] = set()
    for env_name, (field, _required) in sorted(fields.items()):
        if field in used_fields or field in reported:
            continue
        reported.add(field)
        notes.append(
            f"{env_name}（config.py 的 {field}）在**整个仓库**里都找不到使用 —— "
            f"调它不会有任何效果"
        )

    # 4. 文档里有但**整个仓库**都没人读
    #
    #    注意这里必须查使用情况，不能只看"是不是 Settings 字段或 compose 变量"：
    #    API_APP 是 entrypoint.sh 读的（`${API_APP:-...}`），
    #    WEB_DEV_PORT 是 pmtrip.ps1 与 Makefile 读的 —— 它们既不在 Settings 里，
    #    也不出现在 compose 的插值里。第一版据此把它们报成"没人用"，是假阳性。
    for key in sorted(documented):
        if key in fields or key in composed or key in ALLOWED_UNUSED:
            continue
        if field_is_used(key.lower(), haystack, config_text):
            continue
        notes.append(f".env.example 里的 {key} 在整个仓库里都找不到使用 —— 可能是过时的文档")

    print(
        f"Settings 字段 {len(fields)} 个；.env.example 键 {len(documented)} 个；"
        f"compose 引用变量 {len(composed)} 个（跨 {len(set(sum(composed.values(), [])))} 个文件）"
    )
    print()

    print("=== 问题 ===")
    if problems:
        for p in problems:
            print(f"  FAIL  {p}")
    else:
        print("  无")

    print()
    print("=== 提示（不算错） ===")
    if notes:
        for n in notes:
            print(f"  -  {n}")
    else:
        print("  无")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
