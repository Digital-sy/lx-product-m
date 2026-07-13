#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速把飞书款号分类匹配结果上传到领星产品分类。

安全规则：
1. 写入前按批次重新读取领星实时产品详情；
2. product_name 只使用实时详情中的当前品名，禁止使用快照旧品名写回；
3. 写分类时携带实时完整 custom_fields，避免覆盖其他自定义字段；
4. 实时品名为空、实时详情缺失时禁止写入；
5. 写入后复查分类和品名，品名发生变化时标记 name_verify_failed。
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lx_product_m.db import Database, json_dumps
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.product_service import ProductService
from lx_product_m.services.product_write_guard import (
    PRODUCT_SET_API,
    build_guarded_product_set_body,
    clean,
    extract_category,
    extract_product_name,
    fetch_live_products,
    normalize_custom_fields,
    request_with_retry,
    verify_product_name,
)

TASK_TABLE = "lxpm_product_category_task"
MATCH_TABLE = "lxpm_feishu_style_category_match"
SNAPSHOT_TABLE = "lxpm_product_category_snapshot"
CHANGE_LOG_TABLE = "lxpm_product_category_change_log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="快速上传飞书匹配结果到领星产品分类")
    parser.add_argument("--batch-no", default="", help="批次号；不传则自动生成")
    parser.add_argument("--statuses", nargs="+", default=["matched"], help="处理状态，默认只处理 matched")
    parser.add_argument("--style-no", action="append", help="只处理指定款号，可重复传")
    parser.add_argument("--limit", type=int, default=0, help="最多上传多少个SKU，默认0表示不限制")
    parser.add_argument("--show", type=int, default=100, help="预览输出行数，默认100")
    parser.add_argument("--delay", type=float, default=0.05, help="每个写入请求后的等待秒数")
    parser.add_argument("--max-retries", type=int, default=5, help="接口失败最大重试次数")
    parser.add_argument("--verify-batch-size", type=int, default=100, help="批量复查每批SKU数")
    parser.add_argument("--live-read-batch-size", type=int, default=100, help="写前实时详情读取批次")
    parser.add_argument("--live-read-delay", type=float, default=0.5, help="实时详情批次间隔秒数")
    parser.add_argument("--skip-verify", action="store_true", help="跳过批量复查；不建议正式使用")
    parser.add_argument("--force", action="store_true", help="即使实时分类已等于目标，也强制写入")
    parser.add_argument("--confirm", action="store_true", help="确认写入领星；不加只预览")
    return parser.parse_args()


def make_batch_no() -> str:
    return "feishu_fast_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def load_candidate_rows(
    db: Database,
    statuses: list[str],
    style_nos: list[str] | None,
    force: bool,
    limit: int,
) -> list[dict[str, Any]]:
    ph = ",".join(["%s"] * len(statuses))
    params: list[Any] = list(statuses)
    sql = f"""
        SELECT
            m.record_id,
            m.style_no,
            m.target_category_path,
            m.target_category_id,
            m.target_category_name,
            m.match_status,
            m.match_message,
            p.sku,
            p.product_name,
            p.category_id AS old_category_id,
            p.category_name AS old_category_name,
            p.category_path AS old_category_path,
            p.custom_fields_json,
            c.title AS target_leaf_name
        FROM `{MATCH_TABLE}` m
        JOIN `{SNAPSHOT_TABLE}` p ON p.spu = m.style_no
        LEFT JOIN lxpm_category c ON c.cid = m.target_category_id
        WHERE m.match_status IN ({ph})
          AND m.target_category_id IS NOT NULL
          AND m.target_category_id > 0
          AND p.sku IS NOT NULL
          AND p.sku <> ''
          AND p.product_name IS NOT NULL
          AND p.product_name <> ''
    """
    if not force:
        sql += " AND (p.category_id IS NULL OR p.category_id <> m.target_category_id)"
    if style_nos:
        style_ph = ",".join(["%s"] * len(style_nos))
        sql += f" AND m.style_no IN ({style_ph})"
        params.extend(style_nos)
    sql += " ORDER BY m.style_no, p.sku"
    if limit and limit > 0:
        sql += " LIMIT %s"
        params.append(limit)
    return db.fetch_all(sql, params)


