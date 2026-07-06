#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lx_product_m.db import Database, json_dumps
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.product_service import PRODUCT_DETAIL_API, PRODUCT_SET_API

MATCH_TABLE = "lxpm_feishu_style_category_match"
SNAPSHOT_TABLE = "lxpm_product_category_snapshot"
LOG_TABLE = "lxpm_product_custom_field_change_log"

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

FIELD_FORMATS = {
    "field_name_value": ("field_name", "field_value"),
    "name_value": ("name", "value"),
    "title_value": ("title", "value"),
    "field_value": ("field", "value"),
    "custom_field_name_value": ("custom_field_name", "custom_field_value"),
}

NAME_KEYS = ("field_name", "name", "title", "field", "custom_field_name")
VALUE_KEYS = ("field_value", "value", "text", "custom_field_value")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="从飞书分类匹配结果维护领星产品自定义字段：季节/品线/开发年份/品类")
    p.add_argument("--batch-no", default="", help="批次号，不传自动生成")
    p.add_argument("--custom-field-key", default="custom_fields", help="product/set中的自定义字段键名，默认 custom_fields")
    p.add_argument("--field-format", choices=sorted(FIELD_FORMATS.keys()), default="field_name_value", help="custom_fields数组内每个字段的结构")
    p.add_argument("--category-mode", choices=["path", "leaf"], default="path", help="品类写完整路径还是末级品类")
    p.add_argument("--statuses", nargs="+", default=["matched"], help="匹配表状态过滤，默认 matched")
    p.add_argument("--style-no", action="append", help="只处理指定款号，可重复传")
    p.add_argument("--sku-like", default="", help="只处理指定 SKU LIKE，例如 BDK003-%%")
    p.add_argument("--limit", type=int, default=0, help="限制处理数量")
    p.add_argument("--show", type=int, default=30, help="预览显示条数")
    p.add_argument("--delay", type=float, default=0.2, help="每个SKU写入后的等待秒数")
    p.add_argument("--max-retries", type=int, default=4, help="临时异常重试次数")
    p.add_argument("--no-merge-existing", action="store_true", help="不读取并合并现有 custom_fields，直接覆盖提交四个字段")
    p.add_argument("--confirm", action="store_true", help="确认写入；不加只预览")
    return p.parse_args()


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def category_value(path: str, mode: str) -> str:
    value = clean_text(path)
    if mode == "leaf" and value:
        for sep in ("/", ">", "｜", "|"):
            if sep in value:
                return value.split(sep)[-1].strip()
    return value


def build_field_entries(row: dict[str, Any], field_format: str, category_mode: str) -> list[dict[str, Any]]:
    name_key, value_key = FIELD_FORMATS[field_format]
    pairs = [
        ("季节", clean_text(row.get("season"))),
        ("品线", clean_text(row.get("used_product_line"))),
        ("开发年份", clean_text(row.get("year_group"))),
        ("品类", category_value(clean_text(row.get("target_category_path")), category_mode)),
    ]
    return [{name_key: name, value_key: value} for name, value in pairs if value]


def extract_field_name(item: dict[str, Any]) -> str:
    for key in NAME_KEYS:
        if clean_text(item.get(key)):
            return clean_text(item.get(key))
    return ""


def merge_fields(existing: list[Any], new_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = [dict(x) for x in existing if isinstance(x, dict)]
    index = {extract_field_name(item): i for i, item in enumerate(merged) if extract_field_name(item)}
    for field in new_fields:
        name = extract_field_name(field)
        if name and name in index:
            merged[index[name]].update(field)
        else:
            merged.append(field)
    return merged


def load_rows(db: Database, statuses: list[str], style_nos: list[str] | None, sku_like: str, limit: int) -> list[dict[str, Any]]:
    ph = ",".join(["%s"] * len(statuses))
    params: list[Any] = list(statuses)
    sql = f"""
        SELECT m.style_no, m.year_group, m.season, m.used_product_line, m.target_category_path,
               m.match_status, p.sku, p.product_name
        FROM `{MATCH_TABLE}` m
        JOIN `{SNAPSHOT_TABLE}` p ON p.spu = m.style_no
        WHERE m.match_status IN ({ph})
          AND p.sku IS NOT NULL AND p.sku <> ''
          AND p.product_name IS NOT NULL AND p.product_name <> ''
    """
    if style_nos:
        sph = ",".join(["%s"] * len(style_nos))
        sql += f" AND m.style_no IN ({sph})"
        params.extend(style_nos)
    if sku_like:
        sql += " AND p.sku LIKE %s"
        params.append(sku_like)
    sql += " ORDER BY m.style_no, p.sku"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    return db.fetch_all(sql, params)


def log_row(db: Database, batch_no: str, row: dict[str, Any], custom_field_key: str, fields: list[dict[str, Any]], status: str, req: dict[str, Any] | None, resp: dict[str, Any] | None, err: str = "") -> None:
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
            custom_field_key,
            json_dumps(fields),
            status,
            json_dumps(req),
            json_dumps(resp),
            err[:2000],
        ),
    )


