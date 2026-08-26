"""版型(modeling) + 价格区间(budget_min/max) 全链路单元测试。

覆盖：
- sku_attributes：normalize_modeling 枚举校验、expand_modeling 同义词归并。
- intent_engine._build_intent_from_llm：全局 modeling/budget_min/budget_max 解析。
- target_slots per-role：modeling 正向覆盖、budget_min/max 数值校验、非法值丢弃。
- role_slots：effective_role_slots/effective_role_budget（global←per-role 覆盖）、
  build_modeling_price_milvus_expr（Milvus expr 字符串）。
- es_intent._build_intent_filters：意图级 modeling terms + price range；per-role
  effective 注入（resolve_es_query_for_role 的 skip_modeling 兜底）。
"""

from __future__ import annotations

import unittest

from backend.intent.intent_engine import _build_intent_from_llm
from backend.intent.role_slots import (
    build_modeling_price_milvus_expr,
    build_role_es_must_not,
    build_role_es_positive_filters,
    effective_role_budget,
    effective_role_slots,
)
from backend.intent.sku_attributes import (
    expand_modeling,
    normalize_attr_enum,
    normalize_modeling,
)
from backend.models import UserIntent
from backend.retrieval.es_intent import _build_intent_filters


class NormalizeModelingTest(unittest.TestCase):
    def test_valid_enum_passthrough(self):
        for v in ("宽松", "基础", "舒适", "修身", "紧身", "超宽松", "ACTIVE"):
            self.assertEqual(normalize_modeling(v), v)
            self.assertEqual(normalize_attr_enum("modeling", v), v)

    def test_invalid_falls_back_to_empty(self):
        self.assertEqual(normalize_modeling("宽松版型"), "")
        self.assertEqual(normalize_modeling(None), "")
        self.assertEqual(normalize_modeling(""), "")
        self.assertEqual(normalize_attr_enum("modeling", "xx"), "")


class ExpandModelingTest(unittest.TestCase):
    def test_synonym_merge(self):
        self.assertEqual(set(expand_modeling("宽松")), {"宽松", "超宽松"})
        self.assertEqual(set(expand_modeling("修身")), {"修身", "紧身"})
        self.assertEqual(expand_modeling("紧身"), ["紧身"])
        self.assertEqual(expand_modeling("超宽松"), ["超宽松"])
        self.assertEqual(expand_modeling("基础"), ["基础"])

    def test_empty_and_invalid(self):
        self.assertEqual(expand_modeling(""), [])
        self.assertEqual(expand_modeling("不存在"), [])


def _raw(**kw) -> dict:
    base = {
        "query_type": "item_to_outfit",
        "anchor_role": "top",
        "gender": "男",
        "season": ["秋"],
        "category": ["短袖T"],
    }
    base.update(kw)
    return base


class BuildIntentFromLlmModelingPriceTest(unittest.TestCase):
    def test_global_modeling_and_budget_parsed(self):
        intent = _build_intent_from_llm("", _raw(
            modeling="宽松", budget_max=500, budget_min=200))
        self.assertEqual(intent.modeling, "宽松")
        self.assertEqual(intent.budget_min, 200.0)
        self.assertEqual(intent.budget_max, 500.0)

    def test_invalid_modeling_dropped(self):
        intent = _build_intent_from_llm("", _raw(modeling="宽松版型"))
        self.assertIsNone(intent.modeling)

    def test_non_positive_budget_dropped(self):
        intent = _build_intent_from_llm("", _raw(budget_min=-1, budget_max="abc"))
        self.assertIsNone(intent.budget_min)
        self.assertIsNone(intent.budget_max)


