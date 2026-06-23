#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新增或编辑领星产品分类。默认只预览，必须加 --confirm 才会写领星。"""
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
    parser = argparse.ArgumentParser(description="新增或编辑领星产品分类")
    parser.add_argument("--id", type=int, help="分类ID；不传为新增，传入为编辑")
    parser.add_argument("--parent-cid", type=int, default=0, help="父级分类ID，默认0")
    parser.add_argument("--title", required=True, help="分类名称")
    parser.add_argument("--category-code", required=True, help="分类简码")
    parser.add_argument("--confirm", action="store_true", help="确认写入领星。不传则只预览")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    action = "编辑" if args.id else "新增"
    print("===== 分类维护预览 =====")
    print(f"动作: {action}")
    print(f"id: {args.id or ''}")
    print(f"parent_cid: {args.parent_cid}")
    print(f"title: {args.title}")
    print(f"category_code: {args.category_code}")

    if not args.confirm:
        print("\n未传 --confirm，本次不写入领星。确认无误后加 --confirm 再执行。")
        return

    db = Database()
    client = LingxingClient(db=db, enable_api_log=True)
    token = await client.generate_token()
    service = CategoryService(client, db)
    result = await service.upsert_category(
        token.token,
        cid=args.id,
        parent_cid=args.parent_cid,
        title=args.title,
        category_code=args.category_code,
    )
    print("✅ 分类维护成功")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
