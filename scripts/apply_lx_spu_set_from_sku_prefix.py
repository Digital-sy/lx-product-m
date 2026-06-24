#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过 /erp/sc/routing/storage/spu/set 把 SKU 关联到 SKU 前缀 SPU。

规则：
  AC1022-Y-XS -> SPU=AC1022, spu_name=AC1022

领星接口：
  /erp/sc/routing/storage/spu/set

请求核心结构：
  {
    "spu": "AC1022",
    "spu_name": "AC1022",
    "status": 1,
    "use_spu_template": 0,
    "sku_list": [
      {
        "sku": "AC1022-Y-XS",
        "product_name": "...",
        "attribute": [{"pa_id": 340, "pai_id": 3909}]
      }
    ]
  }

默认只预览，不写领星；必须加 --confirm。
正式全量前必须先 --sku 单个 SKU 测试并到领星前台确认。
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lx_product_m.db import Database, json_dumps
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.product_service import PRODUCT_DETAIL_API, ProductService
from lx_product_m.sku import extract_spu

SNAPSHOT_TABLE = "lxpm_product_category_snapshot"
TASK_TABLE = "lxpm_spu_set_task"
CHANGE_LOG_TABLE = "lxpm_spu_set_change_log"
SPU_SET_API = "/erp/sc/routing/storage/spu/set"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="用领星 spu/set 接口创建/编辑SPU，并关联SKU")
    parser.add_argument("--batch-no", default="", help="批次号；不传则自动生成")
    parser.add_argument("--sku", action="append", help="只处理指定SKU，可重复传")
    parser.add_argument("--sku-like", default="", help="只处理匹配的SKU，例如 AC1022%%")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少个SKU，默认0不限制")
    parser.add_argument("--show", type=int, default=100, help="预览显示行数")
    parser.add_argument("--attribute-json", required=True, help="sku_list.attribute 的JSON数组，例如 '[{\"pa_id\":340,\"pai_id\":3909}]'")
    parser.add_argument("--group-by-spu", action="store_true", help="按SPU分组提交；默认每个SKU一次请求，更适合先测试")
    parser.add_argument("--status", type=int, default=1, help="SPU状态：0停售，1在售，2开发中，3清仓；默认1")
    parser.add_argument("--use-spu-template", type=int, default=0, choices=[0, 1], help="是否应用SPU信息至新生成SKU，默认0")
    parser.add_argument("--delay", type=float, default=0.2, help="每次写入后的等待秒数，默认0.2")
    parser.add_argument("--max-retries", type=int, default=5, help="最大重试次数，默认5")
    parser.add_argument("--verify-batch-size", type=int, default=100, help="复查每批SKU数，默认100")
    parser.add_argument("--skip-verify", action="store_true", help="跳过复查；不建议正式使用")
    parser.add_argument("--confirm", action="store_true", help="确认写入领星；不加只预览")
    return parser.parse_args()


