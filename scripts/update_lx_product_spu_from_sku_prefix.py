#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把领星产品管理里的 SPU 字段写成 SKU 前缀。

规则：
  BQ084-BK-L   -> BQ084
  ZSY961-SC-XS -> ZSY961

领星文档字段：
  api_spu            SPU
  api_spu_attribute  属性列表；填写 api_spu 时文档要求该字段同时传入

重要：
  product/set 的 model 字段是前台「型号」，不是 SPU，默认禁止写 model。
  正式全量前必须先单个 SKU 测试并到前台确认 SPU 字段确实变化。
"""
from __future__ import annotations

import argparse
import asyncio
import json
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
from lx_product_m.sku import extract_spu

SNAPSHOT_TABLE = "lxpm_product_category_snapshot"
TASK_TABLE = "lxpm_product_spu_write_task"
CHANGE_LOG_TABLE = "lxpm_product_spu_change_log"

CANDIDATE_KEYS = (
    "api_spu",
    "apiSpu",
    "spu",
    "SPU",
    "product_spu",
    "productSpu",
    "productSPU",
    "parent_sku",
    "parentSku",
    "parent_sku_code",
    "parentSkuCode",
    "sku_identifier",
    "skuIdentifier",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把领星产品管理 SPU 字段写成 SKU 前缀")
    parser.add_argument("--batch-no", default="", help="批次号；不传则自动生成")
    parser.add_argument("--sku", action="append", help="只处理指定 SKU，可重复传")
    parser.add_argument("--sku-like", default="", help="只处理匹配前缀/模糊的SKU，例如 ZSY961%%")
    parser.add_argument("--limit", type=int, default=0, help="最多写入多少个SKU，默认0表示不限制")
    parser.add_argument("--show", type=int, default=100, help="预览输出行数，默认100")
    parser.add_argument("--delay", type=float, default=0.1, help="每个写入请求后的等待秒数，默认0.1")
    parser.add_argument("--max-retries", type=int, default=5, help="接口失败最大重试次数，默认5")
    parser.add_argument("--verify-batch-size", type=int, default=100, help="批量复查每批SKU数，默认100")
    parser.add_argument("--field-name", default="api_spu", help="写入字段名，默认 api_spu。model 是型号，默认禁止。")
    parser.add_argument(
        "--api-spu-attribute-json",
        default="[]",
        help="api_spu_attribute 的JSON数组。文档要求填写 api_spu 时同时填写属性列表；默认 [] 仅用于先验证接口返回。",
    )
    parser.add_argument("--allow-model", action="store_true", help="允许写入 model 字段。谨慎：model 已验证是前台型号，不是SPU。")
    parser.add_argument("--only-empty", action="store_true", help="仅当当前字段为空时写入；默认当前值不同也写入")
    parser.add_argument("--force", action="store_true", help="即使当前字段已等于SKU前缀，也强制写入")
    parser.add_argument("--skip-verify", action="store_true", help="跳过批量复查；不建议正式使用")
    parser.add_argument("--confirm", action="store_true", help="确认写入领星；不加只预览")
    return parser.parse_args()


def make_batch_no() -> str:
    return "spu_write_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_api_spu_attribute_json(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"--api-spu-attribute-json 不是合法JSON：{exc}") from exc
    if not isinstance(parsed, list):
        raise SystemExit("--api-spu-attribute-json 必须是JSON数组，例如 '[{\"pa_id\":1,\"pai_id\":101}]'")
    for item in parsed:
        if not isinstance(item, dict):
            raise SystemExit("--api-spu-attribute-json 数组元素必须是对象")
    return parsed


def ensure_tables(db: Database) -> None:
    with db.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{TASK_TABLE}` (
              `id` BIGINT NOT NULL AUTO_INCREMENT,
              `batch_no` VARCHAR(100) NOT NULL COMMENT '批次号',
              `sku` VARCHAR(200) NOT NULL COMMENT 'SKU',
              `product_name` VARCHAR(500) DEFAULT '' COMMENT '品名',
              `old_spu` VARCHAR(200) DEFAULT '' COMMENT '写入前领星返回字段值',
              `target_spu` VARCHAR(200) NOT NULL COMMENT '目标SPU，来自SKU前缀',
              `field_name` VARCHAR(100) NOT NULL DEFAULT 'api_spu' COMMENT '写入product/set的字段名',
              `status` VARCHAR(50) NOT NULL DEFAULT 'pending' COMMENT 'pending/running/write_success/success/failed/verify_failed/skipped',
              `error_message` TEXT NULL COMMENT '失败原因',
              `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (`id`),
              KEY `idx_batch_no` (`batch_no`),
              KEY `idx_sku` (`sku`),
              KEY `idx_target_spu` (`target_spu`),
              KEY `idx_status` (`status`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='lx-product-m：产品SPU字段写入任务表'
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{CHANGE_LOG_TABLE}` (
              `id` BIGINT NOT NULL AUTO_INCREMENT,
              `batch_no` VARCHAR(100) NOT NULL COMMENT '批次号',
              `task_id` BIGINT DEFAULT NULL COMMENT '任务ID',
              `sku` VARCHAR(200) NOT NULL COMMENT 'SKU',
              `product_name` VARCHAR(500) DEFAULT '' COMMENT '品名',
              `old_spu` VARCHAR(200) DEFAULT '' COMMENT '写入前字段值',
              `new_spu` VARCHAR(200) DEFAULT '' COMMENT '目标SPU',
              `verify_spu` VARCHAR(200) DEFAULT '' COMMENT '复查字段值',
              `field_name` VARCHAR(100) NOT NULL DEFAULT 'api_spu' COMMENT '写入product/set的字段名',
              `status` VARCHAR(50) NOT NULL COMMENT 'write_success/success/failed/verify_failed/skipped',
              `request_json` JSON NULL COMMENT '写入请求体',
              `response_json` JSON NULL COMMENT '写入响应体',
              `verify_response_json` JSON NULL COMMENT '复查响应体',
              `error_message` TEXT NULL COMMENT '错误信息',
              `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (`id`),
              KEY `idx_batch_no` (`batch_no`),
              KEY `idx_task_id` (`task_id`),
              KEY `idx_sku` (`sku`),
              KEY `idx_status` (`status`),
              KEY `idx_created_at` (`created_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='lx-product-m：产品SPU字段写入日志'
            """
        )


