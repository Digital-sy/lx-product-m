#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书多维表只读客户端。"""
from __future__ import annotations

from typing import Any

import httpx

from .config import settings


class FeishuBitableClient:
    def __init__(self, app_token: str, table_id: str, view_id: str | None = None) -> None:
        settings.validate_feishu()
        self.app_id = settings.feishu_app_id
        self.app_secret = settings.feishu_app_secret
        self.api_base = settings.feishu_api_base
        self.app_token = app_token
        self.table_id = table_id
        self.view_id = view_id
        self._tenant_token: str | None = None
        self.timeout = httpx.Timeout(settings.api_timeout_seconds, connect=15)
        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self.timeout)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    async def get_tenant_token(self) -> str:
        if self._tenant_token:
            return self._tenant_token
        url = f"{self.api_base}/auth/v3/tenant_access_token/internal"
        body = {"app_id": self.app_id, "app_secret": self.app_secret}
        client = await self._client()
        resp = await client.post(url, json=body, headers={"Content-Type": "application/json; charset=utf-8"})
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"获取飞书 tenant_access_token 失败：{result}")
        self._tenant_token = result.get("tenant_access_token")
        if not self._tenant_token:
            raise RuntimeError(f"飞书 token 返回结构异常：{result}")
        return self._tenant_token

    async def _headers(self) -> dict[str, str]:
        token = await self.get_tenant_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def list_fields(self) -> list[dict[str, Any]]:
        url = f"{self.api_base}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields"
        client = await self._client()
        resp = await client.get(url, headers=await self._headers())
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"获取飞书字段失败：{result}")
        return result.get("data", {}).get("items", []) or []

    async def list_records(self, page_size: int = 10, max_records: int = 10) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page_token: str | None = None
        page_size = max(1, min(page_size, 500))
        client = await self._client()
        while True:
            url = f"{self.api_base}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"
            params: dict[str, Any] = {"page_size": page_size}
            if self.view_id:
                params["view_id"] = self.view_id
            if page_token:
                params["page_token"] = page_token
            resp = await client.get(url, headers=await self._headers(), params=params)
            result = resp.json()
            if result.get("code") != 0:
                raise RuntimeError(f"读取飞书记录失败：{result}")
            data = result.get("data", {}) or {}
            items = data.get("items") or []
            rows.extend(items)
            if len(rows) >= max_records:
                return rows[:max_records]
            if not data.get("has_more"):
                return rows
            page_token = data.get("page_token")
            if not page_token:
                return rows


def extract_feishu_text(value: Any) -> str:
    """把飞书各种字段值尽量转成可读文本，仅用于探测输出。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = extract_feishu_text(item)
            if text:
                parts.append(text)
        return ", ".join(parts)
    if isinstance(value, dict):
        for key in ("text", "name", "value", "email", "en_name"):
            if value.get(key):
                return str(value.get(key)).strip()
        if "link" in value and "text" in value:
            return str(value.get("text") or value.get("link") or "").strip()
        return str(value)
    return str(value).strip()