def make_batch_no() -> str:
    return "spu_set_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_attribute_json(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"--attribute-json 不是合法JSON：{exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise SystemExit("--attribute-json 必须是非空JSON数组，例如 '[{\"pa_id\":340,\"pai_id\":3909}]'")
    for item in parsed:
        if not isinstance(item, dict):
            raise SystemExit("--attribute-json 数组元素必须是对象")
        if item.get("pa_id") in (None, "") or item.get("pai_id") in (None, ""):
            raise SystemExit("--attribute-json 每个元素都必须包含 pa_id 和 pai_id")
    return parsed


def ensure_tables(db: Database) -> None:
    with db.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{TASK_TABLE}` (
              `id` BIGINT NOT NULL AUTO_INCREMENT,
              `batch_no` VARCHAR(100) NOT NULL,
              `spu` VARCHAR(200) NOT NULL,
              `sku` VARCHAR(200) NOT NULL,
              `product_name` VARCHAR(500) DEFAULT '',
              `attribute_json` JSON NULL,
              `status` VARCHAR(50) NOT NULL DEFAULT 'pending',
              `error_message` TEXT NULL,
              `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (`id`),
              KEY `idx_batch_no` (`batch_no`),
              KEY `idx_spu` (`spu`),
              KEY `idx_sku` (`sku`),
              KEY `idx_status` (`status`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='lx-product-m：spu/set任务表'
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{CHANGE_LOG_TABLE}` (
              `id` BIGINT NOT NULL AUTO_INCREMENT,
              `batch_no` VARCHAR(100) NOT NULL,
              `task_id` BIGINT DEFAULT NULL,
              `spu` VARCHAR(200) NOT NULL,
              `sku` VARCHAR(200) DEFAULT '',
              `status` VARCHAR(50) NOT NULL,
              `request_json` JSON NULL,
              `response_json` JSON NULL,
              `verify_response_json` JSON NULL,
              `error_message` TEXT NULL,
              `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (`id`),
              KEY `idx_batch_no` (`batch_no`),
              KEY `idx_task_id` (`task_id`),
              KEY `idx_spu` (`spu`),
              KEY `idx_sku` (`sku`),
              KEY `idx_status` (`status`),
              KEY `idx_created_at` (`created_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='lx-product-m：spu/set日志表'
            """
        )


def load_rows(db: Database, skus: list[str] | None, sku_like: str, limit: int) -> list[dict[str, Any]]:
    sql = f"""
        SELECT sku, product_name
        FROM `{SNAPSHOT_TABLE}`
        WHERE sku IS NOT NULL AND sku <> ''
          AND product_name IS NOT NULL AND product_name <> ''
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
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    out: list[dict[str, Any]] = []
    for row in db.fetch_all(sql, params):
        sku = str(row.get("sku") or "").strip()
        spu = extract_spu(sku)
        if not spu:
            continue
        out.append({
            "spu": spu,
            "sku": sku,
            "product_name": str(row.get("product_name") or "").strip(),
        })
    return out


def print_preview(rows: list[dict[str, Any]], attribute: list[dict[str, Any]], show: int, batch_no: str, group_by_spu: bool) -> None:
    print("===== 预览：领星 spu/set 写入 =====")
    print(f"批次号：{batch_no}")
    print(f"提交方式：{'按SPU分组' if group_by_spu else '每个SKU单独提交'}")
    print(f"attribute：{json.dumps(attribute, ensure_ascii=False)}")
    print(f"待处理SKU数：{len(rows)}")
    print(f"目标SPU数：{len(set(r['spu'] for r in rows))}")
    print()
    headers = ["SKU", "目标SPU", "产品名"]
    widths = [32, 20, 70]
    print(" ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-" * 130)
    for r in rows[:show]:
        values = [r["sku"], r["spu"], r["product_name"]]
        print(" ".join(str(v)[:w].ljust(w) for v, w in zip(values, widths)))
    if len(rows) > show:
        print(f"... 仅显示前{show}条，共{len(rows)}条")


def insert_tasks(db: Database, batch_no: str, rows: list[dict[str, Any]], attribute: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sql = f"""
        INSERT INTO `{TASK_TABLE}`
        (`batch_no`, `spu`, `sku`, `product_name`, `attribute_json`, `status`)
        VALUES (%s,%s,%s,%s,%s,'pending')
    """
    tasks: list[dict[str, Any]] = []
    with db.cursor() as cur:
        for r in rows:
            cur.execute(sql, (batch_no, r["spu"], r["sku"], r["product_name"], json_dumps(attribute)))
            task = dict(r)
            task["task_id"] = cur.lastrowid
            tasks.append(task)
    return tasks


def update_task(db: Database, task_id: int, status: str, error_message: str = "") -> None:
    db.execute(f"UPDATE `{TASK_TABLE}` SET status=%s, error_message=%s WHERE id=%s", (status, error_message[:5000], task_id))


def write_log(db: Database, batch_no: str, status: str, req: dict[str, Any] | None, resp: dict[str, Any] | None, verify: dict[str, Any] | None = None, err: str = "", task_id: int | None = None, spu: str = "", sku: str = "") -> None:
    db.execute(
        f"""
        INSERT INTO `{CHANGE_LOG_TABLE}`
        (`batch_no`, `task_id`, `spu`, `sku`, `status`, `request_json`, `response_json`, `verify_response_json`, `error_message`)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (batch_no, task_id, spu, sku, status, json_dumps(req), json_dumps(resp), json_dumps(verify), err[:5000]),
    )


def is_token_error(resp: dict[str, Any] | None) -> bool:
    return bool(resp) and (str(resp.get("code")) == "2001005" or "token" in str(resp.get("msg") or resp.get("message") or "").lower())


def is_retryable_error(resp: dict[str, Any] | None) -> bool:
    if not resp:
        return False
    msg = str(resp.get("msg") or resp.get("message") or "")
    details = str(resp.get("error_details") or "")
    # 明确业务校验不要重试。
    if "必须" in details or "不能为空" in details or "不存在" in details or "重复" in details:
        return False
    return str(resp.get("code")) in {"500", "502", "503", "504"} or "请求连接异常" in msg or "稍后再试" in msg


async def request_with_retry(client: LingxingClient, token: str, body: dict[str, Any], max_retries: int) -> tuple[dict[str, Any], str]:
    current_token = token
    last_resp: dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        resp = await client.request(current_token, SPU_SET_API, "POST", req_body=body)
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


def build_spu_body(spu: str, sku_rows: list[dict[str, Any]], attribute: list[dict[str, Any]], status: int, use_spu_template: int) -> dict[str, Any]:
    return {
        "spu": spu,
        "spu_name": spu,
        "status": status,
        "use_spu_template": use_spu_template,
        "sku_list": [
            {
                "sku": r["sku"],
                "product_name": r["product_name"],
                "attribute": attribute,
            }
            for r in sku_rows
        ],
    }


def group_tasks(tasks: list[dict[str, Any]], group_by_spu: bool) -> list[list[dict[str, Any]]]:
    if not group_by_spu:
        return [[t] for t in tasks]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in tasks:
        groups[t["spu"]].append(t)
    return list(groups.values())


async def write_groups(db: Database, batch_no: str, tasks: list[dict[str, Any]], attribute: list[dict[str, Any]], group_by_spu: bool, status_value: int, use_spu_template: int, delay: float, max_retries: int) -> Counter:
    client = LingxingClient(db=db, enable_api_log=True)
    token = (await client.generate_token()).token
    counter: Counter = Counter()
    groups = group_tasks(tasks, group_by_spu)
    total_groups = len(groups)

    for idx, g in enumerate(groups, 1):
        spu = g[0]["spu"]
        body = build_spu_body(spu, g, attribute, status_value, use_spu_template)
        for task in g:
            update_task(db, int(task["task_id"]), "running")
        try:
            resp, token = await request_with_retry(client, token, body, max_retries)
            if str(resp.get("code")) == "0":
                for task in g:
                    update_task(db, int(task["task_id"]), "write_success")
                    write_log(db, batch_no, "write_success", body, resp, task_id=int(task["task_id"]), spu=spu, sku=task["sku"])
                    counter["write_success"] += 1
            else:
                err = str(resp)
                for task in g:
                    update_task(db, int(task["task_id"]), "failed", err)
                    write_log(db, batch_no, "failed", body, resp, err=err, task_id=int(task["task_id"]), spu=spu, sku=task["sku"])
                    counter["failed"] += 1
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            for task in g:
                update_task(db, int(task["task_id"]), "failed", err)
                write_log(db, batch_no, "failed", body, None, err=err, task_id=int(task["task_id"]), spu=spu, sku=task["sku"])
                counter["failed"] += 1
        print(f"写入进度：{idx}/{total_groups}组，SKU结果={dict(counter)}")
        if delay > 0:
            await asyncio.sleep(delay)
    return counter


def load_verify_tasks(db: Database, batch_no: str) -> list[dict[str, Any]]:
    return db.fetch_all(f"SELECT id AS task_id, spu, sku FROM `{TASK_TABLE}` WHERE batch_no=%s AND status='write_success' ORDER BY id", (batch_no,))


async def verify_tasks(db: Database, batch_no: str, batch_size: int, max_retries: int) -> Counter:
    tasks = load_verify_tasks(db, batch_no)
    if not tasks:
        return Counter()
    skus = [str(t["sku"]) for t in tasks]
    task_by_sku = {str(t["sku"]): t for t in tasks}
    client = LingxingClient(db=db, enable_api_log=True)
    token = (await client.generate_token()).token
    service = ProductService(client, db)
    counter: Counter = Counter()
    batch_size = max(1, min(batch_size, 100))

    for i in range(0, len(skus), batch_size):
        batch = skus[i:i + batch_size]
        resp = await client.request(token, PRODUCT_DETAIL_API, "POST", req_body={"skus": batch})
        if is_token_error(resp):
            token = (await client.generate_token()).token
            resp = await client.request(token, PRODUCT_DETAIL_API, "POST", req_body={"skus": batch})
        if str(resp.get("code")) != "0":
            err = str(resp)
            for sku in batch:
                task = task_by_sku[sku]
                update_task(db, int(task["task_id"]), "verify_failed", err)
                write_log(db, batch_no, "verify_failed", None, None, resp, err, int(task["task_id"]), str(task["spu"]), sku)
                counter["verify_failed"] += 1
            continue
        products = resp.get("data") or []
        returned = {str(p.get("sku") or p.get("SKU") or ""): p for p in products}
        for sku in batch:
            task = task_by_sku[sku]
            product = returned.get(sku)
            target_spu = str(task["spu"])
            if product:
                service.save_product_snapshot(product)
            verify_spu = str((product or {}).get("spu") or (product or {}).get("spu_name") or (product or {}).get("api_spu") or "").strip()
            ok = verify_spu == target_spu
            status = "success" if ok else "verify_failed"
            err = "" if ok else f"复查SPU不一致：verify_spu={verify_spu!r}, target_spu={target_spu!r}"
            update_task(db, int(task["task_id"]), status, err)
            write_log(db, batch_no, status, None, None, {"data": [product] if product else []}, err, int(task["task_id"]), target_spu, sku)
            counter[status] += 1
        print(f"复查进度：{min(i + batch_size, len(skus))}/{len(skus)}，{dict(counter)}")
    return counter


async def main() -> None:
    args = parse_args()
    attribute = parse_attribute_json(args.attribute_json)
    batch_no = args.batch_no or make_batch_no()
    db = Database()
    rows = load_rows(db, args.sku, args.sku_like, args.limit)
    print_preview(rows, attribute, args.show, batch_no, args.group_by_spu)

    if not rows:
        print("没有需要处理的SKU。")
        return
    if not args.confirm:
        print("\n当前为预览模式，未创建任务、未写领星。确认无误后加 --confirm 执行。")
        return

    ensure_tables(db)
    tasks = insert_tasks(db, batch_no, rows, attribute)
    print(f"\n已创建任务：{len(tasks)} 条，开始调用 spu/set...")
    write_counter = await write_groups(
        db,
        batch_no,
        tasks,
        attribute,
        args.group_by_spu,
        args.status,
        args.use_spu_template,
        args.delay,
        args.max_retries,
    )
    print(f"写入完成：{dict(write_counter)}")

    if args.skip_verify:
        print("已跳过复查。")
        print(f"批次号：{batch_no}")
        return

    print("开始复查...")
    verify_counter = await verify_tasks(db, batch_no, args.verify_batch_size, args.max_retries)
    print("\n===== spu/set 写入完成 =====")
    print(f"写入：{dict(write_counter)}")
    print(f"复查：{dict(verify_counter)}")
    print(f"批次号：{batch_no}")


if __name__ == "__main__":
    asyncio.run(main())
