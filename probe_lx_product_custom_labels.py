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
from lx_product_m.services.product_service import PRODUCT_DETAIL_API, ProductService

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


def is_retryable(resp: dict[str, Any]) -> bool:
    code = str(resp.get("code"))
    msg = str(resp.get("msg") or resp.get("message") or "").lower()
    return code in {"3001008", "500", "502", "503", "504"} or "too frequently" in msg or "稍后再试" in msg


async def fetch_product_with_retry(client: LingxingClient, token: str, sku: str, max_retries: int = 6) -> list[dict[str, Any]]:
    last_resp: dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        resp = await client.request(token, PRODUCT_DETAIL_API, "POST", req_body={"skus": [sku]})
        last_resp = resp
        if str(resp.get("code")) == "0":
            return resp.get("data") or []
        if is_retryable(resp) and attempt < max_retries:
            wait_s = min(60, 5 * (attempt + 1))
            print(f"接口频率限制/临时异常，{wait_s}s 后重试：{resp}")
            await asyncio.sleep(wait_s)
            continue
        raise RuntimeError(f"查询产品详情失败：{resp}")
    raise RuntimeError(f"查询产品详情失败：{last_resp}")


async def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python probe_lx_product_custom_labels.py <SKU>")
        sys.exit(1)

    sku = sys.argv[1].strip()
    db = Database()
    client = LingxingClient(db=db)
    service = ProductService(client, db)
    token = (await client.generate_token()).token

    rows = await fetch_product_with_retry(client, token, sku)
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
