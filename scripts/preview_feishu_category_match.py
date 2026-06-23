#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读预览：飞书款式信息 → 领星分类路径匹配。不写库、不写领星。"""
from __future__ import annotations

import argparse
import asyncio
import re
from collections import Counter
from pathlib import Path
import sys
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lx_product_m.db import Database
from lx_product_m.feishu_client import FeishuBitableClient, extract_feishu_text

KNOWN_LINES = ["S-基础款", "设计师款", "特征款", "基础款", "测试款", "RS款"]


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
    parser = argparse.ArgumentParser(description="预览飞书款式与领星分类匹配")
    parser.add_argument("--url", help="飞书多维表URL，可自动提取 app_token/table_id/view_id")
    parser.add_argument("--app-token", help="飞书多维表 app_token")
    parser.add_argument("--table-id", help="飞书 table_id")
    parser.add_argument("--view-id", help="飞书 view_id，可选")
    parser.add_argument("--limit", type=int, default=200, help="读取记录数，默认200；传0表示尽量全量读取")
    parser.add_argument("--show", type=int, default=100, help="输出明细行数，默认100")
    parser.add_argument("--line-source", choices=["field", "task"], default="field", help="品线来源：field=飞书品线字段，task=开款任务文本")
    return parser.parse_args()


def parse_year_season(task_text: str) -> tuple[str, str, str]:
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


def parse_line_from_task(task_text: str) -> str:
    task_text = task_text or ""
    for line in KNOWN_LINES:
        if line in task_text:
            return line
    return ""


def load_category_paths(db: Database) -> dict[str, dict]:
    rows = db.fetch_all("SELECT cid, title, full_path, is_leaf FROM lxpm_category")
    return {str(row.get("full_path") or ""): row for row in rows if row.get("full_path")}


async def read_feishu_records(app_token: str, table_id: str, view_id: str | None, limit: int) -> list[dict]:
    client = FeishuBitableClient(app_token=app_token, table_id=table_id, view_id=view_id)
    if limit and limit > 0:
        return await client.list_records(page_size=min(limit, 500), max_records=limit)
    # 全量读取：复用 list_records，给一个较大的上限，避免无限循环。
    return await client.list_records(page_size=500, max_records=200000)


def row_text(row: dict, key: str) -> str:
    return extract_feishu_text(row.get(key)).strip()


def make_preview(record: dict, categories: dict[str, dict], line_source: str) -> dict:
    row = record.get("fields", {}) or {}
    style_no = row_text(row, "款号")
    task = row_text(row, "开款任务")
    field_line = row_text(row, "品线")
    task_line = parse_line_from_task(task)
    year_group, season, parse_problem = parse_year_season(task)

    if line_source == "task":
        line = task_line or field_line
    else:
        line = field_line or task_line

    target_path = f"{year_group}/{season}/{line}" if year_group and season and line else ""
    target = categories.get(target_path) if target_path else None

    warnings = []
    if parse_problem:
        warnings.append(parse_problem)
    if not line:
        warnings.append("缺少品线")
    if field_line and task_line and field_line != task_line:
        warnings.append(f"品线冲突:字段={field_line},任务={task_line}")
    if target_path and not target:
        warnings.append("领星分类不存在")
    if target and int(target.get("is_leaf") or 0) != 1:
        warnings.append("目标分类不是叶子节点")

    if not style_no:
        status = "invalid"
        warnings.append("缺少款号")
    elif warnings:
        status = "warning" if target else "invalid"
    else:
        status = "matched"

    return {
        "style_no": style_no,
        "task": task,
        "field_line": field_line,
        "task_line": task_line,
        "line": line,
        "year_group": year_group,
        "season": season,
        "target_path": target_path,
        "target_cid": target.get("cid") if target else "",
        "status": status,
        "message": "；".join(warnings),
    }


def print_table(rows: list[dict], show: int) -> None:
    headers = ["款号", "开款任务", "品线字段", "任务品线", "使用品线", "目标分类路径", "cid", "状态", "提示"]
    widths = [14, 28, 10, 10, 10, 28, 8, 10, 38]
    print(" ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-" * 170)
    for item in rows[:show]:
        values = [
            item["style_no"],
            item["task"],
            item["field_line"],
            item["task_line"],
            item["line"],
            item["target_path"],
            item["target_cid"],
            item["status"],
            item["message"],
        ]
        print(" ".join(str(v or "")[:w].ljust(w) for v, w in zip(values, widths)))
    if len(rows) > show:
        print(f"... 仅显示前 {show} 条，共 {len(rows)} 条")


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

    db = Database()
    categories = load_category_paths(db)
    records = await read_feishu_records(app_token, table_id, view_id, args.limit)
    previews = [make_preview(record, categories, args.line_source) for record in records]

    print("===== 飞书 → 领星分类匹配预览，只读，不写库、不写领星 =====")
    print(f"读取记录数：{len(records)}")
    print(f"领星分类路径数：{len(categories)}")
    print(f"品线来源：{args.line_source}")
    status_counter = Counter(item["status"] for item in previews)
    print("状态分布：" + ", ".join(f"{k}={v}" for k, v in status_counter.items()))
    print()
    print_table(previews, args.show)


if __name__ == "__main__":
    asyncio.run(main())
