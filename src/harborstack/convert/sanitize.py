"""脱敏层。

structural 模式「保结构弃内容」：字典键保留，字符串叶子替换为
``{"_len": 长度, "_sha256_8": 哈希前 8 位}``，数字/布尔原样保留
（token 数、判分、耗时等结构信息仍然可用）。
full 模式只做超长截断，适用于内网调试。
"""

from __future__ import annotations

import hashlib
from typing import Any

MAX_FULL_LEN = 65536
MODES = ("structural", "full")


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]


def sanitize(value: Any, mode: str = "structural", _depth: int = 0) -> Any:
    if mode == "full":
        if isinstance(value, str) and len(value) > MAX_FULL_LEN:
            return value[:MAX_FULL_LEN] + "...[truncated %d chars]" % len(value)
        return value

    if _depth > 12:
        return {"_truncated": True}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if value == "":
            return ""
        return {"_len": len(value), "_sha256_8": _sha8(value)}
    if isinstance(value, dict):
        return {str(k): sanitize(v, mode, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, mode, _depth + 1) for v in value]
    return {"_repr": type(value).__name__}
