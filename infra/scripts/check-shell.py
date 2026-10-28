#!/usr/bin/env python3
"""Shell / Makefile 卫生检查。

**为什么需要**：这些文件在本项目的开发环境里**无法执行**（Windows 沙箱禁止
WSL 的 bash 启动，报 `Bash/E_ACCESSDENIED`），所以它们从来没被真正跑过 ——
一次语法错误会一直藏到 CI 或用户那里。

本脚本做两件事：

1. **`bash -n`**（语法检查，不执行）跑遍所有 ``*.sh``。
   有 bash 时执行；没有时**明确报告"已跳过"**而不是假装通过 ——
   一个静默跳过的检查比没有检查更糟，因为它会给人虚假的信心。
   权威执行在 CI（ubuntu-latest 上有 bash）。

2. **Makefile 卫生**（不需要任何 shell）：
   * recipe 行必须以 **TAB** 开头 —— 空格会得到 `missing separator`
   * recipe 里引用 shell 变量必须写 ``$$VAR``；写成 ``$VAR`` 会被 Make
     当成变量展开（``$V`` + 字面量 ``AR``），而且**不会报错**，只是行为诡异
   * 括号/引号配平

用法：
    python infra/scripts/check-shell.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

OK = "OK  "
BAD = "FAIL"
SKIP = "SKIP"


def shell_scripts() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.sh"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8")
    return [ROOT / p for p in out.split("\n") if p.strip()]


def bash_works(bash: str) -> bool:
    """先验证 bash **本身**能跑，再拿它去判别人的错。

    本项目的 Windows 开发环境里 `bash.exe` 是存在的（WSL 启动器），但沙箱
    禁止它真正启动，报 `Bash/E_ACCESSDENIED` —— 而且那条错误是 UTF-16，
    Python 解析不出可读的 stderr。若直接把它当成"语法错误"，就会把**所有**
    shell 脚本都冤枉一遍。

    这个检查器第一版就是这么干的：4 个脚本全报 FAIL 却没有任何错误信息。
    凡是"用一个工具去判别人"的检查器，都得先确认那个工具自己能用。
    """
    probe = "if [ 1 = 1 ]; then exit 0; fi"
    try:
        r = subprocess.run(
            [bash, "-n", "-c", probe], capture_output=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def check_shell_syntax() -> tuple[int, list[str]]:
    """对所有 .sh 跑 `bash -n`。"""
    scripts = shell_scripts()
    bash = shutil.which("bash")
    if bash is None or not bash_works(bash):
        reason = "找不到 bash" if bash is None else "bash 存在但无法启动（沙箱限制？）"
        print(f"{SKIP} {reason} —— 跳过 {len(scripts)} 个 shell 脚本的语法检查")
        print("     （本项目的 Windows 开发环境禁止启动 WSL 的 bash；CI 上会真的执行）")
        return 0, [str(s.relative_to(ROOT)) for s in scripts]

    failures = 0
    for path in scripts:
        r = subprocess.run(
            [bash, "-n", str(path)], capture_output=True, text=True, errors="replace"
        )
        rel = path.relative_to(ROOT)
        if r.returncode == 0:
            print(f"{OK} {rel}")
        else:
            failures += 1
            print(f"{BAD} {rel}")
            for line in (r.stderr or "").strip().splitlines()[:5]:
                print(f"       {line}")
    return failures, []


def check_makefile() -> int:
    path = ROOT / "Makefile"
    if not path.is_file():
        print(f"{SKIP} 没有 Makefile")
        return 0

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    failures = 0

    targets: dict[str, int] = {}
    in_recipe = False
    for i, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            in_recipe = False
            continue

        # 目标定义行
        m = re.match(r"^([A-Za-z][A-Za-z0-9_.-]*)\s*:(?!=)", line)
        if m and not line.startswith("\t"):
            name = m.group(1)
            if name in targets:
                print(f"{BAD} Makefile:{i} 目标 {name} 重复定义（上一次在第 {targets[name]} 行）")
                failures += 1
            targets[name] = i
            in_recipe = True
            continue

        if line.startswith("\t"):
            # recipe 行。检查 $ 转义。
            #
            # shell 变量必须写 $$VAR / $${VAR}。写成 $VAR 时 Make 会把它
            # 当成变量 $V 展开，然后拼上字面量 "AR" —— 不报错，但值是错的。
            # 同理 $1 必须写 $$1（位置参数）。
            #
            # 合法形式：$$（转义）、$(MAKE_VAR)、${MAKE_VAR}、$@ $< $? 等自动变量。
            #
            # ⚠️ `${var}`（花括号形式）**刻意不检查**：它与合法 Make 变量
            #    `${MAKE_VAR}` 语法完全同形，无法区分，flag 它必然冤枉
            #    `$(COMPOSE)` 这类写法。一个会报假问题的检查器比没有检查器更糟。
            body = line[1:]
            for hit in re.finditer(
                r"(?<!\$)\$(?![\$({@<?^*+%])([A-Za-z_0-9][A-Za-z0-9_]*)", body
            ):
                print(
                    f"{BAD} Makefile:{i} recipe 里的 ${hit.group(1)} 没有写成 "
                    f"$${hit.group(1)} —— Make 会把它当成变量展开（不报错，但值是错的）"
                )
                failures += 1
            # 引号配平
            if body.count("'") % 2 or body.count('"') % 2:
                # 反斜杠续行会让单行的引号计数天然不平衡，所以只对**不以 \ 结尾**的行报警
                if not body.rstrip().endswith("\\"):
                    print(f"{BAD} Makefile:{i} 引号数量为奇数（且不是续行）")
                    failures += 1
            continue

        if in_recipe and line.startswith(" "):
            print(
                f"{BAD} Makefile:{i} recipe 行用空格缩进 —— 必须是 TAB，"
                f"否则 make 会报 'missing separator'"
            )
            failures += 1
            continue

        in_recipe = False

    if failures == 0:
        print(f"{OK} Makefile（{len(targets)} 个目标，TAB 与 $ 转义检查通过）")
    return failures


def main() -> int:
    print("=== shell 脚本语法 ===")
    failures, skipped = check_shell_syntax()
    print()
    print("=== Makefile 卫生 ===")
    failures += check_makefile()
    print()
    if failures:
        print(f"发现 {failures} 个问题")
        return 1
    if skipped:
        print(f"通过（其中 {len(skipped)} 个 shell 脚本未做语法检查 —— 本机没有 bash）")
    else:
        print("通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
