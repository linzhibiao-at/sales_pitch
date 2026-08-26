"""scripts/outfit_item_builder 回归测试。

锁定 dphs / outfits_unique 两个搭配源的 outfit item 直接复用 build_catalog.py
产出的 skus.jsonl 记录：color 块、attributes（sex）、顶层 gender/season/color_series
等全部来自 skus.jsonl，与 micro_guide item、ES skus 索引保持单一事实源一致。
"""

from __future__ import annotations

import unittest

from scripts.outfit_item_builder import build_outfit_item_from_sku


def _sku_record() -> dict:
    return {
        "sku_id": "SH1",
        "spu_id": "SP1",
        "id_goods": 123,
        "id_pa": 7,
        "title": "女子老爹鞋",
        "role": "shoes",
        "category_l1": "鞋类",
        "category_l2": "老爹鞋",
        "category_l3": "运动鞋",
        "series": "ORIGINALE",
        "sub_series": "鞋",
        "price": 699.0,
        "gender": ["女"],
        "season": ["夏"],
        "color_series": ["灰色系"],
        "color_name": "灰",
        "color_family": "灰",
        "attr_name": "灰",
        "scene_domain": "daily",
        "length_class": "n/a",
        "modeling": "",
        "coverage": "",
        "layer": "",
        "is_intimate": False,
        "occasion_tags": ["休闲"],
        "style_tags": ["时尚"],
        "search_keywords": "鞋,灰",
        "search_text": "女子老爹鞋",
        "up_down_raw": "",
        "display_image": "http://img/d.jpg",
        "tryon_image": "http://img/t.jpg",
        "index_images": ["http://img/i1.jpg"],
    }


class BuildOutfitItemFromSkuTest(unittest.TestCase):
    def test_attrs_projected_from_sku_record(self):
        item = build_outfit_item_from_sku("SH1", _sku_record(), is_master=True)
        # 顶层属性来自 skus.jsonl
        self.assertEqual(item["sku_id"], "SH1")
        self.assertEqual(item["attrAlias"], "SH1")
        self.assertEqual(item["idAlias"], "SP1")
        self.assertEqual(item["spu_id"], "SP1")
        self.assertEqual(item["idGoods"], 123)
        self.assertEqual(item["role"], "shoes")
        self.assertEqual(item["title"], "女子老爹鞋")
        self.assertEqual(item["category_l1"], "鞋类")
        self.assertEqual(item["category_l2"], "老爹鞋")
        self.assertEqual(item["price"], 699.0)
        self.assertEqual(item["gender"], ["女"])
        self.assertEqual(item["season"], ["夏"])
        self.assertEqual(item["color_series"], ["灰色系"])
        self.assertEqual(item["scene_domain"], "daily")
        self.assertTrue(item["is_master"])
        self.assertTrue(item["isMaster"])

    def test_color_block_built_from_catalog(self):
        """color 块从 skus.jsonl 重建（旧版 dphs/unique 这里恒为 {}）。"""
        item = build_outfit_item_from_sku("SH1", _sku_record(), is_master=False)
        self.assertEqual(item["color"].get("idPa"), 7)
        self.assertEqual(item["color"].get("attrName"), "灰")
        self.assertEqual(item["color"].get("colorName"), "灰")
        self.assertEqual(item["color"].get("attrAlias"), "SH1")

    def test_attributes_sex_from_gender(self):
        """attributes.sex 由 gender 列表首元素填充（outfit_recall gender 过滤回退用）。"""
        item = build_outfit_item_from_sku("SH1", _sku_record(), is_master=False)
        self.assertEqual(item["attributes"].get("sex"), "女")
        self.assertEqual(item["attributes"].get("season"), ["夏"])
        self.assertEqual(item["attributes"].get("category_l1"), "鞋类")
        self.assertEqual(item["attributes"].get("scene_domain"), "daily")

    def test_images_cover_prefers_tryon(self):
        item = build_outfit_item_from_sku("SH1", _sku_record(), is_master=False)
        self.assertEqual(item["images"].get("cover"), "http://img/t.jpg")

    def test_missing_record_degrades_gracefully(self):
        """SKU 不在 skus.jsonl 时退化为最小 item，不抛异常。"""
        item = build_outfit_item_from_sku("MISSING", None, is_master=False)
        self.assertEqual(item["sku_id"], "MISSING")
        self.assertEqual(item["role"], "")
        self.assertEqual(item["gender"], [])
        self.assertEqual(item["price"], 0.0)
        self.assertEqual(item["color"].get("idPa"), 0)
        self.assertFalse(item["is_master"])


if __name__ == "__main__":
    unittest.main()
