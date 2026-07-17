from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from openpyxl import load_workbook

from scripts import color_system_tagging as round1
from scripts import color_system_tagging_round2 as round2


class Round2RulesTest(unittest.TestCase):
    def test_clean_and_bounded_year_rules(self) -> None:
        for raw, expected in {"2023年": "2023", " 2024 ": "2024", "２０２４年": "2024", None: ""}.items():
            self.assertEqual(round2.clean_development_year(raw), expected)
        for raw in ("历史", "2023", "2024", "２０２４年"):
            self.assertEqual(round2.classify_development_year(raw), ("A2023", round2.STATUS_CONVERT))
        for raw in ("2025", "2100"):
            self.assertEqual(round2.classify_development_year(raw), ("待定", round2.STATUS_KEEP_FUTURE))
        for raw in ("23", "0", "999", "1999", "2101", "9999", "ABC"):
            self.assertEqual(round2.classify_development_year(raw), ("待定", round2.STATUS_KEEP_UNEXPECTED))
        self.assertEqual(round2.classify_development_year(""), ("待定", round2.STATUS_KEEP_EMPTY))

    def test_extract_keeps_raw_for_audit(self) -> None:
        fields = [{"id": round2.DEVELOPMENT_YEAR_FIELD_ID, "name": "开发年份", "val_text": "2024 "}]
        self.assertEqual(round2.extract_development_year(fields), "2024 ")

    def test_first_round_file_locks_pending_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round1.xlsx"
            round1.create_dryrun_workbook([
                {"SKU": "AA-BK-S", "关联 MSKU": "AA-BK-S", "SPU": "AA", "开售日期": None, "首销店铺": "", "拟打值": "待定", "近半年是否有销量": "否"},
                {"SKU": "BB-WH-M", "关联 MSKU": "BB-WH-M", "SPU": "BB", "开售日期": None, "首销店铺": "", "拟打值": "A2023", "近半年是否有销量": "是"},
            ], path)
            self.assertEqual(round2.load_first_round_pending_skus(path, expected_count=1), ["AA-BK-S"])
            with self.assertRaisesRegex(RuntimeError, "数量不符"):
                round2.load_first_round_pending_skus(path, expected_count=9823)


class Round2WorkbookTest(unittest.TestCase):
    def test_workbook_reports_unexpected_values(self) -> None:
        rows = round2.prepare_round2_rows(["A", "B", "C", "D"], {"A": "2024 ", "B": "2025", "C": "", "D": "23"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round2.xlsx"
            round2.create_round2_workbook(rows, path)
            values = list(load_workbook(path, data_only=True)["汇总"].values)
            self.assertIn(("转 A2023", 1, None), values)
            self.assertIn(("剩余待定", 3, None), values)
            self.assertIn(("意外值", 1, None), values)
            self.assertIn(("23", 1, round2.STATUS_KEEP_UNEXPECTED), values)


class Round2ApplyTest(unittest.IsolatedAsyncioTestCase):
    async def test_apply_only_writes_a2023_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "round2.xlsx"
            round2.create_round2_workbook(round2.prepare_round2_rows(["AA-BK-S", "BB-WH-M"], {"AA-BK-S": "2024", "BB-WH-M": "2025"}), review)
            args = round2.parse_args(["--apply", "--review-file", str(review), "--field-id", "999", "--allow-outside-low-peak", "--delay", "0", "--verify-delay", "0"])
            before = {"sku": "AA-BK-S", "product_name": "AA product", "custom_fields": [{"id": "999", "name": "颜色体系", "val_text": "待定"}]}
            after = {"sku": "AA-BK-S", "product_name": "AA product", "custom_fields": [{"id": "999", "name": "颜色体系", "val_text": "A2023"}]}
            client = MagicMock(); client.generate_token = AsyncMock(return_value=SimpleNamespace(token="t")); client.aclose = AsyncMock()
            live = AsyncMock(side_effect=[({"AA-BK-S": before}, "t"), ({"AA-BK-S": after}, "t")])
            with (
                patch.object(round1, "Database", return_value=MagicMock()),
                patch.object(round1, "LingxingClient", return_value=client),
                patch.object(round1, "fetch_live_products", new=live),
                patch.object(round1, "request_with_retry", new=AsyncMock(return_value=({"code": "0"}, "t"))) as write,
            ):
                code = await round1.apply_review_file(args, write_values={"A2023"})
            self.assertEqual(code, 0)
            self.assertEqual(live.await_args_list[0].args[2], ["AA-BK-S"])
            write.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
