#!/usr/bin/env python3
"""发布前的泄漏扫描。

只查**已经进入 git 的文件**（工作区里的 .env / data 不在范围内 —— 它们本来
就不该被跟踪）。CI 与本地都能跑，不需要任何第三方依赖::

    python infra/scripts/check-leaks.py

设计原则：**规则少而准**。一个总在误报的检查会被关掉，比没有检查更糟。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 只扫这些后缀。二进制（图片、模型）直接跳过。
TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".sh", ".ps1",
    ".yml", ".yaml", ".toml", ".json", ".sql", ".ini", ".cfg", ".example",
    ".html", ".css", ".gitignore", ".gitattributes", ".editorconfig",
}

#: 已确认的个人标识（用户名 / 旧口令都用过它）。
#: 拆成两段拼出来：否则这份文件自己就会命中下面那条规则。
_PERSONAL_MARK = "xue" + "yanhan"

#: 「会不会写进日志/文档里」的硬编码秘密。宁可漏报也不要把正常文案判成泄漏。
RULES: list[tuple[str, re.Pattern[str]]] = [
    # OpenAI / DeepSeek 风格的 Key。文档里只允许出现 sk-…<占位>。
    ("疑似真实 API Key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    # Windows 用户目录里带真实用户名（占位符 <you> 放行）。
    (
        "Windows 用户目录含真实用户名",
        re.compile(r"C:\\Users\\(?!<you>|Public|Default|WDAGUtilityAccount)[A-Za-z0-9._-]+"),
    ),
    # 本项目开发机的目录名，已全部替换成 <repo>。
    ("本机绝对路径", re.compile(r"[A-Za-z]:\\myCode", re.IGNORECASE)),
    # 已确认的个人标识。
    ("个人标识", re.compile(_PERSONAL_MARK, re.IGNORECASE)),
]


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def _history_has_env() -> bool:
    """`.env` 是否曾经被提交过（哪怕后来删了）。"""
    out = subprocess.run(
        ["git", "log", "--all", "--oneline", "--", ".env"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return bool(out)


def main() -> int:
    # Windows 控制台默认 GBK，直接 print 中文/符号会 UnicodeEncodeError。
    # CI 是 UTF-8，本地不是 —— 两边都要能跑，所以显式重配一次。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    problems: list[str] = []

    if _history_has_env():
        problems.append("git 历史里出现过 .env —— 需要用 git filter-repo 清洗并轮换全部凭据")

    tracked = _tracked()
    if ".env" in tracked:
        problems.append(".env 被 git 跟踪了（应当只在 .gitignore 里）")

    for rel in tracked:
        path = ROOT / rel
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in RULES:
                if pattern.search(line):
                    problems.append(f"{rel}:{lineno}: {label}")

    if problems:
        print("发现 %d 处疑似泄漏：\n" % len(problems))
        for item in problems:
            print(f"  [x] {item}")
        print("\n修法：换成占位符（<you> / <repo> / <ADMIN_PASSWORD>）或撤销该凭据。")
        return 1

    print(f"OK 泄漏扫描通过（已检查 {len(tracked)} 个跟踪文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
