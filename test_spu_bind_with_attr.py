#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import sys

from lx_product_m.db import Database
from lx_product_m.lingxing_client import LingxingClient
from lx_product_m.services.spu_service import SpuService, SPU_CHANGE_LOG_DDL

DEFAULT_ATTRIBUTE = [{"pa_id": 340, "pai_id": 3909}]


def parse_attribute() -> list[dict]:
    if len(sys.argv) >= 4:
        value = json.loads(sys.argv[3])
        if not isinstance(value, list):
            raise SystemExit("attribute-json must be a JSON array")
        return value
    return DEFAULT_ATTRIBUTE


async def main() -> None:
    if len(sys.argv) < 3:
        print('Usage: python test_spu_bind_with_attr.py <SKU> <SPU> \'[{"pa_id":340,"pai_id":3909}]\'')
        sys.exit(1)

    sku = sys.argv[1].strip()
    spu = sys.argv[2].strip()
    attribute = parse_attribute()

    db = Database()
    db.execute(SPU_CHANGE_LOG_DDL)
    client = LingxingClient(db=db)
    service = SpuService(client, db)
    token = (await client.generate_token()).token

    print(f"bind sku={sku} spu={spu}")
    print("attribute=", json.dumps(attribute, ensure_ascii=False))
    result = await service.ensure_spu_and_bind_sku(
        token,
        spu=spu,
        sku=sku,
        batch_no="spu-test",
        sku_attribute=attribute,
    )
    print("result=", result)

    detail = await service.get_spu_detail(token, spu=spu)
    skus = [item.get("sku") for item in (detail or {}).get("sku_list") or []]
    print(f"verify spu={spu}, ps_id={(detail or {}).get('ps_id')}, sku_count={len(skus)}, skus={skus}")


if __name__ == "__main__":
    asyncio.run(main())
