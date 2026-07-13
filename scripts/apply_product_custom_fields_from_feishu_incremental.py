#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日增量维护领星产品自定义字段。

安全规则：
1. 写入前重新读取领星实时产品详情；
2. product_name 只使用实时品名；
3. 只替换季节、品线、开发年份、品类，其他实时自定义字段完整保留；
4. 当前分类原样携带，避免 product/set 顺带覆盖；
5. 写入后复查品名和目标自定义字段。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lx_product_m.db import Database, json_dumps
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.product_service import ProductService
from lx_product_m.services.product_write_guard import (
    PRODUCT_SET_API,
    build_guarded_product_set_body,
    clean,
    extract_custom_fields,
    extract_product_name,
    fetch_live_products,
    normalize_custom_fields,
    request_with_retry,
    target_fields_match,
    verify_product_name,
)

MATCH_TABLE = "lxpm_feishu_style_category_match"
SNAPSHOT_TABLE = "lxpm_product_category_snapshot"
LOG_TABLE = "lxpm_product_custom_field_change_log"

FIELD_IDS = {
    "季节": "207714670595318277",
    "品线": "207714670595318275",
    "开发年份": "207714670595318273",
    "品类": "207714671567742465",
}

DDL = """
CREATE TABLE IF NOT EXISTS `lxpm_product_custom_field_change_log` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `batch_no` VARCHAR(64) NOT NULL,
  `sku` VARCHAR(128) NOT NULL,
  `style_no` VARCHAR(128) NOT NULL DEFAULT '',
  `custom_field_key` VARCHAR(128) NOT NULL DEFAULT 'custom_fields',
  `fields_json` JSON NULL,
  `status` VARCHAR(32) NOT NULL,
  `request_json` JSON NULL,
  `response_json` JSON NULL,
  `error_message` VARCHAR(2000) NOT NULL DEFAULT '',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_batch` (`batch_no`),
  KEY `idx_sku` (`sku`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每日增量维护领星产品自定义字段，只写新增或字段值发生变化的SKU")
    parser.add_argument("--batch-no", default="")
    parser.add_argument("--style-no", action="append")
    parser.add_argument("--sku-like", default="")
    parser.add_argument("--statuses", nargs="+", default=["matched", "warning"])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--show", type=int, default=50)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--live-read-batch-size", type=int, default=100)
    parser.add_argument("--verify-batch-size", type=int, default=100)
    parser.add_argument("--force", action="store_true", help="忽略历史成功日志，强制检查筛选范围内所有SKU")
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args()


def strip_line(value: str) -> str:
    text = clean(value)
    for prefix in ("S-", "s-", "S_", "s_", "S ", "s "):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def full_year(value: str) -> str:
    text = clean(value)
    if text.isdigit() and len(text) == 2:
        number = int(text)
        return str(2000 + number if number < 80 else 1900 + number)
    return text


def split_task(task: str) -> list[str]:
    return [part.strip() for part in clean(task).split("-") if part.strip()]


def task_year(row: dict[str, Any]) -> str:
    parts = split_task(row.get("task_text"))
    return full_year(parts[0] if parts else clean(row.get("year_group")))


def task_line(row: dict[str, Any]) -> str:
    parts = split_task(row.get("task_text"))
    if len(parts) >= 4:
        return strip_line(parts[-2])
    return strip_line(clean(row.get("product_line_from_task")) or clean(row.get("used_product_line")))


def task_category(row: dict[str, Any]) -> str:
    parts = split_task(row.get("task_text"))
    if len(parts) >= 5:
        return parts[-1]
    name = clean(row.get("target_category_name"))
    line = task_line(row)
    if name and strip_line(name) != line:
        return strip_line(name)
    path_leaf = clean(row.get("target_category_path")).split("/")[-1].strip()
    return strip_line(name or path_leaf)


def expected_values(row: dict[str, Any]) -> dict[str, str]:
    return {
        "季节": clean(row.get("season")),
        "品线": task_line(row),
        "开发年份": task_year(row),
        "品类": task_category(row),
    }


def build_fields(row: dict[str, Any]) -> list[dict[str, str]]:
    fields = []
    for name, value in expected_values(row).items():
        if value:
            fields.append({"id": FIELD_IDS[name], "name": name, "val": value})
    return fields


def load_rows(db: Database, args: argparse.Namespace) -> list[dict[str, Any]]:
    placeholders = ",".join(["%s"] * len(args.statuses))
    params: list[Any] = list(args.statuses)
    sql = f"""
        SELECT m.style_no, m.task_text, m.year_group, m.season,
               m.product_line_from_task, m.used_product_line,
               m.target_category_path, m.target_category_name, m.match_status,
               p.sku, p.product_name
        FROM `{MATCH_TABLE}` m
        JOIN `{SNAPSHOT_TABLE}` p ON p.spu = m.style_no
        WHERE m.match_status IN ({placeholders})
          AND p.sku IS NOT NULL AND p.sku <> ''
          AND p.product_name IS NOT NULL AND p.product_name <> ''
    """
    if args.style_no:
        style_placeholders = ",".join(["%s"] * len(args.style_no))
        sql += f" AND m.style_no IN ({style_placeholders})"
        params.extend(args.style_no)
    if args.sku_like:
        sql += " AND p.sku LIKE %s"
        params.append(args.sku_like)
    sql += " ORDER BY m.style_no, p.sku"
    if args.limit:
        sql += " LIMIT %s"
        params.append(args.limit)
    return db.fetch_all(sql, params)


def load_latest_success_fields(db: Database) -> dict[str, list[dict[str, str]]]:
    sql = f"""
        SELECT sku, fields_json
        FROM (
            SELECT sku, fields_json,
                   ROW_NUMBER() OVER (PARTITION BY sku ORDER BY id DESC) AS rn
            FROM `{LOG_TABLE}`
            WHERE status = 'success'
              AND custom_field_key = 'custom_fields'
        ) t
        WHERE rn = 1
    """
    return {
        clean(row.get("sku")): normalize_custom_fields(row.get("fields_json"))
        for row in db.fetch_all(sql)
    }


def select_changed(
    rows: list[dict[str, Any]],
    latest: dict[str, list[dict[str, str]]],
    force: bool,
) -> tuple[list[dict[str, Any]], Counter]:
    counter: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []
    for row in rows:
        current = normalize_custom_fields(build_fields(row))
        previous = latest.get(clean(row.get("sku")))
        if force:
            row["_change_reason"] = "force"
            selected.append(row)
            counter["force"] += 1
        elif not previous:
            row["_change_reason"] = "new"
            selected.append(row)
            counter["new"] += 1
        elif previous != current:
            row["_change_reason"] = "changed"
            selected.append(row)
            counter["changed"] += 1
        else:
            counter["skipped_unchanged_log"] += 1
    return selected, counter


def log_row(
    db: Database,
    batch_no: str,
    row: dict[str, Any],
    fields: list[dict[str, str]],
    status: str,
    request_json: dict[str, Any] | None,
    response_json: dict[str, Any] | None,
    error: str = "",
) -> None:
    db.execute(
        f"""
        INSERT INTO `{LOG_TABLE}`
        (`batch_no`,`sku`,`style_no`,`custom_field_key`,`fields_json`,`status`,`request_json`,`response_json`,`error_message`)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            batch_no,
            row.get("sku"),
            row.get("style_no") or "",
            "custom_fields",
            json_dumps(fields),
            status,
            json_dumps(request_json),
            json_dumps(response_json),
            error[:2000],
        ),
    )