class PerRoleModelingPriceTest(unittest.TestCase):
    def test_per_role_modeling_positive(self):
        intent = _build_intent_from_llm("", _raw(
            target_slots={
                "bottoms": {
                    "positive": {"modeling": "修身", "budget_max": 400},
                    "negative": {},
                },
            },
        ))
        eff = effective_role_slots(intent, "bottoms")
        self.assertEqual(eff["modeling"], "修身")
        bmin, bmax = effective_role_budget(intent, "bottoms")
        self.assertIsNone(bmin)
        self.assertEqual(bmax, 400.0)

    def test_per_role_modeling_negative(self):
        intent = _build_intent_from_llm("", _raw(
            target_slots={
                "bottoms": {"positive": {}, "negative": {"modeling": ["修身"]}},
            },
        ))
        must_not = build_role_es_must_not(intent, "bottoms")
        # 修身 → {修身, 紧身} 并集 terms
        self.assertTrue(any(
            f == {"terms": {"modeling": ["修身", "紧身"]}} or
            f == {"terms": {"modeling": ["紧身", "修身"]}}
            for f in must_not
        ), must_not)

    def test_per_role_budget_invalid_dropped(self):
        intent = _build_intent_from_llm("", _raw(
            target_slots={
                "top": {"positive": {"budget_max": "免费"}, "negative": {}},
            },
        ))
        bmin, bmax = effective_role_budget(intent, "top")
        self.assertIsNone(bmin)
        self.assertIsNone(bmax)

    def test_global_modeling_inherited_when_no_per_role(self):
        intent = _build_intent_from_llm("", _raw(modeling="宽松"))
        # 无 per-role 覆盖时，effective 继承全局
        self.assertEqual(effective_role_slots(intent, "bottoms")["modeling"], "宽松")

    def test_per_role_modeling_overrides_global(self):
        intent = _build_intent_from_llm("", _raw(
            modeling="宽松",
            target_slots={
                "bottoms": {"positive": {"modeling": "修身"}, "negative": {}},
            },
        ))
        self.assertEqual(effective_role_slots(intent, "bottoms")["modeling"], "修身")
        # 其它 role 仍继承全局
        self.assertEqual(effective_role_slots(intent, "top")["modeling"], "宽松")


class MilvusExprTest(unittest.TestCase):
    def test_modeling_synonym_expanded_in_expr(self):
        intent = _build_intent_from_llm("", _raw(
            target_slots={
                "top": {"positive": {"modeling": "宽松"}, "negative": {}},
            },
        ))
        expr = build_modeling_price_milvus_expr(intent, "top")
        self.assertIsNotNone(expr)
        self.assertIn('modeling in ["宽松","超宽松"]', expr)

    def test_price_range_in_expr(self):
        intent = _build_intent_from_llm("", _raw(
            target_slots={
                "shoes": {
                    "positive": {"budget_min": 800, "budget_max": 1500},
                    "negative": {},
                },
            },
        ))
        expr = build_modeling_price_milvus_expr(intent, "shoes")
        self.assertIsNotNone(expr)
        self.assertIn("price >= 800.0", expr)
        self.assertIn("price <= 1500.0", expr)

    def test_no_constraint_returns_none(self):
        intent = _build_intent_from_llm("", _raw())
        self.assertIsNone(build_modeling_price_milvus_expr(intent, "top"))


class EsIntentFiltersTest(unittest.TestCase):
    def test_intent_level_modeling_terms_and_price_range(self):
        intent = _build_intent_from_llm("", _raw(
            modeling="宽松", budget_min=200, budget_max=500))
        filters = _build_intent_filters(intent)
        # modeling 同义词展开为 terms
        modeling_f = [f for f in filters if "modeling" in f.get("terms", {})]
        self.assertEqual(len(modeling_f), 1)
        self.assertEqual(set(modeling_f[0]["terms"]["modeling"]), {"宽松", "超宽松"})
        # price 区间
        price_f = [f for f in filters if "price" in f.get("range", {})]
        self.assertEqual(len(price_f), 1)
        rng = price_f[0]["range"]["price"]
        self.assertEqual(rng["gte"], 200.0)
        self.assertEqual(rng["lte"], 500.0)

    def test_skip_modeling_and_skip_budget(self):
        intent = _build_intent_from_llm("", _raw(
            modeling="宽松", budget_max=500))
        filters = _build_intent_filters(intent, skip_modeling=True, skip_budget=True)
        self.assertFalse(any("modeling" in f.get("terms", {}) for f in filters))
        self.assertFalse(any("price" in f.get("range", {}) for f in filters))

    def test_per_role_modeling_es_terms_exclude_in_positive_filters(self):
        """build_role_es_positive_filters 在 exclude_slots 含 modeling 时不应产出 modeling。"""
        intent = _build_intent_from_llm("", _raw(
            target_slots={
                "top": {"positive": {"modeling": "宽松"}, "negative": {}},
            },
        ))
        filters = build_role_es_positive_filters(
            intent, "top", exclude_slots=("color_series", "category", "modeling"))
        self.assertFalse(any("modeling" in f.get("terms", {}) for f in filters))

        # 不排除时产出同义词展开 terms
        filters2 = build_role_es_positive_filters(
            intent, "top", exclude_slots=("color_series", "category"))
        self.assertTrue(any(
            set(f.get("terms", {}).get("modeling", [])) == {"宽松", "超宽松"}
            for f in filters2
        ), filters2)


if __name__ == "__main__":
    unittest.main()
