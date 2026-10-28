#!/usr/bin/env python3
"""手写的静态检查：给这份 Web 前端做两件事。

## 为什么不是 tsc

本会话里 WSL 被拒（`Wsl/E_ACCESSDENIED`），Windows 侧没有 node/npm，
npm registry 也连不上 —— 装不了 typescript，所以**没法做真正的类型检查**。
`node --check` 也不行：它不认 `.tsx`，也不对 `.ts` 做类型擦除。

所以退一步做两件**机械可查**的事，它们恰好覆盖最容易犯的错：

1. **括号/引号平衡**（跳过字符串、模板串、注释）—— 抓漏写的 `}`、`/>`、
   没闭合的模板串。这类错会让整个构建失败。
2. **命名的 import 必须在目标文件里真的有 export** —— 抓"引用了不存在的
   导出"。这类错我改完就发现了一次（`CardList` 里 import 了没导出的东西）。

**它不是类型检查**，覆盖不了类型错误。这一条必须如实说。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parents[1]
SRC = WEB / "src"

#: 外部包（react / axios 等）不做导出核对 —— 没有它们的源码
EXTERNAL = re.compile(r"^[^./]")

OPEN = {"(": ")", "[": "]", "{": "}"}
CLOSE = {")": "(", "]": "[", "}": "{"}


class BalanceError(Exception):
    pass


def scan_balance(text: str, where: str) -> list[str]:
    """括号平衡。返回错误列表（空 = 通过）。

    跳过：行注释、块注释、单/双引号字符串、模板串（含 `${}` 里的表达式）。
    """
    errors: list[str] = []
    stack: list[tuple[str, int]] = []
    i = 0
    line = 1
    n = len(text)

    while i < n:
        ch = text[i]

        if ch == "\n":
            line += 1
            i += 1
            continue

        # 注释
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            if j < 0:
                errors.append(f"{where}:{line} 块注释没有闭合")
                return errors
            line += text.count("\n", i, j)
            i = j + 2
            continue

        # 字符串与模板串
        if ch in "\"'`":
            quote = ch
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "\n":
                    if quote != "`":
                        errors.append(f"{where}:{line} 字符串没有闭合（换行前缺少 {quote}）")
                        return errors
                    line += 1
                if text[j] == quote:
                    break
                j += 1
            else:
                errors.append(f"{where}:{line} 字符串/模板串没有闭合")
                return errors
            i = j + 1
            continue

        if ch in OPEN:
            stack.append((ch, line))
            i += 1
            continue

        if ch in CLOSE:
            if not stack:
                errors.append(f"{where}:{line} 多余的 {ch!r}")
                return errors
            opener, opened_at = stack.pop()
            if OPEN[opener] != ch:
                errors.append(
                    f"{where}:{line} {ch!r} 与第 {opened_at} 行的 {opener!r} 不匹配"
                )
                return errors
            i += 1
            continue

        i += 1

    if stack:
        opener, opened_at = stack[-1]
        errors.append(f"{where}: 第 {opened_at} 行的 {opener!r} 没有闭合")
    return errors


IMPORT_RE = re.compile(
    r"import\s+(?:type\s+)?(?:([\w$]+)\s*,\s*)?(?:\{([^}]*)\}|\*\s+as\s+[\w$]+)?\s*from\s*['\"]([^'\"]+)['\"]",
    re.S,
)
EXPORT_RE = re.compile(
    r"export\s+(?:declare\s+)?(?:const|let|var|function|async\s+function|class|interface|type|enum)\s+([\w$]+)"
)
EXPORT_LIST_RE = re.compile(r"export\s*\{([^}]*)\}")
EXPORT_DEFAULT_RE = re.compile(r"export\s+default\b")


def exports_of(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    names = set(EXPORT_RE.findall(text))
    for chunk in EXPORT_LIST_RE.findall(text):
        for part in chunk.split(","):
            part = part.strip()
            if not part:
                continue
            if " as " in part:
                part = part.split(" as ", 1)[1].strip()
            names.add(part)
    if EXPORT_DEFAULT_RE.search(text):
        names.add("default")
    return names


def resolve(spec: str, importer: Path) -> Path | None:
    """把 import 说明符解析成文件。

    ⚠️ `..` 必须**手工折叠**（`os.path.normpath`），不能用 `Path.resolve()`：
    后者在 Windows 上遇到不存在的中间目录时会保留字面的 `..`，
    于是 `../../../shared/api` 被算成 `src/features/recognize/simple` 之外的东西，
    报出一串假警报 —— 正是 `ok/12` §七 记的那类错：**报"缺失"之前先确认路径约定**。
    """
    if EXTERNAL.match(spec):
        return None
    if not spec.startswith("."):
        return None
    joined = os.path.normpath(os.path.join(str(importer.parent), *spec.split("/")))
    base = Path(joined)
    for cand in (
        base.with_suffix(".ts"),
        base.with_suffix(".tsx"),
        base / "index.ts",
        base / "index.tsx",
        base,
    ):
        if cand.is_file():
            return cand
    return None


def main() -> int:
    files = sorted(list(SRC.rglob("*.ts")) + list(SRC.rglob("*.tsx")))
    errors: list[str] = []
    checked_imports = 0

    for path in files:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(WEB)
        errors.extend(scan_balance(text, str(rel)))

        for _default, named, spec in IMPORT_RE.findall(text):
            target = resolve(spec, path)
            if target is None:
                if not EXTERNAL.match(spec):
                    errors.append(f"{rel}: 无法解析的 import 路径 {spec!r}")
                continue
            available = exports_of(target)
            for raw in named.split(","):
                name = raw.strip()
                if not name:
                    continue
                # `type Foo` / `Foo as Bar` 都要取下真名
                name = re.sub(r"^type\s+", "", name)
                if " as " in name:
                    name = name.split(" as ", 1)[0].strip()
                checked_imports += 1
                if name not in available:
                    errors.append(
                        f"{rel}: import {{ {name} }} from {spec!r} —— "
                        f"{target.name} 里没有导出 {name}"
                    )

    print(f"检查了 {len(files)} 个文件、{checked_imports} 条命名 import")
    if errors:
        print(f"\n发现 {len(errors)} 个问题：")
        for err in errors:
            print("  -", err)
        return 1
    print("括号平衡与命名 import 都对上了（**这不是类型检查**）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
