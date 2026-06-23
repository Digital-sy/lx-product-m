#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书款号分类匹配同步：飞书款式表 → MySQL 匹配表。

默认只预览，不写库。加 --confirm 才写入 lxpm_feishu_style_category_match。
不调用领星写入接口。
"""
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

from lx_product_m.db import Database, json_dumps
from lx_product_m.feishu_client import FeishuBitableClient, extract_feishu_text

KNOWN_LINES = ["S-基础款", "设计师款", "特征款", "基础款", "测试款", "RS款"]
TABLE_NAME = "lxpm_feishu_style_category_match"
DDL_PATH = PROJECT_ROOT / "sql" / "001_create_feishu_style_category_match.sql"


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
    parser = argparse.ArgumentParser(description="同步飞书款号分类匹配结果到MySQL")
    parser.add_argument("--url", help="飞书多维表URL，可自动提取 app_token/table_id/view_id")
    parser.add_argument("--app-token", help="飞书多维表 app_token")
    parser.add_argument("--table-id", help="飞书 table_id")
    parser.add_argument("--view-id", help="飞书 view_id，可选")
    parser.add_argument("--limit", type=int, default=0, help="读取记录数，默认0表示全量")
    parser.add_argument("--show", type=int, default=50, help="预览输出行数，默认50")
    parser.add_argument("--init-table", action="store_true", help="先创建/校验目标表")
    parser.add_argument("--confirm", action="store_true", help="确认写入MySQL；不加则只预览")
    return parser.parse_args()


def row_text(row: dict, key: str) -> str:
    return extract_feishu_text(row.get(key)).strip()


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
    return await client.list_records(page_size=500, max_records=200000)


def build_match_row(record: dict, categories: dict[str, dict], style_counts: Counter) -> dict:
    row = record.get("fields", {}) or {}
    record_id = record.get("record_id") or record.get("id") or ""
    style_no = row_text(row, "款号")
    shop_name = row_text(row, "店铺")
    task_text = row_text(row, "开款任务")
    season_field = row_text(row, "季节")
    product_line_field = row_text(row, "品线")
    product_line_from_task = parse_line_from_task(task_text)

    # 正式规则：品线优先使用飞书「品线」字段；任务品线只做校验提示。
    used_product_line = product_line_field or product_line_from_task
    year_group, season, parse_problem = parse_year_season(task_text)
    target_category_path = (
        f"{year_group}/{season}/{used_product_line}"
        if year_group and season and used_product_line
        else ""
    )
    target = categories.get(target_category_path) if target_category_path else None
    style_no_count = style_counts.get(style_no, 0) if style_no else 0
    is_duplicate = 1 if style_no_count > 1 else 0

    messages = []
    if parse_problem:
        messages.append(parse_problem)
    if not style_no:
        messages.append("缺少款号")
    if not used_product_line:
        messages.append("缺少品线")
    if product_line_field and product_line_from_task and product_line_field != product_line_from_task:
        messages.append(f"品线冲突:字段={product_line_field},任务={product_line_from_task}")
    if target_category_path and not target:
        messages.append("领星分类不存在")
    if target and int(target.get("is_leaf") or 0) != 1:
        messages.append("目标分类不是叶子节点")
    if is_duplicate:
        messages.append(f"同款号重复{style_no_count}条")

    if not style_no or not target_category_path or not target:
        match_status = "invalid" if not target_category_path else "not_found"
    elif is_duplicate:
        match_status = "duplicate"
    elif messages:
        match_status = "warning"
    else:
        match_status = "matched"

    return {
        "record_id": record_id,
        "style_no": style_no,
        "shop_name": shop_name,
        "task_text": task_text,
        "season_field": season_field,
        "product_line_field": product_line_field,
        "product_line_from_task": product_line_from_task,
        "used_product_line": used_product_line,
        "year_group": year_group,
        "season": season,
        "target_category_path": target_category_path,
        "target_category_id": target.get("cid") if target else None,
        "target_category_name": target.get("title") if target else "",
        "match_status": match_status,
        "match_message": "；".join(messages),
        "style_no_count": style_no_count or 1,
        "is_duplicate": is_duplicate,
        "raw_json": record,
    }


def ensure_table(db: Database) -> None:
    if not DDL_PATH.exists():
        raise RuntimeError(f"DDL文件不存在：{DDL_PATH}")
    sql = DDL_PATH.read_text(encoding="utf-8")
    db.execute(sql)


def save_rows(db: Database, rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = f"""
        INSERT INTO `{TABLE_NAME}`
        (`record_id`, `style_no`, `shop_name`, `task_text`, `season_field`,
         `product_line_field`, `product_line_from_task`, `used_product_line`,
         `year_group`, `season`, `target_category_path`, `target_category_id`,
         `target_category_name`, `match_status`, `match_message`, `style_no_count`,
         `is_duplicate`, `raw_json`, `synced_at`)
        VALUES
        (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON DUPLICATE KEY UPDATE
          `style_no`=VALUES(`style_no`),
          `shop_name`=VALUES(`shop_name`),
          `task_text`=VALUES(`task_text`),
          `season_field`=VALUES(`season_field`),
          `product_line_field`=VALUES(`product_line_field`),
          `product_line_from_task`=VALUES(`product_line_from_task`),
          `used_product_line`=VALUES(`used_product_line`),
          `year_group`=VALUES(`year_group`),
          `season`=VALUES(`season`),
          `target_category_path`=VALUES(`target_category_path`),
          `target_category_id`=VALUES(`target_category_id`),
          `target_category_name`=VALUES(`target_category_name`),
          `match_status`=VALUES(`match_status`),
          `match_message`=VALUES(`match_message`),
          `style_no_count`=VALUES(`style_no_count`),
          `is_duplicate`=VALUES(`is_duplicate`),
          `raw_json`=VALUES(`raw_json`),
          `synced_at`=NOW()
    """
    params = [
        (
            r["record_id"],
            r["style_no"],
            r["shop_name"],
            r["task_text"],
            r["season_field"],
            r["product_line_field"],
            r["product_line_from_task"],
            r["used_product_line"],
            r["year_group"],
            r["season"],
            r["target_category_path"],
            r["target_category_id"],
            r["target_category_name"],
            r["match_status"],
            r["match_message"],
            r["style_no_count"],
            r["is_duplicate"],
            json_dumps(r["raw_json"]),
        )
        for r in rows
    ]
    total = 0
    batch_size = 500
    with db.cursor() as cur:
        for i in range(0, len(params), batch_size):
            batch = params[i : i + batch_size]
            cur.executemany(sql, batch)
            total += len(batch)
            print(f"已写入/更新 {total}/{len(params)}")
    return total


def print_preview(rows: list[dict], show: int) -> None:
    headers = ["款号", "开款任务", "品线", "任务品线", "目标分类路径", "cid", "状态", "提示"]
    widths = [14, 30, 10, 10, 30, 8, 10, 45]
    print(" ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-" * 170)
    for r in rows[:show]:
        values = [
            r["style_no"],
            r["task_text"],
            r["used_product_line"],
            r["product_line_from_task"],
            r["target_category_path"],
            r["target_category_id"] or "",
            r["match_status"],
            r["match_message"],
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
    if args.init_table or args.confirm:
        ensure_table(db)

    categories = load_category_paths(db)
    records = await read_feishu_records(app_token, table_id, view_id, args.limit)
    style_counts = Counter(row_text((rec.get("fields", {}) or {}), "款号") for rec in records)
    rows = [build_match_row(rec, categories, style_counts) for rec in records]

    print("===== 飞书款号分类匹配同步 =====")
    print("规则：年份/季节取开款任务；品线优先取飞书「品线」字段；任务品线仅校验")
    print(f"读取飞书记录：{len(records)}")
    print(f"领星分类路径：{len(categories)}")
    print("状态分布：" + ", ".join(f"{k}={v}" for k, v in Counter(r['match_status'] for r in rows).items()))
    print()
    print_preview(rows, args.show)

    if not args.confirm:
        print("\n当前为预览模式，未写入MySQL。确认无误后加 --confirm 写入。")
        return

    written = save_rows(db, rows)
    print(f"\n完成：写入/更新 {written} 条到 `{TABLE_NAME}`")


if __name__ == "__main__":
    asyncio.run(main())
