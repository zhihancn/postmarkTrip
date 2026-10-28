"""HTTP 层的共享常量。

放这里而不是各处 `status.XXX` 直接取用的原因：starlette 1.7 把
``HTTP_422_UNPROCESSABLE_ENTITY`` 改名成了 ``HTTP_422_UNPROCESSABLE_CONTENT``，
旧名仍可用但每次使用都会发一条弃用警告。本项目声明的依赖下界是
``fastapi>=0.115``，它既可能带上旧名、也可能带上新名 —— 直接用新名会让
旧环境下 ``AttributeError``，直接用旧名会在新环境下刷警告。

所以这里"新名优先、数字兜底"：两种环境都是 422，且都不产生警告。
42x 之外的状态码没有这个问题（没有被改名），照常用 ``status.HTTP_*``。
"""

from __future__ import annotations

from fastapi import status

__all__ = ["UNPROCESSABLE"]

#: 422 Unprocessable Content（旧称 Unprocessable Entity）
UNPROCESSABLE: int = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)
