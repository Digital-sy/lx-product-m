#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把飞书款号分类匹配结果应用到领星产品 SKU 分类。

安全规则：
1. 默认只预览，不写任务、不写领星。
2. 加 --confirm 后才会创建任务并调用领星产品分类写入接口。
3. 数据链路：lxpm_feishu_style_category_match.style_no -> lxpm_product_category_snapshot.spu -> SKU。
4. 只处理 match_status in matched/warning 且 target_category_id 不为空的数据。
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lx_product_m.config import settings
from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.product_service import ProductService

TASK_TABLE = "lxpm_product_category_task"
MATCH_TABLE = "lxpm_feishu_style_category_match"
SNAPSHOT_TABLE = "lxpm_product_category_snapshot"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="飞书款号匹配结果上传领星产品分类")
    parser.add_argument("--batch-no", default="", help="批次号；不传则自动生成")
    parser.add_argument(
        "--statuses",
        nargs="+",
        default=["matched", "warning"],
        help="允许上传的飞书匹配状态，默认 matched warning",
    )
    parser.add_argument("--style-no", action="append", help="只处理指定款号，可重复传")
    parser.add_argument("--limit", type=int, default=0, help="最多上传多少个SKU，默认0表示不限制")
    parser.add_argument("--show", type=int, default=100, help="预览输出行数，默认100")
    parser.add_argument("--force", action="store_true", help="即使本地快照分类已等于目标分类，也强制写领星")
    parser.add_argument("--create-tasks-only", action="store_true", help="只创建任务，不调用领星写入")
    parser.add_argument("--confirm", action="store_true", help="确认写入任务/领星；不加则只预览")
    return parser.parse_args()


def make_batch_no() -> str:
    return "feishu_match_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def load_candidate_rows(db: Database, statuses: list[str], style_nos: list[str] | None = None) -> list[dict[str, Any]]:
    placeholders = ",".join(["%s"] * len(statuses))
    params: list[Any] = list(statuses)
    sql = f"""
        SELECT
            m.record_id,
            m.style_no,
            m.task_text,
            m.product_line_field,
            m.product_line_from_task,
            m.used_product_line,
            m.target_category_path,
            m.target_category_id,
            m.target_category_name,
            m.match_status,
            m.match_message,
            p.sku,
            p.product_name,
            p.category_id AS old_category_id,
            p.category_name AS old_category_name,
            p.category_path AS old_category_path
        FROM `{MATCH_TABLE}` m
        LEFT JOIN `{SNAPSHOT_TABLE}` p
          ON p.spu = m.style_no
        WHERE m.match_status IN ({placeholders})
          AND m.target_category_id IS NOT NULL
          AND m.target_category_id > 0
    """
    if style_nos:
        style_ph = ",".join(["%s"] * len(style_nos))
        sql += f" AND m.style_no IN ({style_ph})"
        params.extend(style_nos)
    sql += " ORDER BY m.style_no, p.sku"
    return db.fetch_all(sql, params)


def build_plan(rows: list[dict[str, Any]], force: bool = False, limit: int = 0) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    to_update: list[dict[str, Any]] = []
    already_ok: list[dict[str, Any]] = []
    missing_style_set: set[str] = set()

    for row in rows:
        sku = str(row.get("sku") or "").strip()
        style_no = str(row.get("style_no") or "").strip()
        if not sku:
            if style_no:
                missing_style_set.add(style_no)
            continue
        target_id = int(row.get("target_category_id") or 0)
        old_id = row.get("old_category_id")
        try:
            old_id_int = int(old_id) if old_id not in (None, "") else None
        except (TypeError, ValueError):
            old_id_int = None
        if not force and old_id_int == target_id:
            already_ok.append(row)
        else:
            to_update.append(row)

    if limit and limit > 0:
        to_update = to_update[:limit]
    return to_update, already_ok, sorted(missing_style_set)


