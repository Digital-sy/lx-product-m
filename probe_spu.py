#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SPU 探测脚本。

用途：
1. 读取侧：调 batchGetProductInfo，打印返回里所有和 spu/属性 相关的字段名。
2. 写入侧：探测多属性产品列表/详情/属性列表接口路径。

说明：
- batchGetProductInfo 实测可能不返回 SPU 字段，即使前台已挂 SPU。
- 正式复查建议以查询多属性产品列表/详情为准。
- 本脚本只探测查询类接口，不调用 spu/set 写入接口。

用法：
    python probe_spu.py <SKU>
"""
from __future__ import annotations

import asyncio
import json
import sys

from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient

PRODUCT_DETAIL_API = "/erp/sc/routing/data/local_inventory/batchGetProductInfo"

# 官方文档路径优先；后面保留旧候选路径用于排查不同版本路由。
SPU_LIST_CANDIDATES = [
    "/erp/sc/routing/storage/spu/spuList",
    "/erp/sc/routing/storage/spu/list",
    "/erp/sc/routing/data/local_inventory/spuList",
    "/erp/sc/routing/data/local_inventory/spu/list",
    "/erp/sc/storage/spu/list",
]
SPU_INFO_CANDIDATES = [
    "/erp/sc/routing/storage/spu/info",
    "/erp/sc/routing/storage/spu/spuInfo",
    "/erp/sc/routing/data/local_inventory/spuInfo",
    "/erp/sc/routing/data/local_inventory/spu/info",
]
ATTRIBUTE_LIST_CANDIDATES = [
    "/erp/sc/routing/storage/productAttribute/list",
    "/erp/sc/routing/storage/product_attribute/list",
    "/erp/sc/routing/storage/spu/attribute/list",
    "/erp/sc/routing/storage/attribute/list",
    "/erp/sc/routing/data/local_inventory/attribute",
]


def _walk_keys(obj, prefix=""):
    """递归打印包含 spu / attribute / attr 的键。"""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            low = str(k).lower()
            if "spu" in low or "attr" in low:
                hits.append((path, v))
            hits.extend(_walk_keys(v, path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:3]):
            hits.extend(_walk_keys(item, f"{prefix}[{i}]"))
    return hits


async def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python probe_spu.py <SKU>")
        sys.exit(1)
    sku = sys.argv[1].strip()

    db = Database()
    client = LingxingClient(db=db)
    token = (await client.generate_token()).token

    # ---------- 1. 读取侧：产品详情里的 SPU 字段 ----------
    print(f"\n===== 1) batchGetProductInfo: {sku} =====")
    result = await client.request(
        token, PRODUCT_DETAIL_API, "POST", req_body={"skus": [sku]}
    )
    print(f"code={result.get('code')} message={result.get('message') or result.get('msg')}")
    data = result.get("data") or []
    if data:
        product = data[0]
        hits = _walk_keys(product)
        if hits:
            print("检测到 SPU/属性相关字段：")
            for path, value in hits:
                preview = json.dumps(value, ensure_ascii=False, default=str)
                print(f"  {path} = {preview[:200]}")
        else:
            print("详情返回里没有任何 spu/attr 相关字段。")
            print("→ 若该 SKU 在界面上确实已挂 SPU，说明详情接口不透出 SPU，")
            print("  复查校验需改走『查询多属性产品列表/详情』接口。")
        print("\n完整顶层字段名：", sorted(product.keys()))
    else:
        print("详情为空，请确认 SKU 是否存在。")

    # ---------- 2. 写入侧：探测多属性产品接口路径 ----------
    async def probe(name: str, candidates: list[str], body: dict) -> None:
        print(f"\n===== 2) 探测 {name} =====")
        for path in candidates:
            try:
                r = await client.request(token, path, "POST", req_body=body)
                code = r.get("code")
                msg = str(r.get("message") or r.get("msg") or "")[:120]
                details = str(r.get("error_details") or "")[:160]
                print(f"  {path}\n    -> code={code} msg={msg} details={details}")
            except Exception as exc:
                print(f"  {path}\n    -> 异常: {exc}")
            await asyncio.sleep(1.2)

    await probe("查询多属性产品列表", SPU_LIST_CANDIDATES, {"offset": 0, "length": 20})
    await probe("查询多属性产品详情", SPU_INFO_CANDIDATES, {"spu": "PROBE_NOT_EXIST"})
    await probe("查询产品属性列表", ATTRIBUTE_LIST_CANDIDATES, {"offset": 0, "length": 20})

    print("\n判断标准：返回『服务不存在/路由不存在』= 路径错误；")
    print("返回 code=0 或参数校验类错误（如 SPU 不存在、缺少必填参数）= 路径正确。")


if __name__ == "__main__":
    asyncio.run(main())
