from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from openpyxl import load_workbook

from lx_product_m.services import product_write_guard
from scripts import color_system_tagging as tagging


def row(sku: str, value: str = "A2023") -> dict[str, object]:
    return {
        "SKU": sku,
        "关联 MSKU": sku,
        "SPU": sku.split("-", 1)[0],
        "开售日期": date(2024, 6, 30) if value == "A2023" else None,
        "首销店铺": "sy_us",
        "拟打值": value,
        "近半年是否有销量": "是" if value == "A2023" else "否",
    }


class RulesTest(unittest.TestCase):
    def test_first_round_boundary_and_sql(self) -> None:
        self.assertEqual(tagging.classify_opening_date(date(2024, 6, 30)), "A2023")
        self.assertIsNone(tagging.classify_opening_date(date(2024, 7, 1)))
        self.assertEqual(tagging.classify_opening_date(None), "待定")
        sql = tagging.color_system_sql(["sy_order.total_order_2024"], ["sy_us"])
        self.assertIn("SELECT msku, MIN(order_date) AS first_sale_date", sql)
        self.assertIn("MIN(f.first_sale_date) AS opening_date", sql)
        self.assertIn("WHEN LEFT(r.sku_key, 3) = 0x583030", sql)
        self.assertNotIn("GROUP BY spu", sql)

    def test_prepare_rows_and_low_peak(self) -> None:
        prepared, skipped = tagging.prepare_analysis_rows([
            {"sku": "AA-BK-S", "associated_msku": "AA-BK-S", "opening_date": date(2024, 6, 30), "first_sale_stores": "sy_us", "has_recent_sales": 1},
            {"sku": "BB-WH-M", "associated_msku": "BB-WH-M", "opening_date": None, "first_sale_stores": None, "has_recent_sales": 0},
            {"sku": "CC-RD-L", "associated_msku": "CC-RD-L", "opening_date": date(2024, 7, 1), "first_sale_stores": "jq_us", "has_recent_sales": 1},
        ])
        self.assertEqual([x["拟打值"] for x in prepared], ["A2023", "待定"])
        self.assertEqual(skipped, 1)
        self.assertTrue(tagging.is_beijing_low_peak(datetime(2026, 1, 1, 17, 30, tzinfo=timezone.utc)))
        self.assertFalse(tagging.is_beijing_low_peak(datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)))

    def test_first_round_main_forces_a2023_filter(self) -> None:
        with patch.object(tagging, "apply_review_file", new=AsyncMock(return_value=0)) as apply:
            self.assertEqual(tagging.main(["--apply", "--review-file", "review.xlsx", "--allow-outside-low-peak"]), 0)
        self.assertEqual(apply.await_args.kwargs["write_values"], {"A2023"})


class WorkbookTest(unittest.TestCase):
    def test_workbook_and_review_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.xlsx"
            tagging.create_dryrun_workbook([row("AA-BK-S"), row("BB-WH-M", "待定")], path)
            wb = load_workbook(path, data_only=True)
            self.assertEqual(wb.sheetnames, ["拟打标明细", "汇总"])
            wb.close()
            self.assertEqual(len(tagging.load_review_file(path)), 2)
            wb = load_workbook(path)
            wb["拟打标明细"]["A3"] = "AA-BK-S"
            wb.save(path)
            wb.close()
            with self.assertRaisesRegex(ValueError, "重复 SKU"):
                tagging.load_review_file(path)


class VerifyTest(unittest.TestCase):
    def expected(self) -> dict[str, object]:
        return {
            "sku": "AA-BK-S", "product_name": "AA product", "category_id": 7, "category": "Top",
            "custom_fields": [
                {"id": "111", "name": "季节", "val": "春夏"},
                {"id": "999", "name": "颜色体系", "val": "A2023"},
            ],
        }

    def live(self) -> dict[str, object]:
        return {
            "sku": "AA-BK-S", "product_name": "AA product", "category_id": 7, "category": "Top",
            "custom_fields": [
                {"id": "999", "name": "颜色体系", "val_text": "A2023"},
                {"id": "111", "name": "季节", "val_text": "春夏"},
            ],
        }

    def test_verify_complete_result(self) -> None:
        self.assertEqual(tagging.verify_product_set_result(self.live(), self.expected()), (True, ""))
        changed = self.live()
        changed["custom_fields"][1]["val_text"] = "秋冬"  # type: ignore[index]
        ok, message = tagging.verify_product_set_result(changed, self.expected())
        self.assertFalse(ok)
        self.assertIn("自定义字段", message)


