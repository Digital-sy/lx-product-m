#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修改单个产品分类。默认只预览，必须加 --confirm 才会写领星。"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.product_service import ProductService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="修改单个产品分类")
    parser.add_argument("--sku", required=True, help="要修改的SKU")
    parser.add_argument("--category-id", type=int, help="目标分类ID，优先推荐")
    parser.add_argument("--category-name", help="目标分类名称；名称重复时会报错")
    parser.add_argument("--batch-no", default="manual", help="批次号，默认 manual")
    parser.add_argument("--confirm", action="store_true", help="确认写入领星。不传则只预览")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if not args.category_id and not args.category_name:
        raise SystemExit("必须传 --category-id 或 --category-name")

    db = Database()
    client = LingxingClient(db=db, enable_api_log=True)
    token = await client.generate_token()
    service = ProductService(client, db)

    target = service.resolve_target_category(args.category_id, args.category_name)
    products = await service.query_and_save(token.token, [args.sku])
    if not products:
        raise SystemExit(f"SKU不存在或详情为空：{args.sku}")
    product = products[0]
    old_id, old_name = service.extract_category(product)

    print("===== 修改预览 =====")
    print(f"SKU: {args.sku}")
    print(f"品名: {product.get('product_name') or product.get('productName') or ''}")
    print(f"当前分类: {old_id or ''} {old_name or ''}")
    print(f"目标分类: {target['cid']} {target['title']} | {target.get('full_path') or ''}")

    if not args.confirm:
        print("\n未传 --confirm，本次不写入领星。确认无误后加 --confirm 再执行。")
        return

    result = await service.update_product_category(
        token.token,
        sku=args.sku,
        category_id=int(target["cid"]),
        batch_no=args.batch_no,
    )
    print("✅ 分类修改并复查成功")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
