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
    parser.add_argument("--clean-preview", action="store_true", help="输出款号/开款任务/品线/目标分类路径清洗预览")
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


def collect_options(field: dict) -> dict[str, str]:
    """提取字段里的 option id -> name 映射。"""
    options: list[dict] = []
    prop = field.get("property") or {}
    if isinstance(prop.get("options"), list):
        options.extend(prop.get("options") or [])
    type_info = prop.get("type") or {}
    if isinstance(type_info, dict):
        ui_prop = type_info.get("ui_property") or {}
        if isinstance(ui_prop, dict) and isinstance(ui_prop.get("options"), list):
            options.extend(ui_prop.get("options") or [])
    result: dict[str, str] = {}
    for opt in options:
        oid = str(opt.get("id") or "").strip()
        name = str(opt.get("name") or "").strip()
        if oid:
            result[oid] = name
    return result


def build_option_maps(fields: list[dict]) -> dict[str, dict[str, str]]:
    maps: dict[str, dict[str, str]] = {}
    for field in fields:
        name = str(field.get("field_name") or "")
        opt_map = collect_options(field)
        if name and opt_map:
            maps[name] = opt_map
    return maps


def readable_value(field_name: str, value, option_maps: dict[str, dict[str, str]]) -> str:
    opt_map = option_maps.get(field_name) or {}
    if isinstance(value, list) and opt_map:
        names = []
        for item in value:
            key = str(item)
            names.append(opt_map.get(key, key))
        return ", ".join([x for x in names if x])
    if isinstance(value, str) and opt_map:
        return opt_map.get(value, value)
    return extract_feishu_text(value)


def parse_task_text(task_text: str) -> tuple[str, str, str]:
    """从开款任务中解析 年份归类/季节/状态说明。"""
    task_text = (task_text or "").strip()
    if not task_text:
        return "", "", "缺少开款任务"

    year_group = ""
    season = ""

    m = re.search(r"(历史|\d{2}|20\d{2})", task_text)
    if m:
        year_raw = m.group(1)
        if year_raw == "历史":
            year_group = "历史"
        else:
            yy = int(year_raw[-2:])
            year_group = "历史" if yy <= 23 else f"{yy:02d}"

    if "春夏" in task_text:
        season = "春夏"
    elif "秋冬" in task_text:
        season = "秋冬"

    problems = []
    if not year_group:
        problems.append("未解析年份")
    if not season:
        problems.append("未解析季节")
    return year_group, season, "；".join(problems)


def clean_preview(records: list[dict], option_maps: dict[str, dict[str, str]]) -> None:
    print("\n===== 清洗预览，不写库、不写领星 =====")
    headers = ["款号", "开款任务", "季节字段", "品线", "解析年份", "解析季节", "目标分类路径", "状态"]
    widths = [14, 28, 12, 12, 10, 10, 28, 20]
    print(" ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-" * 140)

    for record in records:
        row = record.get("fields", {}) or {}
        style_no = readable_value("款号", row.get("款号"), option_maps)
        task = readable_value("开款任务", row.get("开款任务"), option_maps)
        season_field = readable_value("季节", row.get("季节"), option_maps)
        line = readable_value("品线", row.get("品线"), option_maps)
        year_group, season, problem = parse_task_text(task)
        target_path = ""
        status = "可生成"
        if year_group and season and line:
            target_path = f"{year_group}/{season}/{line}"
        else:
            status = problem or "字段不足"
            if not line:
                status = (status + "；" if status else "") + "缺少品线"
        values = [style_no, task, season_field, line, year_group, season, target_path, status]
        print(" ".join(str(v or "")[:w].ljust(w) for v, w in zip(values, widths)))


def print_option_maps(option_maps: dict[str, dict[str, str]]) -> None:
    print("\n===== 字段选项映射 =====")
    if not option_maps:
        print("未发现字段选项")
        return
    for field_name, opt_map in option_maps.items():
        print(f"\n{field_name}:")
        for oid, name in opt_map.items():
            print(f"  {oid} = {name}")


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
    option_maps = build_option_maps(fields)

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

    print_option_maps(option_maps)

    print(f"\n===== 样例记录，前 {len(records)} 条 =====")
    for idx, record in enumerate(records, 1):
        print(f"\n--- Record {idx}: {record.get('record_id', '')} ---")
        row = record.get("fields", {}) or {}
        for field in fields:
            name = field.get("field_name", "")
            if name in row:
                print(f"{name}: {readable_value(name, row.get(name), option_maps)}")

    if args.clean_preview:
        clean_preview(records, option_maps)

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
