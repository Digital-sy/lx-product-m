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

    async def sku_exists(self, token: str, sku: str) -> bool:
        result = await self.client.request(token, PRODUCT_DETAIL_API, "POST", req_body={"skus": [sku]})
        if not self._success(result):
            raise RuntimeError(f"校验 SKU 是否存在失败：{result}")
        for item in result.get("data") or []:
            if str(item.get("sku") or item.get("SKU") or "").strip() == sku:
                return True
        return False

    async def batch_sku_exists(self, token: str, skus: list[str]) -> set[str]:
        """批量校验 SKU 是否存在，batchGetProductInfo 一次最多按 100 个 SKU 请求。"""
        clean = []
        seen = set()
        for sku in skus:
            value = (sku or "").strip()
            if value and value not in seen:
                clean.append(value)
                seen.add(value)
        exists: set[str] = set()
        for i in range(0, len(clean), 100):
            batch = clean[i:i + 100]
            result = await self.client.request(token, PRODUCT_DETAIL_API, "POST", req_body={"skus": batch})
            if not self._success(result):
                raise RuntimeError(f"批量校验 SKU 是否存在失败：{result}")
            for item in result.get("data") or []:
                sku = str(item.get("sku") or item.get("SKU") or "").strip()
                if sku:
                    exists.add(sku)
            if settings.collection_delay_seconds > 0 and i + 100 < len(clean):
                await asyncio.sleep(settings.collection_delay_seconds)
        return exists

    async def fetch_spu_list(self, token: str, page_size: int = 200) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        page_size = max(1, min(page_size, 200))
        while True:
            body = {"offset": offset, "length": page_size}
            result = await self.client.request(token, SPU_LIST_API, "POST", req_body=body)
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
        result = await self.client.request(token, SPU_INFO_API, "POST", req_body=body)
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
            raise RuntimeError(f"SKU 在领星不存在：{sku}")
        elif sku in result.get("failed", []):
            raise RuntimeError(f"SPU 绑定失败：{sku} -> {spu}")
        elif sku in result.get("verify_failed", []):
            raise RuntimeError(f"SPU 写入接口成功，但复查未发现 SKU 关联：{sku}")
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
        """同一个 SPU 下多 SKU 合并绑定。

        将同 SPU 的多个 SKU 合成一次 spu/set，避免逐个 SKU 重复执行
        sku_exists -> spu/info -> spu/set -> verify。
        """
        spu = self.validate_spu_name(spu)
        normalized_attribute = self.normalize_attribute(sku_attribute)
        summary: dict[str, Any] = {
            "success": [],
            "skipped": [],
            "blocked": [],
            "failed": [],
            "verify_failed": [],
            "skipped_already_bound": [],
            "spu_sku_count": 0,
        }
        rows: list[dict[str, Any]] = []
        seen = set()
        for row in sku_rows:
            sku = str(row.get("sku") or "").strip()
            if not sku or sku in seen:
                continue
            seen.add(sku)
            rows.append({"sku": sku, "product_name": str(row.get("product_name") or "").strip()})
        if not rows:
            return summary

        if not allow_create_sku:
            existing_skus = await self.batch_sku_exists(token, [r["sku"] for r in rows])
            active_rows = []
            for row in rows:
                if row["sku"] in existing_skus:
                    active_rows.append(row)
                else:
                    err = f"SKU 在领星不存在：{row['sku']}"
                    self._write_change_log(batch_no, task_id, row["sku"], spu, "blocked", None, None, None, err)
                    summary["blocked"].append(row["sku"])
            rows = active_rows
            if not rows:
                return summary

        detail = await self.get_spu_detail(token, spu=spu)
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
            new_entries = []
            for row in pending_rows:
                entry: dict[str, Any] = {"sku": row["sku"], "attribute": normalized_attribute}
                if row.get("product_name"):
                    entry["product_name"] = row["product_name"]
                new_entries.append(entry)

            body: dict[str, Any] = {
                "spu": spu,
                "spu_name": (spu_name or spu),
                "sku_list": existing_entries + new_entries,
            }
            if detail:
                for field in _CARRY_OVER_FIELDS:
                    value = detail.get(field)
                    if field == "spu_name":
                        body["spu_name"] = str(value or spu_name or spu)
                    elif value not in (None, "", 0):
                        body[field] = value

            response = await self.client.request(token, SPU_SET_API, "POST", req_body=body)
            if not self._success(response) and not normalized_attribute and self._looks_like_attribute_error(response):
                for entry in body["sku_list"]:
                    if not entry.get("attribute"):
                        entry["attribute"] = [{"pa_id": "", "pai_id": ""}]
                response = await self.client.request(token, SPU_SET_API, "POST", req_body=body)

            if self._success(response):
                break

            already_bound_sku = self._extract_already_bound_sku(response)
            if already_bound_sku:
                removed = False
                next_pending = []
                for row in pending_rows:
                    if row["sku"] == already_bound_sku:
                        self._write_change_log(batch_no, task_id, row["sku"], spu, "skipped_already_bound", body, response, None, str(response))
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
                self._write_change_log(batch_no, task_id, row["sku"], spu, "failed", body, response, None, str(response))
                summary["failed"].append(row["sku"])
            return summary

        if settings.collection_delay_seconds > 0:
            await asyncio.sleep(settings.collection_delay_seconds)
        verify = await self.get_spu_detail(token, spu=spu)
        verify_entries = self._extract_sku_entries(verify)
        verify_skus = {entry["sku"] for entry in verify_entries}
        summary["spu_sku_count"] = len(verify_skus)
        for row in pending_rows:
            ok = row["sku"] in verify_skus
            status = "success" if ok else "verify_failed"
            err = "" if ok else "写入接口成功，但复查未发现 SKU 关联"
            self._write_change_log(batch_no, task_id, row["sku"], spu, status, body, response, verify, err)
            summary[status].append(row["sku"])
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
                    token, spu=spu, sku_rows=rows, batch_no=batch_no, sku_attribute=sku_attribute
                )
                for status, skus in result.items():
                    if not isinstance(skus, list):
                        continue
                    if status == "success":
                        summary["success"].extend(skus)
                    elif status == "skipped":
                        summary["skipped"].extend(skus)
                    elif status in ("failed", "blocked", "verify_failed"):
                        summary["failed"].extend(skus)
            except Exception as exc:
                print(f"[FAILED] spu={spu}: {exc}")
                summary["failed"].extend([str(r.get("sku") or "") for r in rows])
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
            entries.append({"sku": sku, "attribute": attribute})
        return entries

    @staticmethod
    def _looks_like_attribute_error(result: dict[str, Any]) -> bool:
        text = str(result.get("message") or "") + str(result.get("error_details") or "")
        return ("attribute" in text.lower()) or ("属性" in text)

    @staticmethod
    def _extract_already_bound_sku(result: dict[str, Any]) -> str:
        text = " ".join(str(result.get(key) or "") for key in ("message", "msg", "error_details"))
        m = _ALREADY_BOUND_RE.search(text)
        return m.group(1).strip() if m else ""

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
