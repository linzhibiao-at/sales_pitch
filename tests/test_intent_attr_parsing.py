"""意图模块解析 length_class/coverage/scene_domain 单元测试。

验证：
- normalize_attr_enum：合法值直通，非法/缺失 → 中性默认。
- _build_intent_from_llm：LLM raw 输出经归一落到 UserIntent。
- resolve_anchor_attrs：LLM 解析值（确定）填入 anchor_attrs；n/a/"" 不屏蔽 enrich。
- 端到端：梭织两件套(LLM coverage=full) × 五分裤 被「全身装×上下装」规则拦截；
  冲锋衣(LLM scene=outdoor) × 骑行裤 被 outdoor×cycling 跨营规则拦截。
"""

from __future__ import annotations

import unittest

from backend.intent.intent_engine import _build_intent_from_llm, resolve_anchor_attrs
from backend.intent.sku_attributes import normalize_attr_enum
from backend.ranking.outfit_conflict import (
    build_attr_es_filter,
    build_scene_domain_es_filter,
    check_companion_conflict,
)


class NormalizeAttrEnumTest(unittest.TestCase):
    def test_valid_values_passthrough(self):
        self.assertEqual(normalize_attr_enum("length_class", "long"), "long")
        self.assertEqual(normalize_attr_enum("length_class", "short"), "short")
        self.assertEqual(normalize_attr_enum("length_class", "n/a"), "n/a")
        self.assertEqual(normalize_attr_enum("coverage", "full"), "full")
        self.assertEqual(normalize_attr_enum("scene_domain", "outdoor"), "outdoor")
        self.assertEqual(normalize_attr_enum("scene_domain", ""), "")

    def test_invalid_falls_back_to_neutral(self):
        self.assertEqual(normalize_attr_enum("length_class", "LONG"), "n/a")
        self.assertEqual(normalize_attr_enum("length_class", "xxx"), "n/a")
        self.assertEqual(normalize_attr_enum("length_class", None), "n/a")
        self.assertEqual(normalize_attr_enum("coverage", "FULL"), "n/a")
        self.assertEqual(normalize_attr_enum("scene_domain", "sport"), "")
        self.assertEqual(normalize_attr_enum("scene_domain", None), "")

    def test_unknown_key_returns_raw(self):
        self.assertEqual(normalize_attr_enum("no_such_key", "abc"), "abc")


class BuildIntentFromLlmAttrTest(unittest.TestCase):
    def _raw(self, **kw) -> dict:
        base = {
            "query_type": "item_to_outfit",
            "anchor_role": "top",
            "gender": "男",
            "season": ["秋", "冬"],
            "category": ["单层冲锋衣"],
        }
        base.update(kw)
        return base

    def test_llm_attrs_normalized_onto_intent(self):
        intent = _build_intent_from_llm("", self._raw(
            length_class="long", coverage="full", scene_domain="outdoor"))
        self.assertEqual(intent.length_class, "long")
        self.assertEqual(intent.coverage, "full")
        self.assertEqual(intent.scene_domain, "outdoor")

    def test_llm_invalid_attrs_fall_back_neutral(self):
        intent = _build_intent_from_llm("", self._raw(
            length_class="LONG", coverage="FULL", scene_domain="sport"))
        self.assertEqual(intent.length_class, "n/a")
        self.assertEqual(intent.coverage, "n/a")
        self.assertEqual(intent.scene_domain, "")

    def test_llm_missing_attrs_fall_neutral(self):
        intent = _build_intent_from_llm("", self._raw())
        # 缺失 → 中性默认：length/coverage → n/a，scene → ""
        self.assertEqual(intent.length_class, "n/a")
        self.assertEqual(intent.coverage, "n/a")
        self.assertEqual(intent.scene_domain, "")


class ResolveAnchorAttrsLlmAttrTest(unittest.TestCase):
    def test_llm_definitive_attrs_fill_virtual_anchor(self):
        intent = _build_intent_from_llm("", {
            "anchor_role": "top", "gender": "男", "season": ["秋", "冬"],
            "category": ["梭织两件套"],
            "length_class": "long", "coverage": "full", "scene_domain": "outdoor",
        })
        attrs = resolve_anchor_attrs(intent, None, 0.6, 0.9)
        self.assertEqual(attrs["length_class"], "long")
        self.assertEqual(attrs["coverage"], "full")
        self.assertEqual(attrs["scene_domain"], "outdoor")

    def test_llm_na_does_not_shadow_enrich(self):
        """LLM 给 length=n/a 时不应屏蔽 enrich 从 category 派生的 long。"""
        intent = _build_intent_from_llm("", {
            "anchor_role": "top", "gender": "男", "season": ["秋", "冬"],
            "category": ["单层冲锋衣"],
            "length_class": "n/a", "coverage": "upper", "scene_domain": "",
        })
        attrs = resolve_anchor_attrs(intent, None, 0.6, 0.9)
        self.assertEqual(attrs["length_class"], "long")  # enrich 从 冲锋衣 派生
        self.assertEqual(attrs["scene_domain"], "outdoor")  # enrich 扫 category_l2


class EndToEndAttrConflictTest(unittest.TestCase):
    def test_liangjiantao_full_coverage_rejects_short_pants(self):
        """梭织两件套(LLM coverage=full) × 五分裤 → 全身装×上下装 规则拦截。"""
        intent = _build_intent_from_llm("", {
            "anchor_role": "top", "gender": "男童", "season": ["春", "夏"],
            "category": ["梭织两件套"],
            "length_class": "long", "coverage": "full", "scene_domain": "daily",
        })
        attrs = resolve_anchor_attrs(intent, None, 0.6, 0.9)
        five = {
            "role": "bottoms", "category_l2": "梭织五分裤",
            "length_class": "short", "coverage": "lower",
            "scene_domain": "daily", "is_intimate": False,
        }
        self.assertTrue(check_companion_conflict(attrs, five))
        # ES 预过滤也应排除 coverage=full 与 length_class=short
        es = build_attr_es_filter(attrs, "bottoms")
        must_not = es["must_not"]
        self.assertIn({"term": {"length_class": "short"}}, must_not)
        self.assertIn({"term": {"coverage": "full"}}, must_not)

    def test_chongfengyi_outdoor_rejects_cycling_pants(self):
        """冲锋衣(LLM scene=outdoor) × 骑行裤(cycling) → 跨营规则拦截。"""
        intent = _build_intent_from_llm("", {
            "anchor_role": "top", "gender": "男", "season": ["秋", "冬"],
            "category": ["单层冲锋衣"],
            "length_class": "long", "coverage": "upper", "scene_domain": "outdoor",
        })
        attrs = resolve_anchor_attrs(intent, None, 0.6, 0.9)
        cycling = {
            "role": "bottoms", "category_l2": "针织裤",
            "length_class": "long", "coverage": "lower",
            "scene_domain": "cycling", "is_intimate": False,
        }
        self.assertTrue(check_companion_conflict(attrs, cycling))
        # scene_domain 正向隔离：outdoor 锚点仅允许 outdoor，cycling 不在允许集
        scene_filter = build_scene_domain_es_filter(attrs, "bottoms")
        self.assertIsNotNone(scene_filter)
        allow = scene_filter["terms"]["scene_domain"]
        self.assertIn("outdoor", allow)
        self.assertNotIn("cycling", allow)


if __name__ == "__main__":
    unittest.main()
