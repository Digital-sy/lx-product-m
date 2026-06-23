#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领星产品分类服务。"""
from __future__ import annotations

import asyncio
from typing import Any

from ..config import settings
from ..db import Database, json_dumps
from ..lingxing_client import LingxingClient

CATEGORY_LIST_API = "/erp/sc/routing/data/local_inventory/category"
CATEGORY_SET_API = "/erp/sc/routing/storage/category/set"


class CategoryService:
    def __init__(self, client: LingxingClient, db: Database) -> None:
        self.client = client
        self.db = db

    @staticmethod
    def _success(result: dict[str, Any]) -> bool:
        return str(result.get("code")) == "0"

    async def fetch_categories(
        self,
        token: str,
        ids: list[int] | None = None,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        """分页读取分类列表。"""
        all_rows: list[dict[str, Any]] = []
        offset = 0
        page_size = min(page_size, 1000)

        while True:
            body: dict[str, Any] = {"offset": offset, "length": page_size}
            if ids:
                body["ids"] = ids

            result = await self.client.request(token, CATEGORY_LIST_API, "POST", req_body=body)
            if not self._success(result):
                raise RuntimeError(f"查询分类列表失败：{result}")

            rows = result.get("data") or []
            total = int(result.get("total") or 0)
            all_rows.extend(rows)

            if ids:
                break
            if not rows or len(rows) < page_size:
                break
            offset += page_size
            if total and offset >= total:
                break
            await asyncio.sleep(settings.collection_delay_seconds)

        return all_rows

    def save_categories(self, rows: list[dict[str, Any]]) -> int:
        """保存分类列表，并计算 full_path / level_no / is_leaf。"""
        if not rows:
            return 0

        normalized: dict[int, dict[str, Any]] = {}
        for item in rows:
            cid = int(item.get("cid") or item.get("id") or 0)
            if cid <= 0:
                continue
            parent_cid = int(item.get("parent_cid") or 0)
            normalized[cid] = {
                "cid": cid,
                "parent_cid": parent_cid,
                "title": str(item.get("title") or "").strip(),
                "category_code": str(item.get("category_code") or "").strip(),
                "raw_json": item,
            }

        child_parent_set = {row["parent_cid"] for row in normalized.values() if row["parent_cid"]}

        def build_path(cid: int, seen: set[int] | None = None) -> tuple[str, int]:
            seen = seen or set()
            if cid in seen:
                return normalized[cid]["title"], 1
            seen.add(cid)
            row = normalized[cid]
            parent = row["parent_cid"]
            if not parent or parent not in normalized:
                return row["title"], 1
            parent_path, parent_level = build_path(parent, seen)
            return f"{parent_path}/{row['title']}", parent_level + 1

        with self.db.cursor() as cur:
            for cid, row in normalized.items():
                full_path, level_no = build_path(cid)
                is_leaf = 0 if cid in child_parent_set else 1
                cur.execute(
                    """
                    REPLACE INTO `lxpm_category`
                    (`cid`, `parent_cid`, `title`, `category_code`, `full_path`, `level_no`,
                     `is_leaf`, `raw_json`, `synced_at`)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    """,
                    (
                        row["cid"],
                        row["parent_cid"],
                        row["title"],
                        row["category_code"],
                        full_path,
                        level_no,
                        is_leaf,
                        json_dumps(row["raw_json"]),
                    ),
                )
        return len(normalized)

    def get_category_by_id(self, cid: int) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM `lxpm_category` WHERE `cid`=%s", (cid,))

    def get_category_by_title(self, title: str) -> dict[str, Any] | None:
        rows = self.db.fetch_all("SELECT * FROM `lxpm_category` WHERE `title`=%s", (title,))
        if not rows:
            return None
        if len(rows) > 1:
            paths = "; ".join(str(row.get("full_path") or row.get("title")) for row in rows[:10])
            raise RuntimeError(f"分类名称不唯一：{title}，请改用 category_id。候选：{paths}")
        return rows[0]

    async def upsert_category(
        self,
        token: str,
        title: str,
        category_code: str,
        parent_cid: int = 0,
        cid: int | None = None,
    ) -> dict[str, Any]:
        """新增或编辑分类。cid 为空时新增，不为空时编辑。"""
        item: dict[str, Any] = {
            "parent_cid": parent_cid,
            "title": title,
            "category_code": category_code,
        }
        action_type = "create"
        if cid:
            item["id"] = cid
            action_type = "update"

        # 实测此接口必须使用 data=数组；data=对象会进入内部错误。
        body = {"data": [item]}
        result = await self.client.request(token, CATEGORY_SET_API, "POST", req_body=body)
        status = "success" if self._success(result) else "failed"
        new_cid = cid
        data = result.get("data") or []
        if isinstance(data, dict):
            new_cid = int(data.get("id") or data.get("cid") or new_cid or 0) or None
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            new_cid = int(data[0].get("id") or data[0].get("cid") or new_cid or 0) or None

        with self.db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO `lxpm_category_change_log`
                (`action_type`, `cid`, `parent_cid`, `title`, `category_code`, `status`,
                 `request_json`, `response_json`, `error_message`)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    action_type,
                    new_cid,
                    parent_cid,
                    title,
                    category_code,
                    status,
                    json_dumps(body),
                    json_dumps(result),
                    "" if status == "success" else str(result.get("error_details") or result.get("message") or result.get("msg") or ""),
                ),
            )

        if status != "success":
            raise RuntimeError(f"分类{action_type}失败：{result}")
        return result