def parse_json_maybe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:  # noqa: BLE001
        return None


def value_to_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


def extract_current_value(product: dict[str, Any] | None, field_name: str = "api_spu") -> str:
    if not isinstance(product, dict):
        return ""
    if field_name:
        value = value_to_text(product.get(field_name))
        if value:
            return value
        if field_name == "api_spu":
            value = value_to_text(product.get("apiSpu"))
            if value:
                return value
    for key in CANDIDATE_KEYS:
        val = value_to_text(product.get(key))
        if val:
            return val
    custom_fields = product.get("custom_fields") or product.get("custom_field_list") or product.get("customFields") or product.get("customFieldList")
    if isinstance(custom_fields, list):
        for item in custom_fields:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("field_name") or item.get("fieldName") or item.get("title") or "").strip().lower()
            if name in {"spu", "父sku", "父sku编码", "款号", "api_spu"}:
                return value_to_text(item.get("value") or item.get("field_value") or item.get("fieldValue") or item.get("text"))
    return ""


def should_write(old_value: str, target_spu: str, force: bool, only_empty: bool) -> bool:
    if not target_spu:
        return False
    if force:
        return True
    if only_empty:
        return not old_value
    return old_value != target_spu


def load_candidate_rows(db: Database, skus: list[str] | None, sku_like: str, force: bool, only_empty: bool, limit: int, field_name: str) -> list[dict[str, Any]]:
    sql = f"""
        SELECT sku, product_name, raw_json
        FROM `{SNAPSHOT_TABLE}`
        WHERE sku IS NOT NULL
          AND sku <> ''
          AND product_name IS NOT NULL
          AND product_name <> ''
    """
    params: list[Any] = []
    if skus:
        ph = ",".join(["%s"] * len(skus))
        sql += f" AND sku IN ({ph})"
        params.extend(skus)
    if sku_like:
        sql += " AND sku LIKE %s"
        params.append(sku_like)
    sql += " ORDER BY sku"

    rows = db.fetch_all(sql, params)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        sku = str(row.get("sku") or "").strip()
        target_spu = extract_spu(sku)
        raw_json = parse_json_maybe(row.get("raw_json"))
        old_value = extract_current_value(raw_json if isinstance(raw_json, dict) else None, field_name=field_name)
        if not should_write(old_value, target_spu, force=force, only_empty=only_empty):
            continue
        item = dict(row)
        item["old_spu"] = old_value
        item["target_spu"] = target_spu
        candidates.append(item)
        if limit and len(candidates) >= limit:
            break
    return candidates


