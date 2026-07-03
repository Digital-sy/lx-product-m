#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.spu_service import SpuService, SPU_CHANGE_LOG_DDL
from lx_product_m.sku import extract_spu

SNAPSHOT_TABLE = "lxpm_product_category_snapshot"
LOG_TABLE = "lxpm_spu_change_log"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="每日SPU绑定任务：只处理未绑定过的SKU")
    p.add_argument("--batch-no", default="", help="批次号，不传自动生成")
    p.add_argument("--sku-like", default="", help="可选，只处理指定SKU LIKE")
    p.add_argument("--limit", type=int, default=0, help="可选，限制候选数量")
    p.add_argument("--show", type=int, default=30, help="预览显示条数")
    p.add_argument("--attribute-json", required=True, help='例如 [{"pa_id":340,"pai_id":3909}]')
    p.add_argument("--delay", type=float, default=0.2, help="每条间隔秒数")
    p.add_argument("--refresh-seconds", type=int, default=1200, help="访问凭据刷新间隔秒数，默认20分钟")
    p.add_argument("--confirm", action="store_true", help="确认写入；不加只预览")
    return p.parse_args()


def parse_attribute(value: str) -> list[dict[str, Any]]:
    data = json.loads(value)
    if not isinstance(data, list) or not data:
        raise SystemExit("--attribute-json 必须是非空JSON数组")
    for item in data:
        if not isinstance(item, dict) or item.get("pa_id") in (None, "") or item.get("pai_id") in (None, ""):
            raise SystemExit("--attribute-json 每项必须包含非空 pa_id / pai_id")
    return data


def table_exists(db: Database, table: str) -> bool:
    try:
        return bool(db.fetch_all("SHOW TABLES LIKE %s", [table]))
    except Exception:
        return False


def load_known_bound(db: Database) -> set[str]:
    if not table_exists(db, LOG_TABLE):
        return set()
    sql = f"""
        SELECT DISTINCT sku
        FROM `{LOG_TABLE}`
        WHERE sku IS NOT NULL AND sku <> ''
          AND (
              status IN ('success', 'skipped', 'already_bound', 'skipped_already_bound')
              OR error_message LIKE '%%当前已经关联%%'
              OR error_message LIKE '%%已经关联了%%'
          )
    """
    return {str(r.get("sku") or "").strip() for r in db.fetch_all(sql) if r.get("sku")}


def load_candidates(db: Database, sku_like: str, limit: int, known: set[str]) -> tuple[list[dict[str, str]], int]:
    sql = f"""
        SELECT sku, product_name
        FROM `{SNAPSHOT_TABLE}`
        WHERE sku IS NOT NULL AND sku <> ''
    """
    params: list[Any] = []
    if sku_like:
        sql += " AND sku LIKE %s"
        params.append(sku_like)
    sql += " ORDER BY sku"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    rows: list[dict[str, str]] = []
    skipped = 0
    for r in db.fetch_all(sql, params):
        sku = str(r.get("sku") or "").strip()
        if not sku:
            continue
        if sku in known:
            skipped += 1
            continue
        spu = extract_spu(sku)
        if not spu:
            continue
        rows.append({"sku": sku, "spu": spu, "product_name": str(r.get("product_name") or "")})
    return rows, skipped


def already_bound_error(exc: Exception) -> bool:
    text = str(exc)
    return "当前已经关联" in text or "已经关联了" in text


def token_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "2001005" in text
        or "access token not match" in text
        or "access_token" in text
        or "token" in text and ("not match" in text or "invalid" in text or "过期" in text or "失效" in text)
    )


async def bind_one(service: SpuService, token: str, row: dict[str, str], attr: list[dict[str, Any]], batch_no: str) -> str:
    result = await service.ensure_spu_and_bind_sku(
        token,
        spu=row["spu"],
        sku=row["sku"],
        product_name=row["product_name"],
        sku_attribute=attr,
        batch_no=batch_no,
    )
    return str(result.get("status") or "success")


async def main() -> None:
    args = parse_args()
    batch_no = args.batch_no or "spu_daily_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    attr = parse_attribute(args.attribute_json)
    db = Database()
    db.execute(SPU_CHANGE_LOG_DDL)

    known = load_known_bound(db)
    rows, skipped_known = load_candidates(db, args.sku_like, args.limit, known)

    print("===== 每日SPU绑定任务 =====")
    print("批次号：", batch_no)
    print("历史已绑定跳过：", skipped_known)
    print("本次待处理：", len(rows))
    print("目标SPU数：", len({r["spu"] for r in rows}))
    print("attribute：", json.dumps(attr, ensure_ascii=False))
    for r in rows[: args.show]:
        print(r["sku"], "->", r["spu"], r["product_name"][:60])
    if not args.confirm:
        print("预览模式，未写入。加 --confirm 执行。")
        return

    client = LingxingClient(db=db, enable_api_log=True)
    service = SpuService(client, db)
    token = (await client.generate_token()).token
    token_at = time.monotonic()
    counter: Counter[str] = Counter()

    async def refresh_token(reason: str) -> str:
        nonlocal token_at
        print(f"[AUTH] {reason}，重新生成 token")
        new_token = (await client.generate_token()).token
        token_at = time.monotonic()
        return new_token

    for i, r in enumerate(rows, 1):
        if args.refresh_seconds and time.monotonic() - token_at >= args.refresh_seconds:
            token = await refresh_token("定时刷新")
        try:
            status = await bind_one(service, token, r, attr, batch_no)
            counter[status] += 1
            print(f"[{i}/{len(rows)}] {r['sku']} -> {r['spu']} {status}")
        except Exception as exc:  # noqa: BLE001
            if token_error(exc):
                try:
                    token = await refresh_token(f"检测到访问凭据异常：{exc}")
                    status = await bind_one(service, token, r, attr, batch_no)
                    counter[status] += 1
                    print(f"[{i}/{len(rows)}] {r['sku']} -> {r['spu']} {status} after_auth_refresh")
                except Exception as retry_exc:  # noqa: BLE001
                    counter["failed"] += 1
                    print(f"[{i}/{len(rows)}] FAILED {r['sku']} -> {r['spu']} after_auth_refresh: {retry_exc}")
            elif already_bound_error(exc):
                counter["skipped_already_bound"] += 1
                print(f"[{i}/{len(rows)}] {r['sku']} -> {r['spu']} skipped_already_bound")
            else:
                counter["failed"] += 1
                print(f"[{i}/{len(rows)}] FAILED {r['sku']} -> {r['spu']}: {exc}")
        if args.delay:
            await asyncio.sleep(args.delay)

    print("===== 完成 =====")
    print("批次号：", batch_no)
    print("历史已绑定跳过：", skipped_known)
    print("结果：", dict(counter))


if __name__ == "__main__":
    asyncio.run(main())
