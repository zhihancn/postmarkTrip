#!/usr/bin/env python3
"""检查文本文件的编码约定。

**为什么需要这个**：编码错误不会在开发时显现，只在特定环境里炸。

  * **无 BOM 的 UTF-8 ``.ps1``** → Windows PowerShell 5.1 会按系统 ANSI
    代码页（中文 Windows 上是 GBK）去读，于是所有中文注释与字符串变成乱码，
    脚本**解析失败**。本仓库真的发生过：一次普通的文本编辑把 `pmtrip.ps1`
    的 BOM 剥掉了，而它在 Git Bash / VS Code / CI 里看起来完全正常。
  * **带 BOM 的 ``.sh``** → BOM 落在 shebang 之前，`#!/bin/sh` 不再是文件的
    开头，bash 直接报错。
  * **带 BOM 的 ``alembic.ini``** → Alembic 用 `encoding="locale"` 读它，
    BOM 会变成第一个配置项键名的一部分。
  * **UTF-16 的文本文件** → Linux 容器里的工具链基本都不认。

规则：

  ==================  ==========================
  ``*.ps1``           UTF-8 **with** BOM
  其他文本文件          UTF-8 **without** BOM
  任何文件             不得是 UTF-16
  ==================  ==========================

用法：
    python infra/scripts/check-encoding.py          # 检查（退出码非 0 表示失败）
    python infra/scripts/check-encoding.py --fix    # 就地修正

**输出刻意只用 ASCII 标记**（``OK`` / ``FAIL``），不用 ``✓`` / ``✗``：
本脚本要在任意终端里运行，而中文 Windows 控制台的代码页（GBK）编不出
那两个符号 —— 一个"检查编码"的脚本自己因为编码问题崩掉，就太讽刺了。
中文正文没问题，GBK 能编码中文；编不了的只有符号。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

OK = "OK  "
BAD = "FAIL"
UTF8_BOM = b"\xef\xbb\xbf"
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")

#: 按扩展名跳过（内容可能是任意字节）
BINARY_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".ico", ".pdf", ".zip", ".gz", ".tgz", ".7z", ".woff", ".woff2",
    ".ttf", ".otf", ".eot", ".dump", ".bin", ".so", ".dll", ".exe",
    ".pyc", ".onnx", ".pth", ".safetensors",
}


def tracked_files() -> list[Path]:
    """只用 git 跟踪的文件 —— 不去管构建产物与本地杂物。"""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return [Path(p) for p in out.split("\0") if p]


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def main() -> int:
    fix = "--fix" in sys.argv[1:]
    problems: list[tuple[Path, str, str]] = []  # (路径, 问题, 修法)
    fixed: list[Path] = []

    for path in tracked_files():
        if not path.is_file():
            continue
        if path.suffix.lower() in BINARY_EXTS:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not data or looks_binary(data):
            continue

        wants_bom = path.suffix.lower() == ".ps1"
        has_utf8_bom = data.startswith(UTF8_BOM)
        is_utf16 = data.startswith(UTF16_BOMS)

        if is_utf16:
            problems.append((path, "是 UTF-16 编码", "转成 UTF-8"))
            continue

        if wants_bom and not has_utf8_bom:
            problems.append(
                (
                    path,
                    "是 UTF-8 但没有 BOM",
                    "加 BOM（否则 Windows PowerShell 5.1 会按 ANSI 读，中文变乱码）",
                )
            )
            if fix:
                path.write_bytes(UTF8_BOM + data)
                fixed.append(path)
        elif not wants_bom and has_utf8_bom:
            problems.append(
                (
                    path,
                    "是 UTF-8 with BOM",
                    "去掉 BOM（BOM 会破坏 #!/bin/sh shebang、alembic.ini 的键名等）",
                )
            )
            if fix:
                path.write_bytes(data[len(UTF8_BOM) :])
                fixed.append(path)

    if not problems:
        print(f"{OK} 编码约定检查通过")
        return 0

    print(f"发现 {len(problems)} 个编码问题：")
    for path, issue, how in problems:
        mark = "已修" if fix else "需修"
        print(f"  {BAD} [{mark}] {path}：{issue} -- {how}")
    if fixed:
        print(f"\n已就地修正 {len(fixed)} 个文件，请重新提交。")
        return 0
    print("\n加 --fix 可自动修正。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
