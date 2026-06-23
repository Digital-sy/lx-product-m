#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领星 OpenAPI 签名算法。与现有 lx-forecast-pipeline SDK 保持一致。"""
from __future__ import annotations

import base64
import hashlib
from typing import Any

import orjson
from Crypto.Cipher import AES

BLOCK_SIZE = 16


def _pad(text: str) -> str:
    pad_len = BLOCK_SIZE - len(text) % BLOCK_SIZE
    return text + pad_len * chr(pad_len)


def md5_encrypt(text: str) -> str:
    md5 = hashlib.md5()
    md5.update(text.encode("utf-8"))
    return md5.hexdigest()


def aes_encrypt(key: str, data: str) -> str:
    key_bytes = key.encode("utf-8")
    cipher = AES.new(key_bytes, AES.MODE_ECB)
    encrypted = cipher.encrypt(_pad(data).encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def format_params(request_params: dict[str, Any] | None) -> str:
    if not request_params:
        return ""

    pairs = []
    for key in sorted(request_params.keys()):
        value = request_params[key]
        if value == "" or value is None:
            continue
        if isinstance(value, (dict, list)):
            value_str = orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode()
        else:
            value_str = str(value)
        pairs.append(f"{key}={value_str}")
    return "&".join(pairs)


def generate_sign(encrypt_key: str, request_params: dict[str, Any]) -> str:
    canonical_querystring = format_params(request_params)
    md5_str = md5_encrypt(canonical_querystring).upper()
    return aes_encrypt(encrypt_key, md5_str)
