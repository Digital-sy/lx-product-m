#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领星产品写回安全保护。

所有 product/set 写入前重新读取领星实时产品详情：
1. product_name 只使用写入前刚读取到的实时值；
2. 自定义字段按完整实时列表合并，避免覆盖其他字段；
3. 当前分类在非分类变更场景下原样携带；
4. 限流、临时异常和 token 失效自动重试；
5. 实时详情缺失或品名为空时禁止写入。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from ..lingxing_client import LingxingClient

PRODUCT_DETAIL_API = "/erp/sc/routing/data/local_inventory/batchGetProductInfo"
PRODUCT_SET_API = "/erp/sc/routing/storage/product/set"

RATE_LIMIT_CODES = {"103", "3001008", "429"}
RETRYABLE_CODES = RATE_LIMIT_CODES | {"500", "502", "503", "504"}


def clean(value: Any) -> str:
    return str(value or "").strip()


def parse_json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return None


def extract_sku(product: dict[str, Any] | None) -> str:
    product = product or {}
    return clean(product.get("sku") or product.get("SKU"))


def extract_product_name(product: dict[str, Any] | None) -> str:
    product = product or {}
    return clean(product.get("product_name") or product.get("productName"))


def extract_category(product: dict[str, Any] | None) -> tuple[int | None, str]:
    product = product or {}
    raw_id = (
        product.get("category_id")
        or product.get("categoryId")
        or product.get("cid")
        or product.get("category_cid")
    )
    category_id: int | None = None
    try:
        if raw_id not in (None, ""):
            category_id = int(raw_id)
    except (TypeError, ValueError):
        category_id = None
    category_name = clean(
        product.get("category")
        or product.get("category_name")
        or product.get("categoryName")
    )
    return category_id, category_name


def extract_custom_fields(product: dict[str, Any] | None) -> list[dict[str, Any]]:
    product = product or {}
    for key in ("custom_fields", "custom_field_list", "customFields", "customFieldList"):
        value = product.get(key)
        if isinstance(value, list):
            return value
    return []


