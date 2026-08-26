"""complementary 召回的 progressive relax 测试：skip_slots 移除对应 expr 片段。"""
from __future__ import annotations

import unittest

from backend.models import UserIntent
from backend.services.complementary_recall import _build_role_milvus_expr


def _intent() -> UserIntent:
    return UserIntent(
        anchor_role="bottoms", target_roles=["top"], gender="男", season=["春"],
    )


class ComplementarySkipTest(unittest.TestCase):
    def test_skip_up_time_drops_up_time_clause(self):
        anchor = {"role": "bottoms"}
        base = _build_role_milvus_expr(_intent(), "top", anchor_row=anchor)
        self.assertIn("up_time", base)
        relaxed = _build_role_milvus_expr(
            _intent(), "top", anchor_row=anchor, skip_slots={"up_time"},
        )
        self.assertNotIn("up_time", relaxed)

    def test_skip_anchor_attr_drops_is_intimate(self):
        anchor = {"role": "bottoms"}
        base = _build_role_milvus_expr(_intent(), "top", anchor_row=anchor)
        self.assertIn("is_intimate", base)
        relaxed = _build_role_milvus_expr(
            _intent(), "top", anchor_row=anchor, skip_slots={"anchor_attr_must_not"},
        )
        self.assertNotIn("is_intimate", relaxed)

    def test_hard_slots_survive_full_drop(self):
        # 丢弃所有可放宽 slot；gender/season 必须保留
        anchor = {"role": "bottoms"}
        all_soft = {
            "modeling", "length_class", "coverage", "series", "scene_domain",
            "color_series", "category_l2", "anchor_attr_must_not", "up_time", "price",
        }
        expr = _build_role_milvus_expr(
            _intent(), "top", anchor_row=anchor, skip_slots=all_soft,
        )
        self.assertIn("男", expr)        # gender survives
        self.assertIn("春", expr)        # season survives
        self.assertNotIn("up_time", expr)
        self.assertNotIn("is_intimate", expr)

    def test_skip_color_series_drops_pairing_clause(self):
        # 锚点有色系 → 触发 pairing 色系 clause；skip color_series 应移除
        anchor = {"role": "bottoms", "color_series": ["白色系"]}
        base = _build_role_milvus_expr(_intent(), "top", anchor_row=anchor)
        self.assertIn("array_contains_any(color_series", base)
        relaxed = _build_role_milvus_expr(
            _intent(), "top", anchor_row=anchor, skip_slots={"color_series"},
        )
        self.assertNotIn("array_contains_any(color_series", relaxed)


if __name__ == "__main__":
    unittest.main()


# ── _search_one_role 端到端 progressive relax 集成测试 ─────────────────────────
from unittest import mock as _mock


class _FakeMilvus:
    """按 expr 是否含 'up_time' 决定空或 1 命中。"""
    def __init__(self):
        self.calls: list[str] = []
    def search_sku_complementary_vectors(self, vector, top_k, expr=None):
        self.calls.append(expr or "")
        if "up_time" in (expr or ""):
            return []
        return [("S1", 0.5)]
    @staticmethod
    def hit_to_similarity(d):
        return float(d)


class ComplementarySearchRelaxTest(unittest.TestCase):
    def test_drops_up_time_on_zero_hits(self):
        from backend.services.complementary_recall import _search_one_role
        milvus = _FakeMilvus()
        sku_r = _mock.MagicMock()
        sku_r.get_sku.return_value = {
            "sku_id": "S1", "role": "top", "category_l2": "短袖",
            "color_series": ["白色系"], "season": ["春"], "gender": ["男"],
            "scene_domain": "daily", "is_intimate": False,
        }
        intent = _intent()  # bottoms anchor → top target, gender 男, season 春
        anchor = {"sku_id": "ANCHOR", "role": "bottoms", "scene_domain": "daily"}
        with _mock.patch("backend.services.complementary_recall.get_relax_config",
                         return_value=(True, ["up_time"], 1)):
            results = _search_one_role(
                milvus, sku_r, [0.1] * 8, "top", intent,
                "ANCHOR", "", 10, anchor_row=anchor,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["sku_id"], "S1")
        # 第一轮 expr 含 up_time（0 命中），第二轮不含（命中）
        self.assertGreaterEqual(len(milvus.calls), 2)
        self.assertIn("up_time", milvus.calls[0])
        self.assertNotIn("up_time", milvus.calls[-1])

    def test_master_switch_off_no_drop(self):
        from backend.services.complementary_recall import _search_one_role
        milvus = _FakeMilvus()
        sku_r = _mock.MagicMock()
        sku_r.get_sku.return_value = {"sku_id": "S1", "role": "top"}
        intent = _intent()
        anchor = {"sku_id": "ANCHOR", "role": "bottoms", "scene_domain": "daily"}
        with _mock.patch("backend.services.complementary_recall.get_relax_config",
                         return_value=(False, ["up_time"], 1)):
            results = _search_one_role(
                milvus, sku_r, [0.1] * 8, "top", intent,
                "ANCHOR", "", 10, anchor_row=anchor,
            )
        self.assertEqual(len(results), 0)  # 0 命中，无放宽
        self.assertEqual(len(milvus.calls), 1)
        self.assertIn("up_time", milvus.calls[0])
