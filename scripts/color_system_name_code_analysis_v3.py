#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""颜色体系只读诊断 V3：在 V2 基础上修复订单年度表字段名大小写兼容。

安全边界：只读数据库、只生成 Excel、没有 --apply。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import color_system_name_code_analysis as base
from scripts import color_system_name_code_analysis_v2 as v2


def discover_order_tables(conn: Any) -> list[str]:
    """兼容 DictCursor 返回 table_name/TABLE_NAME 或其他大小写形式。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name AS table_name
            FROM information_schema.tables
            WHERE table_schema='sy_order'
              AND table_name REGEXP '^total_order_[0-9]{4}$'
            ORDER BY table_name
            """
        )
        rows = list(cur.fetchall())

    tables: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not row:
            continue
        table_name = (
            row.get("table_name")
            or row.get("TABLE_NAME")
            or next(iter(row.values()), None)
        )
        if table_name:
            tables.append(f"sy_order.{table_name}")
    return tables


def install_overrides() -> None:
    base.discover_order_tables = discover_order_tables
    v2.install_overrides()


def main(argv: Sequence[str] | None = None) -> int:
    install_overrides()
    return base.main(argv)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(130)
