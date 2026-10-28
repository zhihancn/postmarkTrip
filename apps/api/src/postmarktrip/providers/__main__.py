"""让 ``python -m postmarktrip.providers`` 可用。

``providers`` 是包（``__init__.py`` 里已经有 ``main()``），所以 ``-m`` 会去找
``__main__`` 子模块而不是执行 ``__init__``。缺这个文件时报错是
"No module named postmarktrip.providers.__main__; 'postmarktrip.providers'
is a package and cannot be directly executed" —— 而那条信息不会告诉你
要改用 ``python -c``。
"""

from __future__ import annotations

from postmarktrip.providers import main

if __name__ == "__main__":
    raise SystemExit(main())
