#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性热修：让领星业务请求体序列化方式与旧SDK保持一致。"""
from pathlib import Path

path = Path(__file__).resolve().parent.parent / "lx_product_m" / "lingxing_client.py"
text = path.read_text(encoding="utf-8")

if "import httpx\nimport orjson" not in text:
    text = text.replace("import httpx\n", "import httpx\nimport orjson\n")

# 兼容令牌接口 code 为字符串。
text = text.replace('if payload.get("code") != 200:', 'if str(payload.get("code")) != "200":')

# 兼容业务接口返回 msg 字段和字符串 code。
text = text.replace('message = str(response_json.get("message") or "")', 'message = str(response_json.get("message") or response_json.get("msg") or "")')
text = text.replace('success = 1 if code == 0 and not error_message else 0', 'success = 1 if str(code) == "0" and not error_message else 0')

old = '''        try:
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
'''
new = '''        body_content = orjson.dumps(req_body, option=orjson.OPT_SORT_KEYS) if req_body else None
        try:
            async with await self._client() as client:
                resp = await client.request(
                    method,
                    url,
                    params=req_params,
                    content=body_content,
                    headers={"Content-Type": "application/json"} if req_body else None,
                )
            response_payload = resp.json()
            return response_payload
'''
if old in text:
    text = text.replace(old, new)
elif "body_content = orjson.dumps(req_body" in text:
    pass
else:
    raise RuntimeError("未找到可替换的请求体发送代码，请打开 lx_product_m/lingxing_client.py 手动检查")

path.write_text(text, encoding="utf-8")
print(f"hotfix applied: {path}")