async def verify_written(
    db: Database,
    client: LingxingClient,
    token: str,
    batch_no: str,
    written_rows: list[dict[str, Any]],
    max_retries: int,
    batch_size: int,
) -> tuple[Counter, str]:
    counter: Counter[str] = Counter()
    service = ProductService(client, db)
    batch_size = max(1, min(batch_size, 100))
    current_token = token

    for start in range(0, len(written_rows), batch_size):
        batch_rows = written_rows[start:start + batch_size]
        skus = [clean(row.get("sku")) for row in batch_rows]
        try:
            live_map, current_token = await fetch_live_products(
                client,
                current_token,
                skus,
                max_retries=max_retries,
                batch_size=batch_size,
                delay_seconds=0.5,
            )
        except Exception as exc:  # noqa: BLE001
            for row in batch_rows:
                log_row(db, batch_no, row, row["_target_fields"], "verify_failed", None, None, str(exc))
                counter["verify_failed"] += 1
            continue

        for row in batch_rows:
            sku = clean(row.get("sku"))
            product = live_map.get(sku)
            if not product:
                log_row(db, batch_no, row, row["_target_fields"], "verify_failed", None, None, "复查未返回SKU")
                counter["verify_failed"] += 1
                continue

            service.save_product_snapshot(product)
            name_ok, name_error = verify_product_name(product, row["_expected_product_name"])
            fields_ok = target_fields_match(extract_custom_fields(product), row["_target_fields"])
            if not name_ok:
                status = "name_verify_failed"
                error = name_error
            elif not fields_ok:
                status = "verify_failed"
                error = "复查目标自定义字段不一致"
            else:
                status = "success"
                error = ""
            log_row(
                db,
                batch_no,
                row,
                row["_target_fields"],
                status,
                None,
                {"data": [product], "name_guard_expected": row["_expected_product_name"]},
                error,
            )
            counter[status] += 1
        print(f"复查进度：{min(start + batch_size, len(written_rows))}/{len(written_rows)}，{dict(counter)}")
    return counter, current_token


