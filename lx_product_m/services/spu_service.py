#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领星 SPU（多属性产品）服务。"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from ..config import settings
from ..db import Database, json_dumps
from ..lingxing_client import LingxingClient
from .product_write_guard import (
    extract_product_name,
    fetch_live_products,
    request_with_retry,
    verify_product_name,
)

SPU_LIST_API = "/erp/sc/routing/storage/spu/spuList"
SPU_INFO_API = "/erp/sc/routing/storage/spu/info"
SPU_SET_API = "/erp/sc/routing/storage/spu/set"
PRODUCT_DETAIL_API = "/erp/sc/routing/data/local_inventory/batchGetProductInfo"

SPU_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_ALREADY_BOUND_RE = re.compile(r"SKU-([^\s]+?)当前已经关联")
_CARRY_OVER_FIELDS = ("spu_name", "model", "unit", "status", "cid", "bid", "description")


class SpuService:
    def __init__(self, client: LingxingClient, db: Database) -> None:
        self.client = client
        self.db = db
        self._current_token: str | None = None

    @staticmethod
    def _success(result: dict[str, Any]) -> bool:
        return str(result.get("code")) == "0"

    @staticmethod
    def _is_spu_not_found(result: dict[str, Any]) -> bool:
        text = " ".join(str(result.get(key) or "") for key in ("message", "msg", "error_details")).lower()
        return (
            "未找到该spu" in text
            or "未找到该 spu" in text
            or "未找到spu" in text
            or "未找到" in text and "spu" in text
            or "spu不存在" in text
            or "spu 不存在" in text
            or "not found" in text
        )

    @staticmethod
    def validate_spu_name(spu: str) -> str:
        value = (spu or "").strip()
        if not value:
            raise RuntimeError("SPU 名称为空")
        if not SPU_NAME_PATTERN.match(value):
            raise RuntimeError(f"SPU 名称不合法（仅允许数字/字母/横杠/下划线）：{value!r}")
        return value

    @staticmethod
    def normalize_attribute(attribute: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if not attribute:
            return []
        out: list[dict[str, Any]] = []
        for item in attribute:
            if not isinstance(item, dict):
                continue
            pa_id = item.get("pa_id")
            pai_id = item.get("pai_id")
            if pa_id in (None, "") or pai_id in (None, ""):
                continue
            out.append({"pa_id": pa_id, "pai_id": pai_id})
        return out

    def _token(self, token: str) -> str:
        return self._current_token or token

    async def sku_exists(self, token: str, sku: str) -> bool:
        products, current_token = await fetch_live_products(
            self.client,
            self._token(token),
            [sku],
            max_retries=5,
            batch_size=100,
            delay_seconds=0,
        )
        self._current_token = current_token
        return sku in products

    async def batch_sku_exists(self, token: str, skus: list[str]) -> set[str]:
        products, current_token = await fetch_live_products(
            self.client,
            self._token(token),
            skus,
            max_retries=5,
            batch_size=100,
            delay_seconds=max(settings.collection_delay_seconds, 0.5),
        )
        self._current_token = current_token
        return set(products)

    async def fetch_spu_list(self, token: str, page_size: int = 200) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        page_size = max(1, min(page_size, 200))
        current_token = self._token(token)
        while True:
            body = {"offset": offset, "length": page_size}
            result, current_token = await request_with_retry(
                self.client,
                current_token,
                SPU_LIST_API,
                body,
                max_retries=5,
            )
            self._current_token = current_token
            if not self._success(result):
                raise RuntimeError(f"查询多属性产品列表失败：{result}")
            items = result.get("data") or []
            total = int(result.get("total") or 0)
            rows.extend(items)
            if not items or len(items) < page_size:
                break
            offset += page_size
            if total and offset >= total:
                break
            await asyncio.sleep(max(settings.collection_delay_seconds, 1.0))
        return rows

    async def get_spu_detail(self, token: str, spu: str | None = None, ps_id: int | None = None) -> dict[str, Any] | None:
        if not spu and not ps_id:
            raise RuntimeError("get_spu_detail 需要提供 spu 或 ps_id")
        body: dict[str, Any] = {}
        if ps_id:
            body["ps_id"] = ps_id
        if spu:
            body["spu"] = spu
        result, current_token = await request_with_retry(
            self.client,
            self._token(token),
            SPU_INFO_API,
            body,
            max_retries=5,
        )
        self._current_token = current_token
        if not self._success(result):
            if self._is_spu_not_found(result):
                return None
            raise RuntimeError(f"查询多属性产品详情失败：{result}")
        data = result.get("data")
        return data if data else None

    async def ensure_spu_and_bind_sku(
        self,
        token: str,
        spu: str,
        sku: str,
        spu_name: str | None = None,
        product_name: str | None = None,
        sku_attribute: list[dict[str, Any]] | None = None,
        allow_create_sku: bool = False,
        batch_no: str = "manual",
        task_id: int | None = None,
    ) -> dict[str, Any]:
        result = await self.ensure_spu_and_bind_skus(
            token,
            spu=spu,
            sku_rows=[{"sku": sku, "product_name": product_name or ""}],
            spu_name=spu_name,
            sku_attribute=sku_attribute,
            allow_create_sku=allow_create_sku,
            batch_no=batch_no,
            task_id=task_id,
        )
        status = "success"
        if sku in result.get("skipped", []):
            status = "skipped"
        elif sku in result.get("blocked", []):
            raise RuntimeError(f"SKU 在领星不存在或实时品名为空：{sku}")
        elif sku in result.get("failed", []):
            raise RuntimeError(f"SPU 绑定失败：{sku} -> {spu}")
        elif sku in result.get("verify_failed", []):
            raise RuntimeError(f"SPU 写入接口成功，但复查未发现 SKU 关联：{sku}")
        elif sku in result.get("name_verify_failed", []):
            raise RuntimeError(f"SPU 写入后品名复查失败：{sku}")
        elif sku in result.get("skipped_already_bound", []):
            status = "skipped_already_bound"
        return {"sku": sku, "spu": spu, "status": status, "spu_sku_count": result.get("spu_sku_count", 0)}

    async def ensure_spu_and_bind_skus(
        self,
        token: str,
        spu: str,
        sku_rows: list[dict[str, Any]],
        spu_name: str | None = None,
        sku_attribute: list[dict[str, Any]] | None = None,
        allow_create_sku: bool = False,
        batch_no: str = "manual",
        task_id: int | None = None,
    ) -> dict[str, Any]:
        """同一个 SPU 下多 SKU 合并绑定，并保护全部 SKU 当前品名。

        spu/set 会整体提交 sku_list，因此写入前会重新读取：
        - 本次新增 SKU 的实时品名；
        - SPU 下全部既有 SKU 的实时品名。
        任一既有 SKU 无法取得实时品名时，整组停止写入。
        """
        spu = self.validate_spu_name(spu)
        normalized_attribute = self.normalize_attribute(sku_attribute)
        summary: dict[str, Any] = {
            "success": [],
            "skipped": [],
            "blocked": [],
            "failed": [],
            "verify_failed": [],
            "name_verify_failed": [],
            "skipped_already_bound": [],
            "spu_sku_count": 0,
        }
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in sku_rows:
            sku = str(row.get("sku") or "").strip()
            if not sku or sku in seen:
                continue
            seen.add(sku)
            rows.append({"sku": sku, "product_name": str(row.get("product_name") or "").strip()})
        if not rows:
            return summary

        current_token = self._token(token)

        # 先读取本次候选 SKU 的实时详情，快照品名不参与写回。
        if not allow_create_sku:
            live_requested, current_token = await fetch_live_products(
                self.client,
                current_token,
                [row["sku"] for row in rows],
                max_retries=5,
                batch_size=100,
                delay_seconds=max(settings.collection_delay_seconds, 0.5),
            )
            self._current_token = current_token
            active_rows: list[dict[str, Any]] = []
            for row in rows:
                product = live_requested.get(row["sku"])
                live_name = extract_product_name(product)
                if product and live_name:
                    row["product_name"] = live_name
                    active_rows.append(row)
                else:
                    err = f"SKU实时详情不存在或实时品名为空，禁止SPU写入：{row['sku']}"
                    self._write_change_log(batch_no, task_id, row["sku"], spu, "blocked", None, None, None, err)
                    summary["blocked"].append(row["sku"])
            rows = active_rows
            if not rows:
                return summary

        detail = await self.get_spu_detail(current_token, spu=spu)
        current_token = self._token(current_token)
        existing_entries = self._extract_sku_entries(detail)
        existing_set = {entry["sku"] for entry in existing_entries}
        pending_rows: list[dict[str, Any]] = []
        for row in rows:
            if row["sku"] in existing_set:
                self._write_change_log(batch_no, task_id, row["sku"], spu, "skipped", None, None, None, "SKU 已在该 SPU 下，跳过")
                summary["skipped"].append(row["sku"])
            else:
                pending_rows.append(row)

        if not pending_rows:
            summary["spu_sku_count"] = len(existing_entries)
            return summary

        while pending_rows:
            # spu/set 是整组写入：必须保护既有和新增 SKU 的实时品名。
            protected_skus = [entry["sku"] for entry in existing_entries] + [row["sku"] for row in pending_rows]
            live_all, current_token = await fetch_live_products(
                self.client,
                current_token,
                protected_skus,
                max_retries=5,
                batch_size=100,
                delay_seconds=max(settings.collection_delay_seconds, 0.5),
            )
            self._current_token = current_token

            missing_existing = [
                entry["sku"]
                for entry in existing_entries
                if not extract_product_name(live_all.get(entry["sku"]))
            ]
            if missing_existing:
                err = (
                    "SPU现有SKU实时品名读取失败，为防止整体覆盖已停止写入："
                    + ",".join(missing_existing[:20])
                )
                for row in pending_rows:
                    self._write_change_log(batch_no, task_id, row["sku"], spu, "guard_blocked", None, None, None, err)
                    summary["failed"].append(row["sku"])
                return summary

            missing_pending = [
                row["sku"]
                for row in pending_rows
                if not extract_product_name(live_all.get(row["sku"]))
            ]
            if missing_pending:
                next_pending: list[dict[str, Any]] = []
                for row in pending_rows:
                    if row["sku"] in missing_pending:
                        err = "新增SKU实时品名为空，禁止SPU写入"
                        self._write_change_log(batch_no, task_id, row["sku"], spu, "blocked", None, None, None, err)
                        summary["blocked"].append(row["sku"])
                    else:
                        next_pending.append(row)
                pending_rows = next_pending
                if not pending_rows:
                    return summary
                continue

            prewrite_names = {
                sku: extract_product_name(product)
                for sku, product in live_all.items()
                if extract_product_name(product)
            }

            safe_existing_entries: list[dict[str, Any]] = []
            for entry in existing_entries:
                safe_entry = dict(entry)
                safe_entry["product_name"] = prewrite_names[entry["sku"]]
                safe_existing_entries.append(safe_entry)

            new_entries: list[dict[str, Any]] = []
            for row in pending_rows:
                row["product_name"] = prewrite_names[row["sku"]]
                new_entries.append(
                    {
                        "sku": row["sku"],
                        "product_name": row["product_name"],
                        "attribute": normalized_attribute,
                    }
                )

            body: dict[str, Any] = {
                "spu": spu,
                "spu_name": (spu_name or spu),
                "sku_list": safe_existing_entries + new_entries,
            }
            if detail:
                for field in _CARRY_OVER_FIELDS:
                    value = detail.get(field)
                    if field == "spu_name":
                        body["spu_name"] = str(value or spu_name or spu)
                    elif value not in (None, "", 0):
                        body[field] = value

            log_body = dict(body)
            log_body["_write_guard"] = {
                "product_name_source": "live_product_detail",
                "protected_sku_count": len(prewrite_names),
                "protected_existing_sku_count": len(safe_existing_entries),
                "protected_new_sku_count": len(new_entries),
            }

            response, current_token = await request_with_retry(
                self.client,
                current_token,
                SPU_SET_API,
                body,
                max_retries=5,
            )
            self._current_token = current_token
            if not self._success(response) and not normalized_attribute and self._looks_like_attribute_error(response):
                for entry in body["sku_list"]:
                    if not entry.get("attribute"):
                        entry["attribute"] = [{"pa_id": "", "pai_id": ""}]
                response, current_token = await request_with_retry(
                    self.client,
                    current_token,
                    SPU_SET_API,
                    body,
                    max_retries=5,
                )
                self._current_token = current_token

            if self._success(response):
                break

            already_bound_sku = self._extract_already_bound_sku(response)
            if already_bound_sku:
                next_pending = []
                removed = False
                for row in pending_rows:
                    if row["sku"] == already_bound_sku:
                        self._write_change_log(
                            batch_no,
                            task_id,
                            row["sku"],
                            spu,
                            "skipped_already_bound",
                            log_body,
                            response,
                            None,
                            str(response),
                        )
                        summary["skipped_already_bound"].append(row["sku"])
                        removed = True
                    else:
                        next_pending.append(row)
                pending_rows = next_pending
                if removed and pending_rows:
                    continue
                if removed and not pending_rows:
                    return summary

            for row in pending_rows:
                self._write_change_log(batch_no, task_id, row["sku"], spu, "failed", log_body, response, None, str(response))
                summary["failed"].append(row["sku"])
            return summary

        if settings.collection_delay_seconds > 0:
            await asyncio.sleep(settings.collection_delay_seconds)

        verify = await self.get_spu_detail(current_token, spu=spu)
        current_token = self._token(current_token)
        verify_entries = self._extract_sku_entries(verify)
        verify_skus = {entry["sku"] for entry in verify_entries}
        summary["spu_sku_count"] = len(verify_skus)

        # 复查所有参与整组提交的 SKU 品名是否保持不变。
        post_products, current_token = await fetch_live_products(
            self.client,
            current_token,
            list(prewrite_names),
            max_retries=5,
            batch_size=100,
            delay_seconds=max(settings.collection_delay_seconds, 0.5),
        )
        self._current_token = current_token
        changed_names: dict[str, str] = {}
        for sku, expected_name in prewrite_names.items():
            name_ok, name_error = verify_product_name(post_products.get(sku), expected_name)
            if not name_ok:
                changed_names[sku] = name_error

        # 既有 SKU 也属于本次整组提交范围，检测到品名变化必须记录。
        pending_skus = {row["sku"] for row in pending_rows}
        for sku, error in changed_names.items():
            if sku not in pending_skus:
                self._write_change_log(
                    batch_no,
                    task_id,
                    sku,
                    spu,
                    "name_verify_failed",
                    {"_write_guard": {"expected_product_name": prewrite_names.get(sku)}},
                    response,
                    {"data": [post_products.get(sku)] if post_products.get(sku) else []},
                    error,
                )
                summary["name_verify_failed"].append(sku)

        for row in pending_rows:
            sku = row["sku"]
            if sku in changed_names:
                status = "name_verify_failed"
                error = changed_names[sku]
            elif sku not in verify_skus:
                status = "verify_failed"
                error = "写入接口成功，但复查未发现 SKU 关联"
            else:
                status = "success"
                error = ""
            self._write_change_log(
                batch_no,
                task_id,
                sku,
                spu,
                status,
                {"_write_guard": {"expected_product_name": prewrite_names.get(sku)}},
                response,
                verify,
                error,
            )
            summary[status].append(sku)
        return summary

    async def bind_batch(
        self,
        token: str,
        pairs: list[tuple[str, str]],
        batch_no: str,
        sku_attribute: list[dict[str, Any]] | None = None,
    ) -> dict[str, list[str]]:
        summary: dict[str, list[str]] = {"success": [], "skipped": [], "failed": []}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for sku, spu in pairs:
            grouped.setdefault(spu, []).append({"sku": sku, "product_name": ""})
        for spu, rows in grouped.items():
            try:
                result = await self.ensure_spu_and_bind_skus(
                    token,
                    spu=spu,
                    sku_rows=rows,
                    batch_no=batch_no,
                    sku_attribute=sku_attribute,
                )
                for status, skus in result.items():
                    if not isinstance(skus, list):
                        continue
                    if status == "success":
                        summary["success"].extend(skus)
                    elif status == "skipped":
                        summary["skipped"].extend(skus)
                    elif status in ("failed", "blocked", "verify_failed", "name_verify_failed"):
                        summary["failed"].extend(skus)
            except Exception as exc:
                print(f"[FAILED] spu={spu}: {exc}")
                summary["failed"].extend([str(row.get("sku") or "") for row in rows])
            if settings.collection_delay_seconds > 0:
                await asyncio.sleep(settings.collection_delay_seconds)
        return summary

    @staticmethod
    def _extract_sku_entries(detail: dict[str, Any] | None) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for item in (detail or {}).get("sku_list") or []:
            sku = str(item.get("sku") or "").strip()
            if not sku:
                continue
            attribute = []
            for attr in item.get("attribute") or []:
                if isinstance(attr, dict):
                    pa_id = attr.get("pa_id")
                    pai_id = attr.get("pai_id")
                    if pa_id not in (None, "") and pai_id not in (None, ""):
                        attribute.append({"pa_id": pa_id, "pai_id": pai_id})
            entry: dict[str, Any] = {"sku": sku, "attribute": attribute}
            product_name = str(item.get("product_name") or item.get("productName") or "").strip()
            if product_name:
                entry["product_name"] = product_name
            entries.append(entry)
        return entries

    @staticmethod
    def _looks_like_attribute_error(result: dict[str, Any]) -> bool:
        text = str(result.get("message") or "") + str(result.get("error_details") or "")
        return ("attribute" in text.lower()) or ("属性" in text)

    @staticmethod
    def _extract_already_bound_sku(result: dict[str, Any]) -> str:
        text = " ".join(str(result.get(key) or "") for key in ("message", "msg", "error_details"))
        match = _ALREADY_BOUND_RE.search(text)
        return match.group(1).strip() if match else ""

    def _write_change_log(
        self,
        batch_no: str,
        task_id: int | None,
        sku: str,
        spu: str,
        status: str,
        req: dict[str, Any] | None,
        resp: dict[str, Any] | None,
        verify_resp: dict[str, Any] | None,
        err: str,
    ) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO `lxpm_spu_change_log`
                (`batch_no`, `task_id`, `sku`, `spu`, `status`,
                 `request_json`, `response_json`, `verify_response_json`, `error_message`)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (batch_no, task_id, sku, spu, status, json_dumps(req), json_dumps(resp), json_dumps(verify_resp), err[:1000]),
            )


SPU_CHANGE_LOG_DDL = """
CREATE TABLE IF NOT EXISTS `lxpm_spu_change_log` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `batch_no` VARCHAR(64) NOT NULL DEFAULT 'manual',
  `task_id` BIGINT UNSIGNED NULL,
  `sku` VARCHAR(128) NOT NULL,
  `spu` VARCHAR(128) NOT NULL,
  `status` VARCHAR(32) NOT NULL,
  `request_json` JSON NULL,
  `response_json` JSON NULL,
  `verify_response_json` JSON NULL,
  `error_message` VARCHAR(1000) NOT NULL DEFAULT '',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_sku` (`sku`),
  KEY `idx_spu` (`spu`),
  KEY `idx_batch` (`batch_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""
