#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import asyncio
import json
import re
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
from lx_product_m.services.product_service import PRODUCT_SET_API

FIELD_RE = re.compile(r"^[A-Za-z0-9_]+$")
MATCH_TABLE = "lxpm_feishu_style_category_match"
SNAPSHOT_TABLE = "lxpm_product_category_snapshot"
LOG_TABLE = "lxpm_product_custom_label_change_log"
DDL = """
CREATE TABLE IF NOT EXISTS `lxpm_product_custom_label_change_log` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `batch_no` VARCHAR(64) NOT NULL,
  `sku` VARCHAR(128) NOT NULL,
  `style_no` VARCHAR(128) NOT NULL DEFAULT '',
  `label_field` VARCHAR(128) NOT NULL DEFAULT '',
  `labels_json` JSON NULL,
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
    p = argparse.ArgumentParser(description="从飞书分类匹配结果维护领星产品自定义标签")
    p.add_argument("--batch-no", default="")
    p.add_argument("--label-field", required=True, help="product/set里自定义标签字段名，先用probe脚本确认")
    p.add_argument("--mode", choices=["segments", "path", "line"], default="segments")
    p.add_argument("--payload-type", choices=["list", "comma", "json_string"], default="list")
    p.add_argument("--statuses", nargs="+", default=["matched"])
    p.add_argument("--style-no", action="append")
    p.add_argument("--extra-label", action="append", default=[])
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--show", type=int, default=50)
    p.add_argument("--delay", type=float, default=0.05)
    p.add_argument("--confirm", action="store_true")
    return p.parse_args()


def token_error(resp: dict[str, Any]) -> bool:
    text = str(resp.get("msg") or resp.get("message") or "").lower()
    return str(resp.get("code")) == "2001005" or "token" in text


def labels_for(row: dict[str, Any], mode: str, extra: list[str]) -> list[str]:
    year = str(row.get("year_group") or "").strip()
    season = str(row.get("season") or "").strip()
    line = str(row.get("used_product_line") or "").strip()
    path = str(row.get("target_category_path") or "").strip()
    if mode == "path":
        labels = [path] if path else []
    elif mode == "line":
        labels = [line] if line else []
    else:
        labels = [x for x in [year, season, line] if x]
    labels += [x.strip() for x in extra if x and x.strip()]
    out = []
    seen = set()
    for x in labels:
        if x and x not in seen:
            out.append(x)
            seen.add(x)
    return out


def payload(labels: list[str], payload_type: str) -> Any:
    if payload_type == "comma":
        return ",".join(labels)
    if payload_type == "json_string":
        return json.dumps(labels, ensure_ascii=False)
    return labels


def load_rows(db: Database, statuses: list[str], style_nos: list[str] | None, limit: int) -> list[dict[str, Any]]:
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
    sql += " ORDER BY m.style_no, p.sku"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    return db.fetch_all(sql, params)


def log(db: Database, batch_no: str, row: dict[str, Any], label_field: str, labels: list[str], status: str, req: dict[str, Any] | None, resp: dict[str, Any] | None, err: str = "") -> None:
    db.execute(
        """
        INSERT INTO `lxpm_product_custom_label_change_log`
        (`batch_no`,`sku`,`style_no`,`label_field`,`labels_json`,`status`,`request_json`,`response_json`,`error_message`)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (batch_no, row.get("sku"), row.get("style_no") or "", label_field, json_dumps(labels), status, json_dumps(req), json_dumps(resp), err[:2000]),
    )


async def main() -> None:
    args = parse_args()
    if not FIELD_RE.match(args.label_field):
        raise SystemExit("--label-field 只能包含字母、数字、下划线")
    batch_no = args.batch_no or "custom_label_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    db = Database()
    db.execute(DDL)
    rows = load_rows(db, args.statuses, args.style_no, args.limit)
    print("===== 产品自定义标签维护预览 =====")
    print("批次号：", batch_no)
    print("字段名：", args.label_field)
    print("待处理：", len(rows))
    for r in rows[: args.show]:
        print(r["sku"], "->", labels_for(r, args.mode, args.extra_label), r.get("product_name", "")[:50])
    if not args.confirm:
        print("预览模式，未写入。确认字段名无误后加 --confirm。")
        return
    client = LingxingClient(db=db, enable_api_log=True)
    token = (await client.generate_token()).token
    counter: Counter[str] = Counter()
    for i, r in enumerate(rows, 1):
        labels = labels_for(r, args.mode, args.extra_label)
        body = {"sku": r["sku"], "product_name": r["product_name"], args.label_field: payload(labels, args.payload_type)}
        resp = await client.request(token, PRODUCT_SET_API, "POST", req_body=body)
        if token_error(resp):
            token = (await client.generate_token()).token
            resp = await client.request(token, PRODUCT_SET_API, "POST", req_body=body)
        if str(resp.get("code")) == "0":
            counter["success"] += 1
            log(db, batch_no, r, args.label_field, labels, "success", body, resp)
        else:
            counter["failed"] += 1
            log(db, batch_no, r, args.label_field, labels, "failed", body, resp, str(resp))
        if i % 100 == 0 or i == len(rows):
            print(f"进度：{i}/{len(rows)} {dict(counter)}")
        if args.delay:
            await asyncio.sleep(args.delay)
    print("完成：", dict(counter))


if __name__ == "__main__":
    asyncio.run(main())
