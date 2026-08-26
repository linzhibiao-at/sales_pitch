"""series 系列隔离单元测试（镜像 tests/test_scene_domain.py 结构）。

覆盖：
  - _series_allow_set：同系-only 默认 + 例外（不含中性 ""，空系列不算匹配）
  - build_series_milvus_expr：正向允许集下推（GOLF→series == "GOLF"；无 series→None）
  - build_series_es_filter：ES 正向隔离 filter（镜像 Milvus expr）
  - 鞋豁免：target_role=shoes 或 anchor role=shoes → None（鞋线 ≠ apparel series）
  - check_companion_conflict：同系可搭、跨系列互斥（双向）、companion='' 冲突、鞋豁免
  - 有向例外（series_allow）：FILA FUSION LIFE→X 放行、反向冲突（非对称）
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.intent.sku_attributes import normalize_series
from backend.intent.role_slots import build_role_milvus_expr_parts
from backend.ranking.outfit_conflict import (
    _series_allow_set,
    build_series_es_filter,
    build_series_milvus_expr,
    check_companion_conflict,
)


class TestSeriesAllowSet(unittest.TestCase):
    def test_self_only_default(self):
        # 未列系列 → 仅自身（不含中性 ""）
        self.assertEqual(_series_allow_set("GOLF"), ["GOLF"])
        self.assertEqual(_series_allow_set("ORIGINALE"), ["ORIGINALE"])

    def test_empty_anchor_unconstrained(self):
        self.assertEqual(_series_allow_set(""), [])
        self.assertEqual(_series_allow_set(None), [])  # type: ignore[arg-type]

    def test_exception_union(self):
        # 例外表注入后：FILA FUSION LIFE 允许自身 + X + CLASSICS（不含中性）
        custom = {"FILA FUSION LIFE": ["FILA FUSION X", "FILA FUSION CLASSICS"]}
        with patch(
            "backend.ranking.outfit_conflict._load_series_config",
            return_value=custom,
        ):
            from backend.ranking import outfit_conflict as oc
            oc._load_series_config.cache_clear()
            try:
                allow = _series_allow_set("FILA FUSION LIFE")
            finally:
                oc._load_series_config.cache_clear()
        self.assertEqual(
            allow,
            ["FILA FUSION CLASSICS", "FILA FUSION LIFE", "FILA FUSION X"],
        )


class TestBuildSeriesMilvusExpr(unittest.TestCase):
    def test_golf_anchor_allows_self_only(self):
        expr = build_series_milvus_expr({"series": "GOLF"}, "top")
        self.assertIsNotNone(expr)
        self.assertIn('"GOLF"', expr)
        self.assertNotIn('""', expr)  # 不再放行中性
        self.assertNotIn('"ORIGINALE"', expr)
        self.assertNotIn('"HERITAGE"', expr)

    def test_shoes_target_exempt(self):
        # 鞋线 ≠ apparel series：target_role=shoes → 不加约束
        self.assertIsNone(build_series_milvus_expr({"series": "GOLF"}, "shoes"))

    def test_shoes_anchor_exempt(self):
        # anchor 是鞋（series=MILANO 鞋线）→ 不约束 apparel companion
        self.assertIsNone(build_series_milvus_expr({"series": "MILANO", "role": "shoes"}, "bottoms"))

    def test_neutral_anchor_returns_none(self):
        self.assertIsNone(build_series_milvus_expr({"series": ""}, "top"))
        self.assertIsNone(build_series_milvus_expr({}, "top"))

    def test_no_anchor_returns_none(self):
        self.assertIsNone(build_series_milvus_expr(None, "top"))


class TestBuildSeriesEsFilter(unittest.TestCase):
    def _allow_terms(self, filter_d: dict) -> list[str] | None:
        terms = filter_d.get("terms", {}).get("series")
        return list(terms) if terms else None

    def test_golf_anchor_allows_self_only(self):
        f = build_series_es_filter({"series": "GOLF", "role": "top"}, "top")
        self.assertIsNotNone(f)
        self.assertEqual(sorted(self._allow_terms(f)), ["GOLF"])

    def test_shoes_exempt(self):
        self.assertIsNone(build_series_es_filter({"series": "GOLF"}, "shoes"))
        self.assertIsNone(build_series_es_filter({"series": "MILANO", "role": "shoes"}, "bottoms"))

    def test_neutral_anchor_returns_none(self):
        self.assertIsNone(build_series_es_filter({"series": ""}, "top"))

    def test_no_anchor_returns_none(self):
        self.assertIsNone(build_series_es_filter(None, "top"))


class TestCheckCompanionConflictSeries(unittest.TestCase):
    # 显式置 scene_domain="" 以隔离 scene 规则（series=GOLF 会被 extract_scene_domain
    # 派生为 golf 触发场景冲突规则；真实 SKU 已落 scene_domain，此处只测系列维度）
    def test_same_series_no_conflict(self):
        self.assertFalse(check_companion_conflict(
            {"series": "GOLF", "scene_domain": ""}, {"series": "GOLF", "scene_domain": ""}))
        self.assertFalse(check_companion_conflict(
            {"series": "ORIGINALE", "scene_domain": ""}, {"series": "ORIGINALE", "scene_domain": ""}))

    def test_cross_series_conflict_bidirectional(self):
        # 双向互斥（两个非空不同系列互相不在对方允许集）
        self.assertTrue(check_companion_conflict(
            {"series": "GOLF", "scene_domain": ""}, {"series": "ORIGINALE", "scene_domain": ""}))
        self.assertTrue(check_companion_conflict(
            {"series": "ORIGINALE", "scene_domain": ""}, {"series": "GOLF", "scene_domain": ""}))
        self.assertTrue(check_companion_conflict(
            {"series": "HERITAGE", "scene_domain": ""}, {"series": "FILA X NEMEN", "scene_domain": ""}))

    def test_empty_companion_conflicts(self):
        # anchor 有系列、companion 无系列（非鞋）→ 冲突（空系列不算匹配）
        self.assertTrue(check_companion_conflict(
            {"series": "GOLF", "scene_domain": ""}, {"series": "", "scene_domain": ""}))
        self.assertTrue(check_companion_conflict(
            {"series": "GOLF", "scene_domain": ""}, {"scene_domain": ""}))

    def test_shoes_companion_exempt(self):
        # 鞋豁免：WHITE anchor vs MILANO 鞋 → 不冲突（鞋线 ≠ apparel series）
        self.assertFalse(check_companion_conflict(
            {"series": "WHITE", "scene_domain": "", "role": "top"},
            {"series": "MILANO", "scene_domain": "", "role": "shoes"}))
        self.assertFalse(check_companion_conflict(
            {"series": "GOLF", "scene_domain": ""},
            {"series": "", "scene_domain": "", "role": "shoes"}))

    def test_neutral_anchor_no_conflict(self):
        # anchor 无系列 → 不约束
        self.assertFalse(check_companion_conflict(
            {"series": "", "scene_domain": ""}, {"series": "GOLF", "scene_domain": ""}))
        self.assertFalse(check_companion_conflict(
            {"scene_domain": ""}, {"series": "ORIGINALE", "scene_domain": ""}))


class TestSeriesAllowDirected(unittest.TestCase):
    """有向/非对称语义：series_allow 写 LIFE:[X] 不自动镜像 X:[LIFE]。

    通过 patch _load_series_config 注入自定义有向表，并清缓存避免污染其它用例。
    """

    _custom = {"FILA FUSION LIFE": ["FILA FUSION X"]}

    def setUp(self):
        from backend.ranking import outfit_conflict as oc
        self._oc = oc
        self._patch = patch(
            "backend.ranking.outfit_conflict._load_series_config",
            return_value=self._custom,
        )
        self._patch.start()
        oc._load_series_config.cache_clear()

    def tearDown(self):
        self._patch.stop()
        self._oc._load_series_config.cache_clear()

    def test_life_anchor_allows_x(self):
        f = build_series_es_filter({"series": "FILA FUSION LIFE"}, "top")
        terms = f.get("terms", {}).get("series") if f else None
        self.assertIsNotNone(terms)
        self.assertIn("FILA FUSION X", terms)
        self.assertNotIn("", terms)

    def test_x_anchor_does_not_allow_life(self):
        f = build_series_es_filter({"series": "FILA FUSION X"}, "top")
        terms = f.get("terms", {}).get("series") if f else None
        self.assertIsNotNone(terms)
        self.assertNotIn("FILA FUSION LIFE", terms)

    def test_life_pushes_x_no_conflict(self):
        self.assertFalse(check_companion_conflict(
            {"series": "FILA FUSION LIFE", "scene_domain": ""},
            {"series": "FILA FUSION X", "scene_domain": ""}))

    def test_x_does_not_push_life_conflict(self):
        """X 锚点不推 LIFE → 安全网拒绝（非对称关键点）。"""
        self.assertTrue(check_companion_conflict(
            {"series": "FILA FUSION X", "scene_domain": ""},
            {"series": "FILA FUSION LIFE", "scene_domain": ""}))

    def test_milvus_expr_directed(self):
        expr_life = build_series_milvus_expr({"series": "FILA FUSION LIFE"}, "top")
        self.assertIn('"FILA FUSION X"', expr_life)
        self.assertNotIn('""', expr_life)
        expr_x = build_series_milvus_expr({"series": "FILA FUSION X"}, "top")
        self.assertNotIn('"FILA FUSION LIFE"', expr_x)


class TestIntentSeriesFallback(unittest.TestCase):
    """text_only（无锚点 SKU）时 intent_series 作为回退锚点系列下推隔离。"""

    def test_text_only_intent_series_filters(self):
        # anchor=None + intent_series=GOLF → 仅 GOLF（不含中性）
        expr = build_series_milvus_expr(None, "top", "GOLF")
        self.assertIsNotNone(expr)
        self.assertIn('"GOLF"', expr)
        self.assertNotIn('""', expr)
        self.assertNotIn('"ORIGINALE"', expr)

    def test_text_only_intent_series_es(self):
        f = build_series_es_filter(None, "top", "GOLF")
        self.assertIsNotNone(f)
        self.assertEqual(sorted(f["terms"]["series"]), ["GOLF"])

    def test_anchor_series_overrides_intent(self):
        # 锚点有 series 时以锚点为权威，intent_series 被忽略
        expr = build_series_milvus_expr({"series": "ORIGINALE"}, "top", "GOLF")
        self.assertIn('"ORIGINALE"', expr)
        self.assertNotIn('"GOLF"', expr)

    def test_both_empty_returns_none(self):
        self.assertIsNone(build_series_milvus_expr(None, "top", ""))
        self.assertIsNone(build_series_milvus_expr({}, "top", ""))


class TestNormalizeSeries(unittest.TestCase):
    """series 归一：strip/折叠空白 + 已知集校验（非法→空，避免 0 召回）。"""

    def test_strip_and_collapse(self):
        self.assertEqual(normalize_series("  GOLF "), "GOLF")
        self.assertEqual(normalize_series("FILA  FUSION  LIFE"), "FILA FUSION LIFE")

    def test_known_value_passes(self):
        self.assertEqual(normalize_series("GOLF"), "GOLF")
        self.assertEqual(normalize_series("ORIGINALE"), "ORIGINALE")

    def test_non_canonical_dropped(self):
        # 「FILA FUSION」非 canonical（数据为 FILA FUSION LIFE/X/CLASSICS）→ 丢弃
        self.assertEqual(normalize_series("FILA FUSION"), "")
        self.assertEqual(normalize_series("编造系列"), "")

    def test_empty(self):
        self.assertEqual(normalize_series(""), "")
        self.assertEqual(normalize_series(None), "")  # type: ignore[arg-type]


class TestPerRoleSeries(unittest.TestCase):
    """series 提升为 per-role 标量槽（镜像 scene_domain）。

    覆盖：
      - per-role positive series → ES term / Milvus ``series == "Y"`` 下推
      - per-role negative series → must_not / ``series not in [...]``
      - role_has_explicit_positive 命中（series-only 即触发 bypass）
      - 顶层 intent.series 不被继承进 role effective slots（避免强制全 role 同系）
      - _normalize_target_slots 用 normalize_series 校验 series（非 canonical 丢弃）
      - check_companion_conflict 在 bypass_all 时对 _series_conflict 让路
    """

    def _intent(self, **slots):
        from backend.models import UserIntent
        return UserIntent(
            anchor_role="top",
            target_roles=["bottoms", "shoes"],
            series=slots.get("top_series"),
            target_slots=slots.get("target_slots", {}),
        )

    def test_positive_series_milvus_expr(self):
        intent = self._intent(target_slots={
            "shoes": {"positive": {"series": "FILA FUSION LIFE"}, "negative": {}},
        })
        parts = build_role_milvus_expr_parts(intent, "shoes")
        self.assertIn('series == "FILA FUSION LIFE"', parts)

    def test_positive_series_es_filter(self):
        from backend.intent.role_slots import build_role_es_positive_filters
        intent = self._intent(target_slots={
            "shoes": {"positive": {"series": "FILA FUSION LIFE"}, "negative": {}},
        })
        filters = build_role_es_positive_filters(intent, "shoes")
        self.assertIn({"term": {"series": "FILA FUSION LIFE"}}, filters)

    def test_negative_series_milvus_and_es(self):
        from backend.intent.role_slots import (
            build_role_es_must_not,
            build_role_milvus_expr_parts,
        )
        intent = self._intent(target_slots={
            "bottoms": {"positive": {"series": "HERITAGE"},
                        "negative": {"series": ["ORIGINALE"]}},
        })
        parts = build_role_milvus_expr_parts(intent, "bottoms")
        self.assertIn('series == "HERITAGE"', parts)
        self.assertIn('series not in ["ORIGINALE"]', parts)
        must_not = build_role_es_must_not(intent, "bottoms")
        self.assertIn({"term": {"series": "ORIGINALE"}}, must_not)

    def test_explicit_positive_triggers_bypass(self):
        from backend.intent.role_slots import role_has_explicit_positive
        intent = self._intent(target_slots={
            "shoes": {"positive": {"series": "FILA FUSION LIFE"}, "negative": {}},
        })
        self.assertTrue(role_has_explicit_positive(intent, "shoes"))
        # 顶层 series 存在但 top 无 per-role positive → 不 bypass
        intent2 = self._intent(top_series="GOLF", target_slots={})
        self.assertFalse(role_has_explicit_positive(intent2, "top"))

    def test_top_level_series_not_inherited_into_role(self):
        from backend.intent.role_slots import effective_role_slots
        intent = self._intent(top_series="GOLF", target_slots={})
        # top role 无 per-role series → effective series 必须为 None（不继承顶层 GOLF）
        self.assertIsNone(effective_role_slots(intent, "top").get("series"))
        self.assertIsNone(effective_role_slots(intent, "bottoms").get("series"))

    def test_cross_role_different_series_coexist(self):
        # 上衣 GOLF（顶层锚点系列）、下装 HERITAGE（per-role）、鞋 FILA FUSION LIFE（per-role）
        from backend.intent.role_slots import per_role_series
        intent = self._intent(
            top_series="GOLF",
            target_slots={
                "bottoms": {"positive": {"series": "HERITAGE"}, "negative": {}},
                "shoes": {"positive": {"series": "FILA FUSION LIFE"}, "negative": {}},
            },
        )
        self.assertEqual(per_role_series(intent, "bottoms"), "HERITAGE")
        self.assertEqual(per_role_series(intent, "shoes"), "FILA FUSION LIFE")
        # 各 role 的 milvus expr 互不干扰
        self.assertIn('series == "HERITAGE"', build_role_milvus_expr_parts(intent, "bottoms"))
        self.assertIn('series == "FILA FUSION LIFE"', build_role_milvus_expr_parts(intent, "shoes"))

    def test_bypass_yields_series_conflict_for_role(self):
        # 跨系列锚点（GOLF）× 用户要的异系列下装（HERITAGE）：
        # bypass_all=True 时 _series_conflict 安全网让路，不反杀用户值。
        anchor = {"series": "GOLF", "scene_domain": ""}
        companion = {"series": "HERITAGE", "scene_domain": ""}
        # 非 bypass → 跨系列冲突
        self.assertTrue(check_companion_conflict(anchor, companion, bypass_all=False))
        # bypass → 让路
        self.assertFalse(check_companion_conflict(anchor, companion, bypass_all=True))

    def test_normalize_target_slots_series_via_normalize_series(self):
        from backend.intent.intent_engine import _normalize_target_slots
        raw = {
            "shoes": {"positive": {"series": "  FILA FUSION LIFE "},
                       "negative": {"series": ["FILA FUSION", "ORIGINALE"]}},
        }
        out = _normalize_target_slots(raw)
        pos = out["shoes"]["positive"]
        neg = out["shoes"]["negative"]
        self.assertEqual(pos["series"], "FILA FUSION LIFE")  # strip+折叠
        # 「FILA FUSION」非 canonical → 丢弃；ORIGINALE 保留
        self.assertEqual(neg["series"], ["ORIGINALE"])

    def test_non_canonical_positive_series_dropped(self):
        from backend.intent.intent_engine import _normalize_target_slots
        raw = {"shoes": {"positive": {"series": "编造系列"}, "negative": {}}}
        out = _normalize_target_slots(raw)
        # 非法 series 丢弃 → 该 role 无 positive → 不产出（或 positive 为空）
        self.assertNotIn("series", out.get("shoes", {}).get("positive", {}))


if __name__ == "__main__":
    unittest.main()