#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性热修：兼容领星令牌接口返回 code='200' 字符串。"""
from pathlib import Path

path = Path(__file__).resolve().parent.parent / "lx_product_m" / "lingxing_client.py"
text = path.read_text(encoding="utf-8")
text = text.replace('if payload.get("code") != 200:', 'if str(payload.get("code")) != "200":')
text = text.replace('message = str(response_json.get("message") or "")', 'message = str(response_json.get("message") or response_json.get("msg") or "")')
text = text.replace('success = 1 if code == 0 and not error_message else 0', 'success = 1 if str(code) == "0" and not error_message else 0')
path.write_text(text, encoding="utf-8")
print(f"hotfix applied: {path}")