def print_preview(rows: list[dict[str, Any]], show: int, batch_no: str, field_name: str, api_spu_attribute: list[dict[str, Any]]) -> None:
    print("===== 预览：领星产品 SPU 字段写入 =====")
    print(f"批次号：{batch_no}")
    print(f"写入字段名：{field_name}")
    if field_name == "api_spu":
        print(f"api_spu_attribute：{json.dumps(api_spu_attribute, ensure_ascii=False)}")
    print(f"待写入SKU数：{len(rows)}")
    print()
    headers = ["SKU", "当前字段值", "目标SPU", "产品名"]
    widths = [32, 20, 20, 48]
    print(" ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-" * 130)
    for r in rows[:show]:
        values = [r.get("sku") or "", r.get("old_spu") or "", r.get("target_spu") or "", r.get("product_name") or ""]
        print(" ".join(str(v)[:w].ljust(w) for v, w in zip(values, widths)))
    if len(rows) > show:
        print(f"... 仅显示前{show}条，共{len(rows)}条")


def insert_tasks(db: Database, batch_no: str, rows: list[dict[str, Any]], field_name: str) -> list[dict[str, Any]]:
    sql = f"""
        INSERT INTO `{TASK_TABLE}`
        (`batch_no`, `sku`, `product_name`, `old_spu`, `target_spu`, `field_name`, `status`)
        VALUES (%s,%s,%s,%s,%s,%s,'pending')
    """
    out: list[dict[str, Any]] = []
    with db.cursor() as cur:
        for r in rows:
            cur.execute(sql, (batch_no, r.get("sku"), r.get("product_name") or "", r.get("old_spu") or "", r.get("target_spu") or "", field_name))
            item = dict(r)
            item["task_id"] = cur.lastrowid
            item["field_name"] = field_name
            out.append(item)
    return out


def update_task(db: Database, task_id: int, status: str, error_message: str = "") -> None:
    db.execute(f"UPDATE `{TASK_TABLE}` SET status=%s, error_message=%s WHERE id=%s", (status, error_message[:5000], task_id))


