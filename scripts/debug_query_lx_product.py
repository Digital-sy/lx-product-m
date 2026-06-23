#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from common import settings
from lingxing import OpenApiBase


SKU = "ZQZ392-AP-L"


def extract_custom_fields(product: dict) -> dict:
    """
    尝试解析领星返回的自定义字段。
    这里先写得宽一点，因为需要先看真实返回结构。
    """
    candidates = [
        product.get("custom_fields"),
        product.get("custom_field_list"),
        product.get("customFields"),
        product.get("customFieldList"),
    ]

    result = {}

    for fields in candidates:
        if not fields:
            continue

        if isinstance(fields, list):
            for item in fields:
                if not isinstance(item, dict):
                    continue

                name = (
                    item.get("name")
                    or item.get("field_name")
                    or item.get("fieldName")
                    or item.get("title")
                    or item.get("id")
                    or ""
                )
                val = (
                    item.get("val")
                    or item.get("value")
                    or item.get("field_value")
                    or item.get("fieldValue")
                    or ""
                )

                if name:
                    result[str(name)] = val

        elif isinstance(fields, dict):
            result.update(fields)

    return result


async def main():
    if not settings.validate():
        raise SystemExit("配置校验失败，请检查 .env")

    config = settings.lingxing_config

    op_api = OpenApiBase(
        host=config["host"],
        app_id=config["app_id"],
        app_secret=config["app_secret"],
        proxy_url=config["proxy_url"],
    )

    token_resp = await op_api.generate_access_token()
    print(f"Token 获取成功，有效期: {token_resp.expires_in} 秒")

    req_body = {
        "skus": [SKU]
    }

    resp = await op_api.request(
        token_resp.access_token,
        "/erp/sc/routing/data/local_inventory/batchGetProductInfo",
        "POST",
        req_body=req_body,
    )

    try:
        result = resp.model_dump()
    except AttributeError:
        result = resp.dict()

    print("\n===== 接口原始返回 =====")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    data = result.get("data") or []
    if not data:
        print(f"\n没有查到 SKU: {SKU}")
        return

    product = data[0]

    print("\n===== 重点字段 =====")
    print("SKU:", product.get("sku"))
    print("品名:", product.get("product_name"))
    print("分类ID:", product.get("category_id") or product.get("categoryId"))
    print("分类:", product.get("category") or product.get("category_name") or product.get("categoryName"))
    print("标签:", product.get("tags") or product.get("tag_list") or product.get("label_list"))

    custom_fields = extract_custom_fields(product)
    print("\n===== 自定义字段解析结果 =====")
    print(json.dumps(custom_fields, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
