#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询产品当前分类并写入 lxpm_product_category_snapshot。"""
from __future__ import annotations

import argparse
import asyncio
import csv
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.product_service import ProductService


def read_skus_from_file(path: str) -> list[str]:
    skus: list[str] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        if "," in sample or "\t" in sample:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
            reader = csv.DictReader(f, dialect=dialect)
            for row in reader:
                sku = row.get("SKU") or row.get("sku") or row.get("Sku") or next(iter(row.values()), "")
                if sku:
                    skus.append(str(sku).strip())
        else:
            for line in f:
                sku = line.strip()
                if sku:
                    skus.append(sku)
    return skus


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查询产品当前分类")
    parser.add_argument("--sku", action="append", help="SKU，可重复传入")
    parser.add_argument("--file", help="SKU文件，支持一列文本或CSV含SKU列")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    skus = []
    if args.sku:
        skus.extend(args.sku)
    if args.file:
        skus.extend(read_skus_from_file(args.file))
    skus = list(dict.fromkeys([sku.strip() for sku in skus if sku and sku.strip()]))
    if not skus:
        raise SystemExit("请传 --sku 或 --file")

    db = Database()
    client = LingxingClient(db=db, enable_api_log=True)
    token = await client.generate_token()
    service = ProductService(client, db)
    products = await service.query_and_save(token.token, skus)

    print(f"✅ 查询完成：请求 SKU {len(skus)} 个，接口返回产品 {len(products)} 个")
    for product in products[:20]:
        category_id, category_name = service.extract_category(product)
        print(f"- {product.get('sku')}: {category_id or ''} {category_name or ''} {product.get('product_name') or ''}")
    if len(products) > 20:
        print(f"... 还有 {len(products) - 20} 条，已写入 lxpm_product_category_snapshot")


if __name__ == "__main__":
    asyncio.run(main())
