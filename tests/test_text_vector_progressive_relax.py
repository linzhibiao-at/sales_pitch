"""text/hybrid 召回的 skip 参数测试：skip_slots/skip_modeling/skip_price/skip_up_time 移除对应 expr 片段。"""
from __future__ import annotations

import unittest

from backend.models import UserIntent
from backend.intent.role_slots import (
    build_role_milvus_expr_parts,
    build_modeling_price_milvus_expr,
)


class TextBuilderSkipTest(unittest.TestCase):
    def test_skip_slots_drops_length_class_positive(self):
        i = UserIntent(
            anchor_role="top", target_roles=["bottoms"],
            target_slots={"bottoms": {"positive": {"length_class": "long"}}},
        )
        parts = build_role_milvus_expr_parts(i, "bottoms", include_global=False)
        self.assertTrue(any('length_class == "long"' in p for p in parts))
        parts2 = build_role_milvus_expr_parts(
            i, "bottoms", include_global=False, skip_slots={"length_class"},
        )
        self.assertFalse(any("length_class" in p for p in parts2))

    def test_skip_slots_drops_coverage_positive(self):
        i = UserIntent(
            anchor_role="top", target_roles=["bottoms"],
            target_slots={"bottoms": {"positive": {"coverage": "full"}}},
        )
        parts = build_role_milvus_expr_parts(i, "bottoms", include_global=False)
        self.assertTrue(any('coverage == "full"' in p for p in parts))
        parts2 = build_role_milvus_expr_parts(
            i, "bottoms", include_global=False, skip_slots={"coverage"},
        )
        self.assertFalse(any("coverage" in p for p in parts2))

    def test_skip_slots_drops_color_series_positive(self):
        i = UserIntent(
            anchor_role="top", target_roles=["bottoms"],
            target_slots={"bottoms": {"positive": {"color_series": ["米色"]}}},
        )
        parts = build_role_milvus_expr_parts(i, "bottoms", include_global=False)
        self.assertTrue(any("color_series" in p for p in parts))
        parts2 = build_role_milvus_expr_parts(
            i, "bottoms", include_global=False, skip_slots={"color_series"},
        )
        self.assertFalse(any("color_series" in p for p in parts2))

    def test_modeling_price_skip_flags(self):
        i = UserIntent(
            anchor_role="top", target_roles=["bottoms"],
            target_slots={"bottoms": {"positive": {"modeling": "宽松"}}},
            budget_max=500,
        )
        expr = build_modeling_price_milvus_expr(i, "bottoms") or ""
        self.assertIn("modeling", expr)
        self.assertIn("price", expr)
        # skip both → None
        self.assertIsNone(
            build_modeling_price_milvus_expr(i, "bottoms", skip_modeling=True, skip_price=True)
        )
        # skip only modeling → price remains
        expr_m = build_modeling_price_milvus_expr(i, "bottoms", skip_modeling=True) or ""
        self.assertNotIn("modeling", expr_m)
        self.assertIn("price", expr_m)
        # skip only price → modeling remains
        expr_p = build_modeling_price_milvus_expr(i, "bottoms", skip_price=True) or ""
        self.assertIn("modeling", expr_p)
        self.assertNotIn("price", expr_p)


if __name__ == "__main__":
    unittest.main()


# ── recall_text_vector_skus 端到端 progressive relax 集成测试 ─────────────────
from unittest import mock as _mock


class _FakeTextRetriever:
    """按 attr_expr 是否含 modeling 返回空或 1 命中。"""
    def __init__(self, rows):
        self._rows = rows
        self.calls: list[str] = []

    def recall_by_text_vector_keywords(self, keywords, top_k_per_keyword=None, **kw):
        expr = kw.get("attr_expr") or ""
        self.calls.append(expr)
        if "modeling" in expr:
            return []
        return [("S1", 0.6, 0.6)]

    def get_sku(self, sid):
        return self._rows.get(sid)


class TextRelaxIntegrationTest(unittest.TestCase):
    def test_drops_modeling_on_zero_hits(self):
        from backend.services import outfit_recall as orc
        intent = UserIntent(
            text="长裤",
            anchor_role="top", target_roles=["bottoms"], gender="男", season=["秋"],
            target_slots={"bottoms": {"positive": {"modeling": "宽松"}}},
        )
        anchor_row = {"sku_id": "ANCHOR_TOP", "role": "top", "category_l2": "短袖",
                      "scene_domain": "daily", "color_series": ["白色系"]}
        rows = {
            "S1": {"sku_id": "S1", "role": "bottoms", "category_l2": "梭织长裤",
                   "color_series": ["白色系"], "season": ["秋"], "gender": ["男"],
                   "length_class": "long", "scene_domain": "daily", "is_intimate": False},
        }
        sku_r = _FakeTextRetriever(rows)
        from backend.config import load_config as _rlc
        _cfg = _rlc()
        _cfg.setdefault("recommend", {})["text_recall_mode"] = "dense"
        _cfg["recommend"]["enable_category_l2_pairing"] = False
        _cfg["recommend"]["enable_color_series_pairing"] = False
        with _mock.patch("backend.services.outfit_recall.load_config", return_value=_cfg), \
             _mock.patch("backend.services.outfit_recall.get_relax_config",
                         return_value=(True, ["modeling"], 1)):
            by_role = orc.recall_text_vector_skus(sku_r, intent, anchor_row, trace_id="t")
        self.assertIn("bottoms", by_role)
        self.assertEqual(by_role["bottoms"][0]["sku_id"], "S1")
        # 第一轮 attr_expr 含 modeling（0 命中），第二轮不含（命中）
        self.assertGreaterEqual(len(sku_r.calls), 2)
        self.assertIn("modeling", sku_r.calls[0])
        # driver 在某轮丢弃了 modeling（low-recall retry 之后会用全量过滤器再补一次）
        self.assertTrue(
            any("modeling" not in c for c in sku_r.calls),
            "progressive relax 应至少有一轮丢弃 modeling",
        )

    def test_master_switch_off_no_drop_on_zero(self):
        from backend.services import outfit_recall as orc
        intent = UserIntent(
            text="长裤",
            anchor_role="top", target_roles=["bottoms"], gender="男", season=["秋"],
            target_slots={"bottoms": {"positive": {"modeling": "宽松"}}},
        )
        anchor_row = {"sku_id": "ANCHOR_TOP", "role": "top", "scene_domain": "daily",
                      "color_series": ["白色系"]}
        sku_r = _FakeTextRetriever({})  # get_sku returns None → 0 picked
        from backend.config import load_config as _rlc
        _cfg = _rlc()
        _cfg.setdefault("recommend", {})["text_recall_mode"] = "dense"
        _cfg["recommend"]["enable_category_l2_pairing"] = False
        _cfg["recommend"]["enable_color_series_pairing"] = False
        with _mock.patch("backend.services.outfit_recall.load_config", return_value=_cfg), \
             _mock.patch("backend.services.outfit_recall.get_relax_config",
                         return_value=(False, ["modeling"], 1)):
            by_role = orc.recall_text_vector_skus(sku_r, intent, anchor_row, trace_id="t")
        # 开关关：无放宽（每次调用 attr_expr 都含 modeling），0 命中 → 无 bottoms。
        # 注：0 命中仍会触发 low-recall 降阈值 retry（与旧行为一致），故 calls 可能 >1。
        self.assertNotIn("bottoms", by_role)
        self.assertTrue(all("modeling" in c for c in sku_r.calls),
                         "开关关时不应丢弃 modeling")
