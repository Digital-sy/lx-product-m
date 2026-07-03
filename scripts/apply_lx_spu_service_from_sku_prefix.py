#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 SKU 前缀批量绑定领星 SPU（正式防御版）。

核心逻辑：
  SKU: AC1022-Y-XS -> SPU: AC1022

安全策略：
  1. 使用 SpuService，绑定前先校验 SKU 已存在，避免 spu/set 自动创建垃圾产品。
  2. SPU 已存在时，先读详情并合并已有 sku_list，避免编辑语义为全量替换时覆盖老 SKU。
  3. 必须显式传 attribute-json，例如 '[{"pa_id":340,"pai_id":3909}]'。
  4. 默认只预览；必须加 --confirm 才写领星。
  5. 长任务会按时间自动刷新 token；疑似 token 过期时会刷新后重试当前 SKU 一次。

示例：
  python scripts/apply_lx_spu_service_from_sku_prefix.py \
    --sku-like 'AC1022-%' \
    --limit 20 \
    --attribute-json '[{"pa_id":340,"pai_id":3909}]'

  python -u scripts/apply_lx_spu_service_from_sku_prefix.py \
    --sku-like 'AC1022-%' \
    --limit 20 \
    --attribute-json '[{"pa_id":340,"pai_id":3909}]' \
    --confirm
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
import sys
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.spu_service import SpuService, SPU_CHANGE_LOG_DDL
from lx_product_m.sku import extract_spu

SNAPSHOT_TABLE = "lxpm_product_category_snapshot"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按SKU前缀批量绑定领星SPU（SpuService防御版）")
    parser.add_argument("--batch-no", default="", help="批次号；不传则自动生成")
    parser.add_argument("--sku", action="append", help="只处理指定SKU，可重复传")
    parser.add_argument("--sku-like", default="", help="只处理匹配的SKU，例如 AC1022%%")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少个SKU，默认0不限制")
    parser.add_argument("--show", type=int, default=100, help="预览显示行数")
    parser.add_argument("--attribute-json", required=True, help="sku_list.attribute 的JSON数组，例如 '[{\"pa_id\":340,\"pai_id\":3909}]'")
    parser.add_argument("--delay", type=float, default=0.2, help="每条处理后的等待秒数，默认0.2")
    parser.add_argument("--token-refresh-seconds", type=int, default=1800, help="长任务token刷新间隔秒数，默认1800秒")
    parser.add_argument("--confirm", action="store_true", help="确认写入领星；不加只预览")
    return parser.parse_args()


