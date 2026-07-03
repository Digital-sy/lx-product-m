#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领星 SPU（多属性产品）服务。

接口（来自官方开放接口文档）：
    查询多属性产品列表  POST /erp/sc/routing/storage/spu/spuList   （令牌桶 1，length 上限 200）
    查询多属性产品详情  POST /erp/sc/routing/storage/spu/info      （令牌桶 1，ps_id 与 spu 二选一）
    添加/编辑多属性产品 POST /erp/sc/routing/storage/spu/set       （令牌桶 5）

设计要点：
1. spu/set 的 sku_list 中"提交的 sku 不存在时系统会自动创建"——为防止把
   拼错的 SKU 凭空建成产品，绑定前默认先经 batchGetProductInfo 校验 SKU
   存在（allow_create_sku=False）。
2. 文档未说明编辑时 sku_list 是全量替换还是追加，按"全量替换"防御性处理：
   编辑时先读详情，合并已关联 SKU（保留其 attribute）后整体提交，并把
   spu_name/model/unit/status/cid/bid/description 原样带回，避免清空已维护信息。
3. sku_list>>attribute 标注必填，但官方示例传的是空值占位
   [{"pa_id": "", "pai_id": ""}]。先尝试 []，若报属性相关错误则自动降级
   为空值占位重试一次。
4. 流程与 CategoryService/ProductService 一致：校验 → 查询现状 → 写入 →
   复查 → 落变更日志（lxpm_spu_change_log）。