def write_change_log(db: Database, batch_no: str, task: dict[str, Any], status: str, request_json: dict[str, Any] | None, response_json: dict[str, Any] | None, verify_json: dict[str, Any] | None = None, error_message: str = "", verify_spu: str = "") -> None:
    db.execute(
        f"""
        INSERT INTO `{CHANGE_LOG_TABLE}`
        (`batch_no`, `task_id`, `sku`, `product_name`, `old_spu`, `new_spu`, `verify_spu`,
         `field_name`, `status`, `request_json`, `response_json`, `verify_response_json`, `error_message`)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (batch_no, task.get("task_id"), task.get("sku"), task.get("product_name") or "", task.get("old_spu") or "", task.get("target_spu") or "", verify_spu or "", task.get("field_name") or "api_spu", status, json_dumps(request_json), json_dumps(response_json), json_dumps(verify_json), error_message[:5000]),
    )


def is_token_error(resp: dict[str, Any] | None) -> bool:
    if not resp:
        return False
    return str(resp.get("code")) == "2001005" or "token" in str(resp.get("msg") or resp.get("message") or "").lower()


def is_retryable_error(resp: dict[str, Any] | None) -> bool:
    if not resp:
        return False
    msg = str(resp.get("msg") or resp.get("message") or "")
    return str(resp.get("code")) in {"500", "502", "503", "504"} or "请求连接异常" in msg or "稍后再试" in msg


async def request_with_retry(client: LingxingClient, token: str, api_path: str, body: dict[str, Any], max_retries: int) -> tuple[dict[str, Any], str]:
    current_token = token
    last_resp: dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        resp = await client.request(current_token, api_path, "POST", req_body=body)
        last_resp = resp
        if str(resp.get("code")) == "0":
            return resp, current_token
        if is_token_error(resp):
            print("检测到 access token 失效，重新获取 token 后重试当前请求...")
            current_token = (await client.generate_token()).token
            await asyncio.sleep(0.5)
            continue
        if is_retryable_error(resp) and attempt < max_retries:
            wait_s = min(10, 1 + attempt * 2)
            print(f"接口临时异常，{wait_s}s后重试：{resp}")
            await asyncio.sleep(wait_s)
            continue
        return resp, current_token
    return last_resp, current_token


def build_request_body(task: dict[str, Any], api_spu_attribute: list[dict[str, Any]]) -> dict[str, Any]:
    field_name = str(task.get("field_name") or "api_spu")
    body = {
        "sku": str(task.get("sku") or ""),
        "product_name": str(task.get("product_name") or ""),
    }
    if field_name == "api_spu":
        body["api_spu"] = str(task.get("target_spu") or "")
        body["api_spu_attribute"] = api_spu_attribute
    else:
        body[field_name] = str(task.get("target_spu") or "")
    return body


async def write_products(db: Database, tasks: list[dict[str, Any]], batch_no: str, delay: float, max_retries: int, api_spu_attribute: list[dict[str, Any]]) -> Counter:
    client = LingxingClient(db=db, enable_api_log=True)
    token = (await client.generate_token()).token
    counter: Counter = Counter()
    total = len(tasks)
    for idx, task in enumerate(tasks, 1):
        task_id = int(task["task_id"])
        body = build_request_body(task, api_spu_attribute)
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
    return db.fetch_all(f"SELECT id AS task_id, sku, product_name, old_spu, target_spu, field_name FROM `{TASK_TABLE}` WHERE batch_no=%s AND status='write_success' ORDER BY id", (batch_no,))


async def verify_batch_with_retry(client: LingxingClient, token: str, batch: list[str], max_retries: int) -> tuple[list[dict[str, Any]], str]:
    current_token = token
    body = {"skus": batch}
    for attempt in range(max_retries + 1):
        resp = await client.request(current_token, PRODUCT_DETAIL_API, "POST", req_body=body)
        if str(resp.get("code")) == "0":
            return list(resp.get("data") or []), current_token
        if is_token_error(resp):
            print("复查时 token 失效，重新获取 token 后重试...")
            current_token = (await client.generate_token()).token
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
    token = (await client.generate_token()).token
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
            target_spu = str(task.get("target_spu") or "")
            field_name = str(task.get("field_name") or "api_spu")
            if not product:
                update_task(db, int(task["task_id"]), "verify_failed", "复查未返回SKU")
                write_change_log(db, batch_no, task, "verify_failed", None, None, {"data": products}, "复查未返回SKU")
                counter["verify_failed"] += 1
                continue
            service.save_product_snapshot(product)
            verify_value = extract_current_value(product, field_name=field_name)
            ok = verify_value == target_spu
            status = "success" if ok else "verify_failed"
            err = "" if ok else f"复查字段不一致：field_name={field_name!r}, verify_value={verify_value!r}, target_spu={target_spu!r}"
            update_task(db, int(task["task_id"]), status, err)
            write_change_log(db, batch_no, task, status, None, None, {"data": [product]}, err, verify_spu=verify_value)
            counter[status] += 1
        print(f"复查进度：{min(i + batch_size, len(skus))}/{len(skus)}，{dict(counter)}")
    return counter


async def main() -> None:
    args = parse_args()
    if args.field_name == "model" and not args.allow_model:
        raise SystemExit("错误：model 已验证会写入前台『型号』，不是 SPU。禁止继续；如确实要写型号，需额外加 --allow-model。")
    api_spu_attribute = parse_api_spu_attribute_json(args.api_spu_attribute_json)
    batch_no = args.batch_no or make_batch_no()
    db = Database()
    rows = load_candidate_rows(db, args.sku, args.sku_like, args.force, args.only_empty, args.limit, args.field_name)
    print_preview(rows, args.show, batch_no, args.field_name, api_spu_attribute)

    if not rows:
        print("没有需要写入的SKU。")
        return
    if not args.confirm:
        print("\n当前为预览模式，未创建任务、未写领星。确认无误后加 --confirm 执行。")
        return
    if args.field_name == "api_spu" and not api_spu_attribute:
        print("警告：文档说明填写 api_spu 时 api_spu_attribute 必填；当前传入 []，如果接口失败，需要先获取属性ID和值ID。")

    ensure_tables(db)
    tasks = insert_tasks(db, batch_no, rows, args.field_name)
    print(f"\n已创建任务：{len(tasks)} 条，开始写入领星产品字段 {args.field_name}...")
    write_counter = await write_products(db, tasks, batch_no, args.delay, args.max_retries, api_spu_attribute)
    print(f"写入完成：{dict(write_counter)}")
    if args.skip_verify:
        print("已跳过复查。")
        print(f"批次号：{batch_no}")
        return
    print("开始批量复查...")
    verify_counter = await verify_tasks(db, batch_no, args.verify_batch_size, args.max_retries)
    print("\n===== SPU字段写入完成 =====")
    print(f"写入：{dict(write_counter)}")
    print(f"复查：{dict(verify_counter)}")
    print(f"批次号：{batch_no}")


if __name__ == "__main__":
    asyncio.run(main())