async def main() -> None:
    args = parse_args()
    batch_no = args.batch_no or "custom_fields_incremental_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    db = Database()
    db.execute(DDL)

    try:
        rows = load_rows(db, args)
        latest = {} if args.force else load_latest_success_fields(db)
        targets, stat = select_changed(rows, latest, args.force)

        print("===== 产品自定义字段增量维护（品名保护版） =====")
        print("批次号：", batch_no)
        print("状态范围：", ",".join(args.statuses))
        print("候选SKU：", len(rows))
        print("本次需检查：", len(targets))
        print("筛选统计：", dict(stat))
        print("安全策略：实时品名 + 实时完整自定义字段合并 + 写后复查")
        for row in targets[: args.show]:
            print(row["sku"], row.get("_change_reason"), "->", build_fields(row), clean(row.get("task_text")))
        if not args.confirm:
            print("预览模式，未写入。")
            return

        client = LingxingClient(db=db, enable_api_log=True)
        token = (await client.generate_token()).token
        write_counter: Counter[str] = Counter()
        written_rows: list[dict[str, Any]] = []
        batch_size = max(1, min(args.live_read_batch_size, 100))

        try:
            for start in range(0, len(targets), batch_size):
                batch_rows = targets[start:start + batch_size]
                skus = [clean(row.get("sku")) for row in batch_rows]
                try:
                    live_map, token = await fetch_live_products(
                        client,
                        token,
                        skus,
                        max_retries=args.max_retries,
                        batch_size=batch_size,
                        delay_seconds=0.5,
                    )
                except Exception as exc:  # noqa: BLE001
                    for row in batch_rows:
                        log_row(db, batch_no, row, build_fields(row), "guard_read_failed", None, None, str(exc))
                        write_counter["guard_read_failed"] += 1
                    continue

                for row in batch_rows:
                    sku = clean(row.get("sku"))
                    target_fields = build_fields(row)
                    live_product = live_map.get(sku)
                    try:
                        if not live_product:
                            raise RuntimeError("写前实时详情未返回该SKU")
                        live_name = extract_product_name(live_product)
                        if not live_name:
                            raise RuntimeError("写前实时品名为空，禁止写入")

                        snapshot_name = clean(row.get("product_name"))
                        if snapshot_name and snapshot_name != live_name:
                            write_counter["snapshot_name_stale"] += 1
                            print(f"[GUARD] SKU={sku} 快照品名已变化，改用实时品名：{snapshot_name!r} -> {live_name!r}")

                        if target_fields_match(extract_custom_fields(live_product), target_fields) and not args.force:
                            log_row(
                                db,
                                batch_no,
                                row,
                                target_fields,
                                "success",
                                {"_write_guard": {"skipped_live_unchanged": True, "live_product_name": live_name}},
                                None,
                            )
                            write_counter["skipped_live_unchanged"] += 1
                            continue

                        body, guard_meta = build_guarded_product_set_body(
                            live_product,
                            sku=sku,
                            target_custom_fields=target_fields,
                            preserve_current_category=True,
                        )
                        log_request = dict(body)
                        log_request["_write_guard"] = {
                            **guard_meta,
                            "snapshot_product_name": snapshot_name,
                            "name_source": "live_product_detail",
                            "preserve_non_target_custom_fields": True,
                        }
                        response, token = await request_with_retry(
                            client,
                            token,
                            PRODUCT_SET_API,
                            body,
                            max_retries=args.max_retries,
                        )
                        if clean(response.get("code")) == "0":
                            row["_expected_product_name"] = live_name
                            row["_target_fields"] = target_fields
                            written_rows.append(row)
                            log_row(db, batch_no, row, target_fields, "write_success", log_request, response)
                            write_counter["write_success"] += 1
                        else:
                            log_row(db, batch_no, row, target_fields, "failed", log_request, response, str(response))
                            write_counter["failed"] += 1
                    except Exception as exc:  # noqa: BLE001
                        log_row(db, batch_no, row, target_fields, "guard_blocked", None, None, str(exc))
                        write_counter["guard_blocked"] += 1

                    processed = start + batch_rows.index(row) + 1
                    if processed % 100 == 0 or processed == len(targets):
                        print(f"写入进度：{processed}/{len(targets)} {dict(write_counter)}")
                    if args.delay:
                        await asyncio.sleep(args.delay)

            print("写入完成：", dict(write_counter))
            verify_counter, token = await verify_written(
                db,
                client,
                token,
                batch_no,
                written_rows,
                args.max_retries,
                args.verify_batch_size,
            )
            print("复查完成：", dict(verify_counter))
            print("批次号：", batch_no)
        finally:
            await client.aclose()
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
