from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

from scripts import color_system_tagging_listing_time as listing_time


class ListingColumnTest(unittest.TestCase):
    def test_resolve_exact_listing_created_column(self) -> None:
        columns = [
            {"column_name": "SKU", "data_type": "varchar"},
            {"column_name": "update_time", "data_type": "datetime"},
            {"column_name": "listing_create_time", "data_type": "datetime"},
        ]
        self.assertEqual(listing_time.resolve_column(columns, "", kind="SKU"), "SKU")
        self.assertEqual(
            listing_time.resolve_column(columns, "", kind="Listing创建时间"),
            "listing_create_time",
        )

    def test_requested_column_must_exist(self) -> None:
        columns = [{"column_name": "SKU", "data_type": "varchar"}]
        with self.assertRaisesRegex(RuntimeError, "不存在"):
            listing_time.resolve_column(columns, "missing", kind="Listing创建时间")


class RuleTest(unittest.TestCase):
    def test_date_and_prefix_parsing(self) -> None:
        self.assertEqual(listing_time.as_date(datetime(2024, 6, 30, 9, 0)), date(2024, 6, 30))
        self.assertEqual(listing_time.as_date("2024/06/01 12:00:00"), date(2024, 6, 1))
        self.assertIsNone(listing_time.as_date("bad"))
        self.assertEqual(listing_time.prefix_of(" fab001 ", ("XH", "FAB")), "FAB")

    def test_prepare_rows_and_excluded_uncertain_count(self) -> None:
        fields_a = json.dumps([{"id": "1", "name": "颜色体系", "val_text": "A2023"}], ensure_ascii=False)
        snapshot = [
            {"sku": "OLD-1", "product_name": "old", "spu": "OLD", "custom_fields_json": "[]"},
            {"sku": "NEW-1", "product_name": "new", "spu": "NEW", "custom_fields_json": "[]"},
            {"sku": "XH001", "product_name": "x", "spu": "XH001", "custom_fields_json": "[]"},
            {"sku": "KNOWN", "product_name": "known", "spu": "KNOWN", "custom_fields_json": fields_a},
        ]
        earliest = {
            "OLD-1": datetime(2024, 6, 30, 1, 0),
            "NEW-1": datetime(2024, 7, 1, 1, 0),
        }
        rows, stats, excluded = listing_time.prepare_rows(
            snapshot, earliest, date(2024, 6, 30), ("XH", "FAB", "LCS", "PF")
        )
        by_sku = {row["SKU"]: row for row in rows}
        self.assertEqual(by_sku["OLD-1"]["拟打值"], "A2023")
        self.assertEqual(by_sku["NEW-1"]["拟打值"], "待定")
        self.assertEqual(by_sku["XH001"]["前缀剔除"], "XH")
        self.assertEqual(stats["existing_a2023"], 1)
        self.assertEqual(stats["listing_a2023"], 1)
        self.assertEqual(stats["uncertain_total"], 2)
        self.assertEqual(excluded["XH"], 1)
        self.assertEqual(stats["uncertain_after_exclusion"], 1)


class MainTest(unittest.TestCase):
    def test_apply_forces_a2023_only(self) -> None:
        with patch.object(listing_time, "apply_review_file", new=AsyncMock(return_value=0)) as apply:
            code = listing_time.main(
                ["--apply", "--review-file", "review.xlsx", "--allow-outside-low-peak"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(apply.await_args.kwargs["write_values"], {"A2023"})
        self.assertEqual(
            apply.await_args.kwargs["failure_prefix"],
            "color_system_tagging_listing_time_failures",
        )


if __name__ == "__main__":
    unittest.main()
