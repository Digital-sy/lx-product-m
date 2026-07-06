#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.product_service import PRODUCT_DETAIL_API, PRODUCT_SET_API

FIELD_IDS = {
    "季节": "207714670595318277",
    "品线": "207714670595318275",
    "开发年份": "207714670595318273",
    "品类": "207714671567742465",
}

FORMAT_NAMES = [
    "id_name_val_text",
    "id_name_value",
    "id_name_val",
    "id_val_text",
    "id_value",
    "field_id_value",
    "custom_field_id_value",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="探测领星 product/set 自定义字段可写入的payload格式")
    p.add_argument("--sku", required=True)
    p.add_argument("--season", default="春夏")
    p.add_argument("--line", default="基础款")
    p.add_argument("--year", default="历史")
    p.add_argument("--category", default="基础款")
    p.add_argument("--format", choices=FORMAT_NAMES, default="", help="只测试指定格式；不传则逐个测试，验证成功即停止")
    p.add_argument("--confirm", action="store_true")
    return p.parse_args()


def wanted(a: argparse.Namespace) -> dict[str, str]:
    return {
        "季节": a.season,
        "品线": a.line,
        "开发年份": a.year,
        "品类": a.category,
    }


def make_item(fmt: str, name: str, value: str) -> dict[str, Any]:
    fid = FIELD_IDS[name]
    if fmt == "id_name_val_text":
        return {"id": fid, "name": name, "val_text": value}
    if fmt == "id_name_value":
        return {"id": fid, "name": name, "value": value}
    if fmt == "id_name_val":
        return {"id": fid, "name": name, "val": value}
    if fmt == "id_val_text":
        return {"id": fid, "val_text": value}
    if fmt == "id_value":
        return {"id": fid, "value": value}
    if fmt == "field_id_value":
        return {"field_id": fid, "value": value}
    if fmt == "custom_field_id_value":
        return {"custom_field_id": fid, "value": value}
    raise ValueError(fmt)


def make_fields(fmt: str, values: dict[str, str]) -> list[dict[str, Any]]:
    return [make_item(fmt, name, val) for name, val in values.items()]


def read_field_map(product: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in product.get("custom_fields") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("field_name") or item.get("custom_field_name") or "").strip()
        val = str(item.get("val_text") or item.get("value") or item.get("field_value") or item.get("val") or "").strip()
        if name:
            out[name] = val
    return out


async def get_product(client: LingxingClient, token: str, sku: str) -> tuple[dict[str, Any], str]:
    resp = await client.request(token, PRODUCT_DETAIL_API, "POST", req_body={"skus": [sku]})
    if str(resp.get("code")) != "0" and "token" in str(resp).lower():
        token = (await client.generate_token()).token
        resp = await client.request(token, PRODUCT_DETAIL_API, "POST", req_body={"skus": [sku]})
    if str(resp.get("code")) != "0":
        raise RuntimeError(f"读取产品失败：{resp}")
    rows = resp.get("data") or []
    if not rows:
        raise RuntimeError(f"未查到SKU：{sku}")
    return rows[0], token


async def post_product(client: LingxingClient, token: str, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
    resp = await client.request(token, PRODUCT_SET_API, "POST", req_body=body)
    if str(resp.get("code")) != "0" and "token" in str(resp).lower():
        token = (await client.generate_token()).token
        resp = await client.request(token, PRODUCT_SET_API, "POST", req_body=body)
    return resp, token


async def main() -> None:
    a = parse_args()
    values = wanted(a)
    formats = [a.format] if a.format else FORMAT_NAMES
    db = Database()
    client = LingxingClient(db=db, enable_api_log=True)
    token = (await client.generate_token()).token
    product, token = await get_product(client, token, a.sku)
    product_name = str(product.get("product_name") or "")
    print("SKU=", a.sku)
    print("product_name=", product_name)
    print("当前custom_fields=", json.dumps(product.get("custom_fields") or [], ensure_ascii=False))
    print("目标值=", values)
    if not a.confirm:
        for fmt in formats:
            print("格式", fmt, "body custom_fields=", json.dumps(make_fields(fmt, values), ensure_ascii=False))
        print("预览模式，未写入。加 --confirm 后测试。")
        return

    for fmt in formats:
        flds = make_fields(fmt, values)
        body = {"sku": a.sku, "product_name": product_name, "custom_fields": flds}
        print("\n===== 测试格式:", fmt, "=====")
        print("request custom_fields=", json.dumps(flds, ensure_ascii=False))
        resp, token = await post_product(client, token, body)
        print("response=", json.dumps(resp, ensure_ascii=False, default=str))
        product, token = await get_product(client, token, a.sku)
        got = read_field_map(product)
        print("verify custom_fields=", json.dumps(product.get("custom_fields") or [], ensure_ascii=False))
        ok = all(got.get(k) == v for k, v in values.items())
        print("verified=", ok)
        if ok:
            print("命中可写格式:", fmt)
            return
    print("所有格式均未验证成功。")


if __name__ == "__main__":
    asyncio.run(main())
