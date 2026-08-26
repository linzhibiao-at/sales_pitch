"""target_slots 统一 positive/negative 的 negative 归一测试。

结构：target_slots[role]={"positive":{...},"negative":{slot:[values]}}。
验证：长度词转 length_class、标量列表保留、非法丢弃、合法 L2 保留。
"""

from __future__ import annotations

import unittest

from backend.intent.intent_engine import _normalize_target_slots


def _neg(role: str, slot_map: dict) -> dict:
    return {role: {"positive": {}, "negative": slot_map}}


class NegCategoryToLengthTest(unittest.TestCase):
    def test_short_pants_aggregate_becomes_length_class_short(self):
        out = _normalize_target_slots(_neg("bottoms", {"category": ["短裤类"]}))
        self.assertEqual(out["bottoms"]["negative"]["length_class"], ["short"])
        self.assertNotIn("category", out["bottoms"]["negative"])

    def test_short_alias_also_becomes_length_class(self):
        out = _normalize_target_slots(_neg("bottoms", {"category": ["短裤"]}))
        self.assertEqual(out["bottoms"]["negative"]["length_class"], ["short"])

    def test_long_pants_aggregate_becomes_length_class_long(self):
        out = _normalize_target_slots(_neg("bottoms", {"category": ["长裤"]}))
        self.assertEqual(out["bottoms"]["negative"]["length_class"], ["long"])

    def test_specific_valid_l2_kept_as_category(self):
        out = _normalize_target_slots(_neg("bottoms", {"category": ["梭织短裤"]}))
        self.assertEqual(out["bottoms"]["negative"]["category"], ["梭织短裤"])
        self.assertNotIn("length_class", out["bottoms"]["negative"])

    def test_valid_shoe_category_kept(self):
        out = _normalize_target_slots(_neg("shoes", {"category": ["拖鞋"]}))
        self.assertEqual(out["shoes"]["negative"]["category"], ["拖鞋"])

    def test_unknown_category_dropped(self):
        out = _normalize_target_slots(_neg("bottoms", {"category": ["不存在的品类"]}))
        self.assertNotIn("bottoms", out)

    def test_mixed_aggregate_and_specific(self):
        out = _normalize_target_slots(_neg("bottoms", {"category": ["短裤类", "梭织长裤"]}))
        b = out["bottoms"]["negative"]
        self.assertEqual(b["length_class"], ["short"])
        self.assertEqual(b["category"], ["梭织长裤"])


class NegScalarListTest(unittest.TestCase):
    def test_length_class_list_kept(self):
        out = _normalize_target_slots(_neg("bottoms", {"length_class": ["short"]}))
        self.assertEqual(out["bottoms"]["negative"]["length_class"], ["short"])

    def test_length_class_scalar_also_kept(self):
        out = _normalize_target_slots(_neg("bottoms", {"length_class": "short"}))
        self.assertEqual(out["bottoms"]["negative"]["length_class"], ["short"])

    def test_coverage_list_kept(self):
        out = _normalize_target_slots(_neg("bottoms", {"coverage": ["upper"]}))
        self.assertEqual(out["bottoms"]["negative"]["coverage"], ["upper"])

    def test_scene_domain_list_kept(self):
        out = _normalize_target_slots(_neg("bottoms", {"scene_domain": ["outdoor"]}))
        self.assertEqual(out["bottoms"]["negative"]["scene_domain"], ["outdoor"])

    def test_invalid_scalar_value_dropped(self):
        out = _normalize_target_slots(_neg("bottoms", {"length_class": ["中长"]}))
        self.assertNotIn("bottoms", out)


class GlobalNegTest(unittest.TestCase):
    def test_global_neg_under_star(self):
        out = _normalize_target_slots({"*": {"positive": {}, "negative": {"color_series": ["粉色系"]}}})
        self.assertEqual(out["*"]["negative"]["color_series"], ["粉色系"])
        self.assertEqual(out["*"].get("positive"), {})  # "*" positive 恒空（被忽略）

    def test_star_positive_ignored(self):
        out = _normalize_target_slots({"*": {"positive": {"color": ["黑色"]}, "negative": {"color_series": ["粉色系"]}}})
        self.assertEqual(out.get("*", {}).get("positive"), {})  # "*" 下的 positive 被忽略
        self.assertEqual(out["*"]["negative"]["color_series"], ["粉色系"])


if __name__ == "__main__":
    unittest.main()
