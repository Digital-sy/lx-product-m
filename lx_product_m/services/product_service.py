#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import Any

from ..config import settings
from ..db import Database, json_dumps
from ..lingxing_client import LingxingClient
from ..sku import extract_spu
from .category_service import CategoryService

PRODUCT_DETAIL_API = "/erp/sc/routing/data/local_inventory/batchGetProductInfo"
PRODUCT_LIST_API = "/erp/sc/routing/data/local_inventory/productList"
PRODUCT_SET_API = "/erp/sc/routing/storage/product/set"

SNAPSHOT_REPLACE_SQL = """
REPLACE INTO `lxpm_product_category_snapshot`
(`sku`, `product_name`, `category_id`, `category_name`, `category_path`, `spu`,
 `custom_fields_json`, `raw_json`, `last_api_code`, `last_api_message`, `synced_at`)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
"""


class ProductService:
    def __init__(self, client: LingxingClient, db: Database) -> None:
        self.client = client
        self.db = db
        self.category_service = CategoryService(client, db)
        self._category_cache: dict[int, dict[str, Any]] | None = None

    @staticmethod
    def _success(result: dict[str, Any]) -> bool:
        return str(result.get("code")) == "0"

    def _get_category_cached(self, cid: int) -> dict[str, Any] | None:
        if self._category_cache is None:
            rows = self.db.fetch_all("SELECT * FROM `lxpm_category`")
            cache: dict[int, dict[str, Any]] = {}
            for row in rows:
                try:
                    cache[int(row.get("cid"))] = row
                except Exception:
                    continue
            self._category_cache = cache
        return self._category_cache.get(cid)

    async def fetch_product_list(
        self,
        token: str,
        page_size: int = 1000,
        max_pages: int = 0,
    ) -> list[dict[str, Any]]:
        """分页读取领星本地产品列表。"""
        rows: list[dict[str, Any]] = []
        offset = 0
        page_no = 0
        page_size = max(1, min(page_size, 1000))
        while True:
            page_no += 1
            body = {"offset": offset, "length": page_size}
            result = await self.client.request(token, PRODUCT_LIST_API, "POST", req_body=body)
            if not self._success(result):
                raise RuntimeError(f"查询产品列表失败：{result}")
            items = result.get("data") or []
            if not items:
                break
            rows.extend(items)
            print(f"产品列表同步中：page={page_no}, 本页={len(items)}, 累计={len(rows)}")
            if len(items) < page_size:
                break
            if max_pages and page_no >= max_pages:
                break
            offset += page_size
            if settings.collection_delay_seconds > 0:
                await asyncio.sleep(settings.collection_delay_seconds)
        return rows

    async def batch_get_product_info(self, token: str, skus: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        clean_skus = [x.strip() for x in skus if x and x.strip()]
        for i in range(0, len(clean_skus), 100):
            batch = clean_skus[i:i + 100]
            if not batch:
                continue
            result = await self.client.request(
                token, PRODUCT_DETAIL_API, "POST", req_body={"skus": batch}
            )
            if not self._success(result):
                for sku in batch:
                    self.save_product_snapshot_from_error(sku, result)
                raise RuntimeError(f"查询产品详情失败：{result}")
            rows.extend(result.get("data") or [])
            if settings.collection_delay_seconds > 0 and i + 100 < len(clean_skus):
                await asyncio.sleep(settings.collection_delay_seconds)
        return rows

    def extract_custom_fields(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("custom_fields", "custom_field_list", "customFields", "customFieldList"):
            value = product.get(key)
            if isinstance(value, list):
                return value
        return []

    def extract_category(self, product: dict[str, Any]) -> tuple[int | None, str]:
        raw_id = (
            product.get("category_id")
            or product.get("categoryId")
            or product.get("cid")
            or product.get("category_cid")
        )
        cid: int | None = None
        try:
            if raw_id not in (None, ""):
                cid = int(raw_id)
        except (TypeError, ValueError):
            cid = None
        title = str(
            product.get("category")
            or product.get("category_name")
            or product.get("categoryName")
            or ""
        ).strip()
        return cid, title

    def build_product_snapshot_params(self, product: dict[str, Any]) -> tuple[Any, ...] | None:
        sku = str(product.get("sku") or product.get("SKU") or "").strip()
        if not sku:
            return None
        product_name = str(product.get("product_name") or product.get("productName") or "").strip()
        cid, title = self.extract_category(product)
        path = ""
        if cid:
            cat = self._get_category_cached(cid)
            if cat:
                title = title or str(cat.get("title") or "")
                path = str(cat.get("full_path") or "")
        return (
            sku,
            product_name,
            cid,
            title,
            path,
            extract_spu(sku),
            json_dumps(self.extract_custom_fields(product)),
            json_dumps(product),
            0,
            "success",
        )

    def save_product_snapshot(self, product: dict[str, Any]) -> None:
        params = self.build_product_snapshot_params(product)
        if not params:
            return
        self.db.execute(SNAPSHOT_REPLACE_SQL, params)

    def save_product_snapshots(self, products: list[dict[str, Any]], batch_size: int = 1000) -> int:
        params = [p for p in (self.build_product_snapshot_params(product) for product in products) if p]
        if not params:
            return 0
        return self.db.executemany(SNAPSHOT_REPLACE_SQL, params, batch_size=batch_size)

    def save_product_snapshot_from_error(self, sku: str, result: dict[str, Any]) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                """
                REPLACE INTO `lxpm_product_category_snapshot`
                (`sku`, `last_api_code`, `last_api_message`, `raw_json`, `synced_at`)
                VALUES (%s,%s,%s,%s,NOW())
                """,
                (sku, result.get("code"), str(result.get("message") or result.get("msg") or "")[:500], json_dumps(result)),
            )

    async def query_and_save(self, token: str, skus: list[str]) -> list[dict[str, Any]]:
        products = await self.batch_get_product_info(token, skus)
        self.save_product_snapshots(products)
        return products

    async def sync_product_list_snapshot(self, token: str, page_size: int = 1000, max_pages: int = 0) -> int:
        products = await self.fetch_product_list(token, page_size=page_size, max_pages=max_pages)
        self.save_product_snapshots(products)
        return len(products)

    def resolve_target_category(self, category_id: int | None = None, category_name: str | None = None) -> dict[str, Any]:
        if category_id:
            cat = self.category_service.get_category_by_id(category_id)
            if not cat:
                raise RuntimeError(f"目标分类ID不存在：{category_id}，请先同步分类列表")
            return cat
        if category_name:
            cat = self.category_service.get_category_by_title(category_name)
            if not cat:
                raise RuntimeError(f"目标分类名称不存在：{category_name}，请先同步分类列表")
            return cat
        raise RuntimeError("必须提供 category_id 或 category_name")

    async def update_product_category(
        self,
        token: str,
        sku: str,
        category_id: int | None = None,
        category_name: str | None = None,
        batch_no: str = "manual",
        task_id: int | None = None,
    ) -> dict[str, Any]:
        target = self.resolve_target_category(category_id, category_name)
        new_id = int(target["cid"])
        new_name = str(target["title"])
        products = await self.batch_get_product_info(token, [sku])
        if not products:
            raise RuntimeError(f"SKU 不存在或详情为空：{sku}")
        product = products[0]
        self.save_product_snapshot(product)
        product_name = str(product.get("product_name") or product.get("productName") or "").strip()
        if not product_name:
            raise RuntimeError(f"SKU={sku} 未返回 product_name，已停止")
        old_id, old_name = self.extract_category(product)
        body = {"sku": sku, "product_name": product_name, "category_id": new_id, "category": new_name}
        response = await self.client.request(token, PRODUCT_SET_API, "POST", req_body=body)
        if not self._success(response):
            self._write_change_log(batch_no, task_id, sku, product_name, old_id, old_name, new_id, new_name, None, "", "failed", body, response, None, str(response))
            raise RuntimeError(f"产品分类写入失败：{response}")
        verify_products = await self.batch_get_product_info(token, [sku])
        verify_id = None
        verify_name = ""
        if verify_products:
            self.save_product_snapshot(verify_products[0])
            verify_id, verify_name = self.extract_category(verify_products[0])
        ok = verify_id == new_id or verify_name == new_name
        status = "success" if ok else "verify_failed"
        err = "" if ok else "写入接口成功，但复查未命中目标分类"
        self._write_change_log(batch_no, task_id, sku, product_name, old_id, old_name, new_id, new_name, verify_id, verify_name, status, body, response, {"data": verify_products}, err)
        if not ok:
            raise RuntimeError(err)
        return {
            "sku": sku,
            "old_category_id": old_id,
            "old_category_name": old_name,
            "new_category_id": new_id,
            "new_category_name": new_name,
            "verify_category_id": verify_id,
            "verify_category_name": verify_name,
        }

    def _write_change_log(
        self,
        batch_no: str,
        task_id: int | None,
        sku: str,
        product_name: str,
        old_id: int | None,
        old_name: str,
        new_id: int | None,
        new_name: str,
        verify_id: int | None,
        verify_name: str,
        status: str,
        req: dict[str, Any] | None,
        resp: dict[str, Any] | None,
        verify_resp: dict[str, Any] | None,
        err: str,
    ) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO `lxpm_product_category_change_log`
                (`batch_no`, `task_id`, `sku`, `product_name`,
                 `old_category_id`, `old_category_name`, `new_category_id`, `new_category_name`,
                 `verify_category_id`, `verify_category_name`, `status`,
                 `request_json`, `response_json`, `verify_response_json`, `error_message`)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (batch_no, task_id, sku, product_name, old_id, old_name, new_id, new_name, verify_id, verify_name, status, json_dumps(req), json_dumps(resp), json_dumps(verify_resp), err),
            )
