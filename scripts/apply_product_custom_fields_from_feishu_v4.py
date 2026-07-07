#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
from lx_product_m.services.product_service import PRODUCT_SET_API

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
    p = argparse.ArgumentParser(description="从飞书匹配表维护领星产品自定义字段，字段值按开款任务解析")
    p.add_argument("--batch-no", default="")
    p.add_argument("--style-no", action="append")
    p.add_argument("--sku-like", default="")
    p.add_argument("--statuses", nargs="+", default=["matched", "warning"])
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--show", type=int, default=20)
    p.add_argument("--delay", type=float, default=0.5)
    p.add_argument("--confirm", action="store_true")
    return p.parse_args()


def clean(v: Any) -> str:
    return str(v or "").strip()


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


def split_task(task: str) -> list[str]:
    return [x.strip() for x in clean(task).split("-") if x.strip()]


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
    return name or clean(row.get("target_category_path")).split("/")[-1].strip()


def values(row: dict[str, Any]) -> dict[str, str]:
    return {
        "季节": clean(row.get("season")),
        "品线": task_line(row),
        "开发年份": task_year(row),
        "品类": task_category(row),
    }


def custom_fields(row: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for name, val in values(row).items():
        if val:
            out.append({"id": FIELD_IDS[name], "name": name, "val": val})
    return out


def load_rows(db: Database, a: argparse.Namespace) -> list[dict[str, Any]]:
    ph = ",".join(["%s"] * len(a.statuses))
    params: list[Any] = list(a.statuses)
    sql = f"""
        SELECT m.style_no, m.task_text, m.year_group, m.season,
               m.product_line_from_task, m.used_product_line,
               m.target_category_path, m.target_category_name, m.match_status,
               p.sku, p.product_name
        FROM `{MATCH_TABLE}` m
        JOIN `{SNAPSHOT_TABLE}` p ON p.spu = m.style_no
        WHERE m.match_status IN ({ph})
          AND p.sku IS NOT NULL AND p.sku <> ''
          AND p.product_name IS NOT NULL AND p.product_name <> ''
    """
    if a.style_no:
        sph = ",".join(["%s"] * len(a.style_no))
        sql += f" AND m.style_no IN ({sph})"
        params.extend(a.style_no)
    if a.sku_like:
        sql += " AND p.sku LIKE %s"
        params.append(a.sku_like)
    sql += " ORDER BY m.style_no, p.sku"
    if a.limit:
        sql += " LIMIT %s"
        params.append(a.limit)
    return db.fetch_all(sql, params)


def log_row(db: Database, batch_no: str, row: dict[str, Any], fields: list[dict[str, str]], status: str, req: dict[str, Any] | None, resp: dict[str, Any] | None, err: str = "") -> None:
    db.execute(
        f"""
        INSERT INTO `{LOG_TABLE}`
        (`batch_no`,`sku`,`style_no`,`custom_field_key`,`fields_json`,`status`,`request_json`,`response_json`,`error_message`)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (batch_no, row.get("sku"), row.get("style_no") or "", "custom_fields", json_dumps(fields), status, json_dumps(req), json_dumps(resp), err[:2000]),
    )


def ok(resp: dict[str, Any]) -> bool:
    return str(resp.get("code")) == "0"


def expired(resp: dict[str, Any]) -> bool:
    s = str(resp.get("code")) + " " + str(resp.get("msg") or resp.get("message") or "")
    return "2001005" in s or "token" in s.lower()


async def post(client: LingxingClient, tk: str, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
    resp = await client.request(tk, PRODUCT_SET_API, "POST", req_body=body)
    if expired(resp):
        tk = (await client.generate_token()).token
        resp = await client.request(tk, PRODUCT_SET_API, "POST", req_body=body)
    return resp, tk


async def main() -> None:
    a = parse_args()
    batch_no = a.batch_no or "custom_fields_v4_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    db = Database()
    db.execute(DDL)
    rows = load_rows(db, a)
    print("===== 产品自定义字段维护 V4 =====")
    print("批次号：", batch_no)
    print("状态范围：", ",".join(a.statuses))
    print("待处理：", len(rows))
    for r in rows[: a.show]:
        print(r["sku"], "->", custom_fields(r), clean(r.get("task_text")))
    if not a.confirm:
        print("预览模式，未写入。")
        return

    client = LingxingClient(db=db, enable_api_log=True)
    tk = (await client.generate_token()).token
    counter: Counter[str] = Counter()
    for i, r in enumerate(rows, 1):
        fields = custom_fields(r)
        body = {"sku": r["sku"], "product_name": r["product_name"], "custom_fields": fields}
        try:
            resp, tk = await post(client, tk, body)
            if ok(resp):
                counter["success"] += 1
                log_row(db, batch_no, r, fields, "success", body, resp)
            else:
                counter["failed"] += 1
                log_row(db, batch_no, r, fields, "failed", body, resp, str(resp))
        except Exception as exc:
            counter["failed"] += 1
            log_row(db, batch_no, r, fields, "failed", body, None, str(exc))
        if i % 100 == 0 or i == len(rows):
            print(f"进度：{i}/{len(rows)} {dict(counter)}")
        if a.delay:
            await asyncio.sleep(a.delay)
    print("完成：", dict(counter))


if __name__ == "__main__":
    asyncio.run(main())
