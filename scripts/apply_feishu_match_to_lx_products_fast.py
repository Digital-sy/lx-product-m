#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速把飞书款号分类匹配结果上传到领星产品分类。

相比 apply_feishu_match_to_lx_products.py：
1. 不再每个 SKU 写入前查询一次产品详情，直接使用 lxpm_product_category_snapshot.product_name。
2. 不再每个 SKU 写入后立刻复查，改为写完后按 100 个 SKU 一批批量复查。
3. 支持 --delay 控制每个写入请求之间的间隔，默认 0.05 秒。
4. 写入过程中遇到 access token 失效会自动重新获取 token 并重试当前 SKU。
5. 仍然默认只预览，必须加 --confirm 才写领星。

适合已经全量同步过 lxpm_product_category_snapshot 后使用。
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
from lx_product_m.services.product_service import PRODUCT_DETAIL_API, PRODUCT_SET_API, ProductService

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
    parser.add_argument("--delay", type=float, default=0.05, help="每个写入请求后的等待秒数，默认0.05；报限流可调大")
    parser.add_argument("--max-retries", type=int, default=3, help="接口失败最大重试次数，默认3")
    parser.add_argument("--verify-batch-size", type=int, default=100, help="批量复查每批SKU数，默认100")
    parser.add_argument("--skip-verify", action="store_true", help="跳过批量复查；不建议正式使用")
    parser.add_argument("--force", action="store_true", help="即使本地快照分类已等于目标分类，也强制写入")
    parser.add_argument("--confirm", action="store_true", help="确认写入领星；不加只预览")
    return parser.parse_args()


def make_batch_no() -> str:
    return "feishu_fast_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def is_token_error(resp: dict[str, Any] | None) -> bool:
    if not resp:
        return False
    return str(resp.get("code")) == "2001005" or "token" in str(resp.get("msg") or resp.get("message") or "").lower()


def is_retryable_error(resp: dict[str, Any] | None) -> bool:
    if not resp:
        return False
    msg = str(resp.get("msg") or resp.get("message") or "")
    return str(resp.get("code")) in {"500", "502", "503", "504"} or "请求连接异常" in msg or "稍后再试" in msg