def print_summary(rows: list[dict[str, Any]], to_update: list[dict[str, Any]], already_ok: list[dict[str, Any]], missing_styles: list[str], show: int) -> None:
    styles = {str(r.get("style_no") or "") for r in rows if r.get("style_no")}
    skus = {str(r.get("sku") or "") for r in rows if r.get("sku")}
    status_counter = Counter(str(r.get("match_status") or "") for r in rows if r.get("style_no"))
    update_by_status = Counter(str(r.get("match_status") or "") for r in to_update)

    print("===== 飞书匹配结果 → 领星产品分类上传预览 =====")
    print(f"匹配款号数：{len(styles)}")
    print(f"匹配SKU数：{len(skus)}")
    print("飞书匹配状态分布：" + ", ".join(f"{k}={v}" for k, v in status_counter.items()))
    print(f"待上传SKU数：{len(to_update)}")
    print(f"本地快照已是目标分类SKU数：{len(already_ok)}")
    print(f"未在产品快照中找到SKU的款号数：{len(missing_styles)}")
    print("待上传状态分布：" + ", ".join(f"{k}={v}" for k, v in update_by_status.items()))
    print()

    if missing_styles:
        print("--- 未找到SKU的款号，说明 lxpm_product_category_snapshot 中没有这些 SPU 的SKU ---")
        print(", ".join(missing_styles[:100]))
        if len(missing_styles) > 100:
            print(f"... 仅显示前100个，共{len(missing_styles)}个")
        print()

    print("--- 待上传明细预览 ---")
    headers = ["SKU", "款号", "当前分类", "目标分类", "状态", "提示"]
    widths = [26, 14, 28, 30, 10, 45]
    print(" ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-" * 170)
    for r in to_update[:show]:
        current = f"{r.get('old_category_id') or ''} {r.get('old_category_path') or r.get('old_category_name') or ''}"
        target = f"{r.get('target_category_id') or ''} {r.get('target_category_path') or r.get('target_category_name') or ''}"
        values = [
            r.get("sku") or "",
            r.get("style_no") or "",
            current,
            target,
            r.get("match_status") or "",
            r.get("match_message") or "",
        ]
        print(" ".join(str(v or "")[:w].ljust(w) for v, w in zip(values, widths)))
    if len(to_update) > show:
        print(f"... 仅显示前{show}条，共{len(to_update)}条")


def insert_tasks(db: Database, batch_no: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    sql = f"""
        INSERT INTO `{TASK_TABLE}`
        (`batch_no`, `sku`, `target_category_id`, `target_category_name`, `source_file`, `remark`, `status`)
        VALUES (%s,%s,%s,%s,%s,%s,'pending')
    """
    with db.cursor() as cur:
        for r in rows:
            remark = (
                f"style_no={r.get('style_no')}; "
                f"target_path={r.get('target_category_path')}; "
                f"match_status={r.get('match_status')}; "
                f"message={r.get('match_message') or ''}"
            )
            cur.execute(
                sql,
                (
                    batch_no,
                    r.get("sku"),
                    int(r.get("target_category_id") or 0),
                    r.get("target_category_name") or r.get("target_category_path") or "",
                    "feishu_style_category_match",
                    remark[:1000],
                ),
            )
            task_id = cur.lastrowid
            item = dict(r)
            item["task_id"] = task_id
            tasks.append(item)
    return tasks


def set_task_status(db: Database, task_id: int, status: str, error_message: str = "") -> None:
    db.execute(
        f"UPDATE `{TASK_TABLE}` SET status=%s, error_message=%s WHERE id=%s",
        (status, error_message[:5000], task_id),
    )


async def apply_tasks(db: Database, tasks: list[dict[str, Any]], batch_no: str) -> Counter:
    client = LingxingClient(db=db, enable_api_log=True)
    token_info = await client.generate_token()
    service = ProductService(client, db)
    counter: Counter = Counter()

    for idx, task in enumerate(tasks, 1):
        task_id = int(task["task_id"])
        sku = str(task.get("sku") or "")
        target_category_id = int(task.get("target_category_id") or 0)
        print(f"[{idx}/{len(tasks)}] 上传SKU分类：sku={sku}, target_category_id={target_category_id}")
        try:
            set_task_status(db, task_id, "running")
            await service.update_product_category(
                token_info.token,
                sku=sku,
                category_id=target_category_id,
                batch_no=batch_no,
                task_id=task_id,
            )
            set_task_status(db, task_id, "success")
            counter["success"] += 1
            print("  ✅ success")
        except Exception as exc:  # noqa: BLE001
            set_task_status(db, task_id, "failed", str(exc))
            counter["failed"] += 1
            print(f"  ❌ failed: {exc}")
        await asyncio.sleep(settings.collection_delay_seconds)

    return counter


async def main() -> None:
    args = parse_args()
    batch_no = args.batch_no or make_batch_no()
    db = Database()
    rows = load_candidate_rows(db, args.statuses, args.style_no)
    to_update, already_ok, missing_styles = build_plan(rows, force=args.force, limit=args.limit)
    print_summary(rows, to_update, already_ok, missing_styles, args.show)
    print(f"批次号：{batch_no}")

    if not to_update:
        print("没有需要上传的SKU。")
        return
    if not args.confirm:
        print("\n当前为预览模式，未创建任务、未写领星。确认无误后加 --confirm 执行。")
        return

    tasks = insert_tasks(db, batch_no, to_update)
    print(f"\n已创建任务：{len(tasks)} 条，batch_no={batch_no}")
    if args.create_tasks_only:
        print("已按 --create-tasks-only 停止，未调用领星写入。")
        return

    result = await apply_tasks(db, tasks, batch_no)
    print("\n===== 上传完成 =====")
    print(", ".join(f"{k}={v}" for k, v in result.items()))
    print(f"批次号：{batch_no}")


if __name__ == "__main__":
    asyncio.run(main())
