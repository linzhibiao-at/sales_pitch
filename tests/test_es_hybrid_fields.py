from __future__ import annotations

import unittest

from scripts import build_fila_es_index as esb


class TestSkuDocSearchTextEnriched(unittest.TestCase):
    def test_search_text_uses_build_keyword_text(self):
        row = {
            "sku_id": "S1", "title": "短袖T", "brand_line": "FILA",
            "features": "透气", "color_name": "黑",
        }
        doc = esb.sku_doc(row)
        self.assertIn("短袖T", doc["search_text"])
        self.assertIn("FILA", doc["search_text"])
        self.assertIn("透气", doc["search_text"])

    def test_sku_doc_writes_descent_fields(self):
        row = {
            "sku_id": "S1", "title": "短袖T", "brand_line": "FILA", "year": "2024",
            "features": "透气,速干", "selling_point_label": "凉爽", "technology": "冰感",
            "goods_sn": "A1G", "market_price": 299, "onsell": 1, "sales": 100, "sku_count": 3,
        }
        doc = esb.sku_doc(row)
        self.assertEqual(doc["brand_line"], "FILA")
        self.assertEqual(doc["year"], "2024")
        self.assertEqual(doc["features"], "透气,速干")
        self.assertEqual(doc["selling_point_label"], "凉爽")
        self.assertEqual(doc["technology"], "冰感")
        self.assertEqual(doc["goods_sn"], "A1G")
        self.assertEqual(doc["market_price"], 299.0)
        self.assertEqual(doc["onsell"], 1)
        self.assertEqual(doc["sales"], 100)
        self.assertEqual(doc["sku_count"], 3)


class TestMappingHasNewFields(unittest.TestCase):
    def test_properties_contain_new_fields(self):
        body = esb.skus_mapping()
        props = body["mappings"]["properties"]
        for f in (
            "brand_line", "year", "market_price", "features", "selling_point_label",
            "technology", "goods_sn", "onsell", "sales", "sku_count",
            "min_price", "max_price", "video_url", "product_name_short", "category",
            "length",
        ):
            self.assertIn(f, props, f)
        self.assertEqual(props["market_price"]["type"], "double")
        self.assertEqual(props["onsell"]["type"], "integer")
        self.assertEqual(props["features"]["type"], "text")
        self.assertEqual(props["brand_line"]["type"], "keyword")


if __name__ == "__main__":
    unittest.main()
