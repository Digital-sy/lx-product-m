#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断领星新增分类接口的请求体兼容性。

只针对单个分类尝试不同 payload 结构：
1. 每次请求后都会重新同步分类列表，若目标 full_path 已存在就停止。
2. 默认只打印计划；必须加 --confirm 才会调用领星接口。
3. 仅用于排查 /erp/sc/routing/storage/category/set。
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
from lx_product_m.services.category_service import CategoryService, CATEGORY_SET_API


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="诊断领星新增分类接口 payload 结构")
    parser.add_argument("--parent-cid", type=int, required=True)
    parser.add_argument("--parent-path", required=True, help="父级完整路径，例如 24/春夏")
    parser.add_argument("--title", required=True)
    parser.add_argument("--category-code", default="")
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args()


def target_path(parent_path: str, title: str) -> str:
    return f"{parent_path.rstrip('/')}/{title.strip()}"


def exists_path(db: Database, path: str) -> dict | None:
    return db.fetch_one("SELECT cid, parent_cid, title, category_code, full_path FROM lxpm_category WHERE full_path=%s", (path,))


async def sync_categories(service: CategoryService, token: str) -> None:
    rows = await service.fetch_categories(token)
    service.save_categories(rows)


def build_variants(parent_cid: int, title: str, category_code: str) -> list[tuple[str, dict[str, Any]]]:
    base_int = {"parent_cid": parent_cid, "title": title, "category_code": category_code}
    base_str = {"parent_cid": str(parent_cid), "title": title, "category_code": category_code}
    base_int_id_empty = {"id": "", "parent_cid": parent_cid, "title": title, "category_code": category_code}
    base_str_id_empty = {"id": "", "parent_cid": str(parent_cid), "title": title, "category_code": category_code}
    no_code_int = {"parent_cid": parent_cid, "title": title}
    no_code_str = {"parent_cid": str(parent_cid), "title": title}
    return [
        ("object_parent_int_with_code", {"data": base_int}),
        ("object_parent_str_with_code", {"data": base_str}),
        ("object_parent_int_id_empty_with_code", {"data": base_int_id_empty}),
        ("object_parent_str_id_empty_with_code", {"data": base_str_id_empty}),
        ("array_parent_int_with_code", {"data": [base_int]}),
        ("array_parent_str_with_code", {"data": [base_str]}),
        ("object_parent_int_no_code", {"data": no_code_int}),
        ("object_parent_str_no_code", {"data": no_code_str}),
    ]


async def main() -> None:
    args = parse_args()
    path = target_path(args.parent_path, args.title)
    variants = build_variants(args.parent_cid, args.title, args.category_code)

    print("===== 领星新增分类 payload 诊断 =====")
    print(f"parent_cid: {args.parent_cid}")
    print(f"parent_path: {args.parent_path}")
    print(f"title: {args.title}")
    print(f"category_code: {args.category_code!r}")
    print(f"target_path: {path}")
    print("将尝试以下 payload 结构：")
    for name, body in variants:
        print(f"- {name}: {body}")
    if not args.confirm:
        print("\n当前为预览模式，未调用领星。加 --confirm 才会逐个尝试。")
        return

    db = Database()
    client = LingxingClient(db=db)
    service = CategoryService(client, db)
    token_info = await client.generate_token()
    token = token_info.token

    await sync_categories(service, token)
    found = exists_path(db, path)
    if found:
        print(f"目标分类已存在，停止：{found}")
        return

    for name, body in variants:
        print(f"\n>>> 尝试：{name}")
        result = await client.request(token, CATEGORY_SET_API, "POST", req_body=body)
        print(f"响应：{result}")

        # 写一条分类变更日志，方便回溯。
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO lxpm_category_change_log
                (action_type, cid, parent_cid, title, category_code, status, request_json, response_json, error_message)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    "debug_create",
                    None,
                    args.parent_cid,
                    args.title,
                    args.category_code,
                    "success" if str(result.get("code")) == "0" else "failed",
                    json_dumps(body),
                    json_dumps(result),
                    "" if str(result.get("code")) == "0" else str(result.get("message") or result.get("msg") or result.get("error_details") or ""),
                ),
            )

        await asyncio.sleep(settings.collection_delay_seconds)
        await sync_categories(service, token)
        found = exists_path(db, path)
        if found:
            print(f"✅ 创建成功并已同步：{found}")
            print(f"成功 payload：{name}")
            return

    print("\n所有 payload 都未创建成功。请把上面的每个响应贴回来。")


if __name__ == "__main__":
    asyncio.run(main())