def make_batch_no() -> str:
    return "spu_service_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_attribute_json(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"--attribute-json 不是合法JSON：{exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise SystemExit("--attribute-json 必须是非空JSON数组，例如 '[{\"pa_id\":340,\"pai_id\":3909}]'")
    out: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise SystemExit("--attribute-json 数组元素必须是对象")
        pa_id = item.get("pa_id")
        pai_id = item.get("pai_id")
        if pa_id in (None, "") or pai_id in (None, ""):
            raise SystemExit("--attribute-json 每个元素都必须包含非空 pa_id 和 pai_id")
        out.append({"pa_id": pa_id, "pai_id": pai_id})
    return out


def looks_like_token_error(exc: Exception) -> bool:
    text = str(exc).lower()
    keywords = [
        "token",
        "access_token",
        "refresh_token",
        "授权",
        "鉴权",
        "认证",
        "过期",
        "失效",
        "无效",
        "unauthorized",
        "invalid token",
    ]
    return any(k in text for k in keywords)


def load_rows(db: Database, skus: list[str] | None, sku_like: str, limit: int) -> list[dict[str, Any]]:
    sql = f"""
        SELECT sku, product_name
        FROM `{SNAPSHOT_TABLE}`
        WHERE sku IS NOT NULL AND sku <> ''
    """
    params: list[Any] = []
    if skus:
        placeholders = ",".join(["%s"] * len(skus))
        sql += f" AND sku IN ({placeholders})"
        params.extend(skus)
    if sku_like:
        sql += " AND sku LIKE %s"
        params.append(sku_like)
    sql += " ORDER BY sku"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    rows: list[dict[str, Any]] = []
    for row in db.fetch_all(sql, params):
        sku = str(row.get("sku") or "").strip()
        spu = extract_spu(sku)
        if not spu:
            continue
        rows.append({
            "sku": sku,
            "spu": spu,
            "product_name": str(row.get("product_name") or "").strip(),
        })
    return rows


def print_preview(rows: list[dict[str, Any]], attribute: list[dict[str, Any]], batch_no: str, show: int) -> None:
    print("===== 预览：SpuService 批量绑定 =====")
    print(f"批次号：{batch_no}")
    print(f"待处理SKU数：{len(rows)}")
    print(f"目标SPU数：{len(set(r['spu'] for r in rows))}")
    print(f"attribute：{json.dumps(attribute, ensure_ascii=False)}")
    print()
    print("SKU".ljust(32), "目标SPU".ljust(24), "产品名")
    print("-" * 120)
    for r in rows[:show]:
        print(r["sku"][:32].ljust(32), r["spu"][:24].ljust(24), r["product_name"][:60])
    if len(rows) > show:
        print(f"... 仅显示前{show}条，共{len(rows)}条")


async def run() -> None:
    args = parse_args()
    batch_no = args.batch_no or make_batch_no()
    attribute = parse_attribute_json(args.attribute_json)
    db = Database()
    rows = load_rows(db, args.sku, args.sku_like, args.limit)
    print_preview(rows, attribute, batch_no, args.show)

    if not rows:
        print("没有需要处理的SKU。")
        return
    if not args.confirm:
        print("\n当前为预览模式，未写领星。确认无误后加 --confirm 执行。")
        return

    db.execute(SPU_CHANGE_LOG_DDL)
    client = LingxingClient(db=db, enable_api_log=True)
    service = SpuService(client, db)
    token = (await client.generate_token()).token
    token_generated_at = time.monotonic()
    counter: Counter = Counter()

    async def refresh_token(reason: str) -> str:
        nonlocal token_generated_at
        print(f"[TOKEN] {reason}，重新生成 token ...")
        new_token = (await client.generate_token()).token
        token_generated_at = time.monotonic()
        return new_token

    for idx, row in enumerate(rows, 1):
        if args.token_refresh_seconds > 0 and time.monotonic() - token_generated_at >= args.token_refresh_seconds:
            token = await refresh_token(f"已运行超过 {args.token_refresh_seconds} 秒")

        try:
            result = await service.ensure_spu_and_bind_sku(
                token,
                spu=row["spu"],
                sku=row["sku"],
                product_name=row["product_name"],
                sku_attribute=attribute,
                batch_no=batch_no,
            )
            status = str(result.get("status") or "success")
            counter[status] += 1
            print(f"[{idx}/{len(rows)}] {row['sku']} -> {row['spu']} {status}")
        except Exception as exc:  # noqa: BLE001
            if looks_like_token_error(exc):
                try:
                    token = await refresh_token(f"检测到疑似 token 异常：{exc}")
                    result = await service.ensure_spu_and_bind_sku(
                        token,
                        spu=row["spu"],
                        sku=row["sku"],
                        product_name=row["product_name"],
                        sku_attribute=attribute,
                        batch_no=batch_no,
                    )
                    status = str(result.get("status") or "success")
                    counter[status] += 1
                    print(f"[{idx}/{len(rows)}] {row['sku']} -> {row['spu']} {status} after_token_refresh")
                except Exception as retry_exc:  # noqa: BLE001
                    counter["failed"] += 1
                    print(f"[{idx}/{len(rows)}] FAILED {row['sku']} -> {row['spu']} after_token_refresh: {retry_exc}")
            else:
                counter["failed"] += 1
                print(f"[{idx}/{len(rows)}] FAILED {row['sku']} -> {row['spu']}: {exc}")
        if args.delay > 0:
            await asyncio.sleep(args.delay)

    print("\n===== 完成 =====")
    print(f"批次号：{batch_no}")
    print(f"结果：{dict(counter)}")


if __name__ == "__main__":
    asyncio.run(run())