def print_preview(rows: list[dict[str, Any]], show: int, batch_no: str) -> None:
    print("===== 上传预览：飞书匹配结果 → 领星产品分类 =====")
    print(f"批次号：{batch_no}")
    print(f"待处理SKU数：{len(rows)}")
    print("状态分布：" + ", ".join(f"{k}={v}" for k, v in Counter(clean(r.get("match_status")) for r in rows).items()))
    print("写入保护：写前读取实时品名和完整自定义字段，写后复查品名与分类")
    print()
    headers = ["SKU", "款号", "当前分类", "目标分类", "状态", "快照字段数", "快照品名"]
    widths = [28, 14, 28, 32, 10, 12, 36]
    print(" ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-" * 180)
    for row in rows[:show]:
        current = f"{row.get('old_category_id') or ''} {row.get('old_category_path') or row.get('old_category_name') or ''}"
        target = f"{row.get('target_category_id') or ''} {row.get('target_category_path') or row.get('target_category_name') or ''}"
        values = [
            row.get("sku") or "",
            row.get("style_no") or "",
            current,
            target,
            row.get("match_status") or "",
            str(len(normalize_custom_fields(row.get("custom_fields_json")))),
            row.get("product_name") or "",
        ]
        print(" ".join(str(v)[:w].ljust(w) for v, w in zip(values, widths)))
    if len(rows) > show:
        print(f"... 仅显示前{show}条，共{len(rows)}条")


