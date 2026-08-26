from __future__ import annotations

import json
import unittest

from scripts.etl_common import build_descent_extra_fields, merge_features


class TestMergeFeatures(unittest.TestCase):
    def test_dedup_preserve_order(self):
        self.assertEqual(merge_features("a,b", "b,c"), "a, b, c")

    def test_empty(self):
        self.assertEqual(merge_features("", ""), "")


class TestBuildDescentExtraFields(unittest.TestCase):
    def setUp(self):
        self.master = {
            "id_brand": "1",
            "pro_name": "短袖T",
            "id_alias": "A11M023104G",
            "pro_info": "透气,速干",
            "pro_content": "速干,抗菌",
            "selling_point_label": "凉爽",
            "keyword": "短T,男",
            "market_price": "299",
            "min_price": "179",
            "max_price": "299",
            "onsell": "1",
            "sales": "100",
            "sales_week": "10",
            "sales_month": "40",
            "w_order": "5",
            "video": "https://x/v.mp4",
        }
        self.ext = {"year": "2024", "cat_alias": "短袖T恤", "length": "短", "technology": "冰感"}
        self.color_rows = [
            {"attr_name": "黑", "image_url": "https://x/1.jpg"},
            {"attr_name": "白", "image_url": "https://x/2.jpg"},
        ]

    def test_basic_fields(self):
        f = build_descent_extra_fields(self.master, self.ext, self.color_rows, 3)
        self.assertEqual(f["product_name_short"], "短袖T")
        self.assertEqual(f["goods_sn"], "A11M023104G")
        self.assertEqual(f["brand_line"], "FILA")
        self.assertEqual(f["year"], "2024")
        self.assertEqual(f["category"], "短袖T恤")
        self.assertEqual(f["length"], "短")
        self.assertEqual(f["technology"], "冰感")
        self.assertEqual(f["features"], "透气, 速干, 抗菌")
        self.assertEqual(f["selling_point_label"], "凉爽")
        self.assertEqual(f["keyword"], "短T,男")
        self.assertEqual(f["video_url"], "https://x/v.mp4")
        self.assertEqual(f["sku_count"], 3)
        self.assertEqual(f["onsell"], 1)
        self.assertEqual(f["sales"], 100)
        self.assertEqual(f["sales_week"], 10)
        self.assertEqual(f["sales_month"], 40)
        self.assertEqual(f["w_order"], 5)
        self.assertEqual(f["market_price"], 299.0)
        self.assertEqual(f["min_price"], 179.0)
        self.assertEqual(f["max_price"], 299.0)

    def test_brand_line_map(self):
        self.assertEqual(build_descent_extra_fields({"id_brand": "17"}, {}, [], 0)["brand_line"], "FILA KIDS")
        self.assertEqual(build_descent_extra_fields({"id_brand": "21"}, {}, [], 0)["brand_line"], "FILA FUSION")
        self.assertEqual(build_descent_extra_fields({"id_brand": "10"}, {}, [], 0)["brand_line"], "FILA联名")

    def test_color_images_json(self):
        f = build_descent_extra_fields(self.master, self.ext, self.color_rows, 3)
        parsed = json.loads(f["color_images"])
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["color"], "黑")
        self.assertEqual(parsed[0]["image_url"], "https://x/1.jpg")

    def test_empty_master_is_safe(self):
        f = build_descent_extra_fields({}, {}, [], 0)
        self.assertEqual(f["brand_line"], "")
        self.assertEqual(f["features"], "")
        self.assertEqual(f["sku_count"], 0)
        self.assertEqual(f["onsell"], 0)


if __name__ == "__main__":
    unittest.main()
