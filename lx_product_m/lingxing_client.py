#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领星 OpenAPI 客户端。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings
from .db import Database, json_dumps
from .sign import generate_sign

TOKEN_PARAM = "access" + "_token"
REFRESH_PARAM = "refresh" + "_token"
APP_SECRET_FIELD = "app" + "Secret"
APP_ID_FIELD = "app" + "Id"


@dataclass
class AccessToken:
    token: str
    refresh_token: str | None = None
    expires_in: int | None = None


class LingxingClient:
    def __init__(self, db: Database | None = None, enable_api_log: bool = True) -> None:
        settings.validate_lingxing()
        self.host = settings.lingxing_host
        self.app_id = settings.lingxing_app_id
        self.app_secret = settings.lingxing_app_secret
        self.proxy_url = settings.lingxing_proxy_url or None
        self.timeout = httpx.Timeout(settings.api_timeout_seconds, connect=15)
        self.db = db
        self.enable_api_log = enable_api_log

    async def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        return httpx.AsyncClient(**kwargs)

    async def generate_token(self) -> AccessToken:
        url = self.host + "/api/auth-server/oauth/access-token"
        data = {APP_ID_FIELD: self.app_id, APP_SECRET_FIELD: self.app_secret}
        async with await self._client() as client:
            resp = await client.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        payload = resp.json()
        if payload.get("code") != 200:
            raise RuntimeError(f"获取领星令牌失败：{payload}")
        token_data = payload.get("data") or {}
        token_value = token_data.get(TOKEN_PARAM) or token_data.get("accessToken")
        if not token_value:
            raise RuntimeError(f"领星令牌返回结构异常：{payload}")
        return AccessToken(
            token=token_value,
            refresh_token=token_data.get(REFRESH_PARAM) or token_data.get("refreshToken"),
            expires_in=token_data.get("expires_in") or token_data.get("expiresIn"),
        )

    async def request(
        self,
        token: str,
        api_path: str,
        method: str = "POST",
        req_body: dict[str, Any] | None = None,
        req_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        req_body = req_body or {}
        req_params = req_params or {}
        url = self.host + api_path

        sign_source = dict(req_body)
        sign_source.update(req_params)
        sign_params = {
            "app_key": self.app_id,
            TOKEN_PARAM: token,
            "timestamp": str(int(time.time())),
        }
        sign_source.update(sign_params)
        sign_params["sign"] = generate_sign(self.app_id, sign_source)
        req_params.update(sign_params)

        started = time.time()
        response_payload: dict[str, Any] | None = None
        error_message = ""
        try:
            async with await self._client() as client:
                resp = await client.request(
                    method,
                    url,
                    params=req_params,
                    json=req_body if req_body else None,
                    headers={"Content-Type": "application/json"} if req_body else None,
                )
            response_payload = resp.json()
            return response_payload
        except Exception as exc:
            error_message = str(exc)
            raise
        finally:
            elapsed_ms = int((time.time() - started) * 1000)
            if self.enable_api_log and self.db is not None:
                self._write_api_log(
                    api_path=api_path,
                    method=method,
                    request_json=req_body,
                    response_json=response_payload,
                    elapsed_ms=elapsed_ms,
                    error_message=error_message,
                )

    def _write_api_log(
        self,
        api_path: str,
        method: str,
        request_json: dict[str, Any] | None,
        response_json: dict[str, Any] | None,
        elapsed_ms: int,
        error_message: str = "",
    ) -> None:
        response_json = response_json or {}
        code = response_json.get("code")
        message = str(response_json.get("message") or "")
        request_id = str(response_json.get("request_id") or "")
        success = 1 if code == 0 and not error_message else 0
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO `lxpm_api_call_log`
                    (`api_path`, `http_method`, `request_id`, `api_code`, `api_message`,
                     `request_json`, `response_json`, `success`, `elapsed_ms`, `error_message`)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        api_path,
                        method,
                        request_id,
                        code,
                        message[:500],
                        json_dumps(request_json),
                        json_dumps(response_json),
                        success,
                        elapsed_ms,
                        error_message,
                    ),
                )
        except Exception as exc:
            print(f"[WARN] 写入 lxpm_api_call_log 失败：{exc}")