def insert_tasks(db: Database, batch_no: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sql = f"""
        INSERT INTO `{TASK_TABLE}`
        (`batch_no`, `sku`, `target_category_id`, `target_category_name`, `source_file`, `remark`, `status`)
        VALUES (%s,%s,%s,%s,%s,%s,'pending')
    """
    out: list[dict[str, Any]] = []
    with db.cursor() as cur:
        for row in rows:
            target_name = row.get("target_leaf_name") or row.get("target_category_name") or row.get("target_category_path") or ""
            remark = (
                "live_product_name_guard=1; preserve_live_custom_fields=1; "
                f"style_no={row.get('style_no')}; target_path={row.get('target_category_path')}; "
                f"match_status={row.get('match_status')}; message={row.get('match_message') or ''}"
            )
            cur.execute(
                sql,
                (
                    batch_no,
                    row.get("sku"),
                    int(row.get("target_category_id") or 0),
                    target_name,
                    "feishu_style_category_match_fast_guarded",
                    remark[:1000],
                ),
            )
            item = dict(row)
            item["task_id"] = cur.lastrowid
            out.append(item)
    return out


def update_task(db: Database, task_id: int, status: str, error_message: str = "") -> None:
    db.execute(
        f"UPDATE `{TASK_TABLE}` SET status=%s, error_message=%s WHERE id=%s",
        (status, error_message[:5000], task_id),
    )


def write_change_log(
    db: Database,
    batch_no: str,
    task: dict[str, Any],
    status: str,
    request_json: dict[str, Any] | None,
    response_json: dict[str, Any] | None,
    verify_json: dict[str, Any] | None = None,
    error_message: str = "",
    verify_category_id: int | None = None,
    verify_category_name: str = "",
) -> None:
    product_name = clean(task.get("_write_product_name") or task.get("expected_product_name") or task.get("product_name"))
    db.execute(
        f"""
        INSERT INTO `{CHANGE_LOG_TABLE}`
        (`batch_no`, `task_id`, `sku`, `product_name`,
         `old_category_id`, `old_category_name`, `new_category_id`, `new_category_name`,
         `verify_category_id`, `verify_category_name`, `status`,
         `request_json`, `response_json`, `verify_response_json`, `error_message`)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            batch_no,
            task.get("task_id"),
            task.get("sku"),
            product_name,
            task.get("old_category_id"),
            task.get("old_category_name") or "",
            task.get("target_category_id"),
            task.get("target_leaf_name") or task.get("target_category_name") or "",
            verify_category_id,
            verify_category_name,
            status,
            json_dumps(request_json),
            json_dumps(response_json),
            json_dumps(verify_json),
            error_message[:5000],
        ),
    )


async def write_products(
    db: Database,
    tasks: list[dict[str, Any]],
    batch_no: str,
    delay: float,
    max_retries: int,
    live_batch_size: int,
    live_read_delay: float,
    force: bool,
) -> Counter:
    client = LingxingClient(db=db, enable_api_log=True)
    token = (await client.generate_token()).token
    counter: Counter[str] = Counter()
    total = len(tasks)
    live_batch_size = max(1, min(live_batch_size, 100))

    try:
        for start in range(0, total, live_batch_size):
            batch_tasks = tasks[start:start + live_batch_size]
            skus = [clean(task.get("sku")) for task in batch_tasks]
            try:
                live_map, token = await fetch_live_products(
                    client,
                    token,
                    skus,
                    max_retries=max_retries,
                    batch_size=live_batch_size,
                    delay_seconds=live_read_delay,
                )
            except Exception as exc:  # noqa: BLE001
                err = f"写前实时详情读取失败：{exc}"
                for task in batch_tasks:
                    update_task(db, int(task["task_id"]), "guard_read_failed", err)
                    write_change_log(db, batch_no, task, "guard_read_failed", None, None, error_message=err)
                    counter["guard_read_failed"] += 1
                continue

            for task in batch_tasks:
                task_id = int(task["task_id"])
                sku = clean(task.get("sku"))
                target_id = int(task.get("target_category_id") or 0)
                target_name = clean(task.get("target_leaf_name") or task.get("target_category_name"))
                live_product = live_map.get(sku)
                try:
                    update_task(db, task_id, "running")
                    if not live_product:
                        raise RuntimeError("写前实时详情未返回该SKU")

                    live_name = extract_product_name(live_product)
                    if not live_name:
                        raise RuntimeError("写前实时品名为空，禁止写入")
                    task["_write_product_name"] = live_name
                    task["old_category_id"], task["old_category_name"] = extract_category(live_product)

                    snapshot_name = clean(task.get("product_name"))
                    if snapshot_name and snapshot_name != live_name:
                        counter["snapshot_name_stale"] += 1
                        print(f"[GUARD] SKU={sku} 快照品名已变化，改用实时品名：{snapshot_name!r} -> {live_name!r}")

                    live_category_id, _ = extract_category(live_product)
                    if not force and live_category_id == target_id:
                        update_task(db, task_id, "skipped_live_unchanged")
                        write_change_log(
                            db,
                            batch_no,
                            task,
                            "skipped_live_unchanged",
                            {"_write_guard": {"live_product_name": live_name, "reason": "live_category_already_target"}},
                            None,
                            verify_category_id=live_category_id,
                        )
                        counter["skipped_live_unchanged"] += 1
                        continue

                    body, guard_meta = build_guarded_product_set_body(
                        live_product,
                        sku=sku,
                        target_category_id=target_id,
                        target_category_name=target_name,
                        target_custom_fields=None,
                    )
                    log_request = dict(body)
                    log_request["_write_guard"] = {
                        **guard_meta,
                        "snapshot_product_name": snapshot_name,
                        "name_source": "live_product_detail",
                    }
                    response, token = await request_with_retry(
                        client,
                        token,
                        PRODUCT_SET_API,
                        body,
                        max_retries=max_retries,
                    )
                    if clean(response.get("code")) == "0":
                        update_task(db, task_id, "write_success")
                        write_change_log(db, batch_no, task, "write_success", log_request, response)
                        counter["write_success"] += 1
                    else:
                        err = str(response)
                        update_task(db, task_id, "failed", err)
                        write_change_log(db, batch_no, task, "failed", log_request, response, error_message=err)
                        counter["failed"] += 1
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                    update_task(db, task_id, "guard_blocked", err)
                    write_change_log(db, batch_no, task, "guard_blocked", None, None, error_message=err)
                    counter["guard_blocked"] += 1

                current_index = start + batch_tasks.index(task) + 1
                if current_index % 100 == 0 or current_index == total:
                    print(f"写入进度：{current_index}/{total}，{dict(counter)}")
                if delay > 0:
                    await asyncio.sleep(delay)
    finally:
        await client.aclose()
    return counter


def load_tasks_for_verify(db: Database, batch_no: str) -> list[dict[str, Any]]:
    return db.fetch_all(
        f"""
        SELECT t.id AS task_id,
               t.sku,
               t.target_category_id,
               t.target_category_name,
               l.product_name AS expected_product_name,
               l.old_category_id,
               l.old_category_name,
               l.new_category_id AS target_category_id_from_log,
               l.new_category_name AS target_leaf_name
        FROM `{TASK_TABLE}` t
        JOIN (
            SELECT task_id, product_name, old_category_id, old_category_name,
                   new_category_id, new_category_name
            FROM (
                SELECT task_id, product_name, old_category_id, old_category_name,
                       new_category_id, new_category_name,
                       ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY id DESC) AS rn
                FROM `{CHANGE_LOG_TABLE}`
                WHERE batch_no = %s AND status = 'write_success'
            ) x
            WHERE rn = 1
        ) l ON l.task_id = t.id
        WHERE t.batch_no = %s
          AND t.status = 'write_success'
        ORDER BY t.id
        """,
        (batch_no, batch_no),
    )


async def verify_tasks(db: Database, batch_no: str, batch_size: int, max_retries: int) -> Counter:
    tasks = load_tasks_for_verify(db, batch_no)
    if not tasks:
        return Counter()

    task_by_sku = {clean(task.get("sku")): task for task in tasks}
    skus = list(task_by_sku)
    client = LingxingClient(db=db, enable_api_log=True)
    token = (await client.generate_token()).token
    service = ProductService(client, db)
    counter: Counter[str] = Counter()
    batch_size = max(1, min(batch_size, 100))

    try:
        for start in range(0, len(skus), batch_size):
            batch = skus[start:start + batch_size]
            try:
                products, token = await fetch_live_products(
                    client,
                    token,
                    batch,
                    max_retries=max_retries,
                    batch_size=batch_size,
                    delay_seconds=0.5,
                )
            except Exception as exc:  # noqa: BLE001
                err = f"复查实时详情失败：{exc}"
                for sku in batch:
                    task = task_by_sku[sku]
                    update_task(db, int(task["task_id"]), "verify_failed", err)
                    write_change_log(db, batch_no, task, "verify_failed", None, None, error_message=err)
                    counter["verify_failed"] += 1
                continue

            for sku in batch:
                task = task_by_sku[sku]
                product = products.get(sku)
                if not product:
                    err = "复查未返回SKU"
                    update_task(db, int(task["task_id"]), "verify_failed", err)
                    write_change_log(db, batch_no, task, "verify_failed", None, None, error_message=err)
                    counter["verify_failed"] += 1
                    continue

                service.save_product_snapshot(product)
                verify_id, verify_name = extract_category(product)
                expected_name = clean(task.get("expected_product_name"))
                name_ok, name_error = verify_product_name(product, expected_name)
                target_id = int(task.get("target_category_id_from_log") or task.get("target_category_id") or 0)
                category_ok = verify_id == target_id

                if not name_ok:
                    status = "name_verify_failed"
                    err = name_error
                elif not category_ok:
                    status = "verify_failed"
                    err = f"复查分类不一致：verify_id={verify_id}, target_id={target_id}"
                else:
                    status = "success"
                    err = ""

                task["_write_product_name"] = expected_name
                update_task(db, int(task["task_id"]), status, err)
                write_change_log(
                    db,
                    batch_no,
                    task,
                    status,
                    None,
                    None,
                    {"data": [product], "name_guard_expected": expected_name},
                    err,
                    verify_category_id=verify_id,
                    verify_category_name=verify_name,
                )
                counter[status] += 1
            print(f"复查进度：{min(start + batch_size, len(skus))}/{len(skus)}，{dict(counter)}")
    finally:
        await client.aclose()
    return counter


async def main() -> None:
    args = parse_args()
    batch_no = args.batch_no or make_batch_no()
    db = Database()
    try:
        rows = load_candidate_rows(db, args.statuses, args.style_no, args.force, args.limit)
        print_preview(rows, args.show, batch_no)

        if not rows:
            print("没有需要上传的SKU。")
            return
        if not args.confirm:
            print("\n当前为预览模式，未创建任务、未写领星。确认无误后加 --confirm 执行。")
            return

        tasks = insert_tasks(db, batch_no, rows)
        print(f"\n已创建任务：{len(tasks)} 条，开始安全写入领星...")
        write_counter = await write_products(
            db,
            tasks,
            batch_no,
            args.delay,
            args.max_retries,
            args.live_read_batch_size,
            args.live_read_delay,
            args.force,
        )
        print(f"写入完成：{dict(write_counter)}")

        if args.skip_verify:
            print("已跳过复查。")
            return
        print("开始批量复查分类与品名...")
        verify_counter = await verify_tasks(db, batch_no, args.verify_batch_size, args.max_retries)
        print("\n===== 安全上传完成 =====")
        print(f"写入：{dict(write_counter)}")
        print(f"复查：{dict(verify_counter)}")
        print(f"批次号：{batch_no}")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