"""
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

# SPU 名称仅允许 数字、字母、横杠、下划线
SPU_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# 编辑时需要原样带回的 SPU 标量字段（防止被默认值覆盖）
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
        """领星 spu/info 对不存在 SPU 会返回 code=500 + error_details=未找到该spu。"""
        text = " ".join(
            str(result.get(key) or "")
            for key in ("message", "msg", "error_details")
        ).lower()
        return (
            "未找到该spu" in text
            or "未找到该 spu" in text
            or "未找到spu" in text
            or "spu不存在" in text
            or "spu 不存在" in text
            or "not found" in text
        )

    # ------------------------------------------------------------------ 校验

    @staticmethod
    def validate_spu_name(spu: str) -> str:
        value = (spu or "").strip()
        if not value:
            raise RuntimeError("SPU 名称为空")
        if not SPU_NAME_PATTERN.match(value):
            raise RuntimeError(f"SPU 名称不合法（仅允许数字/字母/横杠/下划线）：{value!r}")
        return value

    async def sku_exists(self, token: str, sku: str) -> bool:
        """校验 SKU 在领星是否已存在，防止 spu/set 把拼错的 SKU 自动建成产品。"""
        result = await self.client.request(
            token, PRODUCT_DETAIL_API, "POST", req_body={"skus": [sku]}
        )
        if not self._success(result):
            raise RuntimeError(f"校验 SKU 是否存在失败：{result}")
        for item in result.get("data") or []:
            if str(item.get("sku") or item.get("SKU") or "").strip() == sku:
                return True
        return False

    # ------------------------------------------------------------------ 读取

    async def fetch_spu_list(self, token: str, page_size: int = 200) -> list[dict[str, Any]]:
        """分页拉取多属性产品列表（length 上限 200，令牌桶 1，注意限速）。"""
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

    async def get_spu_detail(
        self,
        token: str,
        spu: str | None = None,
        ps_id: int | None = None,
    ) -> dict[str, Any] | None:
        """查询单个 SPU 详情。不存在时返回 None。"""
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

    # ------------------------------------------------------------------ 写入

    async def ensure_spu_and_bind_sku(
        self,
        token: str,
        spu: str,
        sku: str,
        spu_name: str | None = None,
        product_name: str | None = None,
        allow_create_sku: bool = False,
        batch_no: str = "manual",
        task_id: int | None = None,
    ) -> dict[str, Any]:
        """确保 SPU 存在并把 SKU 关联到其下，写入后复查。

        - SPU 不存在 → 新建（spu_name 缺省用 spu 本身）；
        - SPU 已存在 → 合并已有关联后整体提交（视为全量替换的防御性写法）；
        - SKU 已关联 → 跳过；
        - SKU 在领星不存在且 allow_create_sku=False → 报错拦截，防止误建产品。
        """
        spu = self.validate_spu_name(spu)
        sku = (sku or "").strip()
        if not sku:
            raise RuntimeError("SKU 为空")

        # 1) SKU 存在性校验（spu/set 会自动创建不存在的 SKU，必须拦）
        if not await self.sku_exists(token, sku):
            if not allow_create_sku:
                err = f"SKU 在领星不存在：{sku}（如需借 spu/set 自动创建请显式传 allow_create_sku=True 并提供 product_name）"
                self._write_change_log(batch_no, task_id, sku, spu, "blocked", None, None, None, err)
                raise RuntimeError(err)
            if not product_name:
                raise RuntimeError(f"SKU {sku} 不存在且未提供 product_name，无法自动创建")

        # 2) 查询 SPU 现状；不存在时 detail=None，后续走新建
        detail = await self.get_spu_detail(token, spu=spu)
        existing_entries = self._extract_sku_entries(detail)
        if any(entry["sku"] == sku for entry in existing_entries):
            self._write_change_log(batch_no, task_id, sku, spu, "skipped",
                                   None, None, None, "SKU 已在该 SPU 下，跳过")
            return {"sku": sku, "spu": spu, "ps_id": (detail or {}).get("ps_id"), "status": "skipped"}

        # 3) 构造 body：新 SKU + 已有关联（保留 attribute），编辑时带回标量字段
        new_entry: dict[str, Any] = {"sku": sku, "attribute": []}
        if product_name:
            new_entry["product_name"] = product_name
        body: dict[str, Any] = {
            "spu": spu,
            "spu_name": (spu_name or spu),
            "sku_list": existing_entries + [new_entry],
        }
        if detail:
            for field in _CARRY_OVER_FIELDS:
                value = detail.get(field)
                if field == "spu_name":
                    body["spu_name"] = str(value or spu_name or spu)
                elif value not in (None, "", 0):
                    body[field] = value

        # 4) 提交；attribute=[] 被拒时降级为空值占位重试一次
        response = await self.client.request(token, SPU_SET_API, "POST", req_body=body)
        if not self._success(response) and self._looks_like_attribute_error(response):
            for entry in body["sku_list"]:
                if not entry.get("attribute"):
                    entry["attribute"] = [{"pa_id": "", "pai_id": ""}]
            response = await self.client.request(token, SPU_SET_API, "POST", req_body=body)
        if not self._success(response):
            self._write_change_log(batch_no, task_id, sku, spu, "failed",
                                   body, response, None, str(response))
            raise RuntimeError(f"SPU 写入失败：{response}")

        # 5) 复查
        await asyncio.sleep(settings.collection_delay_seconds)
        verify = await self.get_spu_detail(token, spu=spu)
        verify_skus = [entry["sku"] for entry in self._extract_sku_entries(verify)]
        ok = sku in verify_skus
        status = "success" if ok else "verify_failed"
        err = "" if ok else "写入接口成功，但复查未发现 SKU 关联"
        self._write_change_log(batch_no, task_id, sku, spu, status, body, response, verify, err)
        if not ok:
            raise RuntimeError(err)
        return {
            "sku": sku,
            "spu": spu,
            "ps_id": (verify or {}).get("ps_id"),
            "status": status,
            "spu_sku_count": len(verify_skus),
        }

    async def bind_batch(
        self,
        token: str,
        pairs: list[tuple[str, str]],
        batch_no: str,
    ) -> dict[str, list[str]]:
        """批量绑定 [(sku, spu), ...]。逐条处理、单条失败不阻断整批。"""
        summary: dict[str, list[str]] = {"success": [], "skipped": [], "failed": []}
        for sku, spu in pairs:
            try:
                result = await self.ensure_spu_and_bind_sku(
                    token, spu=spu, sku=sku, batch_no=batch_no
                )
                summary["skipped" if result["status"] == "skipped" else "success"].append(sku)
            except Exception as exc:
                print(f"[FAILED] sku={sku} spu={spu}: {exc}")
                summary["failed"].append(sku)
            await asyncio.sleep(settings.collection_delay_seconds)
        return summary

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _extract_sku_entries(detail: dict[str, Any] | None) -> list[dict[str, Any]]:
        """从详情提取已关联 SKU 及其 attribute（剔除 pai_name 等只读字段）。"""
        entries: list[dict[str, Any]] = []
        for item in (detail or {}).get("sku_list") or []:
            sku = str(item.get("sku") or "").strip()
            if not sku:
                continue
            attribute = []
            for attr in item.get("attribute") or []:
                if isinstance(attr, dict):
                    attribute.append({"pa_id": attr.get("pa_id"), "pai_id": attr.get("pai_id")})
            entries.append({"sku": sku, "attribute": attribute})
        return entries

    @staticmethod
    def _looks_like_attribute_error(result: dict[str, Any]) -> bool:
        text = str(result.get("message") or "") + str(result.get("error_details") or "")
        return ("attribute" in text.lower()) or ("属性" in text)

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
                (batch_no, task_id, sku, spu, status,
                 json_dumps(req), json_dumps(resp), json_dumps(verify_resp), err[:1000]),
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
