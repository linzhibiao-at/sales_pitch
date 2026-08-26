"""build_fila_guide_outfits_fast._build_item_dict 属性来源回归测试。

锁定 fix：outfit item 的 SKU 属性应统一复用 build_catalog.py 产出的 skus.jsonl
全记录（catalog_sku_rows，单一事实源），与 ES skus 索引、dphs/unique outfit item
保持一致；原始表只保留 skus.jsonl 不携带的字段（images.outfitCd/outfitCps、swatch、
idGoods、isMaster）。
"""

from __future__ import annotations

import unittest
from typing import Any, Dict

from scripts.build_fila_guide_outfits_fast import BuildContext, OutfitBuilder


def _minimal_ctx(
    catalog_rows: Dict[str, dict],
    exts: Dict[int, Dict[str, str]] | None = None,
    images_by_goods: Dict[int, list] | None = None,
) -> BuildContext:
    """构造仅含 _build_item_dict 所需字段的 BuildContext。"""
    return BuildContext(
        masters={},
        exts=exts or {},
        alias_to_gid={},
        attr_by_goods_alias={},
        color_attrs_by_goods={},
        images_by_goods=images_by_goods or {},
        up_down_by_sku={},
        sorted_attr_aliases=(),
        rows_by_attr_alias={},
        by_match={},
        catalog_sku_ids=frozenset(catalog_rows.keys()),
        catalog_sku_images={},
        catalog_sku_role={sid: r.get("role", "") for sid, r in catalog_rows.items()},
        catalog_sku_rows=catalog_rows,
    )


class BuildItemAttrsReuseTest(unittest.TestCase):
    def test_attrs_taken_from_catalog(self):
        """catalog 提供全套属性 → item 各字段 == catalog 值（非原始表派生）。"""
        row = {
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
            "color_series": ["中性色"],
            "color_name": "黑",
            "color_family": "黑",
            "attr_name": "黑",
            "scene_domain": "daily",
            "length_class": "n/a",
            "modeling": "",
            "coverage": "",
            "layer": "",
            "is_intimate": False,
            "occasion_tags": ["休闲"],
            "style_tags": ["时尚"],
            "search_keywords": "鞋,黑",
            "search_text": "女子老爹鞋",
            "material": "",
            "fabric_function": [],
            "age": "成人",
            "brand": "FILA",
            "up_down_raw": "",
            "display_image": "http://img/display.jpg",
            "tryon_image": "http://img/tryon.jpg",
            "index_images": ["http://img/i1.jpg"],
        }
        ctx = _minimal_ctx({"SH1": row})
        builder = OutfitBuilder.from_context(ctx)
        item = builder._build_item_dict(gid=1, alias="SH1", is_master=False, pa=None)

        # 顶层属性来自 catalog
        self.assertEqual(item.get("role"), "shoes")
        self.assertEqual(item.get("title"), "女子老爹鞋")
        self.assertEqual(item.get("category_l1"), "鞋类")
        self.assertEqual(item.get("category_l2"), "老爹鞋")
        self.assertEqual(item.get("price"), 699.0)
        self.assertEqual(item.get("gender"), ["女"])
        self.assertEqual(item.get("season"), ["夏"])
        self.assertEqual(item.get("color_series"), ["中性色"])
        self.assertEqual(item.get("scene_domain"), "daily")
        self.assertEqual(item.get("spu_id"), "SP1")
        self.assertEqual(item.get("idAlias"), "SP1")
        self.assertEqual(item.get("search_text"), "女子老爹鞋")
        # color 块从 catalog 重建
        self.assertEqual(item["color"].get("colorName"), "黑")
        self.assertEqual(item["color"].get("attrName"), "黑")
        self.assertEqual(item["color"].get("idPa"), 7)
        # attributes 同步
        self.assertEqual(item["attributes"].get("sex"), "女")
        self.assertEqual(item["attributes"].get("season"), ["夏"])
        self.assertEqual(item["attributes"].get("category_l1"), "鞋类")
        # images.cover 优先 catalog tryon_image
        self.assertEqual(item["images"].get("cover"), "http://img/tryon.jpg")

    def test_raw_only_fields_preserved(self):
        """skus.jsonl 不携带的字段（outfitCd/outfitCps/swatch/idGoods/isMaster）仍来自原始表。"""
        row = {"sku_id": "T1", "role": "top", "spu_id": "ST1"}
        # images_by_goods 提供 cd/cd2 图片（原始表来源）
        ctx = _minimal_ctx(
            {"T1": row},
            images_by_goods={1: [
                {"image_type": "cd", "id_pa": 0, "order_id": 1, "path": "http://img/cd.jpg"},
                {"image_type": "cd2", "id_pa": 0, "order_id": 1, "path": "http://img/cd2.jpg"},
            ]},
        )
        builder = OutfitBuilder.from_context(ctx)
        item = builder._build_item_dict(gid=1, alias="T1", is_master=True, pa=None)
        self.assertEqual(item.get("idGoods"), 1)
        self.assertTrue(item.get("isMaster"))
        self.assertIn("http://img/cd.jpg", item["images"].get("outfitCd") or [])
        self.assertIn("http://img/cd2.jpg", item["images"].get("outfitCps") or [])

    def test_role_falls_back_to_infer_when_absent_from_catalog(self):
        """catalog_sku_rows 缺该 SKU 时回退 infer_role（鞋类标题命中 → shoes），不崩溃。"""
        ctx = _minimal_ctx({}, exts={1: {"cat_type": "鞋类"}})
        builder = OutfitBuilder.from_context(ctx)
        item = builder._build_item_dict(gid=1, alias="SH1", is_master=False, pa=None)
        self.assertEqual(item.get("role"), "shoes",
                         "catalog 缺记录时应回退 infer_role 得到 shoes")

    def test_partial_catalog_attrs_keep_raw_fallback(self):
        """catalog 只带部分字段时，缺失字段保留原始表派生值，不退化为空。"""
        row = {"sku_id": "P1", "role": "bottoms", "spu_id": "SP1"}
        ctx = _minimal_ctx(
            {"P1": row},
            exts={1: {"series": "SRC_SERIES", "season": "夏", "sex": "女",
                      "cat_type": "服装", "middle_class": "梭织裤"}},
        )
        builder = OutfitBuilder.from_context(ctx)
        item = builder._build_item_dict(gid=1, alias="P1", is_master=False, pa=None)
        # role 来自 catalog
        self.assertEqual(item.get("role"), "bottoms")
        # series/season 缺于 catalog → 保留原始表派生（series 顶层、attributes.season）
        self.assertEqual(item.get("series"), "SRC_SERIES")
        self.assertEqual(item["attributes"].get("season"), "夏")


if __name__ == "__main__":
    unittest.main()
