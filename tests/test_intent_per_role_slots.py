"""per-target-role 槽位（统一 positive/negative）解析与召回下推测试。

结构：target_slots[role]={"positive":{slot:val},"negative":{slot:[vals]}}，
全局否定放 target_slots["*"].negative（无 positive）。

覆盖：
  - _build_intent_from_llm 解析 target_slots（枚举归一、越界丢弃、正否定冲突剔除、
    否定 category 长度词转 length_class）
  - role_slots 合并语义（override、锚点描述性标量不继承、"*" 全局否定合并）
  - build_role_milvus_expr_parts / _build_role_milvus_expr 的正向下推与否定下推
  - _backfill_from_context 多轮继承 target_slots
"""

from __future__ import annotations

import unittest

from backend.intent.intent_engine import (
    _backfill_from_context,
    _build_intent_from_llm,
)
from backend.intent.role_slots import (
    build_role_milvus_expr_parts,
    effective_role_slots,
    role_negative_slots,
)
from backend.models import UserIntent


def _build_role_milvus_expr(intent, role, *, anchor_row=None):
    # 延迟导入避免循环依赖
    from backend.services.complementary_recall import _build_role_milvus_expr as _f
    return _f(intent, role, anchor_row=anchor_row)


class TestParseTargetSlots(unittest.TestCase):
    def test_parse_and_normalize(self):
        raw = {
            "anchor_role": "top",
            "target_roles": ["bottoms", "shoes"],
            "gender": "男",
            "season": ["秋"],
            "category": ["圆领T恤"],
            "target_slots": {
                "shoes": {
                    "positive": {"color": ["黑色"], "color_series": ["黑色系"],
                                 "category": ["运动鞋"], "scene_domain": "golf"},
                    "negative": {},
                },
                "*": {"positive": {}, "negative": {"color_series": ["粉色系"]}},
                "bottoms": {"positive": {}, "negative": {"category": ["短裙"], "length_class": ["short"]}},
            },
        }
        intent = _build_intent_from_llm("配一下黑色的鞋子", raw)
        self.assertEqual(intent.target_slots["shoes"]["positive"]["color"], ["黑色"])
        self.assertEqual(intent.target_slots["shoes"]["positive"]["color_series"], ["黑色系"])
        self.assertEqual(intent.target_slots["shoes"]["positive"]["scene_domain"], "golf")
        self.assertEqual(intent.target_slots["*"]["negative"]["color_series"], ["粉色系"])
        # 「短裙」非合法 L2（真实值为 半身裙）→ 长度词转 length_class=short（与既有去重）
        self.assertEqual(intent.target_slots["bottoms"]["negative"]["length_class"], ["short"])
        self.assertNotIn("category", intent.target_slots["bottoms"]["negative"])

    def test_invalid_enum_dropped(self):
        raw = {
            "target_roles": ["shoes"],
            "target_slots": {
                "shoes": {"positive": {"length_class": "短", "color_series": ["彩虹色"]}, "negative": {}},
                "*": {"positive": {}, "negative": {"length_class": ["中长"]}},
            },
        }
        intent = _build_intent_from_llm("", raw)
        # 「短」非 long/short/n/a → 丢弃；「彩虹色」非基础色系 → 丢弃；否定「中长」同理
        # shoes positive 无任何有效 slot，"*" negative 也无 → 整个 target_slots 为空
        self.assertEqual(intent.target_slots, {})

    def test_neg_pos_conflict_stripped(self):
        raw = {
            "target_roles": ["bottoms"],
            "target_slots": {
                "bottoms": {"positive": {"color_series": ["黑色系"]},
                            "negative": {"color_series": ["黑色系"]}},
            },
        }
        intent = _build_intent_from_llm("", raw)
        # 同 role 同 slot 既 positive 又 negative → 剔除该 negative 项
        self.assertEqual(intent.target_slots["bottoms"]["positive"]["color_series"], ["黑色系"])
        self.assertNotIn("color_series", intent.target_slots["bottoms"]["negative"])


