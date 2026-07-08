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

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lx_product_m.db import Database, json_dumps
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.product_service import PRODUCT_DETAIL_API, PRODUCT_SET_API, ProductService

SNAPSHOT_TABLE = "lxpm_product_category_snapshot"
MATCH_TABLE = "lxpm_feishu_style_category_match"
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
    p = argparse.ArgumentParser(description="按领星现有分类路径兜底补产品自定义字段，用于飞书款号匹配表未覆盖的SKU")
    p.add_argument("--batch-no", default="")
    p.add_argument("--sku-like", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--show", type=int, default=50)
    p.add_argument("--delay", type=float, default=0.5)
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--force", action="store_true")
    p.add_argument("--confirm", action="store_true")
    return p.parse_args()


def clean(v: Any) -> str:
    return str(v or "").strip()


def parse_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(str(v))
    except Exception:
        return None


def strip_line(v: str) -> str:
    s = clean(v)
    for prefix in ("S-", "s-", "S_", "s_", "S ", "s "):
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    return s


def full_year(v: str) -> str:
    s = clean(v)
    if s.isdigit() and len(s) == 2:
        n = int(s)
        return str(2000 + n if n < 80 else 1900 + n)
    return s


def normalize_fields(fields: Any) -> list[dict[str, str]]:
    arr = parse_json(fields)
    if not isinstance(arr, list):
        return []
    out: list[dict[str, str]] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        fid = clean(item.get("id"))
        name = clean(item.get("name"))
        val = clean(item.get("val") or item.get("val_text") or item.get("value") or item.get("field_value"))
        if fid and name and val:
            out.append({"id": fid, "name": name, "val": val})
    return out


def field_key(f: dict[str, str]) -> tuple[str, str]:
    return clean(f.get("id")), clean(f.get("name"))


def only_target_fields(fields: list[dict[str, str]]) -> list[dict[str, str]]:
    ids = set(FIELD_IDS.values())
    names = set(FIELD_IDS.keys())
    return sorted([f for f in fields if f.get("id") in ids or f.get("name") in names], key=lambda x: (x["id"], x["name"]))


def merge_fields(existing: list[dict[str, str]], target: list[dict[str, str]]) -> list[dict[str, str]]:
    target_ids = {f["id"] for f in target}
    target_names = {f["name"] for f in target}
    out = [f for f in existing if f.get("id") not in target_ids and f.get("name") not in target_names]
    out.extend(target)
    return out


def expected_from_path(row: dict[str, Any]) -> list[dict[str, str]]:
    path = clean(row.get("category_path"))
    name = clean(row.get("category_name"))
    parts = [x.strip() for x in path.split("/") if x.strip()]
    if len(parts) < 3:
        return []
    year = full_year(parts[0])
    season = parts[1]
    product_line = strip_line(parts[2])
    category = strip_line(parts[-1] if len(parts) >= 4 else name or parts[2])
    values = {
        "季节": season,
        "品线": product_line,
        "开发年份": year,
        "品类": category,
    }
    return [{"id": FIELD_IDS[k], "name": k, "val": v} for k, v in values.items() if v]


def load_rows(db: Database, a: argparse.Namespace) -> list[dict[str, Any]]:
    params: list[Any] = []
    sql = f"""
        SELECT p.sku, p.spu, p.product_name, p.category_id, p.category_name,
               p.category_path, p.custom_fields_json
        FROM `{SNAPSHOT_TABLE}` p
        LEFT JOIN `{MATCH_TABLE}` m ON m.style_no = p.spu
        WHERE p.sku IS NOT NULL AND p.sku <> ''
          AND p.product_name IS NOT NULL AND p.product_name <> ''
          AND m.style_no IS NULL
          AND p.category_id IS NOT NULL AND p.category_id > 0
          AND p.category_path IS NOT NULL AND p.category_path <> ''
    """
    if a.sku_like:
        sql += " AND p.sku LIKE %s"
        params.append(a.sku_like)
    sql += " ORDER BY p.category_path, p.sku"
    if a.limit:
        sql += " LIMIT %s"
        params.append(a.limit)
    return db.fetch_all(sql, params)


def select_changed(rows: list[dict[str, Any]], force: bool) -> tuple[list[dict[str, Any]], Counter]:
    stat: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []
    for r in rows:
        target = expected_from_path(r)
        if not target:
            stat["skip_unparseable_path"] += 1
            continue
        existing = normalize_fields(r.get("custom_fields_json"))
        existing_target = only_target_fields(existing)
        merged = merge_fields(existing, target)
        r["_target_fields"] = target
        r["_merged_fields"] = merged
        if force:
            r["_reason"] = "force"
            selected.append(r)
            stat["force"] += 1
        elif existing_target != only_target_fields(target):
            r["_reason"] = "new_or_changed"
            selected.append(r)
            stat["new_or_changed"] += 1
        else:
            stat["skipped_unchanged"] += 1
    return selected, stat


def is_token_error(resp: dict[str, Any]) -> bool:
    return str(resp.get("code")) == "2001005" or "token" in str(resp.get("msg") or resp.get("message") or "").lower()


def is_retryable(resp: dict[str, Any]) -> bool:
    msg = str(resp.get("msg") or resp.get("message") or "")
    return str(resp.get("code")) in {"500", "502", "503", "504"} or "请求连接异常" in msg or "稍后再试" in msg


async def request_with_retry(client: LingxingClient, token: str, body: dict[str, Any], max_retries: int) -> tuple[dict[str, Any], str]:
    current = token
    last: dict[str, Any] = {}
    for i in range(max_retries + 1):
        resp = await client.request(current, PRODUCT_SET_API, "POST", req_body=body)
        last = resp
        if str(resp.get("code")) == "0":
            return resp, current
        if is_token_error(resp):
            current = (await client.generate_token()).token
            await asyncio.sleep(0.5)
            continue
        if is_retryable(resp) and i < max_retries:
            await asyncio.sleep(min(10, 1 + i * 2))
            continue
        return resp, current
    return last, current


def log_row(db: Database, batch_no: str, row: dict[str, Any], fields: list[dict[str, str]], status: str, req: dict[str, Any] | None, resp: dict[str, Any] | None, err: str = "") -> None:
    db.execute(
        f"""
        INSERT INTO `{LOG_TABLE}`
        (`batch_no`,`sku`,`style_no`,`custom_field_key`,`fields_json`,`status`,`request_json`,`response_json`,`error_message`)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (batch_no, row.get("sku"), row.get("spu") or "", "custom_fields", json_dumps(fields), status, json_dumps(req), json_dumps(resp), err[:2000]),
    )


async def verify(db: Database, client: LingxingClient, token: str, batch_no: str, rows: list[dict[str, Any]], batch_size: int = 100) -> Counter:
    service = ProductService(client, db)
    counter: Counter[str] = Counter()
    by_sku = {clean(r.get("sku")): r for r in rows}
    skus = list(by_sku.keys())
    current = token
    for i in range(0, len(skus), batch_size):
        batch = skus[i:i + batch_size]
        resp = await client.request(current, PRODUCT_DETAIL_API, "POST", req_body={"skus": batch})
        if is_token_error(resp):
            current = (await client.generate_token()).token
            resp = await client.request(current, PRODUCT_DETAIL_API, "POST", req_body={"skus": batch})
        if str(resp.get("code")) != "0":
            for sku in batch:
                row = by_sku[sku]
                log_row(db, batch_no, row, row["_target_fields"], "verify_failed", None, resp, str(resp))
                counter["verify_failed"] += 1
            continue
        products = resp.get("data") or []
        returned = {clean(p.get("sku") or p.get("SKU")): p for p in products}
        for sku in batch:
            row = by_sku[sku]
            product = returned.get(sku)
            if product:
                service.save_product_snapshot(product)
            actual = only_target_fields(normalize_fields(service.extract_custom_fields(product or {})))
            expected = only_target_fields(row["_target_fields"])
            ok = actual == expected
            status = "success" if ok else "verify_failed"
            err = "" if ok else f"复查自定义字段不一致：actual={actual}, expected={expected}"
            log_row(db, batch_no, row, row["_target_fields"], status, None, {"data": [product] if product else []}, err)
            counter[status] += 1
        print(f"复查进度：{min(i + batch_size, len(skus))}/{len(skus)}，{dict(counter)}")
    return counter


async def main() -> None:
    a = parse_args()
    batch_no = a.batch_no or "custom_fields_category_path_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    db = Database()
    db.execute(DDL)
    rows = load_rows(db, a)
    targets, stat = select_changed(rows, a.force)
    print("===== 分类路径兜底补自定义字段 =====")
    print("批次号：", batch_no)
    print("分类路径候选SKU：", len(rows))
    print("本次需写入：", len(targets))
    print("筛选统计：", dict(stat))
    for r in targets[: a.show]:
        print(r["sku"], r.get("category_path"), "->", r["_target_fields"], r["_reason"])
    if not a.confirm:
        print("预览模式，未写入。")
        return

    client = LingxingClient(db=db, enable_api_log=True)
    token = (await client.generate_token()).token
    write_counter: Counter[str] = Counter()
    written_rows: list[dict[str, Any]] = []
    for i, r in enumerate(targets, 1):
        body = {
            "sku": r["sku"],
            "product_name": r["product_name"],
            "category_id": int(r.get("category_id") or 0),
            "category": clean(r.get("category_name")),
            "custom_fields": r["_merged_fields"],
        }
        resp, token = await request_with_retry(client, token, body, a.max_retries)
        if str(resp.get("code")) == "0":
            log_row(db, batch_no, r, r["_target_fields"], "write_success", body, resp)
            write_counter["write_success"] += 1
            written_rows.append(r)
        else:
            log_row(db, batch_no, r, r["_target_fields"], "failed", body, resp, str(resp))
            write_counter["failed"] += 1
        if i % 100 == 0 or i == len(targets):
            print(f"写入进度：{i}/{len(targets)}，{dict(write_counter)}")
        if a.delay:
            await asyncio.sleep(a.delay)
    print("写入完成：", dict(write_counter))
    verify_counter = await verify(db, client, token, batch_no, written_rows)
    print("复查完成：", dict(verify_counter))
    print("批次号：", batch_no)


if __name__ == "__main__":
    asyncio.run(main())
