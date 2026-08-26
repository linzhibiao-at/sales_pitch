from __future__ import annotations

import unittest

from scripts.hybrid_text import build_keyword_text, build_semantic_text


def _row():
    return {
        "title": "短袖T",
        "search_keywords": "A1,短袖T,男",
        "keyword": "短T",
        "product_name_short": "短T",
        "brand_line": "FILA",
        "series": "GOLF",
        "sub_series": "上衣",
        "category": "短袖T恤",
        "category_l1": "服装",
        "up_down_raw": "上装",
        "gender": ["男"],
        "age": "成人",
        "season": ["夏"],
        "year": "2024",
        "modeling": "修身",
        "length": "短",
        "material": "棉",
        "technology": "冰感",
        "features": "透气,速干",
        "selling_point_label": "凉爽",
        "color_name": "黑",
        "goods_sn": "A1G",
    }


class TestBuildKeywordText(unittest.TestCase):
    def test_title_repeated_three_times(self):
        # 标题在 search_keywords 中也会出现，故断言 boost 带来的 ≥3 次
        t = build_keyword_text(_row())
        self.assertGreaterEqual(t.count("短袖T"), 3)

    def test_includes_rich_fields(self):
        t = build_keyword_text(_row())
        for kw in ("FILA", "GOLF", "冰感", "透气,速干", "凉爽", "黑", "A1G", "2024"):
            self.assertIn(kw, t)

    def test_empty_row_safe(self):
        self.assertEqual(build_keyword_text({}), "")


class TestBuildSemanticText(unittest.TestCase):
    def test_starts_with_title_and_has_kv(self):
        t = build_semantic_text(_row())
        self.assertTrue(t.startswith("短袖T"))
        self.assertIn("品牌线:FILA", t)
        self.assertIn("系列:GOLF", t)
        self.assertIn("品类:短袖T恤", t)

    def test_empty_row_safe(self):
        self.assertEqual(build_semantic_text({}), "")


if __name__ == "__main__":
    unittest.main()