class TestRoleSlotsMerge(unittest.TestCase):
    def _intent(self, **kw) -> UserIntent:
        base = dict(
            anchor_role="top",
            target_roles=["bottoms", "shoes"],
            color_series=["蓝色系"],
            category=["圆领T恤"],
            length_class="long",
            scene_domain="daily",
        )
        base.update(kw)
        return UserIntent(**base)

    def test_override_replaces_global(self):
        i = self._intent(target_slots={"shoes": {
            "positive": {"color_series": ["黑色系"], "category": ["运动鞋"]}, "negative": {}}})
        shoes = effective_role_slots(i, "shoes")
        self.assertEqual(shoes["color_series"], ["黑色系"])
        self.assertEqual(shoes["category"], ["运动鞋"])

    def test_no_override_inherits_shared_list(self):
        i = self._intent(target_slots={"shoes": {
            "positive": {"color_series": ["黑色系"]}, "negative": {}}})
        bottoms = effective_role_slots(i, "bottoms")
        # color_series 不再继承全局（那是锚点颜色，推到其他 role 会零召回）；
        # 由调用方（complementary）用搭配色系兜底。per-role 未指定 → 空。
        self.assertEqual(bottoms["color_series"], [])
        # category 不再继承全局 intent.category（那是锚点品类，推到其他 role 会零召回）
        self.assertEqual(bottoms["category"], [])

    def test_anchor_describing_scalars_not_inherited(self):
        """锚点的 length_class/scene_domain 不应作为 target 默认。"""
        i = self._intent(target_slots={"shoes": {
            "positive": {"scene_domain": "golf"}, "negative": {}}})
        bottoms = effective_role_slots(i, "bottoms")
        self.assertIsNone(bottoms["length_class"])
        self.assertIsNone(bottoms["scene_domain"])
        self.assertIsNone(bottoms["coverage"])
        # shoes 显式给出 scene_domain → 生效
        shoes = effective_role_slots(i, "shoes")
        self.assertEqual(shoes["scene_domain"], "golf")

    def test_negative_global_plus_role_merge(self):
        i = self._intent(target_slots={
            "*": {"positive": {}, "negative": {"color_series": ["粉色系"]}},
            "bottoms": {"positive": {}, "negative": {"category": ["短裙"], "length_class": ["short"]}},
        })
        self.assertEqual(role_negative_slots(i, "shoes"), {"color_series": ["粉色系"]})
        neg = role_negative_slots(i, "bottoms")
        self.assertEqual(set(neg["color_series"]), {"粉色系"})
        self.assertEqual(neg["category"], ["短裙"])
        self.assertEqual(neg["length_class"], ["short"])


