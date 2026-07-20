from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from scripts import color_system_high_confidence_round1_v2 as round1


def make_row(**overrides: str) -> dict[str, str]:
    row = {
        "SKU": "SPU1-BK-M",
        "产品名称": "测试黑色上衣",
        "SPU": "SPU1",
        "当前颜色体系": "",
        "SKU结构": "常规",
        "是否多色套装": "否",
        "颜色代码": "BK",
        "品名识别颜色": "黑色",
        "拟打颜色体系": "A2023",
        "置信度": "高",
        "判定类型": "精确唯一匹配",
        "匹配方式": "代码+品名主映射",
        "处理优先级": "P1-有销量",
        "是否允许未来自动写入": "是",
    }
    row.update(overrides)
    return row


class CompatibilityTest(unittest.TestCase):
    def test_field_value_compatibility(self) -> None:
        self.assertEqual(round1.field_value({"val": "A2023"}), "A2023")
        self.assertEqual(round1.field_value({"val_text": "B2024"}), "B2024")
        self.assertEqual(round1.field_value({}), "")

    def test_safe_row_is_eligible(self) -> None:
        self.assertEqual(round1.eligibility_reason(make_row()), "")

    def test_live_color_values(self) -> None:
        fields = [
            {"id": "x", "name": "其他", "val": "abc"},
            {
                "id": round1.DEFAULT_FIELD_ID,
                "name": round1.COLOR_FIELD_NAME,
                "val": "B2024",
            },
        ]
        self.assertEqual(
            round1.live_color_values(fields, round1.DEFAULT_FIELD_ID),
            {"B2024"},
        )

    def test_review_workbook_can_be_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "review.xlsx"
            source = Path(tmp) / "source.xlsx"
            round1.create_review_workbook(
                [make_row()], round1.Counter(), 1, source, output
            )
            wb = load_workbook(output, read_only=True, data_only=True)
            try:
                self.assertEqual(wb["拟打标明细"]["A2"].value, "SPU1-BK-M")
                self.assertEqual(wb["拟打标明细"]["B2"].value, "A2023")
            finally:
                wb.close()
            self.assertEqual(
                round1.load_review_file(output),
                [{"SKU": "SPU1-BK-M", "拟打值": "A2023"}],
            )


if __name__ == "__main__":
    unittest.main()
