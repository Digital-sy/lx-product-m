#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""颜色体系批量打标：第一轮只写 A2023，并对每批结果做写后复查。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services import product_write_guard
from lx_product_m.services.product_write_guard import (
    PRODUCT_SET_API,
    build_guarded_product_set_body,
    clean,
    extract_custom_fields,
    fetch_live_products,
    request_with_retry,
    target_fields_match,
)
from scripts.color_system_tagging_analysis import (
    DEFAULT_STORES,
    DEFAULT_YEARS,
    DETAIL_HEADERS,
    FIRST_ROUND_CUTOFF,
    OUTPUT_DIR,
    auto_width,
    binary_hex,
    classify_opening_date,
    color_system_sql,
    common_ctes,
    connect_analytics_db,
    create_dryrun_workbook,
    listing_store_filter,
    parse_stores,
    parse_years,
    prepare_analysis_rows,
    raw_order_union,
    resolve_tables,
    run_analysis_query,
    run_dry_run,
    store_filter,
    style_header,
    table_exists,
)

COLOR_FIELD_NAME = "颜色体系"
COLOR_FIELD_ID_ENV = "LX_COLOR_SYSTEM_FIELD_ID"
TARGET_VALUES = {"A2023", "待定"}
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
LOW_PEAK_START, LOW_PEAK_END = time(0, 0), time(6, 0)
RATE_LIMIT_RETRIES, RATE_LIMIT_WAIT_SECONDS = 3, 30.0
FAILURE_HEADERS = ["SKU", "拟打值", "失败阶段", "错误码", "错误信息", "request_id"]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="颜色体系批量打标（第一轮：A2023 存量判定）")
    parser.add_argument("--years", default=",".join(map(str, DEFAULT_YEARS)))
    parser.add_argument("--stores", default=",".join(DEFAULT_STORES), help="逗号分隔；ALL 表示全部店铺")
    parser.add_argument("--output", default="", help="dry-run Excel 输出路径")
    parser.add_argument("--apply", action="store_true", help="写入人工审阅后的清单")
    parser.add_argument("--review-file", default="", help="--apply 时必填")
    parser.add_argument("--field-id", default="", help=f"颜色体系字段 ID；默认读取 {COLOR_FIELD_ID_ENV}")
    parser.add_argument("--batch-size", type=int, default=100, help="写前/写后实时读取批次，最大100")
    parser.add_argument("--delay", type=float, default=0.5, help="每个 SKU 写入后的等待秒数")
    parser.add_argument("--verify-delay", type=float, default=1.0, help="每批成功后复查前等待秒数")
    parser.add_argument("--show", type=int, default=30)
    parser.add_argument("--allow-outside-low-peak", action="store_true")
    return parser.parse_args(argv)


def load_review_file(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"审阅清单不存在：{path}")
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if "拟打标明细" not in wb.sheetnames:
            raise ValueError("审阅清单缺少 sheet：拟打标明细")
        sheet = wb["拟打标明细"]
        headers = {clean(c.value): i for i, c in enumerate(next(sheet.iter_rows(min_row=1, max_row=1))) if clean(c.value)}
        missing = [x for x in ("SKU", "拟打值") if x not in headers]
        if missing:
            raise ValueError(f"审阅清单缺少列：{','.join(missing)}")
        targets, seen = [], set()
        for row_no, cells in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            sku = clean(cells[headers["SKU"]] if headers["SKU"] < len(cells) else "")
            value = clean(cells[headers["拟打值"]] if headers["拟打值"] < len(cells) else "")
            if not sku and not value:
                continue
            if not sku:
                raise ValueError(f"审阅清单第 {row_no} 行 SKU 为空")
            if value not in TARGET_VALUES:
                raise ValueError(f"审阅清单第 {row_no} 行拟打值非法：{value!r}；只允许 A2023/待定")
            if sku in seen:
                raise ValueError(f"审阅清单存在重复 SKU：{sku}")
            seen.add(sku)
            targets.append({"SKU": sku, "拟打值": value})
        return targets
    finally:
        wb.close()