class TestMilvusExprPushdown(unittest.TestCase):
    def _intent(self, **kw) -> UserIntent:
        base = dict(
            anchor_role="top",
            target_roles=["bottoms", "shoes"],
            color_series=["蓝色系"],
            category=["圆领T恤"],
            length_class="long",
            scene_domain="daily",
        )
        base.update(kw)
        return UserIntent(**base)

    def _anchor(self) -> dict:
        return {"role": "top", "length_class": "long", "coverage": "upper",
                "layer": "base", "scene_domain": "daily"}

    def test_shoes_positive_and_negative_pushdown(self):
        i = self._intent(target_slots={
            "shoes": {"positive": {"color_series": ["黑色系"], "category": ["运动鞋"],
                                   "scene_domain": "golf"}, "negative": {}},
            "*": {"positive": {}, "negative": {"color_series": ["粉色系"]}},
        })
        parts = build_role_milvus_expr_parts(i, "shoes")
        self.assertIn('array_contains_any(color_series, ["黑色系"])', parts)
        self.assertIn('category_l2 in ["运动鞋"]', parts)
        self.assertIn('scene_domain == "golf"', parts)
        self.assertIn('not array_contains_any(color_series, ["粉色系"])', parts)
        # 锚点描述性标量不应下推为 target 正向
        self.assertFalse(any(p.startswith("length_class ==") for p in parts))

    def test_anchor_category_not_pushed_to_other_roles(self):
        """intent.category 是锚点品类（如 短袖编织衫），不应作为其他 role 的 category_l2 过滤，
        否则 bottoms/shoes 零召回（complementary 通路 include_global=True）。"""
        i = self._intent(category=["短袖编织衫"], target_slots={
            "bottoms": {"positive": {"color_series": ["粉色系"]}, "negative": {}},
        })
        parts = build_role_milvus_expr_parts(i, "bottoms")  # include_global=True
        self.assertFalse(
            any("短袖编织衫" in p for p in parts),
            f"锚点品类不应出现在 bottoms expr: {parts}",
        )
        self.assertFalse(any(p.startswith("category_l2 in") for p in parts))

    def test_bottoms_negative_pushdown(self):
        i = self._intent(target_slots={
            "bottoms": {"positive": {}, "negative": {"category": ["短裙"], "length_class": ["short"]}},
        })
        expr = _build_role_milvus_expr(i, "bottoms", anchor_row=self._anchor())
        self.assertIn('category_l2 not in ["短裙"]', expr)
        self.assertIn('length_class not in ["short"]', expr)
        # 无 per-role color → 用锚点颜色的搭配色系（ARRAY：array_contains_any），而非全局单色
        self.assertIn("array_contains_any(color_series, [", expr)

    def test_no_per_role_data_no_change(self):
        i = self._intent()
        expr = _build_role_milvus_expr(i, "bottoms", anchor_row=self._anchor())
        # 无 per-role 数据：color_series 用搭配色系兜底（ARRAY：array_contains_any），无否定下推
        self.assertIn("array_contains_any(color_series, [", expr)
        self.assertNotIn("not in", expr)

    def test_scene_domain_override_skips_anchor_isolation(self):
        """用户为 role 显式 scene_domain → 覆盖锚点场景隔离（不再 scene_domain in [...])。"""
        i = self._intent(target_slots={"shoes": {
            "positive": {"scene_domain": "golf"}, "negative": {}}})
        expr = _build_role_milvus_expr(i, "shoes", anchor_row=self._anchor())
        self.assertIn('scene_domain == "golf"', expr)
        self.assertNotIn('scene_domain in [', expr)

    def test_no_scene_override_keeps_anchor_isolation(self):
        i = self._intent()
        expr = _build_role_milvus_expr(i, "shoes", anchor_row=self._anchor())
        self.assertIn('scene_domain in [', expr)
        self.assertNotIn('scene_domain ==', expr)

    def test_include_global_false_only_pushes_overrides(self):
        """通路2（include_global=False）：只推 per-role 覆盖，不推全局 color_series。"""
        i = self._intent(target_slots={"shoes": {
            "positive": {"color_series": ["黑色系"]}, "negative": {}}})
        parts = build_role_milvus_expr_parts(i, "shoes", include_global=False)
        self.assertIn('array_contains_any(color_series, ["黑色系"])', parts)
        self.assertNotIn('蓝色系', " ".join(parts))


class TestESPushdown(unittest.TestCase):
    def _intent(self, **kw) -> UserIntent:
        base = dict(
            anchor_role="top", target_roles=["shoes"],
            color_series=["蓝色系"], category=["圆领T恤"],
        )
        base.update(kw)
        return UserIntent(**base)

    def test_es_positive_and_negative(self):
        from backend.intent.role_slots import (
            build_role_es_must_not,
            build_role_es_positive_filters,
        )
        # 直接构造 UserIntent 不走归一；shoes 的否定直接放 shoes.negative
        i = self._intent(target_slots={
            "shoes": {"positive": {"color_series": ["黑色系"], "category": ["运动鞋"],
                                   "scene_domain": "golf"},
                      "negative": {"length_class": ["short"]}},
            "*": {"positive": {}, "negative": {"color_series": ["粉色系"]}},
        })
        pos = build_role_es_positive_filters(i, "shoes")
        self.assertIn({"term": {"color_series": "黑色系"}}, pos)
        self.assertIn({"term": {"category_l2": "运动鞋"}}, pos)
        self.assertIn({"term": {"scene_domain": "golf"}}, pos)
        mn = build_role_es_must_not(i, "shoes")
        self.assertIn({"term": {"color_series": "粉色系"}}, mn)
        self.assertIn({"term": {"length_class": "short"}}, mn)
        # 全局 color_series 不应在 per-role 正向里（通路3 全局由 cs_filter 负责）
        self.assertFalse(any({"term": {"color_series": "蓝色系"}} == p for p in pos))


class TestBackfillFromContext(unittest.TestCase):
    def test_inherit_per_role_when_empty(self):
        intent = UserIntent(query_type="item_to_outfit", text="", anchor_role="top")
        ctx = {"prev_intent": {
            "target_slots": {"shoes": {"positive": {"color_series": ["黑色系"]}, "negative": {}}},
        }}
        out = _backfill_from_context(intent, ctx)
        self.assertEqual(out.target_slots,
                         {"shoes": {"positive": {"color_series": ["黑色系"]}, "negative": {}}})

    def test_keep_explicit_per_role(self):
        intent = UserIntent(
            target_slots={"shoes": {"positive": {"color_series": ["白色系"]}, "negative": {}}},
        )
        ctx = {"prev_intent": {"target_slots": {
            "shoes": {"positive": {"color_series": ["黑色系"]}, "negative": {}}}}}
        out = _backfill_from_context(intent, ctx)
        # 当前轮显式给出 → 不被上一轮覆盖
        self.assertEqual(out.target_slots["shoes"]["positive"]["color_series"], ["白色系"])


