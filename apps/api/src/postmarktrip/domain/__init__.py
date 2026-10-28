"""领域层出口。

``vocab`` 是**词表的唯一归属**；枚举取值不从数据库读取，也不散落在各处。
"""

from postmarktrip.domain import vocab

__all__ = ["vocab"]
