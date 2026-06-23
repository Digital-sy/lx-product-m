#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步领星产品分类列表到 lxpm_category。"""
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
from lx_product_m.services.category_service import CategoryService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同步领星产品分类列表")
    parser.add_argument("--ids", nargs="*", type=int, help="只同步指定分类ID；不传则全量同步")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    db = Database()
    client = LingxingClient(db=db, enable_api_log=True)
    token = await client.generate_token()
    service = CategoryService(client, db)
    rows = await service.fetch_categories(token.token, ids=args.ids)
    count = service.save_categories(rows)
    print(f"✅ 分类同步完成：接口返回 {len(rows)} 条，入库 {count} 条")


if __name__ == "__main__":
    asyncio.run(main())
