"""规范化序列化与内容指纹。

相同逻辑输入必须得到相同字节串，进而得到相同 sha256，
这是“相同输入稳定复算”的基础。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any


def to_jsonable(value: Any) -> Any:
    """把 Decimal 等不可直接 JSON 化的值转换为稳定的可序列化形式。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: to_jsonable(value[k]) for k in value}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


def canonical(value: Any) -> str:
    """键排序、无空白、非 ASCII 不转义的规范 JSON。"""
    return json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    """对任意结构化取值计算内容指纹。"""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