def normalize_custom_fields(fields: Any) -> list[dict[str, str]]:
    """转换成 product/set 接受的 id/name/val 格式。"""
    arr = parse_json_value(fields)
    if not isinstance(arr, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in arr:
        if not isinstance(item, dict):
            continue
        field_id = clean(item.get("id"))
        name = clean(item.get("name"))
        value = clean(
            item.get("val")
            or item.get("val_text")
            or item.get("value")
            or item.get("field_value")
        )
        if not field_id or not name or not value:
            continue
        key = (field_id, name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": field_id, "name": name, "val": value})
    return out


def merge_custom_fields(existing: Any, target: Any) -> list[dict[str, str]]:
    """只替换目标字段，其他领星实时自定义字段原样保留。"""
    existing_fields = normalize_custom_fields(existing)
    target_fields = normalize_custom_fields(target)
    target_ids = {item["id"] for item in target_fields}
    target_names = {item["name"] for item in target_fields}
    merged = [
        item
        for item in existing_fields
        if item["id"] not in target_ids and item["name"] not in target_names
    ]
    merged.extend(target_fields)
    return merged


def target_fields_match(existing: Any, target: Any) -> bool:
    existing_fields = normalize_custom_fields(existing)
    target_fields = normalize_custom_fields(target)
    by_id = {item["id"]: item["val"] for item in existing_fields}
    by_name = {item["name"]: item["val"] for item in existing_fields}
    for item in target_fields:
        actual = by_id.get(item["id"])
        if actual is None:
            actual = by_name.get(item["name"])
        if actual != item["val"]:
            return False
    return True


def is_token_error(response: dict[str, Any] | None) -> bool:
    response = response or {}
    text = f"{response.get('code')} {response.get('msg') or response.get('message') or ''}".lower()
    return "2001005" in text or "token" in text


def is_retryable_error(response: dict[str, Any] | None) -> bool:
    response = response or {}
    code = clean(response.get("code"))
    message = clean(response.get("msg") or response.get("message"))
    return (
        code in RETRYABLE_CODES
        or "请求过于频繁" in message
        or "稍后再试" in message
        or "请求连接异常" in message
        or "timeout" in message.lower()
    )


def retry_wait(attempt: int, base_seconds: float = 2.0, max_seconds: float = 60.0) -> float:
    return min(max_seconds, base_seconds * (2 ** attempt))


async def request_with_retry(
    client: LingxingClient,
    token: str,
    api_path: str,
    body: dict[str, Any],
    max_retries: int = 5,
    retry_base_seconds: float = 2.0,
    retry_max_seconds: float = 60.0,
) -> tuple[dict[str, Any], str]:
    current_token = token
    last_response: dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        try:
            response = await client.request(current_token, api_path, "POST", req_body=body)
            last_response = response
        except Exception as exc:
            if attempt >= max_retries:
                raise
            wait_seconds = retry_wait(attempt, retry_base_seconds, retry_max_seconds)
            print(f"[RETRY] 接口请求异常，{wait_seconds:.0f}s后重试：{exc}")
            await asyncio.sleep(wait_seconds)
            continue

        if str(response.get("code")) == "0":
            return response, current_token

        if is_token_error(response):
            if attempt >= max_retries:
                return response, current_token
            current_token = (await client.generate_token()).token
            print("[RETRY] access token失效，重新获取后重试")
            await asyncio.sleep(1)
            continue

        if is_retryable_error(response) and attempt < max_retries:
            wait_seconds = retry_wait(attempt, retry_base_seconds, retry_max_seconds)
            print(
                f"[RETRY] 领星限流/临时异常 code={response.get('code')} "
                f"request_id={response.get('request_id') or ''}，{wait_seconds:.0f}s后重试"
            )
            await asyncio.sleep(wait_seconds)
            continue

        return response, current_token
    return last_response, current_token


async def fetch_live_products(
    client: LingxingClient,
    token: str,
    skus: list[str],
    max_retries: int = 5,
    batch_size: int = 100,
    delay_seconds: float = 0.5,
    retry_base_seconds: float = 2.0,
    retry_max_seconds: float = 60.0,
) -> tuple[dict[str, dict[str, Any]], str]:
    """批量读取实时产品详情；返回 SKU->详情 和可能刷新的 token。"""
    clean_skus: list[str] = []
    seen: set[str] = set()
    for sku in skus:
        value = clean(sku)
        if value and value not in seen:
            seen.add(value)
            clean_skus.append(value)

    current_token = token
    result_map: dict[str, dict[str, Any]] = {}
    batch_size = max(1, min(batch_size, 100))
    for start in range(0, len(clean_skus), batch_size):
        batch = clean_skus[start:start + batch_size]
        response, current_token = await request_with_retry(
            client,
            current_token,
            PRODUCT_DETAIL_API,
            {"skus": batch},
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
        )
        if str(response.get("code")) != "0":
            raise RuntimeError(f"读取领星实时产品详情失败：{response}")
        for product in response.get("data") or []:
            sku = extract_sku(product)
            if sku:
                result_map[sku] = product
        if delay_seconds > 0 and start + batch_size < len(clean_skus):
            await asyncio.sleep(delay_seconds)
    return result_map, current_token


def build_guarded_product_set_body(
    live_product: dict[str, Any],
    *,
    sku: str,
    target_category_id: int | None = None,
    target_category_name: str | None = None,
    target_custom_fields: list[dict[str, str]] | None = None,
    preserve_current_category: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """使用实时详情构造安全 product/set 请求。"""
    expected_sku = clean(sku)
    live_sku = extract_sku(live_product)
    if not live_sku or live_sku != expected_sku:
        raise RuntimeError(f"实时详情SKU不一致：expected={expected_sku}, actual={live_sku}")

    live_name = extract_product_name(live_product)
    if not live_name:
        raise RuntimeError(f"SKU={expected_sku} 实时品名为空，禁止写入")

    current_category_id, current_category_name = extract_category(live_product)
    existing_fields = normalize_custom_fields(extract_custom_fields(live_product))
    merged_fields = (
        merge_custom_fields(existing_fields, target_custom_fields)
        if target_custom_fields is not None
        else existing_fields
    )

    body: dict[str, Any] = {
        "sku": expected_sku,
        "product_name": live_name,
    }
    if target_category_id not in (None, 0):
        body["category_id"] = int(target_category_id)
        body["category"] = clean(target_category_name)
    elif preserve_current_category and current_category_id:
        body["category_id"] = int(current_category_id)
        body["category"] = current_category_name

    if merged_fields:
        body["custom_fields"] = merged_fields

    guard_meta = {
        "live_product_name": live_name,
        "live_category_id": current_category_id,
        "live_category_name": current_category_name,
        "live_custom_field_count": len(existing_fields),
        "write_custom_field_count": len(merged_fields),
    }
    return body, guard_meta


def verify_product_name(live_product: dict[str, Any] | None, expected_name: str) -> tuple[bool, str]:
    actual_name = extract_product_name(live_product)
    expected = clean(expected_name)
    if not actual_name:
        return False, "复查品名为空"
    if actual_name != expected:
        return False, f"品名被改变：before={expected!r}, after={actual_name!r}"
    return True, ""