def load_candidate_rows(db: Database, statuses: list[str], style_nos: list[str] | None, force: bool, limit: int) -> list[dict[str, Any]]:
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
            c.title AS target_leaf_name
        FROM `{MATCH_TABLE}` m
        JOIN `{SNAPSHOT_TABLE}` p
          ON p.spu = m.style_no
        LEFT JOIN lxpm_category c
          ON c.cid = m.target_category_id
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
    print("===== 快速上传预览：飞书匹配结果 → 领星产品分类 =====")
    print(f"批次号：{batch_no}")
    print(f"待上传SKU数：{len(rows)}")
    print("状态分布：" + ", ".join(f"{k}={v}" for k, v in Counter(str(r.get("match_status")) for r in rows).items()))
    print()
    print("--- 明细预览 ---")
    headers = ["SKU", "款号", "当前分类", "目标分类", "状态", "产品名"]
    widths = [28, 14, 28, 32, 10, 36]
    print(" ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-" * 160)
    for r in rows[:show]:
        current = f"{r.get('old_category_id') or ''} {r.get('old_category_path') or r.get('old_category_name') or ''}"
        target = f"{r.get('target_category_id') or ''} {r.get('target_category_path') or r.get('target_category_name') or ''}"
        values = [
            r.get("sku") or "",
            r.get("style_no") or "",
            current,
            target,
            r.get("match_status") or "",
            r.get("product_name") or "",
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
        for r in rows:
            target_name = r.get("target_leaf_name") or r.get("target_category_name") or r.get("target_category_path") or ""
            remark = (
                f"fast=1; style_no={r.get('style_no')}; target_path={r.get('target_category_path')}; "
                f"match_status={r.get('match_status')}; message={r.get('match_message') or ''}"
            )
            cur.execute(
                sql,
                (
                    batch_no,
                    r.get("sku"),
                    int(r.get("target_category_id") or 0),
                    target_name,
                    "feishu_style_category_match_fast",
                    remark[:1000],
                ),
            )
            item = dict(r)
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
            task.get("product_name") or "",
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


async def request_with_retry(
    client: LingxingClient,
    token: str,
    api_path: str,
    body: dict[str, Any],
    max_retries: int,
) -> tuple[dict[str, Any], str]:
    current_token = token
    last_resp: dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        resp = await client.request(current_token, api_path, "POST", req_body=body)
        last_resp = resp
        if str(resp.get("code")) == "0":
            return resp, current_token
        if is_token_error(resp):
            print("检测到 access token 失效，重新获取 token 后重试当前请求...")
            token_info = await client.generate_token()
            current_token = token_info.token
            await asyncio.sleep(0.5)
            continue
        if is_retryable_error(resp) and attempt < max_retries:
            wait_s = min(10, 1 + attempt * 2)
            print(f"接口临时异常，{wait_s}s后重试：{resp}")
            await asyncio.sleep(wait_s)
            continue
        return resp, current_token
    return last_resp, current_token


async def write_products(db: Database, tasks: list[dict[str, Any]], batch_no: str, delay: float, max_retries: int) -> Counter:
    client = LingxingClient(db=db, enable_api_log=True)
    token_info = await client.generate_token()
    token = token_info.token
    counter: Counter = Counter()
    total = len(tasks)

    for idx, task in enumerate(tasks, 1):
        task_id = int(task["task_id"])
        sku = str(task.get("sku") or "")
        target_id = int(task.get("target_category_id") or 0)
        target_name = str(task.get("target_leaf_name") or task.get("target_category_name") or "")
        body = {
            "sku": sku,
            "product_name": str(task.get("product_name") or ""),
            "category_id": target_id,
            "category": target_name,
        }
        try:
            update_task(db, task_id, "running")
            resp, token = await request_with_retry(client, token, PRODUCT_SET_API, body, max_retries)
            if str(resp.get("code")) == "0":
                update_task(db, task_id, "write_success")
                write_change_log(db, batch_no, task, "write_success", body, resp)
                counter["write_success"] += 1
            else:
                err = str(resp)
                update_task(db, task_id, "failed", err)
                write_change_log(db, batch_no, task, "failed", body, resp, error_message=err)
                counter["failed"] += 1
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            update_task(db, task_id, "failed", err)
            write_change_log(db, batch_no, task, "failed", body, None, error_message=err)
            counter["failed"] += 1
        if idx % 100 == 0 or idx == total:
            print(f"写入进度：{idx}/{total}，{dict(counter)}")
        if delay > 0:
            await asyncio.sleep(delay)
    return counter


def load_tasks_for_verify(db: Database, batch_no: str) -> list[dict[str, Any]]:
    return db.fetch_all(
        f"""
        SELECT t.id AS task_id,
               t.sku,
               t.target_category_id,
               t.target_category_name,
               p.product_name,
               p.category_id AS old_category_id,
               p.category_name AS old_category_name
        FROM `{TASK_TABLE}` t
        LEFT JOIN `{SNAPSHOT_TABLE}` p ON p.sku = t.sku
        WHERE t.batch_no = %s
          AND t.status = 'write_success'
        ORDER BY t.id
        """,
        (batch_no,),
    )


async def verify_batch_with_retry(
    client: LingxingClient,
    token: str,
    batch: list[str],
    max_retries: int,
) -> tuple[list[dict[str, Any]], str]:
    current_token = token
    body = {"skus": batch}
    for attempt in range(max_retries + 1):
        resp = await client.request(current_token, PRODUCT_DETAIL_API, "POST", req_body=body)
        if str(resp.get("code")) == "0":
            return list(resp.get("data") or []), current_token
        if is_token_error(resp):
            print("复查时 token 失效，重新获取 token 后重试...")
            token_info = await client.generate_token()
            current_token = token_info.token
            await asyncio.sleep(0.5)
            continue
        if is_retryable_error(resp) and attempt < max_retries:
            wait_s = min(10, 1 + attempt * 2)
            print(f"复查接口临时异常，{wait_s}s后重试：{resp}")
            await asyncio.sleep(wait_s)
            continue
        raise RuntimeError(f"批量复查失败：{resp}")
    raise RuntimeError("批量复查失败：超过最大重试次数")


async def verify_tasks(db: Database, batch_no: str, batch_size: int, max_retries: int) -> Counter:
    tasks = load_tasks_for_verify(db, batch_no)
    if not tasks:
        return Counter()
    task_by_sku = {str(t["sku"]): t for t in tasks}
    skus = list(task_by_sku.keys())

    client = LingxingClient(db=db, enable_api_log=True)
    token_info = await client.generate_token()
    token = token_info.token
    service = ProductService(client, db)
    counter: Counter = Counter()
    batch_size = max(1, min(batch_size, 100))

    for i in range(0, len(skus), batch_size):
        batch = skus[i:i + batch_size]
        try:
            products, token = await verify_batch_with_retry(client, token, batch, max_retries)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            for sku in batch:
                task = task_by_sku[sku]
                update_task(db, int(task["task_id"]), "verify_failed", err)
                write_change_log(db, batch_no, task, "verify_failed", None, None, None, err)
                counter["verify_failed"] += 1
            print(f"复查进度：{min(i + batch_size, len(skus))}/{len(skus)}，{dict(counter)}")
            continue
        returned = {str(p.get("sku") or p.get("SKU") or ""): p for p in products}
        for sku in batch:
            task = task_by_sku[sku]
            product = returned.get(sku)
            target_id = int(task.get("target_category_id") or 0)
            if not product:
                update_task(db, int(task["task_id"]), "verify_failed", "复查未返回SKU")
                write_change_log(db, batch_no, task, "verify_failed", None, None, {"data": products}, "复查未返回SKU")
                counter["verify_failed"] += 1
                continue
            service.save_product_snapshot(product)
            verify_id, verify_name = service.extract_category(product)
            ok = verify_id == target_id
            status = "success" if ok else "verify_failed"
            err = "" if ok else f"复查分类不一致：verify_id={verify_id}, target_id={target_id}"
            update_task(db, int(task["task_id"]), status, err)
            write_change_log(
                db,
                batch_no,
                task,
                status,
                None,
                None,
                {"data": [product]},
                err,
                verify_category_id=verify_id,
                verify_category_name=verify_name,
            )
            counter[status] += 1
        print(f"复查进度：{min(i + batch_size, len(skus))}/{len(skus)}，{dict(counter)}")
    return counter


async def main() -> None:
    args = parse_args()
    batch_no = args.batch_no or make_batch_no()
    db = Database()
    rows = load_candidate_rows(db, args.statuses, args.style_no, args.force, args.limit)
    print_preview(rows, args.show, batch_no)

    if not rows:
        print("没有需要上传的SKU。")
        return
    if not args.confirm:
        print("\n当前为预览模式，未创建任务、未写领星。确认无误后加 --confirm 执行。")
        return

    tasks = insert_tasks(db, batch_no, rows)
    print(f"\n已创建任务：{len(tasks)} 条，开始快速写入领星...")
    write_counter = await write_products(db, tasks, batch_no, args.delay, args.max_retries)
    print(f"写入完成：{dict(write_counter)}")

    if args.skip_verify:
        print("已跳过复查。")
        return
    print("开始批量复查...")
    verify_counter = await verify_tasks(db, batch_no, args.verify_batch_size, args.max_retries)
    print("\n===== 快速上传完成 =====")
    print(f"写入：{dict(write_counter)}")
    print(f"复查：{dict(verify_counter)}")
    print(f"批次号：{batch_no}")


if __name__ == "__main__":
    asyncio.run(main())
