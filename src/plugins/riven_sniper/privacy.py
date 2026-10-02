"""用户可见边界上的内部标识识别与脱敏。"""

from __future__ import annotations

import re
from typing import Any


_HIDDEN_IDENTIFIER = re.compile(
    r"(?<![0-9a-f])(?:[0-9a-f]{64}|[0-9a-f]{24})(?![0-9a-f])",
    re.IGNORECASE,
)


def contains_hidden_identifier(value: object) -> bool:
    """判断文本任意位置是否包含服务端内部使用的不透明标识。"""
    return _HIDDEN_IDENTIFIER.search(str(value or "")) is not None


def redact_hidden_identifiers(value: object) -> str:
    """从即将展示的文本中移除内部标识。"""
    return _HIDDEN_IDENTIFIER.sub("—", str(value or ""))


def redact_hidden_identifiers_deep(value: Any) -> Any:
    """递归脱敏即将进入 JSON 等公开边界的字符串值。"""
    if isinstance(value, str):
        return redact_hidden_identifiers(value)
    if isinstance(value, dict):
        return {
            key: redact_hidden_identifiers_deep(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_hidden_identifiers_deep(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_hidden_identifiers_deep(item) for item in value)
    return value


__all__ = [
    "contains_hidden_identifier",
    "redact_hidden_identifiers",
    "redact_hidden_identifiers_deep",
]