def token_error(resp: dict[str, Any]) -> bool:
    text = str(resp.get("msg") or resp.get("message") or "").lower()
    return str(resp.get("code")) == "2001005" or "token" in text


def retryable(resp: dict[str, Any]) -> bool:
    code = str(resp.get("code"))
    text = str(resp.get("msg") or resp.get("message") or "").lower()
    return code in {"3001008", "500", "502", "503", "504"} or "too frequently" in text or "稍后再试" in text or "请求连接异常" in text


async def request_with_retry(client: LingxingClient, token: str, api_path: str, body: dict[str, Any], max_retries: int) -> tuple[dict[str, Any], str]:
    current_token = token
    last_resp: dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        resp = await client.request(current_token, api_path, "POST", req_body=body)
        last_resp = resp
        if str(resp.get("code")) == "0":
            return resp, current_token
        if token_error(resp):
            current_token = (await client.generate_token()).token
            continue
        if retryable(resp) and attempt < max_retries:
            wait_s = min(60, 5 * (attempt + 1))
            print(f"接口临时异常，{wait_s}s 后重试：{resp}")
            await asyncio.sleep(wait_s)
            continue
        return resp, current_token
    return last_resp, current_token


async def fetch_existing_custom_fields(client: LingxingClient, token: str, sku: str, custom_field_key: str, max_retries: int) -> tuple[list[Any], str]:
    resp, token = await request_with_retry(client, token, PRODUCT_DETAIL_API, {"skus": [sku]}, max_retries)
    if str(resp.get("code")) != "0":
        raise RuntimeError(f"查询产品详情失败：{resp}")
    rows = resp.get("data") or []
    if not rows:
        return [], token
    value = rows[0].get(custom_field_key)
    return (value if isinstance(value, list) else []), token


async def main() -> None:
    args = parse_args()
    batch_no = args.batch_no or "custom_fields_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    db = Database()
    db.execute(DDL)
    rows = load_rows(db, args.statuses, args.style_no, args.sku_like, args.limit)

    print("===== 产品自定义字段维护预览 =====")
    print("批次号：", batch_no)
    print("字段键：", args.custom_field_key)
    print("字段结构：", args.field_format)
    print("品类模式：", args.category_mode)
    print("合并现有字段：", not args.no_merge_existing)
    print("待处理：", len(rows))
    for r in rows[: args.show]:
        print(r["sku"], "->", build_field_entries(r, args.field_format, args.category_mode), r.get("product_name", "")[:50])
    if not args.confirm:
        print("预览模式，未写入。小批量确认后加 --confirm。")
        return

    client = LingxingClient(db=db, enable_api_log=True)
    token = (await client.generate_token()).token
    counter: Counter[str] = Counter()

    for i, r in enumerate(rows, 1):
        fields = build_field_entries(r, args.field_format, args.category_mode)
        if not fields:
            counter["skipped_empty"] += 1
            log_row(db, batch_no, r, args.custom_field_key, fields, "skipped_empty", None, None, "四个字段均为空")
            continue
        try:
            submit_fields = fields
            if not args.no_merge_existing:
                existing, token = await fetch_existing_custom_fields(client, token, r["sku"], args.custom_field_key, args.max_retries)
                submit_fields = merge_fields(existing, fields)
            body = {"sku": r["sku"], "product_name": r["product_name"], args.custom_field_key: submit_fields}
            resp, token = await request_with_retry(client, token, PRODUCT_SET_API, body, args.max_retries)
            if str(resp.get("code")) == "0":
                counter["success"] += 1
                log_row(db, batch_no, r, args.custom_field_key, fields, "success", body, resp)
            else:
                counter["failed"] += 1
                log_row(db, batch_no, r, args.custom_field_key, fields, "failed", body, resp, str(resp))
        except Exception as exc:  # noqa: BLE001
            counter["failed"] += 1
            log_row(db, batch_no, r, args.custom_field_key, fields, "failed", None, None, str(exc))
        if i % 50 == 0 or i == len(rows):
            print(f"进度：{i}/{len(rows)} {dict(counter)}")
        if args.delay:
            await asyncio.sleep(args.delay)

    print("完成：", dict(counter))


if __name__ == "__main__":
    asyncio.run(main())
