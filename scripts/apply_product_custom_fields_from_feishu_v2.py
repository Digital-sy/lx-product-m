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
from lx_product_m.services.product_service import PRODUCT_DETAIL_API, PRODUCT_SET_API

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


def args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--batch-no", default="")
    p.add_argument("--style-no", action="append")
    p.add_argument("--sku-like", default="")
    p.add_argument("--statuses", nargs="+", default=["matched"])
    p.add_argument("--category-mode", choices=["leaf", "path"], default="leaf")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--show", type=int, default=20)
    p.add_argument("--delay", type=float, default=0.3)
    p.add_argument("--confirm", action="store_true")
    return p.parse_args()


def text(v: Any) -> str:
    return str(v or "").strip()


def leaf(path: str) -> str:
    value = text(path)
    for sep in ("/", ">", "｜", "|"):
        if sep in value:
            return value.split(sep)[-1].strip()
    return value


def field(name: str, val: str) -> dict[str, str]:
    return {"id": FIELD_IDS[name], "name": name, "val_text": val}


def fields(row: dict[str, Any], category_mode: str) -> list[dict[str, str]]:
    cat = text(row.get("target_category_path"))
    if category_mode == "leaf":
        cat = leaf(cat)
    pairs = [
        ("季节", text(row.get("season"))),
        ("品线", text(row.get("used_product_line"))),
        ("开发年份", text(row.get("year_group"))),
        ("品类", cat),
    ]
    return [field(k, v) for k, v in pairs if v]


def load_rows(db: Database, a: argparse.Namespace) -> list[dict[str, Any]]:
    ph = ",".join(["%s"] * len(a.statuses))
    params: list[Any] = list(a.statuses)
    sql = f"""
        SELECT m.style_no, m.year_group, m.season, m.used_product_line, m.target_category_path,
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


def log(db: Database, batch_no: str, row: dict[str, Any], flds: list[dict[str, str]], status: str, req: dict[str, Any] | None, resp: dict[str, Any] | None, err: str = "") -> None:
    db.execute(
        f"""
        INSERT INTO `{LOG_TABLE}`
        (`batch_no`,`sku`,`style_no`,`custom_field_key`,`fields_json`,`status`,`request_json`,`response_json`,`error_message`)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (batch_no, row.get("sku"), row.get("style_no") or "", "custom_fields", json_dumps(flds), status, json_dumps(req), json_dumps(resp), err[:2000]),
    )


def okay(resp: dict[str, Any]) -> bool:
    return str(resp.get("code")) == "0"


def need_new_auth(resp: dict[str, Any]) -> bool:
    s = str(resp.get("code")) + " " + str(resp.get("msg") or resp.get("message") or "")
    return "2001005" in s or "token" in s.lower()


async def post(client: LingxingClient, auth: str, api: str, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
    resp = await client.request(auth, api, "POST", req_body=body)
    if need_new_auth(resp):
        auth = (await client.generate_token()).token
        resp = await client.request(auth, api, "POST", req_body=body)
    return resp, auth


async def existing_fields(client: LingxingClient, auth: str, sku: str) -> tuple[list[Any], str]:
    resp, auth = await post(client, auth, PRODUCT_DETAIL_API, {"skus": [sku]})
    if not okay(resp):
        raise RuntimeError(f"查询产品详情失败：{resp}")
    rows = resp.get("data") or []
    if not rows:
        return [], auth
    value = rows[0].get("custom_fields")
    return (value if isinstance(value, list) else []), auth


def merge(old: list[Any], new: list[dict[str, str]]) -> list[dict[str, Any]]:
    out = [dict(x) for x in old if isinstance(x, dict)]
    idx = {str(x.get("id") or ""): i for i, x in enumerate(out) if x.get("id")}
    for item in new:
        if item["id"] in idx:
            out[idx[item["id"]]].update(item)
        else:
            out.append(item)
    return out


async def main() -> None:
    a = args()
    batch_no = a.batch_no or "custom_fields_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    db = Database()
    db.execute(DDL)
    rows = load_rows(db, a)
    print("===== 产品自定义字段维护 V2 =====")
    print("批次号：", batch_no)
    print("待处理：", len(rows))
    print("品类模式：", a.category_mode)
    for r in rows[: a.show]:
        print(r["sku"], "->", fields(r, a.category_mode), text(r.get("product_name"))[:50])
    if not a.confirm:
        print("预览模式，未写入。")
        return

    client = LingxingClient(db=db, enable_api_log=True)
    auth = (await client.generate_token()).token
    counter: Counter[str] = Counter()
    for i, r in enumerate(rows, 1):
        flds = fields(r, a.category_mode)
        try:
            old, auth = await existing_fields(client, auth, r["sku"])
            body = {"sku": r["sku"], "product_name": r["product_name"], "custom_fields": merge(old, flds)}
            resp, auth = await post(client, auth, PRODUCT_SET_API, body)
            if okay(resp):
                counter["success"] += 1
                log(db, batch_no, r, flds, "success", body, resp)
            else:
                counter["failed"] += 1
                log(db, batch_no, r, flds, "failed", body, resp, str(resp))
        except Exception as exc:
            counter["failed"] += 1
            log(db, batch_no, r, flds, "failed", None, None, str(exc))
        if i % 20 == 0 or i == len(rows):
            print(f"进度：{i}/{len(rows)} {dict(counter)}")
        if a.delay:
            await asyncio.sleep(a.delay)
    print("完成：", dict(counter))


if __name__ == "__main__":
    asyncio.run(main())
