#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SPU 绑定单点测试。

用法：
    python test_spu_bind.py <SKU> <SPU>

验证顺序建议（用真实测试数据各跑一遍）：
    1. SPU 不存在 → 应新建 SPU 并绑定成功（status=success）
    2. 再跑一次同样参数 → 应跳过（status=skipped）
    3. 同一 SPU 换一个 SKU → 应追加绑定，且第一个 SKU 仍在（看 spu_sku_count）
    4. 传一个不存在的 SKU → 应被拦截报错（status=blocked，不会误建产品）
    5. 传一个含中文的 SPU 名 → 应被本地校验拦截
每一步之后可到 ERP 界面"按SPU"视图人工核对一次。
"""
from __future__ import annotations

import asyncio
import sys

from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.spu_service import SpuService, SPU_CHANGE_LOG_DDL


async def main() -> None:
    if len(sys.argv) < 3:
        print("用法: python test_spu_bind.py <SKU> <SPU>")
        sys.exit(1)
    sku, spu = sys.argv[1].strip(), sys.argv[2].strip()

    db = Database()
    db.execute(SPU_CHANGE_LOG_DDL)  # 幂等建表

    client = LingxingClient(db=db)
    service = SpuService(client, db)
    token = (await client.generate_token()).token

    print(f"绑定 SKU={sku} -> SPU={spu} ...")
    result = await service.ensure_spu_and_bind_sku(token, spu=spu, sku=sku, batch_no="spu-test")
    print("结果：", result)

    detail = await service.get_spu_detail(token, spu=spu)
    skus = [item.get("sku") for item in (detail or {}).get("sku_list") or []]
    print(f"复核：SPU={spu} (ps_id={(detail or {}).get('ps_id')}) 当前关联 {len(skus)} 个 SKU：{skus}")


if __name__ == "__main__":
    asyncio.run(main())
