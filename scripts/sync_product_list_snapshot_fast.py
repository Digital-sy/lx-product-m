#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速同步领星本地产品列表到 lxpm_product_category_snapshot。

特点：
1. 边分页拉取、边批量写库，不把9万+数据全部攒到内存。
2. 每页一次 executemany，避免逐条打开数据库连接。
3. 可重复执行，使用 REPLACE INTO 覆盖快照。
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lx_product_m.config import settings
from lx_product_m.db import Database, json_dumps
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.product_service import PRODUCT_LIST_API, ProductService
from lx_product_m.sku import extract_spu


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="快速同步领星本地产品列表快照")
    parser.add_argument("--page-size", type=int, default=1000, help="每页数量，默认1000，上限1000")
    parser.add_argument("--max-pages", type=int, default=0, help="最多同步多少页，默认0表示全量")
    parser.add_argument("--start-offset", type=int, default=0, help="从指定offset开始，默认0")
    return parser.parse_args()


def load_category_map(db: Database) -> dict[int, dict[str, str]]:
    rows = db.fetch_all("SELECT cid, title, full_path FROM lxpm_category")
    out: dict[int, dict[str, str]] = {}
    for row in rows:
        try:
            cid = int(row.get("cid") or 0)
        except (TypeError, ValueError):
            continue
        if cid:
            out[cid] = {
                "title": str(row.get("title") or ""),
                "full_path": str(row.get("full_path") or ""),
            }
    return out


def extract_category(product: dict[str, Any]) -> tuple[int | None, str]:
    raw_id = (
        product.get("category_id")
        or product.get("categoryId")
        or product.get("cid")
        or product.get("category_cid")
    )
    cid: int | None = None
    try:
        if raw_id not in (None, ""):
            cid = int(raw_id)
    except (TypeError, ValueError):
        cid = None
    title = str(
        product.get("category")
        or product.get("category_name")
        or product.get("categoryName")
        or ""
    ).strip()
    return cid, title


def extract_custom_fields(product: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("custom_fields", "custom_field_list", "customFields", "customFieldList"):
        value = product.get(key)
        if isinstance(value, list):
            return value
    return []


def make_params(products: list[dict[str, Any]], category_map: dict[int, dict[str, str]]) -> list[tuple[Any, ...]]:
    params: list[tuple[Any, ...]] = []
    for product in products:
        sku = str(product.get("sku") or product.get("SKU") or "").strip()
        if not sku:
            continue
        product_name = str(product.get("product_name") or product.get("productName") or "").strip()
        cid, title = extract_category(product)
        path = ""
        if cid and cid in category_map:
            title = title or category_map[cid]["title"]
            path = category_map[cid]["full_path"]
        params.append(
            (
                sku,
                product_name,
                cid,
                title,
                path,
                extract_spu(sku),
                json_dumps(extract_custom_fields(product)),
                json_dumps(product),
                0,
                "success",
            )
        )
    return params


def save_batch(db: Database, products: list[dict[str, Any]], category_map: dict[int, dict[str, str]]) -> int:
    params = make_params(products, category_map)
    if not params:
        return 0
    sql = """
        REPLACE INTO `lxpm_product_category_snapshot`
        (`sku`, `product_name`, `category_id`, `category_name`, `category_path`, `spu`,
         `custom_fields_json`, `raw_json`, `last_api_code`, `last_api_message`, `synced_at`)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
    """
    with db.cursor() as cur:
        cur.executemany(sql, params)
    return len(params)


async def main() -> None:
    args = parse_args()
    page_size = max(1, min(args.page_size, 1000))
    offset = max(0, args.start_offset)
    page_no = offset // page_size
    total_fetched = 0
    total_saved = 0

    db = Database()
    client = LingxingClient(db=db, enable_api_log=True)
    token_info = await client.generate_token()
    service = ProductService(client, db)
    category_map = load_category_map(db)

    print("===== 快速同步领星产品列表快照 =====")
    print(f"page_size={page_size}, start_offset={offset}, max_pages={args.max_pages or 'ALL'}")
    print(f"已加载分类映射：{len(category_map)} 条")

    while True:
        page_no += 1
        body = {"offset": offset, "length": page_size}
        result = await client.request(token_info.token, PRODUCT_LIST_API, "POST", req_body=body)
        if not service._success(result):
            raise RuntimeError(f"查询产品列表失败 offset={offset}: {result}")
        items = result.get("data") or []
        if not items:
            break
        saved = save_batch(db, items, category_map)
        total_fetched += len(items)
        total_saved += saved
        print(f"page={page_no}, offset={offset}, 本页拉取={len(items)}, 本页入库={saved}, 累计拉取={total_fetched}, 累计入库={total_saved}")
        if len(items) < page_size:
            break
        if args.max_pages and page_no >= args.max_pages:
            break
        offset += page_size
        await asyncio.sleep(settings.collection_delay_seconds)

    print(f"✅ 快速同步完成：累计拉取={total_fetched}, 累计入库={total_saved}")


if __name__ == "__main__":
    asyncio.run(main())
