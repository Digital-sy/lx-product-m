from __future__ import annotations

import unittest

from scripts import color_system_name_code_analysis_v3 as v3


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql):
        self.sql = sql

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)


class DiscoverOrderTablesTest(unittest.TestCase):
    def test_accepts_lowercase_and_uppercase_keys(self) -> None:
        conn = _Connection([
            {"TABLE_NAME": "total_order_2024"},
            {"table_name": "total_order_2025"},
        ])
        self.assertEqual(
            v3.discover_order_tables(conn),
            ["sy_order.total_order_2024", "sy_order.total_order_2025"],
        )


if __name__ == "__main__":
    unittest.main()
