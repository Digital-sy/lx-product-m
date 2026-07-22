#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高置信度 A2023 第一轮兼容入口。

修复旧入口错误导入 ``scripts.color_system_tagging.field_value`` 的问题。
先在内存中补充兼容函数，再加载原有清单生成和安全写入逻辑。
不会改变原有写前保护、完整字段合并和写后复查规则。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lx_product_m.services.product_write_guard import clean
from scripts import color_system_tagging as tagging


def field_value(item: dict[str, Any]) -> str:
    """兼容读取领星自定义字段值。"""
    if not isinstance(item, dict):
        return ""
    for key in ("val_text", "val", "value", "field_value"):
        if key in item and item.get(key) is not None:
            value = clean(item.get(key))
            if value:
                return value
    return ""


# 旧脚本使用 ``from scripts.color_system_tagging import field_value``。
# 在导入旧脚本前补充该属性，避免 ImportError。
tagging.field_value = field_value

from scripts import color_system_high_confidence_round1 as base  # noqa: E402


def __getattr__(name: str) -> Any:
    return getattr(base, name)


def main(argv: Sequence[str] | None = None) -> int:
    return base.main(argv)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"执行失败：{type(exc).__name__}: {exc}")
        raise
