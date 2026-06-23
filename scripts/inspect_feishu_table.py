#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探测飞书多维表字段和样例数据；不写数据库，不写领星。"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
import sys
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lx_product_m.feishu_client import FeishuBitableClient, extract_feishu_text


def parse_feishu_url(url: str) -> tuple[str | None, str | None, str | None]:
    """从飞书 bitable URL 尽量提取 app_token/table_id/view_id。"""
    app_token = None
    table_id = None
    view_id = None
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    for i, part in enumerate(parts):
        if part == "base" and i + 1 < len(parts):
            app_token = parts[i + 1]
        elif part.startswith("tbl"):
            table_id = part
        elif part.startswith("vew"):
            view_id = part
    # 有些链接把 table/view 放在 query 或 hash 中，这里再做一次正则兜底。
    full = url
    if not table_id:
        m = re.search(r"(tbl[a-zA-Z0-9]+)", full)
        if m:
            table_id = m.group(1)
    if not view_id:
        m = re.search(r"(vew[a-zA-Z0-9]+)", full)
        if m:
            view_id = m.group(1)
    if not app_token:
        m = re.search(r"/base/([a-zA-Z0-9]+)", full)
        if m:
            app_token = m.group(1)
    return app_token, table_id, view_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="探测飞书多维表字段和样例数据")
    parser.add_argument("--url", help="飞书多维表URL，可自动提取 app_token/table_id/view_id")
    parser.add_argument("--app-token", help="飞书多维表 app_token")
    parser.add_argument("--table-id", help="飞书 table_id")
    parser.add_argument("--view-id", help="飞书 view_id，可选")
    parser.add_argument("--limit", type=int, default=5, help="读取样例记录数，默认5")
    parser.add_argument("--json", action="store_true", help="输出原始JSON，方便复制给ChatGPT分析")
    return parser.parse_args()


def field_type_name(type_code) -> str:
    mapping = {
        1: "多行文本",
        2: "数字",
        3: "单选",
        4: "多选",
        5: "日期",
        7: "复选框",
        11: "人员",
        13: "电话号码",
        15: "超链接",
        17: "附件",
        18: "单向关联",
        19: "查找引用",
        20: "公式",
        21: "双向关联",
        22: "地理位置",
        23: "群组",
        1001: "创建时间",
        1002: "最后更新时间",
        1003: "创建人",
        1004: "修改人",
        1005: "自动编号",
    }
    return mapping.get(type_code, f"未知类型({type_code})")


async def main() -> None:
    args = parse_args()
    app_token = args.app_token
    table_id = args.table_id
    view_id = args.view_id

    if args.url:
        parsed_app, parsed_table, parsed_view = parse_feishu_url(args.url)
        app_token = app_token or parsed_app
        table_id = table_id or parsed_table
        view_id = view_id or parsed_view

    if not app_token or not table_id:
        raise SystemExit("请传 --url，或同时传 --app-token 和 --table-id")

    client = FeishuBitableClient(app_token=app_token, table_id=table_id, view_id=view_id)
    fields = await client.list_fields()
    records = await client.list_records(page_size=max(1, min(args.limit, 500)), max_records=args.limit)

    print("===== 飞书表定位信息 =====")
    print(f"app_token: {app_token}")
    print(f"table_id : {table_id}")
    print(f"view_id  : {view_id or ''}")

    print("\n===== 字段列表 =====")
    print(f"共 {len(fields)} 个字段")
    for idx, field in enumerate(fields, 1):
        name = field.get("field_name", "")
        fid = field.get("field_id", "")
        ftype = field.get("type")
        print(f"{idx:02d}. {name} | field_id={fid} | type={field_type_name(ftype)}")

    print(f"\n===== 样例记录，前 {len(records)} 条 =====")
    for idx, record in enumerate(records, 1):
        print(f"\n--- Record {idx}: {record.get('record_id', '')} ---")
        row = record.get("fields", {}) or {}
        for field in fields:
            name = field.get("field_name", "")
            if name in row:
                print(f"{name}: {extract_feishu_text(row.get(name))}")

    if args.json:
        output = {
            "app_token": app_token,
            "table_id": table_id,
            "view_id": view_id,
            "fields": fields,
            "sample_records": records,
        }
        print("\n===== 原始JSON =====")
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