def is_beijing_low_peak(now: datetime | None = None) -> bool:
    current = (now or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ).time()
    return LOW_PEAK_START <= current < LOW_PEAK_END


def validate_field_id(value: str) -> str:
    field_id = clean(value)
    if field_id and not field_id.isdigit():
        raise ValueError(f"颜色体系字段 ID 非法：{field_id!r}")
    return field_id


def field_ids_from_fields(fields: Any) -> set[str]:
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except json.JSONDecodeError:
            return set()
    if not isinstance(fields, list):
        return set()
    return {clean(x.get("id")) for x in fields if isinstance(x, dict) and clean(x.get("name")) == COLOR_FIELD_NAME and clean(x.get("id"))}


def one_field_id(field_ids: set[str], source: str) -> str:
    if len(field_ids) > 1:
        raise RuntimeError(f"{source} 中发现多个“{COLOR_FIELD_NAME}”字段 ID：{sorted(field_ids)}")
    return next(iter(field_ids), "")


def field_id_from_snapshot(db: Database) -> str:
    try:
        row = db.fetch_one(
            "SELECT custom_fields_json FROM lxpm_product_category_snapshot "
            "WHERE custom_fields_json LIKE %s ORDER BY synced_at DESC LIMIT 1",
            (f'%"name":"{COLOR_FIELD_NAME}"%',),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"警告：无法从产品快照发现颜色体系字段 ID：{exc}")
        return ""
    return one_field_id(field_ids_from_fields((row or {}).get("custom_fields_json")), "产品快照")


async def discover_field_id_from_live(client: LingxingClient, token: str, skus: Sequence[str]) -> tuple[str, str]:
    found, current = set(), token
    sample = list(skus[:500])
    for start in range(0, len(sample), 100):
        live_map, current = await fetch_live_products(
            client, current, sample[start:start + 100], max_retries=RATE_LIMIT_RETRIES,
            batch_size=100, delay_seconds=0.5,
            retry_base_seconds=RATE_LIMIT_WAIT_SECONDS, retry_max_seconds=RATE_LIMIT_WAIT_SECONDS,
        )
        for product in live_map.values():
            found.update(field_ids_from_fields(extract_custom_fields(product)))
        if found:
            break
    return one_field_id(found, "领星实时产品详情"), current


def failure_record(sku: str, value: str, stage: str, error: Any, response: dict[str, Any] | None = None) -> dict[str, str]:
    response = response or {}
    message = clean(response.get("msg") or response.get("message")) or clean(error)
    return {"SKU": sku, "拟打值": value, "失败阶段": stage, "错误码": clean(response.get("code")),
            "错误信息": message[:2000], "request_id": clean(response.get("request_id"))}


def response_succeeded(response: dict[str, Any] | None) -> bool:
    return str((response or {}).get("code")) == "0"


