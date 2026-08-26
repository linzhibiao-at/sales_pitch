"""anchor_graph 通路 intent 符合检测测试。

bug：固定搭配通路只做 target_role/gender/season/冲突过滤，不校验 item 是否符合
intent 的 per-role positive（color/color_series/category/length_class/...）与
negative，导致黑裤（不符"灰色"要求）的搭配存活到最终结果。
"""

from __future__ import annotations

import unittest

from backend.models import UserIntent
from backend.services.outfit_recall import _item_violates_intent


def _intent(**kw) -> UserIntent:
    base = dict(
        anchor_role="top", target_roles=["bottoms", "shoes"],
        gender="女", season=["秋"],
    )
    base.update(kw)
    return UserIntent(**base)


class ItemViolatesIntentTest(unittest.TestCase):
    def test_positive_color_series_mismatch_violates(self):
        i = _intent(target_slots={
            "bottoms": {"positive": {"color": ["灰色"], "color_series": ["灰色系"]}, "negative": {}},
        })
        # 黑色系裤子 ≠ 灰色系 → 违反
        black_pants = {"color_series": ["黑色系"], "color_name": "正黑色",
                       "category_l2": "针织长裤", "length_class": "long", "role": "bottoms"}
        violated, reason = _item_violates_intent(black_pants, i, "bottoms")
        self.assertTrue(violated, f"黑裤应违反灰色要求: {reason}")

    def test_positive_color_series_match_ok(self):
        i = _intent(target_slots={
            "bottoms": {"positive": {"color": ["灰色"], "color_series": ["灰色系"]}, "negative": {}},
        })
        gray_pants = {"color_series": ["灰色系"], "color_name": "浅灰",
                      "category_l2": "针织长裤", "length_class": "long", "role": "bottoms"}
        violated, _ = _item_violates_intent(gray_pants, i, "bottoms")
        self.assertFalse(violated)

    def test_multicolor_partial_match_ok(self):
        """多色 SKU 任一色系命中 positive 即不违反（relaxed 语义）。"""
        i = _intent(target_slots={
            "shoes": {"positive": {"color_series": ["蓝色系"]}, "negative": {}},
        })
        # 蓝丝带/雪白 → ["蓝色系","白色系"]，含蓝色系 → 不违反
        shoes = {"color_series": ["蓝色系", "白色系"], "color_name": "蓝丝带/雪白",
                 "category_l2": "运动鞋", "length_class": "n/a", "role": "shoes"}
        violated, _ = _item_violates_intent(shoes, i, "shoes")
        self.assertFalse(violated)

    def test_multicolor_no_match_violates(self):
        """多色 SKU 与 positive 无交集 → 违反。"""
        i = _intent(target_slots={
            "shoes": {"positive": {"color_series": ["灰色系"]}, "negative": {}},
        })
        shoes = {"color_series": ["蓝色系", "白色系"], "color_name": "蓝丝带/雪白",
                 "category_l2": "运动鞋", "length_class": "n/a", "role": "shoes"}
        violated, _ = _item_violates_intent(shoes, i, "shoes")
        self.assertTrue(violated)

    def test_color_series_derived_from_color_name(self):
        """item 无 color_series 时，从 color_name 派生（与索引建库一致）。"""
        i = _intent(target_slots={
            "bottoms": {"positive": {"color_series": ["灰色系"]}, "negative": {}},
        })
        # color_series 缺失，color_name=正黑色 → 派生 ["黑色系"] ≠ 灰色系 → 违反
        black_pants = {"color_series": [], "color_name": "正黑色",
                       "category_l2": "针织长裤", "length_class": "long", "role": "bottoms"}
        violated, _ = _item_violates_intent(black_pants, i, "bottoms")
        self.assertTrue(violated)

    def test_negative_category_violates(self):
        # 合法 L2 category 否定（解析器保留为 category 否定）
        i = _intent(target_slots={
            "bottoms": {"positive": {}, "negative": {"category": ["梭织短裤"]}},
        })
        shorts = {"color_series": ["黑色系"], "category_l2": "梭织短裤",
                  "length_class": "short", "role": "bottoms"}
        violated, _ = _item_violates_intent(shorts, i, "bottoms")
        self.assertTrue(violated)

    def test_negative_length_class_violates(self):
        i = _intent(target_slots={
            "bottoms": {"positive": {}, "negative": {"length_class": ["short"]}},
        })
        shorts = {"color_series": ["灰色系"], "category_l2": "针织短裤",
                  "length_class": "short", "role": "bottoms"}
        violated, _ = _item_violates_intent(shorts, i, "bottoms")
        self.assertTrue(violated)

    def test_global_negative_applies_to_role(self):
        i = _intent(target_slots={
            "*": {"positive": {}, "negative": {"color_series": ["粉色系"]}},
        })
        pink_shoes = {"color_series": ["粉色系"], "category_l2": "运动鞋",
                      "length_class": "n/a", "role": "shoes"}
        violated, _ = _item_violates_intent(pink_shoes, i, "shoes")
        self.assertTrue(violated)

    def test_no_intent_constraints_passes(self):
        i = _intent()  # 无 target_slots
        any_item = {"color_series": ["黑色系"], "category_l2": "针织长裤",
                    "length_class": "long", "role": "bottoms"}
        violated, _ = _item_violates_intent(any_item, i, "bottoms")
        self.assertFalse(violated)

    # ── 版型 modeling（与 modeling_price.py 的 Milvus/ES 路对齐，补 anchor_graph 路）──

    def test_modeling_positive_mismatch_violates(self):
        """item 版型不在用户要求版型（同义词展开后）内 → 违反。宽松→{宽松,超宽松}。"""
        i = _intent(modeling="宽松")
        # 修身裤不在 {宽松,超宽松} → 违反
        slim_pants = {"category_l2": "针织长裤", "modeling": "修身",
                      "role": "bottoms", "price": 399}
        violated, reason = _item_violates_intent(slim_pants, i, "bottoms")
        self.assertTrue(violated, f"修身应违反宽松要求: {reason}")

    def test_modeling_positive_synonym_match_ok(self):
        """宽松要求下，超宽松（同义词）不违反。"""
        i = _intent(modeling="宽松")
        loose_pants = {"category_l2": "针织长裤", "modeling": "超宽松",
                       "role": "bottoms", "price": 399}
        violated, _ = _item_violates_intent(loose_pants, i, "bottoms")
        self.assertFalse(violated)

    def test_modeling_missing_on_item_not_violating(self):
        """item 无 modeling 字段（约 30% SKU 为空）→ 不据此剔除，交由其它规则。"""
        i = _intent(modeling="宽松")
        no_modeling = {"category_l2": "针织长裤", "modeling": "",
                       "role": "bottoms", "price": 399}
        violated, _ = _item_violates_intent(no_modeling, i, "bottoms")
        self.assertFalse(violated)

    def test_modeling_negative_violates(self):
        """per-role 否定版型（含同义词展开）命中 → 违反。修身→{修身,紧身}。"""
        i = _intent(target_slots={
            "bottoms": {"positive": {}, "negative": {"modeling": ["修身"]}},
        })
        tight_pants = {"category_l2": "针织长裤", "modeling": "紧身",
                       "role": "bottoms", "price": 399}
        violated, _ = _item_violates_intent(tight_pants, i, "bottoms")
        self.assertTrue(violated)

    def test_modeling_per_role_overrides_global(self):
        """per-role 显式版型覆盖全局；该 role 用 per-role，其它 role 用全局。"""
        i = _intent(
            modeling="宽松",
            target_slots={
                "bottoms": {"positive": {"modeling": "修身"}, "negative": {}},
            },
        )
        # bottoms: 修身要求 → 宽松裤违反
        loose = {"category_l2": "针织长裤", "modeling": "宽松",
                 "role": "bottoms", "price": 399}
        self.assertTrue(_item_violates_intent(loose, i, "bottoms")[0])
        # shoes: 无 per-role，继承全局宽松 → 超宽松鞋不违反
        shoe = {"category_l2": "运动鞋", "modeling": "超宽松",
                "role": "shoes", "price": 399}
        self.assertFalse(_item_violates_intent(shoe, i, "shoes")[0])

    # ── 价格区间 budget_min/max（bug：鞋子500以下 召回 >500 鞋）──

    def test_budget_max_over_violates(self):
        """item 价格超 budget_max → 违反（核心 bug）。"""
        i = _intent(budget_max=500)
        pricey_shoes = {"category_l2": "老爹鞋", "price": 699,
                        "role": "shoes", "modeling": ""}
        violated, reason = _item_violates_intent(pricey_shoes, i, "shoes")
        self.assertTrue(violated, f"699>500 应违反: {reason}")

    def test_budget_max_under_ok(self):
        i = _intent(budget_max=500)
        cheap_shoes = {"category_l2": "老爹鞋", "price": 399,
                       "role": "shoes", "modeling": ""}
        violated, _ = _item_violates_intent(cheap_shoes, i, "shoes")
        self.assertFalse(violated)

    def test_budget_min_under_violates(self):
        i = _intent(budget_min=300)
        too_cheap = {"category_l2": "老爹鞋", "price": 199,
                     "role": "shoes", "modeling": ""}
        violated, _ = _item_violates_intent(too_cheap, i, "shoes")
        self.assertTrue(violated)

    def test_budget_range_ok(self):
        i = _intent(budget_min=200, budget_max=500)
        mid = {"category_l2": "老爹鞋", "price": 399,
               "role": "shoes", "modeling": ""}
        violated, _ = _item_violates_intent(mid, i, "shoes")
        self.assertFalse(violated)

    def test_budget_per_role_overrides_global(self):
        """per-role budget_max 覆盖全局；该 role 用 per-role 值。"""
        i = _intent(
            budget_max=1000,
            target_slots={
                "shoes": {"positive": {"budget_max": 500}, "negative": {}},
            },
        )
        # shoes: per-role 500 → 699 违反
        shoe = {"category_l2": "老爹鞋", "price": 699,
                "role": "shoes", "modeling": ""}
        self.assertTrue(_item_violates_intent(shoe, i, "shoes")[0])
        # bottoms: 无 per-role budget，继承全局 1000 → 699 不违反
        pants = {"category_l2": "针织长裤", "price": 699,
                 "role": "bottoms", "modeling": ""}
        self.assertFalse(_item_violates_intent(pants, i, "bottoms")[0])

    def test_global_budget_applies_without_target_slots(self):
        """全局 budget_max 即使无 target_slots 也应过滤（外层 gate 不应跳过）。"""
        i = _intent(budget_max=500)  # 无 target_slots
        pricey = {"category_l2": "老爹鞋", "price": 699,
                  "role": "shoes", "modeling": ""}
        violated, _ = _item_violates_intent(pricey, i, "shoes")
        self.assertTrue(violated)


if __name__ == "__main__":
    unittest.main()
