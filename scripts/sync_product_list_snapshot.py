#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步领星本地产品列表到 lxpm_product_category_snapshot。"""
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
    parser = argparse.ArgumentParser(description="同步领星本地产品列表快照")
    parser.add_argument("--page-size", type=int, default=1000, help="每页数量，默认1000，上限1000")
    parser.add_argument("--max-pages", type=int, default=0, help="最多同步多少页，默认0表示全量")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    db = Database()
    client = LingxingClient(db=db, enable_api_log=True)
    token = await client.generate_token()
    service = ProductService(client, db)
    total = await service.sync_product_list_snapshot(
        token.token,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    print(f"✅ 产品列表快照同步完成：{total} 条")


if __name__ == "__main__":
    asyncio.run(main())