def create_failure_workbook(rows: Sequence[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    sheet = wb.active
    sheet.title = "失败清单"
    sheet.append(FAILURE_HEADERS)
    for row in rows:
        sheet.append([row.get(h, "") for h in FAILURE_HEADERS])
    style_header(sheet)
    sheet.freeze_panes, sheet.auto_filter.ref = "A2", sheet.dimensions
    auto_width(sheet, max_width=80)
    wb.save(output)


def verify_product_set_result(live_product: dict[str, Any] | None, expected_body: dict[str, Any]) -> tuple[bool, str]:
    if not live_product:
        return False, "写后实时详情未返回该 SKU"
    expected_sku = clean(expected_body.get("sku"))
    actual_sku = product_write_guard.extract_sku(live_product)
    if actual_sku != expected_sku:
        return False, f"写后 SKU 不一致：expected={expected_sku!r}, actual={actual_sku!r}"
    expected_name = clean(expected_body.get("product_name"))
    actual_name = product_write_guard.extract_product_name(live_product)
    if not actual_name:
        return False, "复查品名为空"
    if actual_name != expected_name:
        return False, f"品名被改变：before={expected_name!r}, after={actual_name!r}"
    if "category_id" in expected_body:
        actual_id, actual_name = product_write_guard.extract_category(live_product)
        if actual_id != int(expected_body["category_id"]):
            return False, f"分类 ID 被改变：expected={expected_body['category_id']}, actual={actual_id}"
        if actual_name != clean(expected_body.get("category")):
            return False, f"分类名称被改变：expected={clean(expected_body.get('category'))!r}, actual={actual_name!r}"
    expected_fields = expected_body.get("custom_fields") or []
    if expected_fields and not target_fields_match(extract_custom_fields(live_product), expected_fields):
        return False, "写后自定义字段与写入请求不一致（颜色体系或原有字段被改变/丢失）"
    return True, ""


def print_apply_progress(stats: Counter[str], total: int) -> None:
    done = stats["success"] + stats["failed"] + stats["skipped"]
    print(f"写入进度：{done:,}/{total:,} 成功={stats['success']:,} 失败={stats['failed']:,} 跳过={stats['skipped']:,}")


async def apply_review_file(args: argparse.Namespace, *, write_values: set[str] | None = None,
                            failure_prefix: str = "color_system_tagging_failures") -> int:
    if not args.review_file:
        raise ValueError("--apply 必须同时指定 --review-file，禁止直接生成后写入")
    if not args.allow_outside_low_peak and not is_beijing_low_peak():
        now = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        raise RuntimeError(f"当前北京时间 {now} 不在低峰窗口 00:00-06:00；请在凌晨执行，或人工确认后使用 --allow-outside-low-peak")
    review_path = Path(args.review_file).expanduser().resolve()
    review_targets = load_review_file(review_path)
    if write_values is not None:
        invalid = set(write_values) - TARGET_VALUES
        if invalid:
            raise ValueError(f"非法写入值过滤：{sorted(invalid)}")
        targets = [x for x in review_targets if x["拟打值"] in write_values]
    else:
        targets = review_targets
    preserved = len(review_targets) - len(targets)
    print("===== 颜色体系审阅清单写入 =====")
    print(f"审阅清单：{review_path}")
    print(f"处理 SKU：{len(review_targets):,}")
    print(f"计划写入：{len(targets):,}；按规则不写：{preserved:,}")
    print("安全策略：写前实时详情 + 完整字段合并 + product/set + 写后批量复查")
    print("code=103：固定等待 30 秒，最多重试 3 次")
    if not targets:
        print(f"执行统计：处理={len(review_targets):,} 成功=0 失败=0 跳过={preserved:,}")
        return 0

    db, failures = Database(), []
    client = LingxingClient(db=db, enable_api_log=True)
    stats: Counter[str] = Counter(processed=len(review_targets), skipped=preserved)
    try:
        token = (await client.generate_token()).token
        field_id = validate_field_id(args.field_id or os.getenv(COLOR_FIELD_ID_ENV, ""))
        if not field_id:
            field_id = field_id_from_snapshot(db)
        if not field_id:
            field_id, token = await discover_field_id_from_live(client, token, [x["SKU"] for x in targets])
        if not field_id:
            raise RuntimeError(f"无法发现“{COLOR_FIELD_NAME}”字段 ID；请通过 --field-id 或 {COLOR_FIELD_ID_ENV} 显式提供")
        print(f"颜色体系字段 ID：{field_id}")
        batch_size = max(1, min(int(args.batch_size), 100))
        verify_delay = max(0.0, float(getattr(args, "verify_delay", 1.0)))
        for start in range(0, len(targets), batch_size):
            batch = targets[start:start + batch_size]
            try:
                live_map, token = await fetch_live_products(
                    client, token, [x["SKU"] for x in batch], max_retries=RATE_LIMIT_RETRIES,
                    batch_size=batch_size, delay_seconds=0.5,
                    retry_base_seconds=RATE_LIMIT_WAIT_SECONDS, retry_max_seconds=RATE_LIMIT_WAIT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001
                for item in batch:
                    failures.append(failure_record(item["SKU"], item["拟打值"], "写前实时读取", exc))
                    stats["failed"] += 1
                print_apply_progress(stats, len(review_targets))
                continue

            pending_verify: list[tuple[str, str, dict[str, Any]]] = []
            for item in batch:
                sku, value = item["SKU"], item["拟打值"]
                target = {"id": field_id, "name": COLOR_FIELD_NAME, "val": value}
                try:
                    product = live_map.get(sku)
                    if not product:
                        raise RuntimeError("写前实时详情未返回该 SKU")
                    discovered = field_ids_from_fields(extract_custom_fields(product))
                    if discovered and discovered != {field_id}:
                        raise RuntimeError(f"实时详情字段 ID 与配置不一致：configured={field_id}, live={sorted(discovered)}")
                    if target_fields_match(extract_custom_fields(product), [target]):
                        stats["skipped"] += 1
                        continue
                    body, _ = build_guarded_product_set_body(product, sku=sku, target_custom_fields=[target], preserve_current_category=True)
                    response, token = await request_with_retry(
                        client, token, PRODUCT_SET_API, body, max_retries=RATE_LIMIT_RETRIES,
                        retry_base_seconds=RATE_LIMIT_WAIT_SECONDS, retry_max_seconds=RATE_LIMIT_WAIT_SECONDS,
                    )
                    if response_succeeded(response):
                        pending_verify.append((sku, value, body))
                    else:
                        failures.append(failure_record(sku, value, "product/set", response, response=response))
                        stats["failed"] += 1
                except Exception as exc:  # noqa: BLE001
                    failures.append(failure_record(sku, value, "安全合并写入", exc))
                    stats["failed"] += 1
                if args.delay > 0:
                    await asyncio.sleep(args.delay)

            if pending_verify:
                if verify_delay > 0:
                    await asyncio.sleep(verify_delay)
                try:
                    verify_map, token = await fetch_live_products(
                        client, token, [x[0] for x in pending_verify], max_retries=RATE_LIMIT_RETRIES,
                        batch_size=batch_size, delay_seconds=0.5,
                        retry_base_seconds=RATE_LIMIT_WAIT_SECONDS, retry_max_seconds=RATE_LIMIT_WAIT_SECONDS,
                    )
                except Exception as exc:  # noqa: BLE001
                    for sku, value, _ in pending_verify:
                        failures.append(failure_record(sku, value, "写后实时复查", exc))
                        stats["failed"] += 1
                else:
                    for sku, value, body in pending_verify:
                        ok, message = verify_product_set_result(verify_map.get(sku), body)
                        if ok:
                            stats["success"] += 1
                        else:
                            failures.append(failure_record(sku, value, "写后复查", message))
                            stats["failed"] += 1
            print_apply_progress(stats, len(review_targets))
    finally:
        await client.aclose()
        db.close()

    failure_path = None
    if failures:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        failure_path = OUTPUT_DIR / f"{failure_prefix}_{datetime.now(BEIJING_TZ):%Y%m%d_%H%M%S}.xlsx"
        create_failure_workbook(failures, failure_path)
    print(f"执行统计：处理={stats['processed']:,} 成功={stats['success']:,} 失败={stats['failed']:,} 跳过={stats['skipped']:,}")
    if failure_path:
        print(f"失败清单：{failure_path}")
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply:
        return asyncio.run(apply_review_file(args, write_values={"A2023"}))
    if args.review_file:
        raise ValueError("--review-file 只允许与 --apply 一起使用")
    return run_dry_run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"执行失败：{type(exc).__name__}: {exc}")
        raise