class TestResolveEsQueryNoDupFilters(unittest.TestCase):
    """resolve_es_query_for_role：per-role 覆盖不应与 cs_filter/cat2_filter 产生重复条件。"""

    def _intent(self, **kw) -> UserIntent:
        base = dict(
            anchor_role="top",
            target_roles=["bottoms", "shoes"],
            text="搭配黑色裤子和白色鞋子",
            gender="女",
            season=["夏"],
            color_series=["黑色系"],
            category=["圆领T恤"],
        )
        base.update(kw)
        return UserIntent(**base)

    @staticmethod
    def _filter_field_counts(query: dict) -> dict:
        """统计 bool.filter 中 term/terms 子句按字段的命中次数。"""
        counts: dict[str, int] = {}
        flt = (query.get("bool") or {}).get("filter") or []
        for clause in flt:
            if not isinstance(clause, dict):
                continue
            for op in ("term", "terms"):
                node = clause.get(op)
                if isinstance(node, dict):
                    for field in node:
                        counts[field] = counts.get(field, 0) + 1
        return counts

    def _resolve(self, intent, role, *, allowed_cat2, allowed_cs):
        from backend.retrieval.es_intent import resolve_es_query_for_role
        es_query, _meta = resolve_es_query_for_role(
            intent,
            role,
            index_name="skus",
            llm_enabled=False,
            allowed_companion_cat2=allowed_cat2,
            allowed_companion_color_series=allowed_cs,
        )
        return es_query

    def test_per_role_color_series_not_duplicated(self):
        """per-role color_series 由 cs_filter 统一处理，filter 中只出现一次。"""
        intent = self._intent(target_slots={
            "shoes": {"positive": {"color_series": ["白色系"]}, "negative": {}},
        })
        q = self._resolve(intent, "shoes",
                          allowed_cat2=["老爹鞋"], allowed_cs=["黑色系"])
        counts = self._filter_field_counts(q)
        self.assertEqual(counts.get("color_series"), 1)
        self.assertIn({"term": {"color_series": "白色系"}}, q["bool"]["filter"])

    def test_per_role_category_not_duplicated(self):
        """per-role category 覆盖 pairing 列表，filter 中 category_l2 只出现一次。"""
        intent = self._intent(target_slots={
            "bottoms": {"positive": {"category": ["梭织长裤"]}, "negative": {}},
        })
        q = self._resolve(intent, "bottoms",
                          allowed_cat2=["半身裙", "梭织长裤"], allowed_cs=None)
        counts = self._filter_field_counts(q)
        self.assertEqual(counts.get("category_l2"), 1)
        self.assertIn({"term": {"category_l2": "梭织长裤"}}, q["bool"]["filter"])

    def test_per_role_color_and_category_both_not_duplicated(self):
        """用户同时指定 per-role color_series + category：两者均只出现一次。"""
        intent = self._intent(target_slots={
            "bottoms": {"positive": {"color_series": ["黑色系"], "category": ["梭织长裤"]},
                         "negative": {}},
        })
        q = self._resolve(intent, "bottoms",
                          allowed_cat2=["半身裙", "梭织长裤"], allowed_cs=["黑色系"])
        counts = self._filter_field_counts(q)
        self.assertEqual(counts.get("color_series"), 1)
        self.assertEqual(counts.get("category_l2"), 1)

    def test_pairing_list_used_when_no_per_role_category(self):
        """无 per-role category 时沿用 pairing 列表（terms 多值），且仅一次。"""
        intent = self._intent(target_slots={"bottoms": {"positive": {}, "negative": {}}})
        q = self._resolve(intent, "bottoms",
                          allowed_cat2=["半身裙", "梭织长裤"], allowed_cs=None)
        counts = self._filter_field_counts(q)
        self.assertEqual(counts.get("category_l2"), 1)
        self.assertIn({"terms": {"category_l2": ["半身裙", "梭织长裤"]}},
                      q["bool"]["filter"])


if __name__ == "__main__":
    unittest.main()