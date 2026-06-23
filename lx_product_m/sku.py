#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SKU 解析工具。"""
from __future__ import annotations

import re


def normalize_sku(sku: str | None) -> str:
    if sku is None:
        return ""
    value = str(sku).strip()
    if not value:
        return ""
    value = re.sub(r"\d+(?:PSC|PCS)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


def extract_spu(sku: str | None) -> str:
    value = normalize_sku(sku)
    if not value:
        return ""
    return value.split("-", 1)[0]