class ApplyTest(unittest.IsolatedAsyncioTestCase):
    async def test_apply_filters_pending_and_verifies_after_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "review.xlsx"
            tagging.create_dryrun_workbook([row("AA-BK-S"), row("BB-WH-M", "待定")], review)
            args = tagging.parse_args(["--apply", "--review-file", str(review), "--field-id", "999", "--allow-outside-low-peak", "--delay", "0", "--verify-delay", "0"])
            before = {"sku": "AA-BK-S", "product_name": "AA product", "category_id": 7, "category": "Top", "custom_fields": [{"id": "111", "name": "季节", "val_text": "春夏"}, {"id": "999", "name": "颜色体系", "val_text": ""}]}
            after = {"sku": "AA-BK-S", "product_name": "AA product", "category_id": 7, "category": "Top", "custom_fields": [{"id": "111", "name": "季节", "val_text": "春夏"}, {"id": "999", "name": "颜色体系", "val_text": "A2023"}]}
            live_read = AsyncMock(side_effect=[({"AA-BK-S": before}, "t"), ({"AA-BK-S": after}, "t")])
            client = MagicMock()
            client.generate_token = AsyncMock(return_value=SimpleNamespace(token="t"))
            client.aclose = AsyncMock()
            with (
                patch.object(tagging, "Database", return_value=MagicMock()),
                patch.object(tagging, "LingxingClient", return_value=client),
                patch.object(tagging, "fetch_live_products", new=live_read),
                patch.object(tagging, "request_with_retry", new=AsyncMock(return_value=({"code": "0"}, "t"))) as write,
            ):
                code = await tagging.apply_review_file(args, write_values={"A2023"})
            self.assertEqual(code, 0)
            self.assertEqual(live_read.await_count, 2)
            self.assertEqual(live_read.await_args_list[0].args[2], ["AA-BK-S"])
            write.assert_awaited_once()

    async def test_post_write_mismatch_is_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "review.xlsx"
            tagging.create_dryrun_workbook([row("AA-BK-S")], review)
            args = tagging.parse_args(["--apply", "--review-file", str(review), "--field-id", "999", "--allow-outside-low-peak", "--delay", "0", "--verify-delay", "0"])
            before = {"sku": "AA-BK-S", "product_name": "AA product", "custom_fields": [{"id": "999", "name": "颜色体系", "val_text": ""}]}
            after = {"sku": "AA-BK-S", "product_name": "AA product", "custom_fields": [{"id": "999", "name": "颜色体系", "val_text": "待定"}]}
            client = MagicMock(); client.generate_token = AsyncMock(return_value=SimpleNamespace(token="t")); client.aclose = AsyncMock()
            db = MagicMock()
            with (
                patch.object(tagging, "Database", return_value=db),
                patch.object(tagging, "LingxingClient", return_value=client),
                patch.object(tagging, "fetch_live_products", new=AsyncMock(side_effect=[({"AA-BK-S": before}, "t"), ({"AA-BK-S": after}, "t")])),
                patch.object(tagging, "request_with_retry", new=AsyncMock(return_value=({"code": "0"}, "t"))),
                patch.object(tagging, "OUTPUT_DIR", Path(tmp) / "reports"),
            ):
                code = await tagging.apply_review_file(args, write_values={"A2023"})
            self.assertEqual(code, 1)
            self.assertEqual(len(list((Path(tmp) / "reports").glob("*.xlsx"))), 1)

    async def test_fixed_rate_limit_options(self) -> None:
        request = AsyncMock(return_value=({"code": 0, "data": [{"sku": "AA-BK-S"}]}, "t"))
        with patch.object(product_write_guard, "request_with_retry", new=request):
            await product_write_guard.fetch_live_products(MagicMock(), "t", ["AA-BK-S"], max_retries=3, retry_base_seconds=30, retry_max_seconds=30)
        self.assertEqual(request.await_args.kwargs["max_retries"], 3)
        self.assertEqual(request.await_args.kwargs["retry_base_seconds"], 30)


if __name__ == "__main__":
    unittest.main()
