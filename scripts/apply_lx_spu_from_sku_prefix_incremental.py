#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.spu_service import SPU_CHANGE_LOG_DDL, SpuService
from lx_product_m.sku import extract_spu

SNAPSHOT_TABLE = "lxpm_product_category_snapshot"
LOG_TABLE = "lxpm_spu_change_log"
DONE_STATUSES = ("success", "skipped", "skipped_already_bound")
SPU_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="每日增量：按SKU前缀维护领星SPU关联，只处理新增或目标SPU变化的SKU")
    p.add_argument("--batch-no", default="")
    p.add_argument("--sku-like", default="")
    p.add_argument("--style-no", action="append", help="只处理指定SPU/款号，可重复传")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--show", type=int, default=50)
    p.add_argument("--delay", type=float, default=1.0, help="每个SPU组写入后的等待秒数")
    p.add_argument("--attribute-json", default='[{"pa_id":340,"pai_id":3909}]')
    p.add_argument("--force", action="store_true", help="忽略历史成功日志，强制处理筛选范围内SKU")
    p.add_argument("--confirm", action="store_true")
    return p.parse_args()


def clean(v: Any) -> str:
    return str(v or "").strip()


def parse_attribute_json(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except Exception as exc:
        raise SystemExit(f"--attribute-json 不是合法JSON：{exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise SystemExit("--attribute-json 必须是非空JSON数组")
    for item in parsed:
        if not isinstance(item, dict) or item.get("pa_id") in (None, "") or item.get("pai_id") in (None, ""):
            raise SystemExit("--attribute-json 每个元素都必须包含 pa_id 和 pai_id")
    return parsed


def load_rows(db: Database, a: argparse.Namespace) -> tuple[list[dict[str, Any]], Counter]:
    done_list = ",".join(["%s"] * len(DONE_STATUSES))
    params: list[Any] = list(DONE_STATUSES)
    sql = f"""
        SELECT p.sku,
               p.product_name,
               p.spu AS snapshot_spu,
               latest.spu AS latest_done_spu,
               latest.status AS latest_done_status
        FROM `{SNAPSHOT_TABLE}` p
        LEFT JOIN (
            SELECT sku, spu, status
            FROM (
                SELECT sku,
                       spu,
                       status,
                       ROW_NUMBER() OVER (PARTITION BY sku ORDER BY id DESC) AS rn
                FROM `{LOG_TABLE}`
                WHERE status IN ({done_list})
            ) x
            WHERE rn = 1
        ) latest
          ON latest.sku = p.sku
        WHERE p.sku IS NOT NULL AND p.sku <> ''
          AND p.product_name IS NOT NULL AND p.product_name <> ''
    """
    if a.sku_like:
        sql += " AND p.sku LIKE %s"
        params.append(a.sku_like)
    if a.style_no:
        ph = ",".join(["%s"] * len(a.style_no))
        sql += f" AND p.spu IN ({ph})"
        params.extend(a.style_no)
    sql += " ORDER BY p.spu, p.sku"
    if a.limit:
        sql += " LIMIT %s"
        params.append(a.limit)

    stat: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for r in db.fetch_all(sql, params):
        sku = clean(r.get("sku"))
        target_spu = extract_spu(sku)
        if not target_spu:
            stat["skip_no_spu"] += 1
            continue
        if not SPU_PATTERN.match(target_spu):
            stat["skip_invalid_spu"] += 1
            continue
        latest_spu = clean(r.get("latest_done_spu"))
        latest_status = clean(r.get("latest_done_status"))
        if not a.force and latest_status in DONE_STATUSES:
            if latest_status == "skipped_already_bound":
                stat["skip_already_bound_other_spu"] += 1
                continue
            if latest_spu == target_spu:
                stat["skip_unchanged"] += 1
                continue
        reason = "force" if a.force else ("new" if not latest_status else "changed")
        rows.append({
            "sku": sku,
            "spu": target_spu,
            "product_name": clean(r.get("product_name")),
            "reason": reason,
            "latest_done_spu": latest_spu,
            "latest_done_status": latest_status,
        })
        stat[reason] += 1
    return rows, stat


async def main() -> None:
    a = parse_args()
    batch_no = a.batch_no or "spu_incremental_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    attr = parse_attribute_json(a.attribute_json)
    db = Database()
    db.execute(SPU_CHANGE_LOG_DDL)
    rows, stat = load_rows(db, a)

    print("===== SPU 增量绑定 =====")
    print("批次号：", batch_no)
    print("候选需写入SKU：", len(rows))
    print("筛选统计：", dict(stat))
    print("attribute：", json.dumps(attr, ensure_ascii=False))
    for r in rows[: a.show]:
        print(r["sku"], "->", r["spu"], r["reason"], clean(r.get("product_name"))[:80])
    if not rows:
        print("没有需要处理的SPU增量。")
        return
    if not a.confirm:
        print("预览模式，未写入。")
        return

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        grouped[r["spu"]].append({"sku": r["sku"], "product_name": r["product_name"]})

    client = LingxingClient(db=db, enable_api_log=True)
    token = (await client.generate_token()).token
    service = SpuService(client, db)
    counter: Counter[str] = Counter()
    groups = list(grouped.items())
    for i, (spu, sku_rows) in enumerate(groups, 1):
        try:
            result = await service.ensure_spu_and_bind_skus(
                token,
                spu=spu,
                sku_rows=sku_rows,
                spu_name=spu,
                sku_attribute=attr,
                allow_create_sku=False,
                batch_no=batch_no,
            )
            for status, skus in result.items():
                if isinstance(skus, list):
                    counter[status] += len(skus)
        except Exception as exc:
            counter["exception"] += len(sku_rows)
            print(f"[FAILED] spu={spu}, sku_cnt={len(sku_rows)}, error={exc}")
        print(f"进度：{i}/{len(groups)}组，SKU结果={dict(counter)}")
        if a.delay:
            await asyncio.sleep(a.delay)
    print("完成：", dict(counter))


if __name__ == "__main__":
    asyncio.run(main())
