#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测领星本地产品详情中的自定义标签/自定义字段相关字段。

用法：
    python probe_lx_product_custom_labels.py <SKU>

说明：
    该脚本只读不写，用于确认领星 batchGetProductInfo 返回里，
    自定义标签对应的字段名和数据结构。
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.product_service import ProductService

KEYWORDS = (
    "tag",
    "label",
    "custom",
    "mark",
    "标签",
    "自定义",
)


def contains_keyword(key: str) -> bool:
    lower = key.lower()
    return any(k in lower or k in key for k in KEYWORDS)


def print_value(title: str, value: Any) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


async def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python probe_lx_product_custom_labels.py <SKU>")
        sys.exit(1)

    sku = sys.argv[1].strip()
    db = Database()
    client = LingxingClient(db=db)
    service = ProductService(client, db)
    token = (await client.generate_token()).token

    rows = await service.batch_get_product_info(token, [sku])
    if not rows:
        print(f"未查询到SKU：{sku}")
        return

    product = rows[0]
    print(f"SKU={sku}")
    print("\n===== 顶层字段名 =====")
    print(list(product.keys()))

    related = {k: v for k, v in product.items() if contains_keyword(str(k))}
    print_value("疑似自定义标签/自定义字段相关字段", related)

    custom_fields = service.extract_custom_fields(product)
    print_value("ProductService.extract_custom_fields 结果", custom_fields)

    print_value("完整产品详情", product)


if __name__ == "__main__":
    asyncio.run(main())
