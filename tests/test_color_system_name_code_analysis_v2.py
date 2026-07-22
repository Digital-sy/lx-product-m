from __future__ import annotations

import unittest

from scripts import color_system_name_code_analysis as base
from scripts import color_system_name_code_analysis_v2 as v2


class SkuParsingV2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = base.build_mapping_index()

    def test_pd_special_suffix(self) -> None:
        result = v2.parse_sku("TEST-BKPD-M", self.index)
        self.assertEqual(result.color_codes, ("BK",))
        self.assertEqual(result.size, "M")
        self.assertIn("特殊后缀颜色-PD", result.structure)
        self.assertFalse(result.error)

    def test_unknown_full_color_segment_is_not_prefix_split(self) -> None:
        result = v2.parse_sku("ZQZ410-BLP-L", self.index)
        self.assertEqual(result.color_codes, ())
        self.assertEqual(result.size, "L")
        self.assertEqual(result.structure, "未收录颜色代码")
        self.assertIn("完整颜色段 BLP", result.error)
        self.assertIn("禁止按前缀拆分", result.error)

    def test_glued_color_size_still_supported_without_separate_size_segment(self) -> None:
        result = v2.parse_sku("ABC-SN40DD", self.index)
        self.assertEqual(result.color_codes, ("SN",))
        self.assertEqual(result.size, "40DD")
        self.assertEqual(result.structure, "颜色尺码粘连")

    def test_regular_sku_unchanged(self) -> None:
        result = v2.parse_sku("KZ291-SHORT-BK-S", self.index)
        self.assertEqual(result.color_codes, ("BK",))
        self.assertEqual(result.size, "S")
        self.assertEqual(result.style, "SHORT")


if __name__ == "__main__":
    unittest.main()
